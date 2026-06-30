"""
BU Control — Combo Import (ODA + B/L + COA in one flow)
"""
from flask import Blueprint, render_template, request, redirect, url_for, session, flash, g
import sqlite3, os

combo_bp = Blueprint("combo", __name__)

DB_PATH = os.environ.get("DB_PATH", os.path.join(os.path.dirname(__file__), "db", "bu_control.db"))

def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys=ON")
    return g.db


@combo_bp.route("/parse/combo", methods=["GET", "POST"])
def parse_combo():
    if "user_id" not in session:
        return redirect(url_for("login"))

    if request.method == "GET":
        return render_template("parse_combo.html")

    from parse_docs import ODA_PROMPT, BL_PROMPT, COA_PROMPT, call_claude as call_claude_docs, file_to_base64, find_best_match

    db = get_db()

    oda_file = request.files.get("oda_file")
    bl_file  = request.files.get("bl_file")
    coa_file = request.files.get("coa_file")

    if not (oda_file and bl_file and coa_file):
        flash("Please upload all 3 files: ODA, B/L, and COA.", "error")
        return redirect(request.url)

    results = {}

    oda_bytes = oda_file.read()
    ext = oda_file.filename.rsplit(".", 1)[1].lower()
    mime = "application/pdf" if ext == "pdf" else f"image/{ext}"
    b64 = file_to_base64(oda_bytes, mime)
    try:
        results["oda"] = call_claude_docs(ODA_PROMPT, b64, mime)
        results["oda"]["_filename"] = oda_file.filename
    except Exception as e:
        flash(f"ODA extraction failed: {e}", "error")
        return redirect(request.url)

    bl_bytes = bl_file.read()
    ext = bl_file.filename.rsplit(".", 1)[1].lower()
    mime = "application/pdf" if ext == "pdf" else f"image/{ext}"
    b64 = file_to_base64(bl_bytes, mime)
    try:
        results["bl"] = call_claude_docs(BL_PROMPT, b64, mime)
        results["bl"]["_filename"] = bl_file.filename
    except Exception as e:
        flash(f"B/L extraction failed: {e}", "error")
        return redirect(request.url)

    coa_bytes = coa_file.read()
    ext = coa_file.filename.rsplit(".", 1)[1].lower()
    mime = "application/pdf" if ext == "pdf" else f"image/{ext}"
    b64 = file_to_base64(coa_bytes, mime)
    try:
        coa_extracted = call_claude_docs(COA_PROMPT, b64, mime)
        results["coa"] = coa_extracted.get("pages", [{}])[0] if coa_extracted.get("pages") else {}
        results["coa"]["_filename"] = coa_file.filename
    except Exception as e:
        flash(f"COA extraction failed: {e}", "error")
        return redirect(request.url)

    products  = db.execute("SELECT * FROM products  WHERE is_active=1 ORDER BY code").fetchall()
    suppliers = db.execute("SELECT * FROM suppliers WHERE is_active=1 ORDER BY name").fetchall()

    matched_supplier_id = None
    if results["oda"].get("supplier_name"):
        record, score, match_type = find_best_match(
            results["oda"]["supplier_name"], suppliers, name_field="name"
        )
        if record:
            matched_supplier_id = record["id"]

    matched_product_id = None
    extracted_desc = ""
    if results["oda"].get("shipments"):
        extracted_desc = results["oda"]["shipments"][0].get("product_description", "")
        if extracted_desc:
            record, score, match_type = find_best_match(extracted_desc, products, name_field="name")
            if record:
                matched_product_id = record["id"]

    facilities = db.execute("SELECT * FROM storage_facilities WHERE is_active=1 ORDER BY code").fetchall()
    carriers   = db.execute("SELECT * FROM carriers WHERE is_active=1 ORDER BY name").fetchall()

    import uuid
    coa_dir = os.path.join(os.path.dirname(__file__), "uploads", "coas")
    os.makedirs(coa_dir, exist_ok=True)
    safe_name = f"{uuid.uuid4().hex}_{coa_file.filename.replace(' ', '_')}"
    with open(os.path.join(coa_dir, safe_name), "wb") as out:
        out.write(coa_bytes)
    results["coa"]["_saved_filename"] = safe_name

    return render_template("parse_combo_confirm.html",
        results=results,
        products=[dict(p) for p in products],
        suppliers=[dict(s) for s in suppliers],
        facilities=[dict(f) for f in facilities],
        carriers=[dict(c) for c in carriers],
        matched_supplier_id=matched_supplier_id,
        matched_product_id=matched_product_id,
        extracted_desc=extracted_desc,
    )


@combo_bp.route("/parse/combo/confirm", methods=["POST"])
def parse_combo_confirm():
    if "user_id" not in session:
        return redirect(url_for("login"))

    from parse_docs import normalize_container_code, normalize_date

    db = get_db()
    form = request.form

    supplier_id = form.get("supplier_id")
    if not supplier_id:
        supplier_name = form.get("supplier_name", "").strip()
        if supplier_name:
            cur = db.execute("INSERT INTO suppliers (name, country) VALUES (?,?)",
                (supplier_name, form.get("supplier_country","").strip().upper() or None))
            db.commit()
            supplier_id = cur.lastrowid
        else:
            flash("Supplier is required.", "error")
            return redirect(url_for("combo.parse_combo"))

    product_id = form.get("product_id")
    if not product_id:
        flash("Product is required.", "error")
        return redirect(url_for("combo.parse_combo"))

    oda_code = form.get("oda_number")
    existing_oda = db.execute("SELECT id FROM odas WHERE oda_code=?", (oda_code,)).fetchone()
    if existing_oda:
        oda_id = existing_oda["id"]
    else:
        cur = db.execute("""
            INSERT INTO odas (
                oda_code, supplier_id, product_id, total_mt,
                price_eur_per_mt, price_usd_per_mt, currency,
                order_date, paese, incoterm, incoterm_location, incoterm_full,
                customs_regime, vat_rate, payment_terms, source
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            oda_code, supplier_id, product_id,
            float(form.get("total_mt") or 0),
            float(form.get("price_eur") or 0),
            float(form.get("price_usd") or 0) or None,
            form.get("currency", "EUR"),
            form.get("oda_date"),
            form.get("supplier_country"),
            form.get("incoterm"),
            form.get("incoterm_location"),
            form.get("incoterm_full"),
            form.get("customs_regime") or None,
            float(form.get("vat_rate") or 0) or None,
            form.get("payment_terms"),
            "COMBO_PARSER",
        ))
        db.commit()
        oda_id = cur.lastrowid

    bl_number = form.get("bl_number")
    existing_shipment = db.execute(
        "SELECT id FROM shipments WHERE oda_id=? AND bl_number=? AND is_deleted=0",
        (oda_id, bl_number)
    ).fetchone()

    if existing_shipment:
        shipment_id = existing_shipment["id"]
    else:
        last = db.execute("SELECT MAX(shipment_number) AS n FROM shipments WHERE oda_id=?", (oda_id,)).fetchone()
        next_num = (last["n"] or 0) + 1
        cur = db.execute("""
            INSERT INTO shipments (
                oda_id, shipment_number, bl_number, bl_date,
                vessel_name, voyage_ref, port_of_loading, port_of_discharge, source
            ) VALUES (?,?,?,?,?,?,?,?,?)
        """, (
            oda_id, next_num,
            bl_number, form.get("bl_date"),
            form.get("vessel_name"), form.get("voyage_ref"),
            form.get("port_of_loading"), form.get("port_of_discharge"),
            "COMBO_PARSER",
        ))
        db.commit()
        shipment_id = cur.lastrowid

    num_containers = int(form.get("num_containers", 0))
    container_ids = []
    for i in range(1, num_containers + 1):
        code = form.get(f"container_code_{i}")
        if not code:
            continue
        code = normalize_container_code(code)
        nominal_mt = float(form.get(f"nominal_mt_{i}", 23))
        ctype = form.get(f"container_type_{i}", "ISOTANK")
        existing_c = db.execute("SELECT id FROM containers WHERE container_code=?", (code,)).fetchone()
        if existing_c:
            container_ids.append(existing_c["id"])
            continue
        existing_c = db.execute("SELECT id FROM containers WHERE container_code=?", (code,)).fetchone()
        if existing_c:
            container_ids.append(existing_c["id"])
            continue
        cur = db.execute("""
            INSERT INTO containers (
                container_code, shipment_id, container_type,
                nominal_mt, status, source
            ) VALUES (?,?,?,?,'IN_TRANSIT','COMBO_PARSER')
        """, (code, shipment_id, ctype, nominal_mt))
        db.commit()
        container_ids.append(cur.lastrowid)

    production_lot   = form.get("production_lot")
    manufacture_date = normalize_date(form.get("manufacture_date")) or None
    expiration_date  = normalize_date(form.get("expiration_date")) or None
    saved_filename   = form.get("coa_saved_filename")
    original_name    = form.get("coa_original_name")

    for cid in container_ids:
        db.execute("""
            UPDATE containers SET production_lot=?, manufacture_date=?, expiration_date=?
            WHERE id=?
        """, (production_lot, manufacture_date, expiration_date, cid))

        if saved_filename:
            db.execute("""
                INSERT INTO coa_documents
                    (container_id, shipment_id, product_id, lot_number,
                     filename, original_name, uploaded_by)
                VALUES (?,?,?,?,?,?,?)
            """, (cid, shipment_id, product_id, production_lot,
                  saved_filename, original_name, session.get("username","")))

    db.commit()
    flash(f"Combo import complete: ODA {oda_code}, B/L {form.get('bl_number')}, "
          f"{len(container_ids)} container(s) with lot {production_lot}.", "success")
    return redirect(url_for("oda_detail", oda_id=oda_id))
