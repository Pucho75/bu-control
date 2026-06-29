"""
BU Control — Document Parser
Handles ODA, B/L, COA PDF uploads via Claude API
Each parser: extract → confirm screen → write to DB
"""

import os
import base64
import json
import sqlite3
import re
from datetime import datetime
from flask import Blueprint, request, render_template, redirect, url_for, session, flash, g
from werkzeug.utils import secure_filename
import anthropic
from fuzzy_match import find_best_match

parse_bp = Blueprint("parse_docs", __name__)

ANTHROPIC_CLIENT = anthropic.Anthropic()  # uses ANTHROPIC_API_KEY env var
ALLOWED_EXTENSIONS = {"pdf", "png", "jpg", "jpeg"}
DB_PATH = os.environ.get("DB_PATH", os.path.join(os.path.dirname(__file__), "db", "bu_control.db"))

def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys=ON")
    return g.db

def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS

def normalize_container_code(code):
    """Normalize container code: uppercase, remove spaces and dashes.
    EXFU 072559-5 → EXFU0725595
    DHDU 227310-6 → DHDU2273106
    """
    if not code:
        return code
    return re.sub(r"[\s\-]", "", code.strip().upper())

def normalize_date(date_str):
    """Convert various date formats to ISO YYYY-MM-DD.
    Handles: MM/DD/YYYY, DD/MM/YYYY (ambiguous — assumes MM/DD if month<=12),
    DD-MM-YYYY, YYYY-MM-DD (passthrough)
    """
    if not date_str:
        return None
    date_str = date_str.strip()
    # Already ISO
    if re.match(r"^\d{4}-\d{2}-\d{2}$", date_str):
        return date_str
    # MM/DD/YYYY or DD/MM/YYYY
    m = re.match(r"^(\d{1,2})[/\-](\d{1,2})[/\-](\d{4})$", date_str)
    if m:
        a, b, year = int(m.group(1)), int(m.group(2)), m.group(3)
        # If first part > 12, must be DD/MM/YYYY
        if a > 12:
            return f"{year}-{b:02d}-{a:02d}"
        # Otherwise assume MM/DD/YYYY (US format common on COAs)
        return f"{year}-{a:02d}-{b:02d}"
    return date_str  # return as-is if unrecognized

def file_to_base64(file_bytes, mime_type):
    return base64.standard_b64encode(file_bytes).decode("utf-8")

def call_claude(prompt, file_b64, mime_type="image/png"):
    """Call Claude API with a document (PDF or image) and return extracted JSON."""

    # Build content block based on file type
    if mime_type == "application/pdf":
        file_block = {
            "type": "document",
            "source": {
                "type": "base64",
                "media_type": "application/pdf",
                "data": file_b64,
            }
        }
    else:
        file_block = {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": mime_type,
                "data": file_b64,
            }
        }

    response = ANTHROPIC_CLIENT.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2000,
        messages=[{
            "role": "user",
            "content": [
                file_block,
                {"type": "text", "text": prompt}
            ]
        }]
    )
    text = response.content[0].text.strip()
    # Strip markdown fences if present
    text = re.sub(r"^```json\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return json.loads(text)

# ── ODA PARSER ───────────────────────────────────────────────

ODA_PROMPT = """
You are parsing a Purchase Order (ODA) document from an Italian chemical trading company.
Extract the following fields and return ONLY valid JSON, no other text.

The document may have multiple line items — each line item is a separate shipment/parcel.

Return this exact structure:
{
  "oda_number": "string, e.g. 2026-0125",
  "oda_date": "ISO date YYYY-MM-DD",
  "supplier_name": "string",
  "supplier_country": "2-letter ISO country code, e.g. AR, BR, US",
  "incoterm": "string, e.g. FOB, CIF, DAP",
  "incoterm_location": "string, delivery point only e.g. GENOVA, B.AIRES",
  "incoterm_full": "full incoterm string as written e.g. CIF GENOVA T1",
  "customs_regime": "T1 or T2 or null (extract from incoterm string or document)",
  "vat_rate": number or null (VAT percentage e.g. 0 or 22, from VAT/IVA field in footer),
  "currency": "USD or EUR",
  "payment_terms": "string or null",
  "shipments": [
    {
      "line_number": 1,
      "product_description": "string",
      "container_type": "ISOTANK or ONE_TON_CUBE or FLEXIBAG or ATB",
      "num_containers": integer or null,
      "mt_planned": float,
      "price_per_mt": float,
      "delivery_date": "ISO date YYYY-MM-DD or null"
    }
  ]
}

Rules:
- oda_number: extract from "Purchase order XXXX-XXXX" in the document title
- oda_date: extract from "dated YYYY-MM-DD" in the document title
- supplier_country: infer from supplier address if not explicit
- incoterm_location: city/port only, strip T1/T2 suffix (e.g. "GENOVA T1" → "GENOVA")
- incoterm_full: the complete string as written including T1/T2 (e.g. "CIF GENOVA T1")
- customs_regime: look for T1 or T2 in the incoterm string or elsewhere in the document
- vat_rate: look for IVA or VAT percentage in the footer table. 0 or exempt = T1 goods, 22 = T2
- currency: look in footer Value column: "Dollaro USA"/"Dollar"/"USD" → "USD", "Euro"/"EUR"/"€" → "EUR"
  Also check price description in line items (e.g. "USD 1455/MT" → USD). Default EUR only if no indicator.
- mt_planned: the quantity in MT for that line
- price_per_mt: numeric value only, no currency symbol
- container_type: ISOTANK if isotank, ONE_TON_CUBE if IBC/cube, FLEXIBAG if flexibag, ATB if road tanker
- num_containers: number of containers mentioned for that line
- If a field is not found, use null
"""

@parse_bp.route("/parse/oda", methods=["GET", "POST"])
def parse_oda():
    if "user_id" not in session:
        return redirect(url_for("login"))

    if request.method == "GET":
        session.pop("oda_queue", None)
        return render_template("parse_oda.html")

    files = request.files.getlist("document")
    if not files or all(f.filename == "" for f in files):
        flash("No files uploaded.", "error")
        return redirect(request.url)

    db = get_db()
    suppliers = db.execute("SELECT * FROM suppliers WHERE is_active=1 ORDER BY name").fetchall()
    products  = db.execute("SELECT * FROM products  WHERE is_active=1 ORDER BY code").fetchall()

    # Extract all files and build queue
    queue = []
    for f in files:
        if not f or not allowed_file(f.filename):
            continue
        file_bytes = f.read()
        ext = f.filename.rsplit(".", 1)[1].lower()
        mime_type = "application/pdf" if ext == "pdf" else f"image/{ext}"
        b64 = file_to_base64(file_bytes, mime_type)
        try:
            extracted = call_claude(ODA_PROMPT, b64, mime_type)
            extracted["_filename"] = f.filename
            queue.append(extracted)
        except Exception as e:
            import traceback
            traceback.print_exc()
            flash(f"Extraction failed for {f.filename}: {e}", "error")

    if not queue:
        flash("No ODAs could be extracted.", "error")
        return redirect(request.url)

    session["oda_queue"] = queue
    return redirect(url_for("parse_docs.parse_oda_next"))


@parse_bp.route("/parse/oda/next")
def parse_oda_next():
    if "user_id" not in session:
        return redirect(url_for("login"))

    queue = session.get("oda_queue", [])
    if not queue:
        flash("All ODAs processed.", "success")
        return redirect(url_for("oda_status"))

    extracted = queue[0]
    db = get_db()
    suppliers = db.execute("SELECT * FROM suppliers WHERE is_active=1 ORDER BY name").fetchall()
    products  = db.execute("SELECT * FROM products  WHERE is_active=1 ORDER BY code").fetchall()

    # Auto-match supplier — check aliases first, then fuzzy
    matched_supplier_id = None
    supplier_match_type = None
    supplier_match_name = None
    if extracted.get("supplier_name"):
        # 1. Check alias table
        alias_row = db.execute("""
            SELECT sa.supplier_id, s.name FROM supplier_aliases sa
            JOIN suppliers s ON s.id = sa.supplier_id
            WHERE sa.alias = ?
        """, (extracted["supplier_name"],)).fetchone()
        if alias_row:
            matched_supplier_id = alias_row["supplier_id"]
            supplier_match_type = "exact"
            supplier_match_name = alias_row["name"]
        else:
            # 2. Fuzzy match against supplier names
            record, score, match_type = find_best_match(
                extracted["supplier_name"], suppliers, name_field="name"
            )
            if record:
                matched_supplier_id = record["id"]
                supplier_match_type = match_type
                supplier_match_name = record["name"]

    # Auto-match product via alias
    matched_product_id = None
    alias_found = False
    extracted_desc = ""
    if extracted.get("shipments"):
        extracted_desc = extracted["shipments"][0].get("product_description", "")
        if extracted_desc and matched_supplier_id:
            row = db.execute("""
                SELECT product_id FROM product_aliases
                WHERE supplier_id=? AND alias=?
            """, (matched_supplier_id, extracted_desc)).fetchone()
            if row:
                matched_product_id = row["product_id"]
                alias_found = True

    # Pre-fill customs_regime from supplier default if not extracted
    if matched_supplier_id and not extracted.get("customs_regime"):
        sr = db.execute("SELECT customs_regime FROM suppliers WHERE id=?",
                        (matched_supplier_id,)).fetchone()
        if sr and sr["customs_regime"]:
            extracted["customs_regime"] = sr["customs_regime"]

    remaining = len(queue)
    return render_template("parse_oda_confirm.html",
        extracted=extracted,
        suppliers=[dict(s) for s in suppliers],
        products=[dict(p) for p in products],
        matched_supplier_id=matched_supplier_id,
        matched_product_id=matched_product_id,
        supplier_match_type=supplier_match_type,
        supplier_match_name=supplier_match_name,
        alias_found=alias_found,
        extracted_desc=extracted_desc,
        queue_remaining=remaining,
        queue_total=remaining,
    )


@parse_bp.route("/parse/oda/confirm", methods=["POST"])
def parse_oda_confirm():
    if "user_id" not in session:
        return redirect(url_for("login"))

    db = get_db()
    form = request.form

    # Resolve or create supplier
    supplier_id = form.get("supplier_id")
    if not supplier_id:
        db.execute(
            "INSERT OR IGNORE INTO suppliers (name, country) VALUES (?,?)",
            (form["supplier_name"], form.get("supplier_country") or None)
        )
        db.commit()
        supplier_id = db.execute(
            "SELECT id FROM suppliers WHERE name=?", (form["supplier_name"],)
        ).fetchone()["id"]
    supplier_id = int(supplier_id)

    # Save supplier alias if requested (fuzzy match learning)
    extracted_supplier_name = form.get("supplier_name", "").strip()
    if extracted_supplier_name and form.get("save_supplier_alias") == "1":
        existing = db.execute(
            "SELECT id FROM suppliers WHERE name=?", (extracted_supplier_name,)
        ).fetchone()
        if not existing:
            db.execute("""
                INSERT OR IGNORE INTO supplier_aliases (supplier_id, alias, source)
                VALUES (?, ?, 'LEARNED')
            """, (supplier_id, extracted_supplier_name))
            db.commit()

    # Resolve product
    product_id = int(form["product_id"])

    # Save alias if requested (learning mechanism)
    extracted_product_desc = form.get("extracted_product_desc", "").strip()
    if extracted_product_desc and form.get("save_alias") == "1":
        db.execute("""
            INSERT OR IGNORE INTO product_aliases (product_id, supplier_id, alias, source)
            VALUES (?,?,?,'LEARNED')
        """, (product_id, supplier_id, extracted_product_desc))

    # Expected delivery month from first shipment delivery date
    first_delivery = form.get("delivery_date_1", "")
    expected_month = first_delivery[:7] if first_delivery else None

    # Insert ODA
    oda_code = form["oda_number"]
    existing = db.execute("SELECT id FROM odas WHERE oda_code=?", (oda_code,)).fetchone()
    if existing:
        flash(f"ODA {oda_code} already exists in the database.", "error")
        return redirect(url_for("parse_docs.parse_oda"))

    db.execute("""
        INSERT INTO odas (
            oda_code, supplier_id, product_id, total_mt,
            price_eur_per_mt, price_usd_per_mt, currency,
            order_date, paese, incoterm, incoterm_location,
            incoterm_full, customs_regime, vat_rate,
            payment_terms, container_type, num_containers,
            expected_delivery_month, source
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        oda_code,
        supplier_id,
        product_id,
        float(form.get("total_mt", 0) or 0),
        float(form.get("price_eur", 0) or 0),
        float(form.get("price_usd", 0) or 0) or None,
        form.get("currency", "EUR"),
        form["oda_date"],
        form.get("supplier_country") or None,
        form.get("incoterm") or None,
        form.get("incoterm_location") or None,
        form.get("incoterm_full") or None,
        form.get("customs_regime") or None,
        float(form.get("vat_rate", 0) or 0) or None,
        form.get("payment_terms") or None,
        form.get("container_type") or None,
        int(form.get("total_containers", 0) or 0) or None,
        expected_month,
        "ODA_PARSER",
    ))
    db.commit()
    oda_id = db.execute("SELECT id FROM odas WHERE oda_code=?", (oda_code,)).fetchone()["id"]

    # Insert ODA lines (commercial delivery schedule from ODA PDF)
    num_lines = int(form.get("num_lines", 1))
    for i in range(1, num_lines + 1):
        mt = form.get(f"mt_{i}")
        if not mt:
            continue
        db.execute("""
            INSERT INTO oda_lines (
                oda_id, line_number, mt_planned, price_per_mt,
                container_type, num_containers, scheduled_date, source
            ) VALUES (?,?,?,?,?,?,?,?)
        """, (
            oda_id,
            i,
            float(mt),
            float(form.get(f"price_{i}", 0) or 0) or None,
            form.get(f"container_type_{i}") or None,
            int(form.get(f"num_containers_{i}", 0) or 0) or None,
            form.get(f"delivery_date_{i}") or None,
            "ODA_PARSER",
        ))

    db.commit()
    session.pop("oda_draft", None)

    # Pop from queue and go to next
    queue = session.get("oda_queue", [])
    if queue:
        queue.pop(0)
        session["oda_queue"] = queue

    remaining = len(queue)
    if remaining > 0:
        flash(f"ODA {oda_code} imported. {remaining} more to process.", "success")
        return redirect(url_for("parse_docs.parse_oda_next"))

    flash(f"ODA {oda_code} imported.", "success")
    return redirect(url_for("oda_status"))


# ── B/L PARSER ───────────────────────────────────────────────

BL_PROMPT = """
You are parsing a Bill of Lading (B/L) document from an international shipping company.
Extract the following fields and return ONLY valid JSON, no other text.

Return this exact structure:
{
  "bl_number": "string",
  "bl_date": "ISO date YYYY-MM-DD (from 'Shipped on board' date)",
  "vessel_name": "string",
  "voyage_ref": "string",
  "port_of_loading": "string",
  "port_of_discharge": "string",
  "oda_reference": "string or null (look for ORDRN: or ORDER: reference in description)",
  "containers": [
    {
      "container_code": "string (format like DHDU1234567, PCVU1234567 — ISO tank codes)",
      "nominal_mt": float,
      "container_type": "ISOTANK or ONE_TON_CUBE"
    }
  ]
}

Rules:
- bl_date: use the date next to 'Shipped on board' or 'Date' at bottom of document
- vessel_name: from 'Ocean Vessel' field
- voyage_ref: from 'Voy. no.' field
- container_code: extract only the ISO container codes (4 letters + 7 digits format like DHDU 227310-6, normalize to DHDU2273106 without spaces/dashes)
- nominal_mt: weight in MT per container (convert from KG if needed: divide by 1000)
- oda_reference: look for ORDRN: or ORDER: in the description text, extract the order number
- Ignore seal numbers (JL prefix) and other reference numbers
- If a field is not found, use null
"""

@parse_bp.route("/parse/bl", methods=["GET", "POST"])
def parse_bl():
    if "user_id" not in session:
        return redirect(url_for("login"))

    if request.method == "GET":
        session.pop("bl_queue", None)
        db = get_db()
        odas = db.execute("""
            SELECT o.id, o.oda_code, s.name AS supplier_name
            FROM odas o
            JOIN suppliers s ON s.id = o.supplier_id
            WHERE o.is_deleted = 0
            ORDER BY o.order_date DESC
        """).fetchall()
        return render_template("parse_bl.html", odas=odas)

    files = request.files.getlist("document")
    if not files or all(f.filename == "" for f in files):
        flash("No files uploaded.", "error")
        return redirect(request.url)

    db = get_db()
    odas = db.execute("""
        SELECT o.id, o.oda_code, s.name AS supplier_name, o.num_containers, o.container_type
        FROM odas o
        JOIN suppliers s ON s.id = o.supplier_id
        WHERE o.is_deleted = 0
        ORDER BY o.order_date DESC
    """).fetchall()

    queue = []
    for f in files:
        if not f or not allowed_file(f.filename):
            continue
        file_bytes = f.read()
        ext = f.filename.rsplit(".", 1)[1].lower()
        mime_type = "application/pdf" if ext == "pdf" else f"image/{ext}"
        b64 = file_to_base64(file_bytes, mime_type)
        try:
            extracted = call_claude(BL_PROMPT, b64, mime_type)
            # Normalize container codes
            if extracted.get("containers"):
                for c in extracted["containers"]:
                    if c.get("container_code"):
                        c["container_code"] = normalize_container_code(c["container_code"])
            extracted["_filename"] = f.filename
            queue.append(extracted)
        except Exception as e:
            import traceback
            traceback.print_exc()
            flash(f"Extraction failed for {f.filename}: {e}", "error")

    if not queue:
        flash("No B/Ls could be extracted.", "error")
        return redirect(request.url)

    session["bl_queue"] = queue
    session["bl_odas"] = [dict(o) for o in odas]
    return redirect(url_for("parse_docs.parse_bl_next"))


@parse_bp.route("/parse/bl/next")
def parse_bl_next():
    if "user_id" not in session:
        return redirect(url_for("login"))

    queue = session.get("bl_queue", [])
    if not queue:
        flash("All B/Ls processed.", "success")
        return redirect(url_for("containers"))

    extracted = queue[0]
    odas = session.get("bl_odas", [])

    # Try to auto-match ODA
    matched_oda_id = None
    matched_shipments = []
    if extracted.get("oda_reference"):
        for o in odas:
            if o["oda_code"] == extracted["oda_reference"]:
                matched_oda_id = o["id"]
                break

    if matched_oda_id:
        db = get_db()
        matched_shipments = db.execute("""
            SELECT id, shipment_number, bl_date, bl_number
            FROM shipments WHERE oda_id=? AND is_deleted=0
            ORDER BY shipment_number
        """, (matched_oda_id,)).fetchall()
        matched_shipments = [dict(s) for s in matched_shipments]

    remaining = len(queue)
    return render_template("parse_bl_confirm.html",
        extracted=extracted,
        odas=odas,
        matched_oda_id=matched_oda_id,
        matched_shipments=matched_shipments,
        queue_remaining=remaining,
    )


@parse_bp.route("/parse/bl/confirm", methods=["POST"])
def parse_bl_confirm():
    if "user_id" not in session:
        return redirect(url_for("login"))

    db = get_db()
    form = request.form
    oda_id = int(form["oda_id"])

    # Use explicitly selected shipment, or find/create one
    shipment_id = form.get("shipment_id")
    if shipment_id:
        shipment_id = int(shipment_id)
    else:
        # Fallback: find next shipment without a B/L
        shipment = db.execute("""
            SELECT id FROM shipments
            WHERE oda_id=? AND bl_number IS NULL AND is_deleted=0
            ORDER BY shipment_number ASC LIMIT 1
        """, (oda_id,)).fetchone()

        if shipment:
            shipment_id = shipment["id"]
        else:
            # Create new shipment
            last = db.execute(
                "SELECT MAX(shipment_number) AS n FROM shipments WHERE oda_id=?", (oda_id,)
            ).fetchone()
            next_num = (last["n"] or 0) + 1
            try:
                db.execute("""
                    INSERT INTO shipments (oda_id, shipment_number, source)
                    VALUES (?,?,?)
                """, (oda_id, next_num, "BL_PARSER"))
                db.commit()
                shipment_id = db.execute(
                    "SELECT id FROM shipments WHERE oda_id=? AND shipment_number=?",
                    (oda_id, next_num)
                ).fetchone()["id"]
            except Exception as e:
                flash(f"Could not create shipment: {e}", "error")
                return redirect(url_for("parse_docs.parse_bl"))

    # Update shipment with B/L data
    db.execute("""
        UPDATE shipments SET
            bl_number=?, bl_date=?,
            vessel_name=?, voyage_ref=?,
            port_of_loading=?, port_of_discharge=?,
            source='BL_PARSER', updated_at=datetime('now')
        WHERE id=?
    """, (
        form["bl_number"],
        form["bl_date"],
        form["vessel_name"],
        form.get("voyage_ref"),
        form.get("port_of_loading"),
        form.get("port_of_discharge"),
        shipment_id,
    ))

    # Insert containers
    num_containers = int(form.get("num_containers", 0))
    for i in range(1, num_containers + 1):
        code = form.get(f"container_code_{i}")
        if not code:
            continue
        code = normalize_container_code(code)
        nominal_mt = float(form.get(f"nominal_mt_{i}", 23))
        ctype = form.get(f"container_type_{i}", "ISOTANK")

        existing = db.execute(
            "SELECT id FROM containers WHERE container_code=?", (code,)
        ).fetchone()
        if not existing:
            db.execute("""
                INSERT INTO containers (
                    container_code, shipment_id, container_type,
                    nominal_mt, status, source
                ) VALUES (?,?,?,?,'IN_TRANSIT','BL_PARSER')
            """, (code, shipment_id, ctype, nominal_mt))

    # Fetch BCE rate for B/L date and save as provisional fx_rate_invoice on shipment
    bl_date = form.get("bl_date")
    if bl_date:
        try:
            from cost_utils import fetch_ecb_rate
            oda = db.execute("SELECT currency FROM odas WHERE id=?", (oda_id,)).fetchone()
            if oda and oda["currency"] == "USD":
                rate = fetch_ecb_rate(bl_date)
                if rate:
                    db.execute("UPDATE shipments SET fx_rate_invoice=? WHERE id=?",
                               (rate, shipment_id))
                    db.commit()
        except Exception as e:
            pass  # Non-blocking

    db.commit()
    session.pop("bl_draft", None)

    # Pop from queue and go to next
    queue = session.get("bl_queue", [])
    if queue:
        queue.pop(0)
        session["bl_queue"] = queue

    remaining = len(queue)
    if remaining > 0:
        flash(f"B/L {form['bl_number']} imported — {num_containers} containers. {remaining} more to process.", "success")
        return redirect(url_for("parse_docs.parse_bl_next"))

    flash(f"B/L {form['bl_number']} imported — {num_containers} containers.", "success")
    return redirect(url_for("containers"))


# ── COA PARSER ───────────────────────────────────────────────

COA_PROMPT = """
You are parsing a Certificate of Analysis (COA) document from a chemical supplier.
This may be for an ISO tank container OR an ATB road tanker. Each page covers one delivery.
Extract the following fields and return ONLY valid JSON, no other text.

Return this exact structure:
{
  "pages": [
    {
      "container_code": "string or null (ISO container code 4 letters + 7 digits, e.g. PCVU2663922 — null for road tankers)",
      "ddt_number": "string or null (delivery note number, look for: Delivery note NR, DDT, Bolla, Documento di trasporto)",
      "tank_number": "string or null (tank or serbatoio number e.g. S1501)",
      "production_batch": "string (batch or lot number — look for: Lotto NR, Lot NR, BATCH, LOT, PARTITA)",
      "manufacture_date": "ISO date YYYY-MM-DD or null",
      "expiration_date": "ISO date YYYY-MM-DD or null",
      "issue_date": "ISO date YYYY-MM-DD or null",
      "loading_date": "ISO date YYYY-MM-DD or null (look for: Data di carico, Date of loading)"
    }
  ]
}

Rules:
- container_code: only extract if you find a real ISO container code (4 uppercase letters + 7 digits). For road tankers this will be null.
- ddt_number: look for delivery note number, bolla di consegna, DDT number — this links the COA to the ATB road delivery
- tank_number: the production tank (Serbatoio/Tank field) — different from container code
- production_batch: look for Lotto NR, Lot NR, BATCH, LOT, PARTITA
- loading_date: date of loading/consegna — use to match if no DDT number found
- Convert all dates to ISO format YYYY-MM-DD
- If multiple COAs on different pages, return one entry per page
- If a field is not found, use null
"""

@parse_bp.route("/parse/coa", methods=["GET", "POST"])
def parse_coa():
    if "user_id" not in session:
        return redirect(url_for("login"))

    if request.method == "GET":
        return render_template("parse_coa.html")

    files = request.files.getlist("document")
    if not files or all(f.filename == "" for f in files):
        flash("Please upload at least one file.", "error")
        return redirect(request.url)

    db = get_db()
    all_pages = []

    for f in files:
        if not f or not allowed_file(f.filename):
            continue
        file_bytes = f.read()
        ext = f.filename.rsplit(".", 1)[1].lower()
        mime_type = "application/pdf" if ext == "pdf" else f"image/{ext}"
        b64 = file_to_base64(file_bytes, mime_type)

        # Save COA file to repository
        import os, uuid
        coa_dir = os.path.join(os.path.dirname(__file__), "uploads", "coas")
        os.makedirs(coa_dir, exist_ok=True)
        safe_name = f"{uuid.uuid4().hex}_{f.filename.replace(' ', '_')}"
        with open(os.path.join(coa_dir, safe_name), "wb") as out:
            out.write(file_bytes)

        try:
            extracted = call_claude(COA_PROMPT, b64, mime_type)
            pages = extracted.get("pages", [])
        except Exception as e:
            import traceback
            traceback.print_exc()
            flash(f"Extraction failed for {f.filename}: {e}", "error")
            continue

        for page in pages:
            page["_saved_filename"] = safe_name
            page["_original_name"] = f.filename
            code = page.get("container_code")
            container = None
            if code:
                normalized = normalize_container_code(code)
                container = db.execute(
                    "SELECT id, container_code, shipment_id FROM containers WHERE container_code=?",
                    (normalized,)
                ).fetchone()
                if not container and len(normalized) >= 10:
                    core = normalized[:10]
                    container = db.execute(
                        "SELECT id, container_code, shipment_id FROM containers WHERE container_code LIKE ?",
                        (core + "%",)
                    ).fetchone()
                page["container_code"] = normalized if not container else container["container_code"]

            # If still no match, try to infer from filename (e.g. "COA 0725595.pdf" → EXFU0725595)
            if not container:
                filename_hint = re.sub(r"[^A-Z0-9]", "", f.filename.upper().rsplit(".", 1)[0])
                if filename_hint:
                    suggested = db.execute(
                        "SELECT id, container_code FROM containers WHERE container_code LIKE ?",
                        ("%" + filename_hint[-7:] + "%",)
                    ).fetchone()
                    if suggested:
                        container = suggested
                        page["container_code"] = suggested["container_code"]
                        page["code_from_filename"] = True
            # Normalize dates
            page["manufacture_date"] = normalize_date(page.get("manufacture_date"))
            page["expiration_date"]  = normalize_date(page.get("expiration_date"))
            page["issue_date"]       = normalize_date(page.get("issue_date"))
            # If no container found, look up available containers by product
            available_containers = []
            if not container:
                # Try to match by product name from COA
                prod_desc = (page.get("product_description") or "").upper()
                prod_rows = db.execute("""
                    SELECT DISTINCT c.id, c.container_code, c.actual_mt, c.nominal_mt,
                           o.oda_code, p.code AS product_code
                    FROM containers c
                    JOIN shipments s ON s.id = c.shipment_id
                    JOIN odas o ON o.id = s.oda_id
                    JOIN products p ON p.id = o.product_id
                    WHERE c.status IN ('IN_TRANSIT','IN_PORT','IN_STORAGE')
                      AND c.production_lot IS NULL
                      AND c.is_deleted = 0
                    ORDER BY o.order_date DESC, c.id
                """).fetchall()
                available_containers = [dict(r) for r in prod_rows]

            all_pages.append({
                **page,
                "source_file": f.filename,
                "container_found": container is not None and not page.get("code_from_filename"),
                "code_from_filename": page.get("code_from_filename", False),
                "container_id": container["id"] if container else None,
                "available_containers": available_containers,
            })

    if not all_pages:
        flash("No data could be extracted from the uploaded files.", "error")
        return redirect(request.url)

    session["coa_draft"] = all_pages
    return render_template("parse_coa_confirm.html", pages=all_pages)


@parse_bp.route("/parse/coa/confirm", methods=["POST"])
def parse_coa_confirm():
    if "user_id" not in session:
        return redirect(url_for("login"))

    db = get_db()
    form = request.form
    num_pages = int(form.get("num_pages", 0))
    updated = 0

    for i in range(num_pages):
        # Collect container IDs: single, multi-checkbox, manual, or ATB DDT
        container_ids = []

        single_id = form.get(f"container_id_{i}")
        if single_id:
            container_ids.append(single_id)

        multi_ids = form.getlist(f"multi_container_{i}")
        container_ids.extend(multi_ids)

        if not container_ids:
            manual_code = form.get(f"manual_container_code_{i}", "").strip()
            if manual_code:
                normalized = normalize_container_code(manual_code)
                row = db.execute(
                    "SELECT id FROM containers WHERE container_code=?", (normalized,)
                ).fetchone()
                if not row and len(normalized) >= 10:
                    row = db.execute(
                        "SELECT id FROM containers WHERE container_code LIKE ?",
                        (normalized[:10] + "%",)
                    ).fetchone()
                if row:
                    container_ids.append(str(row["id"]))

        if not container_ids:
            ddt_num = form.get(f"ddt_number_{i}", "").strip()
            if ddt_num:
                row = db.execute(
                    "SELECT id FROM containers WHERE ddt_number=? AND is_deleted=0",
                    (ddt_num,)
                ).fetchone()
                if row:
                    container_ids.append(str(row["id"]))

        if not container_ids:
            continue

        production_batch = form.get(f"production_batch_{i}")
        manufacture_date = normalize_date(form.get(f"manufacture_date_{i}")) or None
        expiration_date  = normalize_date(form.get(f"expiration_date_{i}")) or None

        for container_id in container_ids:
            # Update container with COA data
            db.execute("""
                UPDATE containers SET
                    production_lot=?,
                    manufacture_date=?,
                    expiration_date=?,
                    updated_at=datetime('now')
                WHERE id=?
            """, (production_batch, manufacture_date, expiration_date, container_id))

            # Save COA document record
            saved_filename = form.get(f"saved_filename_{i}")
            original_name  = form.get(f"original_name_{i}")
            if saved_filename:
                c = db.execute("SELECT shipment_id FROM containers WHERE id=?", (container_id,)).fetchone()
                sh = db.execute("SELECT oda_id FROM shipments WHERE id=?", (c["shipment_id"],)).fetchone() if c else None
                p_id = db.execute("SELECT product_id FROM odas WHERE id=?", (sh["oda_id"],)).fetchone() if sh else None
                db.execute("""
                    INSERT INTO coa_documents
                        (container_id, shipment_id, product_id, lot_number,
                         filename, original_name, uploaded_by)
                    VALUES (?,?,?,?,?,?,?)
                """, (
                    container_id,
                    c["shipment_id"] if c else None,
                    p_id["product_id"] if p_id else None,
                    production_batch,
                    saved_filename,
                    original_name,
                    session.get("username", "")
                ))
            updated += 1

    db.commit()
    session.pop("coa_draft", None)
    flash(f"COA data applied to {updated} container(s).", "success")
    return redirect(url_for("containers"))
