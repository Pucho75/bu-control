"""
BU Control — ODV Parser and Sales DDT Parser
ODV = Ordine Di Vendita (Sales Order)
DDT = Documento di Trasporto (delivery doc, closes the sale)
"""

import os, re, json, base64
from datetime import datetime
from flask import Blueprint, request, render_template, redirect, url_for, session, flash, g
import sqlite3
import anthropic
from fuzzy_match import find_best_match

sales_parse_bp = Blueprint("sales_parse", __name__)

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

def normalize_date(date_str):
    if not date_str:
        return None
    date_str = str(date_str).strip()
    if re.match(r"^\d{4}-\d{2}-\d{2}$", date_str):
        return date_str
    m = re.match(r"^(\d{1,2})[/\.\-](\d{1,2})[/\.\-](\d{4})$", date_str)
    if m:
        a, b, year = int(m.group(1)), int(m.group(2)), m.group(3)
        if a > 12:
            return f"{year}-{b:02d}-{a:02d}"
        return f"{year}-{a:02d}-{b:02d}"
    return date_str

def call_claude(prompt, file_b64, mime_type="image/png"):
    if mime_type == "application/pdf":
        file_block = {"type": "document", "source": {"type": "base64",
            "media_type": "application/pdf", "data": file_b64}}
    else:
        file_block = {"type": "image", "source": {"type": "base64",
            "media_type": mime_type, "data": file_b64}}
    response = ANTHROPIC_CLIENT.messages.create(
        model="claude-sonnet-4-6", max_tokens=2000,
        messages=[{"role": "user", "content": [
            file_block, {"type": "text", "text": prompt}
        ]}]
    )
    text = response.content[0].text.strip()
    text = re.sub(r"^```json\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return json.loads(text)

def next_sale_code(db):
    """Generate next sale code e.g. ODV-2026-0001"""
    year = datetime.now().year
    row = db.execute("""
        SELECT MAX(CAST(SUBSTR(sale_code, -4) AS INTEGER)) AS n
        FROM sales WHERE sale_code LIKE ?
    """, (f"ODV-{year}-%",)).fetchone()
    n = (row["n"] or 0) + 1
    return f"ODV-{year}-{n:04d}"

# ── ODV PARSER PROMPT ─────────────────────────────────────────

ODV_PROMPT = """
You are parsing an ODV (Ordine Di Vendita / Sales Order / Conferma d'ordine) document
from an Italian chemical trading company. The document may contain MULTIPLE product lines.
Extract the following fields and return ONLY valid JSON, no other text.

Return this exact structure:
{
  "odv_number": "string e.g. 2026-0089/1",
  "odv_date": "ISO date YYYY-MM-DD",
  "customer_name": "string (delivery address name or Destinatario)",
  "customer_country": "2-letter ISO country code",
  "incoterm": "string e.g. DAP, EXW, DDP",
  "incoterm_location": "string, delivery point city only",
  "incoterm_full": "full incoterm string as written e.g. DDP FERRARA",
  "currency": "USD or EUR",
  "payment_terms": "string or null",
  "lines": [
    {
      "line_number": 1,
      "product_code": "string — the article/product code e.g. ESO, DOA, FAME",
      "product_description": "string — full product description",
      "mt_planned": float,
      "price_per_mt": float,
      "delivery_date": "ISO date YYYY-MM-DD or null",
      "destination": "string or null"
    }
  ]
}

Rules:
- odv_number: extract from title "Conferma d'ordine" or "Ordine di vendita" number
- odv_date: extract date from title
- customer_name: from Destinatario / delivery address block
- lines: extract ONE entry per product line — there may be 1, 2 or more products
- product_code: the short article code (Articolo column) e.g. ESO/02/OSOIA -> use ESO, DOA/02/DOA -> use DOA
- price_per_mt: numeric value only, no currency symbol
- currency: look for EUR/EURO or USD in footer
- If a field is not found use null
"""

# ── ODV IMPORT ────────────────────────────────────────────────

@sales_parse_bp.route("/parse/odv", methods=["GET", "POST"])
def parse_odv():
    if "user_id" not in session:
        return redirect(url_for("login"))

    if request.method == "GET":
        session.pop("odv_queue", None)
        return render_template("parse_odv.html")

    files = request.files.getlist("document")
    if not files or all(f.filename == "" for f in files):
        flash("No files uploaded.", "error")
        return redirect(request.url)

    db = get_db()
    customers = db.execute("SELECT * FROM customers WHERE is_active=1 ORDER BY name").fetchall()
    products  = db.execute("SELECT * FROM products  WHERE is_active=1 ORDER BY code").fetchall()
    facilities = db.execute("SELECT * FROM storage_facilities WHERE is_active=1 ORDER BY code").fetchall()

    queue = []
    for f in files:
        if not f or not allowed_file(f.filename):
            continue
        file_bytes = f.read()
        ext = f.filename.rsplit(".", 1)[1].lower()
        mime_type = "application/pdf" if ext == "pdf" else f"image/{ext}"
        b64 = base64.standard_b64encode(file_bytes).decode("utf-8")
        try:
            extracted = call_claude(ODV_PROMPT, b64, mime_type)
            if extracted.get("odv_date"):
                extracted["odv_date"] = normalize_date(extracted["odv_date"])
            for line in extracted.get("lines", []):
                if line.get("delivery_date"):
                    line["delivery_date"] = normalize_date(line["delivery_date"])
            extracted["_filename"] = f.filename
            queue.append(extracted)
        except Exception as e:
            import traceback; traceback.print_exc()
            flash(f"Extraction failed for {f.filename}: {e}", "error")

    if not queue:
        flash("No ODVs could be extracted.", "error")
        return redirect(request.url)

    session["odv_queue"] = queue
    session["odv_customers"] = [dict(c) for c in customers]
    session["odv_products"]  = [dict(p) for p in products]
    session["odv_facilities"] = [dict(f) for f in facilities]
    return redirect(url_for("sales_parse.parse_odv_next"))


@sales_parse_bp.route("/parse/odv/next")
def parse_odv_next():
    if "user_id" not in session:
        return redirect(url_for("login"))

    queue = session.get("odv_queue", [])
    if not queue:
        flash("All ODVs processed.", "success")
        return redirect(url_for("sales_pipeline"))

    extracted = queue[0]
    customers  = session.get("odv_customers", [])
    products   = session.get("odv_products", [])
    facilities = session.get("odv_facilities", [])

    # Fuzzy match customer
    matched_customer_id = None
    customer_match_type = None
    customer_match_name = None
    if extracted.get("customer_name"):
        record, score, match_type = find_best_match(
            extracted["customer_name"], customers, name_field="name"
        )
        if record:
            matched_customer_id = record["id"]
            customer_match_type = match_type
            customer_match_name = record["name"]

    # Match product per line — try exact code first, then fuzzy name
    def match_product(line):
        pid = None
        code = (line.get("product_code") or "").strip().upper()
        desc = (line.get("product_description") or "").strip()
        # 1. Exact code match
        if code:
            for p in products:
                if p["code"].upper() == code or p["code"].upper() in code:
                    pid = p["id"]
                    break
        # 2. Fuzzy name match fallback
        if not pid and desc:
            record, score, match_type = find_best_match(desc, products, name_field="name")
            if record:
                pid = record["id"]
        return pid

    matched_product_ids = [match_product(line) for line in extracted.get("lines", [{}])]

    remaining = len(queue)
    return render_template("parse_odv_confirm.html",
        extracted=extracted,
        customers=customers,
        products=products,
        facilities=facilities,
        matched_customer_id=matched_customer_id,
        customer_match_type=customer_match_type,
        customer_match_name=customer_match_name,
        matched_product_ids=matched_product_ids,
        queue_remaining=remaining,
    )


@sales_parse_bp.route("/parse/odv/confirm", methods=["POST"])
def parse_odv_confirm():
    if "user_id" not in session:
        return redirect(url_for("login"))

    db = get_db()
    form = request.form

    # Resolve or create customer
    customer_id = form.get("customer_id")
    if not customer_id:
        cname = form.get("customer_name", "").strip()
        if cname:
            db.execute("INSERT OR IGNORE INTO customers (name, country) VALUES (?,?)",
                (cname, form.get("customer_country") or None))
            db.commit()
            customer_id = db.execute(
                "SELECT id FROM customers WHERE name=?", (cname,)
            ).fetchone()["id"]
        else:
            flash("Customer is required.", "error")
            return redirect(url_for("sales_parse.parse_odv_next"))
    customer_id = int(customer_id)

    facility_id  = form.get("storage_facility_id") or None
    if facility_id:
        facility_id = int(facility_id)
    num_lines    = int(form.get("num_lines", 1))

    # Create one sale record per ODV line — each line has its own product and price
    created = 0
    for i in range(1, num_lines + 1):
        mt = form.get(f"mt_{i}")
        if not mt:
            continue
        # Per-line product and price
        line_product_id = form.get(f"product_id_{i}") or form.get("product_id")
        if not line_product_id:
            flash(f"Line {i}: product is required.", "error")
            continue
        line_product_id = int(line_product_id)
        price_eur = float(form.get(f"price_eur_{i}") or form.get("price_eur") or 0)
        price_usd = float(form.get(f"price_usd_{i}") or form.get("price_usd") or 0) or None
        exch_rate = float(form.get(f"exchange_rate_{i}") or form.get("exchange_rate") or 0) or None

        odv_number = form.get("odv_number", "").strip()
        # Use real ODV number as sale_code. If multi-line, append line suffix for uniqueness.
        base_code = odv_number if odv_number else next_sale_code(db)
        sale_code = base_code if num_lines == 1 else f"{base_code}-L{i}"

        # Check for existing sale with this odv_ref + product + line to avoid duplicates
        dup = db.execute("""
            SELECT id FROM sales
            WHERE odv_ref=? AND product_id=? AND is_deleted=0
        """, (odv_number, line_product_id)).fetchone()
        if dup:
            flash(f"Line {i}: ODV {odv_number} for this product already exists (sale #{dup['id']}) — skipped.", "warning")
            continue

        # Ensure sale_code uniqueness even if reused
        code_exists = db.execute("SELECT id FROM sales WHERE sale_code=?", (sale_code,)).fetchone()
        if code_exists:
            sale_code = f"{sale_code}-{datetime.now().strftime('%H%M%S')}"

        db.execute("""
            INSERT INTO sales (
                sale_code, customer_id, product_id, destination,
                storage_facility_id, incoterm,
                price_eur_per_mt, price_usd_per_mt, exchange_rate,
                provisional_date, provisional_mt, provisional_entered_by,
                odv_ref, status, source
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            sale_code,
            customer_id,
            line_product_id,
            form.get(f"destination_{i}") or form.get("incoterm_location") or "",
            facility_id,
            form.get("incoterm") or "DAP",
            price_eur,
            price_usd,
            exch_rate,
            form.get(f"delivery_date_{i}") or form.get("odv_date") or datetime.now().strftime("%Y-%m-%d"),
            float(mt),
            session.get("username", "B"),
            odv_number,
            "PROVISIONAL",
            "ODV_PARSER",
        ))
        created += 1

    db.commit()

    # Pop queue
    queue = session.get("odv_queue", [])
    if queue:
        queue.pop(0)
        session["odv_queue"] = queue

    remaining = len(queue)
    if remaining > 0:
        flash(f"ODV {form.get('odv_number')} imported — {created} sale(s). {remaining} more.", "success")
        return redirect(url_for("sales_parse.parse_odv_next"))

    flash(f"ODV {form.get('odv_number')} imported — {created} sale(s) created.", "success")
    return redirect(url_for("sales_pipeline"))


# ── SALES DDT PARSER ──────────────────────────────────────────

SALES_DDT_PROMPT = """
You are parsing an Italian DDT (Documento di Trasporto) for an outbound sale
from a chemical trading company to a customer. The DDT may contain MULTIPLE product lines.

Extract the following fields and return ONLY valid JSON, no other text.

Return this exact structure:
{
  "ddt_number": "string",
  "ddt_date": "ISO date YYYY-MM-DD",
  "customer_name": "string or null",
  "delivery_address": "string or null",
  "carrier_name": "string or null",
  "plate_number": "string or null",
  "odv_reference": "string or null (look for conferma d'ordine N° or RIF ORD number)",
  "production_lots": ["string", ...],
  "lines": [
    {
      "product_code": "string — article code e.g. ESO, DOA",
      "product_description": "string",
      "actual_mt": float,
      "production_lot": "string or null"
    }
  ]
}

Rules:
- ddt_number: look for DDT, N. DDT, Documento di Trasporto number in title
- lines: one entry per product line — extract ALL products listed
- actual_mt per line: net weight in MT for that product. Convert KG to MT if needed.
- product_code: the Articolo code e.g. ESO/02/OSOIA -> ESO, DOA/02/DOA -> DOA
- production_lots: flat ARRAY of ALL lot numbers found anywhere (LOTTO, BATCH, LOT)
- odv_reference: the conferma d'ordine or sales order number referenced
- If a field is not found use null or empty array []
"""

@sales_parse_bp.route("/parse/sales-ddt", methods=["GET", "POST"])
def parse_sales_ddt():
    if "user_id" not in session:
        return redirect(url_for("login"))

    if request.method == "GET":
        db = get_db()
        sales = db.execute("""
            SELECT s.id, s.sale_code, s.provisional_mt, s.status,
                   s.storage_facility_id,
                   c.name AS customer_name, p.code AS product_code,
                   sf.code AS facility_code
            FROM sales s
            JOIN customers c ON c.id = s.customer_id
            JOIN products p  ON p.id = s.product_id
            LEFT JOIN storage_facilities sf ON sf.id = s.storage_facility_id
            WHERE s.status IN ('PROVISIONAL','INSTRUCTED')
              AND s.is_deleted = 0
            ORDER BY s.provisional_date DESC
        """).fetchall()
        facilities = db.execute("SELECT * FROM storage_facilities WHERE is_active=1 ORDER BY code").fetchall()
        return render_template("parse_sales_ddt.html", sales=sales, facilities=facilities,
            today=datetime.now().strftime("%Y-%m-%d"))

    f = request.files.get("document")
    if f and f.filename and allowed_file(f.filename):
        file_bytes = f.read()
        ext = f.filename.rsplit(".", 1)[1].lower()
        mime_type = "application/pdf" if ext == "pdf" else f"image/{ext}"
        b64 = base64.standard_b64encode(file_bytes).decode("utf-8")
        try:
            extracted = call_claude(SALES_DDT_PROMPT, b64, mime_type)
            if extracted.get("ddt_date"):
                extracted["ddt_date"] = normalize_date(extracted["ddt_date"])
        except Exception as e:
            import traceback; traceback.print_exc()
            flash(f"Extraction failed: {e}", "error")
            return redirect(request.url)
        session["sales_ddt_draft"] = extracted
    else:
        # Manual mode — no extraction
        session["sales_ddt_draft"] = {}

    db = get_db()
    sales = db.execute("""
        SELECT s.id, s.sale_code, s.provisional_mt, s.status,
               s.storage_facility_id,
               c.name AS customer_name, p.code AS product_code
        FROM sales s
        JOIN customers c ON c.id = s.customer_id
        JOIN products p  ON p.id = s.product_id
        WHERE s.status IN ('PROVISIONAL','INSTRUCTED') AND s.is_deleted=0
        ORDER BY s.provisional_date DESC
    """).fetchall()
    facilities = db.execute("SELECT * FROM storage_facilities WHERE is_active=1 ORDER BY code").fetchall()

    extracted = session.get("sales_ddt_draft", {})

    # Try to match containers for extracted lots
    lot_containers = []
    matched_lot_ids = set()
    for lot in extracted.get("production_lots") or ([extracted.get("production_lot")] if extracted.get("production_lot") else []):
        if not lot:
            continue
        rows = db.execute("""
            SELECT c.id, c.container_code, c.production_lot,
                   COALESCE(c.actual_mt, c.nominal_mt) AS mt,
                   COALESCE(c.actual_mt, c.nominal_mt) - COALESCE((
                       SELECT SUM(sl.mt_drawn) FROM sale_lots sl WHERE sl.container_id = c.id
                   ), 0) AS available_mt,
                   o.oda_code, o.id AS oda_id, s2.bl_number, s2.id AS shipment_id,
                   sf.code AS facility_code, sf.id AS facility_id,
                   o.price_eur_per_mt AS purchase_price,
                   c.status
            FROM containers c
            JOIN shipments s2 ON s2.id = c.shipment_id
            JOIN odas o       ON o.id  = s2.oda_id
            LEFT JOIN storage_facilities sf ON sf.id = c.storage_facility_id
            WHERE c.production_lot = ?
              AND c.status IN ('IN_STORAGE', 'PARTIALLY_SOLD')
              AND c.is_deleted = 0
        """, (lot,)).fetchall()
        for r in rows:
            lot_containers.append(dict(r))
            matched_lot_ids.add(r["id"])

    # Also load ALL available containers for manual allocation
    all_available = db.execute("""
        SELECT c.id, c.container_code, c.production_lot,
               COALESCE(c.actual_mt, c.nominal_mt) AS mt,
               COALESCE(c.actual_mt, c.nominal_mt) - COALESCE((
                   SELECT SUM(sl.mt_drawn) FROM sale_lots sl WHERE sl.container_id = c.id
               ), 0) AS available_mt,
               o.oda_code, o.id AS oda_id, s2.bl_number, s2.id AS shipment_id,
               sf.code AS facility_code, sf.id AS facility_id,
               o.price_eur_per_mt AS purchase_price,
               c.status
        FROM containers c
        JOIN shipments s2 ON s2.id = c.shipment_id
        JOIN odas o       ON o.id  = s2.oda_id
        LEFT JOIN storage_facilities sf ON sf.id = c.storage_facility_id
        WHERE c.status IN ('IN_STORAGE', 'PARTIALLY_SOLD')
          AND c.is_deleted = 0
          AND COALESCE(c.actual_mt, c.nominal_mt) - COALESCE((
              SELECT SUM(sl.mt_drawn) FROM sale_lots sl WHERE sl.container_id = c.id
          ), 0) > 0
        ORDER BY c.customs_clearance_date ASC NULLS LAST,
                 c.storage_entry_date ASC NULLS LAST,
                 o.order_date ASC
    """).fetchall()

    # Build sale_lines: try to match ODV reference to group sales by product
    odv_ref = extracted.get("odv_reference") or extracted.get("odv_ref") or ""
    # Find matching sales by ODV ref
    matched_sales = []
    if odv_ref:
        matched_sales = db.execute("""
            SELECT s.id, s.sale_code, s.provisional_mt, s.status,
                   s.storage_facility_id, s.price_eur_per_mt,
                   c.name AS customer_name, p.code AS product_code, p.id AS product_id
            FROM sales s
            JOIN customers c ON c.id = s.customer_id
            JOIN products p  ON p.id = s.product_id
            WHERE s.odv_ref = ? AND s.status IN ('PROVISIONAL','INSTRUCTED') AND s.is_deleted=0
            ORDER BY s.id
        """, (odv_ref,)).fetchall()

    # Build per-product lot containers filtered by product
    def get_lot_containers_for_product(product_id, lots):
        rows = []
        seen = set()
        for lot in lots:
            if not lot:
                continue
            rs = db.execute("""
                SELECT c.id, c.container_code, c.production_lot,
                       COALESCE(c.actual_mt, c.nominal_mt) AS mt,
                       COALESCE(c.actual_mt, c.nominal_mt) - COALESCE((
                           SELECT SUM(sl.mt_drawn) FROM sale_lots sl WHERE sl.container_id = c.id
                       ), 0) AS available_mt,
                       o.oda_code, o.id AS oda_id, s2.bl_number, s2.id AS shipment_id,
                       sf.code AS facility_code, sf.id AS facility_id,
                       o.price_eur_per_mt AS purchase_price, c.status
                FROM containers c
                JOIN shipments s2 ON s2.id = c.shipment_id
                JOIN odas o       ON o.id  = s2.oda_id
                LEFT JOIN storage_facilities sf ON sf.id = c.storage_facility_id
                WHERE c.production_lot = ?
                  AND o.product_id = ?
                  AND c.status IN ('IN_STORAGE', 'PARTIALLY_SOLD')
                  AND c.is_deleted = 0
            """, (lot, product_id)).fetchall()
            for r in rs:
                if r["id"] not in seen:
                    rows.append(dict(r))
                    seen.add(r["id"])
        return rows

    def get_all_available_for_product(product_id):
        return db.execute("""
            SELECT c.id, c.container_code, c.production_lot,
                   COALESCE(c.actual_mt, c.nominal_mt) AS mt,
                   COALESCE(c.actual_mt, c.nominal_mt) - COALESCE((
                       SELECT SUM(sl.mt_drawn) FROM sale_lots sl WHERE sl.container_id = c.id
                   ), 0) AS available_mt,
                   o.oda_code, o.id AS oda_id, s2.bl_number, s2.id AS shipment_id,
                   sf.code AS facility_code, sf.id AS facility_id,
                   o.price_eur_per_mt AS purchase_price, c.status
            FROM containers c
            JOIN shipments s2 ON s2.id = c.shipment_id
            JOIN odas o       ON o.id  = s2.oda_id
            LEFT JOIN storage_facilities sf ON sf.id = c.storage_facility_id
            WHERE c.status IN ('IN_STORAGE', 'PARTIALLY_SOLD')
              AND o.product_id = ?
              AND c.is_deleted = 0
              AND COALESCE(c.actual_mt, c.nominal_mt) - COALESCE((
                  SELECT SUM(sl.mt_drawn) FROM sale_lots sl WHERE sl.container_id = c.id
              ), 0) > 0
            ORDER BY c.customs_clearance_date ASC NULLS LAST,
                     c.storage_entry_date ASC NULLS LAST,
                     o.order_date ASC
        """, (product_id,)).fetchall()

    lots = extracted.get("production_lots") or (
        [extracted.get("production_lot")] if extracted.get("production_lot") else []
    )

    if matched_sales:
        # Multi-product mode
        sale_lines = []
        for s in matched_sales:
            lc = get_lot_containers_for_product(s["product_id"], lots)
            av = get_all_available_for_product(s["product_id"])
            matched_ids = {c["id"] for c in lc}

            # Auto-FIFO: if no lot-matched containers, suggest from available in FIFO order
            fifo_suggested = []
            if not lc and av:
                remaining = s["provisional_mt"] or 0
                for c in av:
                    if remaining <= 0:
                        break
                    draw = min(c["available_mt"], remaining)
                    fifo_suggested.append({"container": dict(c), "draw_mt": round(draw, 3)})
                    remaining -= draw

            sale_lines.append({
                "sale": dict(s),
                "lot_containers": lc,
                "all_available": [dict(r) for r in av],
                "matched_ids": matched_ids,
                "fifo_suggested": fifo_suggested,
            })
        carriers = db.execute("SELECT * FROM carriers WHERE is_active=1 ORDER BY name").fetchall()
        return render_template("parse_sales_ddt_confirm.html",
            extracted=extracted,
            sale_lines=sale_lines,
            sales=[dict(s) for s in sales],
            facilities=facilities,
            carriers=[dict(c) for c in carriers],
            selected_facility_id=sale_lines[0]["lot_containers"][0]["facility_id"]
                if sale_lines and sale_lines[0]["lot_containers"] else None,
            today=datetime.now().strftime("%Y-%m-%d"),
        )
    else:
        # Single-product mode (legacy)
        carriers = db.execute("SELECT * FROM carriers WHERE is_active=1 ORDER BY name").fetchall()
        return render_template("parse_sales_ddt_confirm.html",
            extracted=extracted,
            sale_lines=None,
            sales=[dict(s) for s in sales],
            facilities=facilities,
            carriers=[dict(c) for c in carriers],
            lot_containers=lot_containers,
            all_available=[dict(r) for r in all_available],
            matched_lot_ids=matched_lot_ids,
            selected_facility_id=lot_containers[0]["facility_id"] if lot_containers else None,
            today=datetime.now().strftime("%Y-%m-%d"),
        )



@sales_parse_bp.route("/parse/sales-ddt/confirm", methods=["POST"])
def parse_sales_ddt_confirm():
    if "user_id" not in session:
        return redirect(url_for("login"))

    db = get_db()
    form = request.form
    ddt_number      = form.get("ddt_number", "").strip()
    ddt_date        = form.get("ddt_date", "")
    ddt_facility_id = form.get("storage_facility_id") or None

    # Transport cost — 100% on first sale line
    transport_eur = float(form.get("transport_to_customer_eur") or 0)
    carrier_id    = form.get("carrier_id") or None
    transport_override_reason = form.get("transport_override_reason", "").strip() or None

    # Collect sale_ids: sale_id_1, sale_id_2, ... or legacy sale_id
    sale_ids = []
    i = 1
    while form.get(f"sale_id_{i}"):
        sale_ids.append(int(form[f"sale_id_{i}"]))
        i += 1
    if not sale_ids and form.get("sale_id"):
        sale_ids = [int(form["sale_id"])]

    all_lots = []
    executed_codes = []

    for idx, sale_id in enumerate(sale_ids):
        sale = db.execute(
            "SELECT storage_facility_id, sale_code, price_eur_per_mt, actual_mt FROM sales WHERE id=?",
            (sale_id,)
        ).fetchone()
        if not sale:
            continue

        actual_mt = float(form.get(f"actual_mt_{idx+1}") or form.get("actual_mt") or 0) or None

        # Transport 100% on first line
        line_transport = transport_eur if idx == 0 else 0.0
        transp_per_mt  = round(line_transport / actual_mt, 4) if actual_mt and line_transport else 0.0

        odv_facility_id = sale["storage_facility_id"]
        depot_note = ""
        if odv_facility_id and ddt_facility_id and str(odv_facility_id) != str(ddt_facility_id):
            odv_f = db.execute("SELECT code FROM storage_facilities WHERE id=?", (odv_facility_id,)).fetchone()
            ddt_f = db.execute("SELECT code FROM storage_facilities WHERE id=?", (ddt_facility_id,)).fetchone()
            depot_note = f"Depot: {odv_f['code'] if odv_f else '?'} → {ddt_f['code'] if ddt_f else '?'}"

        db.execute("""
            UPDATE sales SET
                status='EXECUTED',
                ddt_number=?,
                ddt_load_date=?,
                actual_mt=?,
                storage_facility_id=COALESCE(?,storage_facility_id),
                transport_to_customer_eur=?,
                carrier_id=COALESCE(?,carrier_id),
                transport_override_reason=?,
                notes=CASE WHEN ? != '' THEN COALESCE(notes||' | ','') || ? ELSE notes END,
                ddt_entered_by=?,
                ddt_entered_at=datetime('now'),
                updated_at=datetime('now')
            WHERE id=?
        """, (ddt_number, ddt_date, actual_mt, ddt_facility_id,
              line_transport, carrier_id, transport_override_reason,
              depot_note, depot_note, session.get("username", ""), sale_id))

        sale_price = sale["price_eur_per_mt"] or 0
        num_rows   = int(form.get(f"alloc_num_rows_{idx+1}") or form.get("alloc_num_rows") or 0)
        import sys
        print(f"DEBUG sale_id={sale_id} idx={idx} num_rows={num_rows}", file=sys.stderr)
        print(f"DEBUG form keys: {[k for k in form.keys() if 'alloc' in k]}", file=sys.stderr)
        lots_assigned = []

        for j in range(num_rows):
            prefix = f"alloc_{idx+1}_"
            cid         = form.get(f"{prefix}container_id_{j}")
            shipment_id = form.get(f"{prefix}shipment_id_{j}")
            lot         = form.get(f"{prefix}lot_{j}", "")
            purchase_price = float(form.get(f"{prefix}purchase_price_{j}") or 0)
            draw        = float(form.get(f"{prefix}draw_mt_{j}") or 0)
            if not cid or draw <= 0:
                continue

            landed    = purchase_price + transp_per_mt
            margin_pm = sale_price - landed
            db.execute("""
                INSERT INTO sale_lots (
                    sale_id, shipment_id, container_id, mt_drawn,
                    purchase_price_eur_per_mt,
                    transport_to_customer_eur_per_mt,
                    landed_cost_eur_per_mt,
                    sale_price_eur_per_mt, margin_eur_per_mt, margin_eur_total
                ) VALUES (?,?,?,?,?,?,?,?,?,?)
            """, (sale_id, shipment_id, cid, draw,
                  purchase_price, transp_per_mt, landed,
                  sale_price, margin_pm, margin_pm * draw))

            avail = db.execute(
                "SELECT COALESCE(actual_mt, nominal_mt) AS mt FROM containers WHERE id=?", (cid,)
            ).fetchone()
            total_drawn = db.execute(
                "SELECT COALESCE(SUM(mt_drawn),0) AS n FROM sale_lots WHERE container_id=?", (cid,)
            ).fetchone()["n"]
            new_status = "FULLY_SOLD" if total_drawn >= (avail["mt"] or 0) - 0.001 else "PARTIALLY_SOLD"
            db.execute("UPDATE containers SET status=? WHERE id=?", (new_status, cid))
            if lot:
                lots_assigned.append(lot)

        # Set fifo_assigned=1 if any MT was drawn, regardless of lot names
        if num_rows > 0:
            db.execute("UPDATE sales SET fifo_assigned=1 WHERE id=?", (sale_id,))
        all_lots.extend(lots_assigned)
        executed_codes.append(sale["sale_code"])

    try:
        db.commit()
    except Exception as e:
        import sys
        print(f"DEBUG COMMIT ERROR: {e}", file=sys.stderr)
        raise
    session.pop("sales_ddt_draft", None)
    msg = f"DDT {ddt_number} — {len(executed_codes)} sale(s) EXECUTED: {', '.join(executed_codes)}."
    if all_lots:
        msg += f" Lots: {', '.join(set(all_lots))}."
    flash(msg, "success")
    return redirect(url_for("sales_pipeline"))

# ── TRANSPORT INSTRUCTION LOG ─────────────────────────────────

@sales_parse_bp.route("/sales/<int:sale_id>/instruct", methods=["POST"])
def log_transport_instruction(sale_id):
    if "user_id" not in session:
        return redirect(url_for("login"))

    db = get_db()
    form = request.form

    db.execute("""
        UPDATE sales SET
            status='INSTRUCTED',
            instruction_date=?,
            carrier_booking_ref=?,
            instructed_mt=?,
            instruction_entered_by=?,
            updated_at=datetime('now')
        WHERE id=? AND status='PROVISIONAL'
    """, (
        form.get("instruction_date") or datetime.now().strftime("%Y-%m-%d"),
        form.get("carrier_ref", "").strip() or None,
        float(form.get("instructed_mt") or 0) or None,
        session.get("username", ""),
        sale_id,
    ))
    db.commit()
    flash("Transport instruction logged — sale moved to INSTRUCTED.", "success")
    return redirect(url_for("sales_pipeline"))
