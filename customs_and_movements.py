"""
BU Control — Customs Clearance, DDT Parser, COA Manual Assignment,
Demurrage Tracker
"""

import os, re, json, base64
from datetime import datetime, timedelta
from flask import Blueprint, request, render_template, redirect, url_for, session, flash, g
import sqlite3
import anthropic

ops_bp = Blueprint("ops", __name__)

DB_PATH = os.environ.get("DB_PATH", os.path.join(os.path.dirname(__file__), "db", "bu_control.db"))
ANTHROPIC_CLIENT = anthropic.Anthropic()
ALLOWED_EXTENSIONS = {"pdf", "png", "jpg", "jpeg"}

def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys=ON")
    return g.db

def allowed_file(f):
    return "." in f and f.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS

def normalize_container_code(code):
    if not code:
        return code
    return re.sub(r"[\s\-]", "", code.strip().upper())

def normalize_date(date_str):
    if not date_str:
        return None
    date_str = str(date_str).strip()
    if re.match(r"^\d{4}-\d{2}-\d{2}$", date_str):
        return date_str
    # DD/MM/YYYY or MM/DD/YYYY
    m = re.match(r"^(\d{1,2})[/\.\-](\d{1,2})[/\.\-](\d{4})$", date_str)
    if m:
        a, b, year = int(m.group(1)), int(m.group(2)), m.group(3)
        if a > 12:
            return f"{year}-{b:02d}-{a:02d}"
        return f"{year}-{a:02d}-{b:02d}"
    # YYYY.MM.DD
    m = re.match(r"^(\d{4})[/\.\-](\d{2})[/\.\-](\d{2})$", date_str)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    return date_str

def call_claude(prompt, file_b64, mime_type="image/png"):
    if mime_type == "application/pdf":
        file_block = {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": file_b64}}
    else:
        file_block = {"type": "image", "source": {"type": "base64", "media_type": mime_type, "data": file_b64}}
    response = ANTHROPIC_CLIENT.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2000,
        messages=[{"role": "user", "content": [file_block, {"type": "text", "text": prompt}]}]
    )
    text = response.content[0].text.strip()
    text = re.sub(r"^```json\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return json.loads(text)

# ── CUSTOMS CLEARANCE ─────────────────────────────────────────

@ops_bp.route("/customs", methods=["GET", "POST"])
def customs_clearance():
    if "user_id" not in session:
        return redirect(url_for("login"))

    db = get_db()

    if request.method == "POST":
        form = request.form
        container_ids = request.form.getlist("container_id")
        clearance_date = form["clearance_date"]
        facility_id = form["storage_facility_id"]
        total_customs_cost = float(form.get("total_customs_cost_eur", 0) or 0)
        num_containers = len(container_ids)

        for cid in container_ids:
            # Split customs cost equally across containers
            cost_per_ctr = round(total_customs_cost / num_containers, 2) if num_containers else 0
            actual_mt = form.get(f"actual_mt_{cid}")

            # Compute empty return deadline
            leasing_company_id = form.get(f"leasing_company_id_{cid}")
            deadline = None
            if leasing_company_id:
                rate = db.execute("""
                    SELECT free_days FROM leasing_rates
                    WHERE leasing_company_id=?
                      AND valid_from <= ?
                    ORDER BY valid_from DESC LIMIT 1
                """, (leasing_company_id, clearance_date)).fetchone()
                if rate:
                    d = datetime.strptime(clearance_date, "%Y-%m-%d")
                    deadline = (d + timedelta(days=rate["free_days"])).strftime("%Y-%m-%d")

            db.execute("""
                UPDATE containers SET
                    customs_clearance_date=?,
                    actual_mt=COALESCE(?,actual_mt),
                    customs_cost_eur=?,
                    storage_facility_id=?,
                    storage_entry_date=?,
                    leasing_company_id=COALESCE(?,leasing_company_id),
                    empty_return_deadline=COALESCE(?,empty_return_deadline),
                    status='IN_STORAGE',
                    updated_at=datetime('now')
                WHERE id=?
            """, (
                clearance_date,
                float(actual_mt) if actual_mt else None,
                cost_per_ctr if cost_per_ctr else None,
                facility_id,
                clearance_date,
                leasing_company_id or None,
                deadline,
                cid
            ))

            # Record movement
            db.execute("""
                INSERT INTO container_movements (
                    container_id, movement_type, movement_date,
                    to_facility_id, mt_moved, reference_doc,
                    entry_status, phase1_entered_by, source
                ) VALUES (?,?,?,?,?,?,?,?,?)
            """, (
                cid, "CUSTOMS_CLEARANCE", clearance_date,
                facility_id,
                float(actual_mt) if actual_mt else 0,
                form.get("reference_doc", ""),
                "COMPLETE",
                session.get("username", ""),
                "MANUAL"
            ))

        db.commit()
        flash(f"Customs clearance recorded for {num_containers} container(s).", "success")
        return redirect(url_for("ops.customs_clearance"))

    # GET — show pending containers (IN_TRANSIT sea containers)
    # Filter by ODA or shipment
    oda_filter = request.args.get("oda_id", "")
    shipment_filter = request.args.get("shipment_id", "")

    where = ["c.status IN ('IN_TRANSIT','IN_PORT')", "c.transport_mode = 'SEA'", "c.is_deleted = 0"]
    params = []
    if oda_filter:
        where.append("o.id = ?"); params.append(oda_filter)
    if shipment_filter:
        where.append("s.id = ?"); params.append(shipment_filter)

    containers = db.execute(f"""
        SELECT c.*, s.bl_number, s.vessel_name, o.oda_code,
               p.code AS product_code, su.name AS supplier_name,
               sf.name AS current_facility
        FROM containers c
        JOIN shipments s  ON s.id  = c.shipment_id
        JOIN odas o       ON o.id  = s.oda_id
        JOIN products p   ON p.id  = o.product_id
        JOIN suppliers su ON su.id = o.supplier_id
        LEFT JOIN storage_facilities sf ON sf.id = c.storage_facility_id
        WHERE {' AND '.join(where)}
        ORDER BY o.oda_code, s.shipment_number, c.container_code
    """, params).fetchall()

    odas = db.execute("""
        SELECT o.id, o.oda_code, s.name AS supplier_name
        FROM odas o JOIN suppliers s ON s.id=o.supplier_id
        WHERE o.is_deleted=0 ORDER BY o.order_date DESC
    """).fetchall()

    shipments = db.execute("""
        SELECT s.id, s.bl_number, s.shipment_number, o.oda_code
        FROM shipments s JOIN odas o ON o.id=s.oda_id
        WHERE s.is_deleted=0 ORDER BY o.order_date DESC, s.shipment_number
    """).fetchall()

    facilities = db.execute("SELECT * FROM storage_facilities WHERE is_active=1 ORDER BY code").fetchall()
    leasing_cos = db.execute("SELECT * FROM container_leasing_companies WHERE is_active=1 ORDER BY name").fetchall()

    return render_template("customs_clearance.html",
        containers=containers, odas=odas, shipments=shipments,
        facilities=facilities, leasing_cos=leasing_cos,
        oda_filter=oda_filter, shipment_filter=shipment_filter,
    )


# ── COA MANUAL ASSIGNMENT ─────────────────────────────────────

@ops_bp.route("/coa/assign", methods=["GET", "POST"])
def coa_assign():
    if "user_id" not in session:
        return redirect(url_for("login"))

    db = get_db()

    if request.method == "POST":
        form = request.form
        container_ids = form.getlist("container_id")
        production_lot = form.get("production_lot", "").strip()
        manufacture_date = normalize_date(form.get("manufacture_date"))
        expiration_date  = normalize_date(form.get("expiration_date"))
        coa_date         = normalize_date(form.get("coa_date"))

        updated = 0
        for cid in container_ids:
            db.execute("""
                UPDATE containers SET
                    production_lot=?,
                    manufacture_date=?,
                    expiration_date=?,
                    coa_date=?,
                    updated_at=datetime('now')
                WHERE id=?
            """, (production_lot or None, manufacture_date, expiration_date, coa_date, cid))
            updated += 1

        db.commit()
        flash(f"COA data assigned to {updated} container(s).", "success")
        return redirect(url_for("ops.coa_assign"))

    # GET — filters
    product_filter   = request.args.get("product_id", "")
    shipment_filter  = request.args.get("shipment_id", "")
    unassigned_only  = request.args.get("unassigned", "1")

    where = ["c.is_deleted=0"]
    params = []
    if product_filter:
        where.append("o.product_id=?"); params.append(product_filter)
    if shipment_filter:
        where.append("c.shipment_id=?"); params.append(shipment_filter)
    if unassigned_only == "1":
        where.append("c.production_lot IS NULL")

    containers = db.execute(f"""
        SELECT c.*, s.bl_number, s.ddt_number, o.oda_code,
               p.code AS product_code, su.name AS supplier_name
        FROM containers c
        JOIN shipments s  ON s.id  = c.shipment_id
        JOIN odas o       ON o.id  = s.oda_id
        JOIN products p   ON p.id  = o.product_id
        JOIN suppliers su ON su.id = o.supplier_id
        WHERE {' AND '.join(where)}
        ORDER BY o.oda_code, s.shipment_number, c.container_code
    """, params).fetchall()

    products  = db.execute("SELECT * FROM products  WHERE is_active=1 ORDER BY code").fetchall()
    shipments = db.execute("""
        SELECT s.id, s.bl_number, s.ddt_number, s.shipment_number, o.oda_code
        FROM shipments s JOIN odas o ON o.id=s.oda_id
        WHERE s.is_deleted=0 ORDER BY o.order_date DESC, s.shipment_number
    """).fetchall()

    return render_template("coa_assign.html",
        containers=containers, products=products, shipments=shipments,
        product_filter=product_filter, shipment_filter=shipment_filter,
        unassigned_only=unassigned_only,
    )


# ── DDT PARSER ───────────────────────────────────────────────

DDT_PROMPT = """
You are parsing an Italian DDT (Documento di Trasporto) combined with a COA (Certificato di Analisi).
The document may have 1 or 2 pages — page 1 is the DDT, page 2 is the COA.
Extract all relevant fields and return ONLY valid JSON, no other text.

Return this exact structure:
{
  "ddt_number": "string (DOC. N. or DDT number e.g. 3912)",
  "ddt_date": "ISO date YYYY-MM-DD (DEL field on DDT)",
  "carrier_name": "string or null (transport company)",
  "plate_number": "string or null (TARGA AUTOMEZZO)",
  "trailer_plate": "string or null",
  "driver": "string or null",
  "from_location": "string or null (pickup location)",
  "to_location": "string or null (delivery location)",
  "container_code": "string or null (ISO tank code if present, null for ATB road tankers)",
  "container_type": "ISOTANK or ATB or ONE_TON_CUBE",
  "product_description": "string or null",
  "actual_mt": "float or null (net weight in MT — look for quantity in T column, or PESO RISCONTRATO, or KG/1000)",
  "gross_weight_kg": "float or null",
  "tare_weight_kg": "float or null",
  "oda_reference": "string or null (look for Vs/Rif., Vs. Rif., Riferimento, Rif. ordine, NS. RIFERIMENTO)",
  "production_lot": "string or null (look for Lotto in product line OR Lotto NR / Lot NR on COA page)",
  "manufacture_date": "ISO date YYYY-MM-DD or null (from COA: Data di carico, Date of loading)",
  "expiration_date": "ISO date YYYY-MM-DD or null (from COA: scadenza, expiry)"
}

Rules:
- ddt_number: look for DOC. N. field or number after DDT
- actual_mt: for Rainoldi-style DDT look for quantity column in T (Tonnellate) — e.g. 22,540 T = 22.54 MT
- container_type: if IMBALLO is Autobotte use ATB, if Isotank use ISOTANK, if IBC/cubitainer use ONE_TON_CUBE
- oda_reference: Vs/Rif. field contains supplier order ref e.g. 2026-0039
- production_lot: may appear in product description line as "Lotto XXXXXX" OR on COA page
- This document may be 2 pages (DDT + COA) — extract lot from whichever page has it
- If a field is not found use null
"""

@ops_bp.route("/parse/ddt", methods=["GET", "POST"])
def parse_ddt():
    if "user_id" not in session:
        return redirect(url_for("login"))

    if request.method == "GET":
        session.pop("ddt_queue", None)
        db = get_db()
        odas = db.execute("""
            SELECT o.id, o.oda_code, s.name AS supplier_name
            FROM odas o JOIN suppliers s ON s.id=o.supplier_id
            WHERE o.is_deleted=0 ORDER BY o.order_date DESC
        """).fetchall()
        return render_template("parse_ddt.html", odas=odas)

    files = request.files.getlist("document")
    if not files or all(f.filename == "" for f in files):
        flash("No files uploaded.", "error")
        return redirect(request.url)

    db = get_db()
    odas = db.execute("""
        SELECT o.id, o.oda_code, s.name AS supplier_name
        FROM odas o JOIN suppliers s ON s.id=o.supplier_id
        WHERE o.is_deleted=0 ORDER BY o.order_date DESC
    """).fetchall()
    carriers = db.execute("SELECT * FROM carriers WHERE is_active=1 ORDER BY name").fetchall()
    facilities = db.execute("SELECT * FROM storage_facilities WHERE is_active=1 ORDER BY code").fetchall()
    products = db.execute("SELECT * FROM products WHERE is_active=1 ORDER BY code").fetchall()

    queue = []
    for f in files:
        if not f or not allowed_file(f.filename):
            continue
        file_bytes = f.read()
        ext = f.filename.rsplit(".", 1)[1].lower()
        mime_type = "application/pdf" if ext == "pdf" else f"image/{ext}"
        b64 = base64.standard_b64encode(file_bytes).decode("utf-8")
        try:
            extracted = call_claude(DDT_PROMPT, b64, mime_type)
            if extracted.get("container_code"):
                extracted["container_code"] = normalize_container_code(extracted["container_code"])
            if extracted.get("ddt_date"):
                extracted["ddt_date"] = normalize_date(extracted["ddt_date"])
            extracted["_filename"] = f.filename
            queue.append(extracted)
        except Exception as e:
            import traceback; traceback.print_exc()
            flash(f"Extraction failed for {f.filename}: {e}", "error")

    if not queue:
        flash("No DDTs could be extracted.", "error")
        return redirect(request.url)

    session["ddt_queue"] = queue
    session["ddt_odas"] = [dict(o) for o in odas]
    session["ddt_carriers"] = [dict(c) for c in carriers]
    session["ddt_facilities"] = [dict(f) for f in facilities]
    session["ddt_products"] = [dict(p) for p in products]
    return redirect(url_for("ops.parse_ddt_next"))


@ops_bp.route("/parse/ddt/next")
def parse_ddt_next():
    if "user_id" not in session:
        return redirect(url_for("login"))
    queue = session.get("ddt_queue", [])
    if not queue:
        flash("All DDTs processed.", "success")
        return redirect(url_for("containers"))
    extracted = queue[0]
    return render_template("parse_ddt_confirm.html",
        extracted=extracted,
        odas=session.get("ddt_odas", []),
        carriers=session.get("ddt_carriers", []),
        facilities=session.get("ddt_facilities", []),
        products=session.get("ddt_products", []),
        queue_remaining=len(queue),
    )


@ops_bp.route("/parse/ddt/confirm", methods=["POST"])
def parse_ddt_confirm():
    if "user_id" not in session:
        return redirect(url_for("login"))

    db = get_db()
    form = request.form
    oda_id = int(form["oda_id"])

    # Create shipment (DDT-based)
    last = db.execute("SELECT MAX(shipment_number) AS n FROM shipments WHERE oda_id=?", (oda_id,)).fetchone()
    next_num = (last["n"] or 0) + 1
    db.execute("""
        INSERT INTO shipments (
            oda_id, shipment_number, transport_mode,
            ddt_number, ddt_date, carrier_id, plate_number,
            source
        ) VALUES (?,?,?,?,?,?,?,?)
    """, (
        oda_id, next_num, "ROAD",
        form.get("ddt_number"), form.get("ddt_date"),
        form.get("carrier_id") or None,
        form.get("plate_number") or None,
        "DDT_PARSER"
    ))
    db.commit()
    shipment_id = db.execute(
        "SELECT id FROM shipments WHERE oda_id=? AND shipment_number=?", (oda_id, next_num)
    ).fetchone()["id"]

    # Determine container type
    ctype = form.get("container_type", "ATB")
    container_code = normalize_container_code(form.get("container_code", "")) or None
    nominal_mt = float(form.get("actual_mt") or form.get("nominal_mt") or 0)

    # For ATB — no container code, just MT
    db.execute("""
        INSERT INTO containers (
            container_code, shipment_id, container_type, transport_mode,
            nominal_mt, actual_mt, ddt_number, plate_number,
            production_lot, manufacture_date, expiration_date,
            storage_facility_id, storage_entry_date,
            status, source, entered_by
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        container_code,
        shipment_id,
        ctype,
        "ROAD",
        nominal_mt,
        float(form.get("actual_mt") or 0) or None,
        form.get("ddt_number"),
        form.get("plate_number") or None,
        form.get("production_lot") or None,
        form.get("manufacture_date") or None,
        form.get("expiration_date") or None,
        form.get("storage_facility_id") or None,
        form.get("ddt_date") or None,
        "IN_STORAGE" if form.get("storage_facility_id") else "IN_TRANSIT_ROAD",
        "DDT_PARSER",
        session.get("username", "")
    ))
    db.commit()

    # Close ODA if checkbox was ticked
    if form.get("close_oda"):
        db.execute("UPDATE odas SET is_closed=1, updated_at=datetime('now') WHERE id=?", (oda_id,))
        db.commit()

    # Pop queue
    queue = session.get("ddt_queue", [])
    if queue:
        queue.pop(0)
        session["ddt_queue"] = queue

    remaining = len(queue)
    if remaining > 0:
        flash(f"DDT {form.get('ddt_number')} imported. {remaining} more to process.", "success")
        return redirect(url_for("ops.parse_ddt_next"))

    flash(f"DDT {form.get('ddt_number')} imported.", "success")
    return redirect(url_for("containers"))


# ── DEMURRAGE TRACKER ─────────────────────────────────────────

@ops_bp.route("/demurrage")
def demurrage():
    if "user_id" not in session:
        return redirect(url_for("login"))

    db = get_db()
    status_filter = request.args.get("status", "")

    rows = db.execute("SELECT * FROM v_demurrage_tracker").fetchall()
    if status_filter:
        rows = [r for r in rows if r["return_status"] == status_filter]

    leasing_cos = db.execute("SELECT * FROM container_leasing_companies WHERE is_active=1 ORDER BY name").fetchall()
    total_exposure = sum(r["estimated_demurrage_eur"] or 0 for r in rows if r["return_status"] in ("OVERDUE",))

    return render_template("demurrage.html",
        rows=rows, status_filter=status_filter,
        leasing_cos=leasing_cos, total_exposure=total_exposure,
    )


@ops_bp.route("/demurrage/<int:container_id>/return", methods=["POST"])
def mark_returned(container_id):
    if "user_id" not in session:
        return redirect(url_for("login"))

    db = get_db()
    return_date = request.form.get("return_date") or datetime.now().strftime("%Y-%m-%d")
    actual_cost = request.form.get("demurrage_cost_eur") or None

    db.execute("""
        UPDATE containers SET
            empty_returned_date=?,
            demurrage_cost_eur=?,
            updated_at=datetime('now')
        WHERE id=?
    """, (return_date, float(actual_cost) if actual_cost else None, container_id))
    db.commit()
    flash("Empty return recorded.", "success")
    return redirect(url_for("ops.demurrage"))
