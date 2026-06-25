"""
BU Control — Vessel Arrival & Port Management
Handles: Avviso Arrivo Merce parsing, manual arrival entry, vessel page
"""

import os, re, json, base64
from datetime import datetime
from flask import Blueprint, request, render_template, redirect, url_for, session, flash, g
import sqlite3
import anthropic

arrival_bp = Blueprint("arrival", __name__)

DB_PATH = os.environ.get("DB_PATH", os.path.join(os.path.dirname(__file__), "db", "bu_control.db"))
ANTHROPIC_CLIENT = anthropic.Anthropic()
ALLOWED_EXTENSIONS = {"pdf", "png", "jpg", "jpeg"}

def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys=ON")
    return g.db

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
    m = re.match(r"^(\d{1,2})[/\.\-](\d{1,2})[/\.\-](\d{4})$", date_str)
    if m:
        a, b, year = int(m.group(1)), int(m.group(2)), m.group(3)
        if a > 12:
            return f"{year}-{b:02d}-{a:02d}"
        return f"{year}-{a:02d}-{b:02d}"
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

# ── AVVISO ARRIVO PARSER PROMPT ───────────────────────────────

AVVISO_PROMPT = """
You are parsing an Italian "Avviso Arrivo Merce" (Cargo Arrival Notice) document
sent by a port agent to a trading company.

Extract the following fields and return ONLY valid JSON, no other text.

Return this exact structure:
{
  "reference": "string (Ns. rif field)",
  "arrival_date": "ISO date YYYY-MM-DD (vessel arrival date at port)",
  "port_of_discharge": "string (Porto Sbarco field)",
  "port_of_loading": "string or null (Porto Imbarco field)",
  "incoterm": "string or null (Resa field)",
  "vessel_date": "ISO date YYYY-MM-DD or null (Del field next to Nave)",
  "product_description": "string (Descrizione Merce)",
  "containers": [
    {
      "container_code": "string (Id. container column, normalize: no spaces/dashes, uppercase)",
      "container_type": "ISOTANK or ONE_TON_CUBE or FLEXIBAG",
      "gross_weight_kg": float or null
    }
  ],
  "total_containers": integer,
  "total_weight_kg": float or null
}

Rules:
- arrival_date: look for the vessel arrival date — often stated as "previsione di arrivo a [port] della nave succitata il: DD/MM/YYYY" or in the Del field next to Nave
- container_code: normalize to no spaces or dashes (e.g. DHDU2191435)
- container_type: "I" in Tipo column = ISOTANK
- gross_weight_kg: from Peso Lordo column per container
- If a field is not found use null
"""

# ── VESSEL PAGE ───────────────────────────────────────────────

@arrival_bp.route("/vessels")
def vessels():
    if "user_id" not in session:
        return redirect(url_for("login"))

    db = get_db()

    # Group containers by vessel/voyage — show all active sea shipments
    vessels = db.execute("""
        SELECT
            COALESCE(c.vessel_name, s.vessel_name, '— unknown vessel —') AS vessel_name,
            COALESCE(c.voyage_ref, s.voyage_ref)    AS voyage_ref,
            s.port_of_discharge,
            MIN(c.eta_date)                          AS earliest_eta,
            MAX(c.eta_date)                          AS latest_eta,
            MAX(c.ata_date)                          AS ata_date,
            COUNT(DISTINCT c.id)                     AS num_containers,
            SUM(COALESCE(c.actual_mt, c.nominal_mt)) AS total_mt,
            GROUP_CONCAT(DISTINCT o.oda_code)        AS oda_codes,
            GROUP_CONCAT(DISTINCT p.code)            AS products,
            -- Status logic
            CASE
                WHEN COUNT(DISTINCT c.id) FILTER (WHERE c.status = 'IN_TRANSIT') = 0
                     AND COUNT(DISTINCT c.id) FILTER (WHERE c.status = 'IN_PORT') > 0
                     THEN 'IN_PORT'
                WHEN MAX(c.ata_date) IS NOT NULL THEN 'IN_PORT'
                WHEN MIN(c.eta_date) < date('now') AND MAX(c.ata_date) IS NULL THEN 'LIKELY_ARRIVED'
                ELSE 'AT_SEA'
            END AS vessel_status,
            COUNT(DISTINCT c.id) FILTER (WHERE c.status = 'IN_PORT')    AS ctrs_in_port,
            COUNT(DISTINCT c.id) FILTER (WHERE c.status = 'IN_STORAGE') AS ctrs_in_storage,
            COUNT(DISTINCT c.id) FILTER (WHERE c.customs_clearance_date IS NOT NULL) AS ctrs_cleared
        FROM containers c
        JOIN shipments s  ON s.id  = c.shipment_id
        JOIN odas o       ON o.id  = s.oda_id
        JOIN products p   ON p.id  = o.product_id
        WHERE c.transport_mode = 'SEA'
          AND c.status NOT IN ('FULLY_SOLD', 'SCRAPPED')
          AND c.is_deleted = 0
        GROUP BY COALESCE(c.vessel_name, s.vessel_name), COALESCE(c.voyage_ref, s.voyage_ref)
        ORDER BY
            CASE WHEN MAX(c.ata_date) IS NOT NULL THEN 1 ELSE 0 END,
            MIN(c.eta_date) ASC NULLS LAST
    """).fetchall()

    return render_template("vessels.html", vessels=vessels)


# ── ARRIVAL ENTRY (parse + manual) ───────────────────────────

@arrival_bp.route("/arrival", methods=["GET", "POST"])
def arrival():
    if "user_id" not in session:
        return redirect(url_for("login"))

    db = get_db()

    if request.method == "GET":
        # Load in-transit containers for manual selection
        in_transit = db.execute("""
            SELECT c.id, c.container_code,
                   COALESCE(c.vessel_name, s.vessel_name) AS vessel_name,
                   COALESCE(c.voyage_ref, s.voyage_ref)   AS voyage_ref,
                   c.eta_date, o.oda_code, p.code AS product_code,
                   COALESCE(c.actual_mt, c.nominal_mt)    AS mt
            FROM containers c
            JOIN shipments s ON s.id = c.shipment_id
            JOIN odas o      ON o.id = s.oda_id
            JOIN products p  ON p.id = o.product_id
            WHERE c.status IN ('IN_TRANSIT', 'IN_PORT')
              AND c.transport_mode = 'SEA'
              AND c.is_deleted = 0
            ORDER BY COALESCE(c.vessel_name, s.vessel_name), c.eta_date
        """).fetchall()

        # Group by vessel for easier selection
        vessels_map = {}
        for c in in_transit:
            key = f"{c['vessel_name']} / {c['voyage_ref'] or '—'}"
            if key not in vessels_map:
                vessels_map[key] = []
            vessels_map[key].append(dict(c))

        return render_template("arrival.html",
            vessels_map=vessels_map,
            today=datetime.now().strftime("%Y-%m-%d"),
        )

    # POST — could be PDF upload or manual
    mode = request.form.get("mode", "manual")

    if mode == "pdf":
        f = request.files.get("document")
        if not f or not f.filename:
            flash("No file uploaded.", "error")
            return redirect(request.url)

        file_bytes = f.read()
        ext = f.filename.rsplit(".", 1)[1].lower()
        mime_type = "application/pdf" if ext == "pdf" else f"image/{ext}"
        b64 = base64.standard_b64encode(file_bytes).decode("utf-8")

        try:
            extracted = call_claude(AVVISO_PROMPT, b64, mime_type)
        except Exception as e:
            import traceback; traceback.print_exc()
            flash(f"Extraction failed: {e}", "error")
            return redirect(request.url)

        # Normalize dates and container codes
        extracted["arrival_date"] = normalize_date(extracted.get("arrival_date"))
        for c in extracted.get("containers", []):
            c["container_code"] = normalize_container_code(c.get("container_code"))

        # Try to match containers to DB
        matched = []
        for c in extracted.get("containers", []):
            code = c.get("container_code")
            row = None
            if code:
                row = db.execute(
                    "SELECT id, container_code, status FROM containers WHERE container_code=?",
                    (code,)
                ).fetchone()
                if not row:
                    row = db.execute(
                        "SELECT id, container_code, status FROM containers WHERE container_code LIKE ?",
                        (code[:10] + "%",)
                    ).fetchone()
            matched.append({
                **c,
                "container_id": row["id"] if row else None,
                "current_status": row["status"] if row else None,
                "found": row is not None,
            })

        session["arrival_draft"] = {
            "extracted": extracted,
            "matched": matched,
        }
        return render_template("arrival_confirm.html",
            extracted=extracted,
            matched=matched,
            today=datetime.now().strftime("%Y-%m-%d"),
        )

    # Manual mode — save directly
    arrival_date = request.form.get("arrival_date")
    container_ids = request.form.getlist("container_id")
    notes = request.form.get("notes", "")

    if not container_ids:
        flash("Please select at least one container.", "error")
        return redirect(request.url)

    updated = 0
    for cid in container_ids:
        db.execute("""
            UPDATE containers SET
                ata_date=?,
                status='IN_PORT',
                updated_at=datetime('now')
            WHERE id=? AND status IN ('IN_TRANSIT','IN_TRANSIT_ROAD')
        """, (arrival_date, cid))
        updated += 1

    db.commit()
    flash(f"Arrival recorded for {updated} container(s) on {arrival_date}.", "success")
    return redirect(url_for("arrival.vessels"))


@arrival_bp.route("/arrival/confirm", methods=["POST"])
def arrival_confirm():
    if "user_id" not in session:
        return redirect(url_for("login"))

    db = get_db()
    form = request.form
    arrival_date = form["arrival_date"]
    num = int(form.get("num_containers", 0))
    updated = 0

    for i in range(num):
        cid = form.get(f"container_id_{i}")
        include = form.get(f"include_{i}")
        if not cid or not include:
            continue
        db.execute("""
            UPDATE containers SET
                ata_date=?,
                status='IN_PORT',
                updated_at=datetime('now')
            WHERE id=? AND status IN ('IN_TRANSIT', 'IN_TRANSIT_ROAD')
        """, (arrival_date, cid))
        updated += 1

    db.commit()
    session.pop("arrival_draft", None)
    flash(f"Arrival recorded for {updated} container(s).", "success")
    return redirect(url_for("arrival.vessels"))
