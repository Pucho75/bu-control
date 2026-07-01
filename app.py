"""
BU Control Tool — Flask Application
Italian trading company — chemicals/biofuels distribution
"""

import sqlite3
import os
from datetime import datetime, timedelta
from functools import wraps
from flask import (
    Flask, render_template, request, redirect,
    url_for, session, flash, g, jsonify
)
from werkzeug.security import generate_password_hash, check_password_hash

# ── App setup ────────────────────────────────────────────────
APP_VERSION = "1.4.1"

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "change-this-in-production")
app.permanent_session_lifetime = timedelta(hours=4)

# Jinja2 enumerate filter (needed by parse confirm templates)
app.jinja_env.filters["enumerate"] = enumerate

# Register document parser blueprint
from parse_docs import parse_bp
app.register_blueprint(parse_bp)

# Register admin blueprint
from admin import admin_bp
app.register_blueprint(admin_bp)

# Register ops blueprint (customs, DDT, COA assign, demurrage)
from customs_and_movements import ops_bp
app.register_blueprint(ops_bp)

# Register arrival blueprint (vessel page, Avviso Arrivo parser)
from arrival import arrival_bp
app.register_blueprint(arrival_bp)

# Register sales parse blueprint (ODV, sales DDT)
from parse_sales import sales_parse_bp
app.register_blueprint(sales_parse_bp)

from bug_reports import bug_reports_bp
app.register_blueprint(bug_reports_bp)

from combo_import import combo_bp
app.register_blueprint(combo_bp)

DB_PATH = os.environ.get("DB_PATH", os.path.join(os.path.dirname(__file__), "db", "bu_control.db"))

# ── Role definitions ─────────────────────────────────────────
ROLES = {
    "CEO":               {"label": "CEO",              "sees_prices": True,  "sees_margins": True,  "can_enter_sales": True,  "can_enter_ops": True},
    "BU_DIRECTOR":       {"label": "BU Director",      "sees_prices": True,  "sees_margins": True,  "can_enter_sales": True,  "can_enter_ops": True},
    "LOGISTICS_ADMIN":   {"label": "Logistics Admin",  "sees_prices": True,  "sees_margins": False, "can_enter_sales": True,  "can_enter_ops": True},
    "LOGISTICS":         {"label": "Logistics",        "sees_prices": False, "sees_margins": False, "can_enter_sales": False, "can_enter_ops": True},
    "READ_ONLY":         {"label": "Read Only",        "sees_prices": False, "sees_margins": False, "can_enter_sales": False, "can_enter_ops": False},
}

# ── Database ─────────────────────────────────────────────────
def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA journal_mode=WAL")
        g.db.execute("PRAGMA foreign_keys=ON")
    return g.db

@app.teardown_appcontext
def close_db(exc):
    db = g.pop("db", None)
    if db:
        db.close()

def init_db():
    """Initialise schema and create demo users if DB is empty."""
    schema_path = os.path.join(os.path.dirname(__file__), "bu_control_schema_v1.2.sql")
    db = sqlite3.connect(DB_PATH)
    if os.path.exists(schema_path):
        with open(schema_path) as f:
            db.executescript(f.read())
    # Seed demo users if none exist
    cur = db.execute("SELECT COUNT(*) FROM users")
    if cur.fetchone()[0] == 0:
        demo_users = [
            ("ceo",       "ceo@company.it",       "CEO",             "demo1234"),
            ("li",        "li@company.it",         "BU_DIRECTOR",     "demo1234"),
            ("b",         "b@company.it",          "LOGISTICS_ADMIN", "demo1234"),
            ("g",         "g@company.it",          "LOGISTICS",       "demo1234"),
            ("logistics", "log@company.it",        "LOGISTICS",       "demo1234"),
        ]
        for username, email, role, pw in demo_users:
            db.execute(
                "INSERT INTO users (username, email, role, password_hash) VALUES (?,?,?,?)",
                (username, email, role, generate_password_hash(pw))
            )
        db.commit()
    db.close()

# ── Auth helpers ─────────────────────────────────────────────
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login", next=request.path))
        if session.get("must_change_password") and request.endpoint != "change_password":
            return redirect(url_for("change_password"))
        return f(*args, **kwargs)
    return decorated


@app.route("/change-password", methods=["GET", "POST"])
def change_password():
    if "user_id" not in session:
        return redirect(url_for("login"))
    error = None
    if request.method == "POST":
        pw1 = request.form.get("password", "")
        pw2 = request.form.get("password2", "")
        if len(pw1) < 8:
            error = "La password deve essere di almeno 8 caratteri."
        elif pw1 != pw2:
            error = "Le password non coincidono."
        else:
            from werkzeug.security import generate_password_hash
            db = get_db()
            db.execute("UPDATE users SET password_hash=?, must_change_password=0 WHERE id=?",
                (generate_password_hash(pw1), session["user_id"]))
            db.commit()
            session["must_change_password"] = False
            flash("Password aggiornata con successo.", "success")
            return redirect(url_for("dashboard"))
    return render_template("change_password.html", error=error)

def role_required(*roles):
    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            if session.get("role") not in roles:
                flash("Access denied.", "error")
                return redirect(url_for("dashboard"))
            return f(*args, **kwargs)
        return decorated
    return decorator

def current_role():
    return session.get("role", "")

def can(permission):
    role = current_role()
    return ROLES.get(role, {}).get(permission, False)

@app.context_processor
def inject_globals():
    return {
        "current_role": current_role(),
        "role_label":   ROLES.get(current_role(), {}).get("label", ""),
        "can":          can,
        "now":          datetime.now(),
        "app_version":  APP_VERSION,
    }

# ── Auth routes ──────────────────────────────────────────────
@app.route("/login", methods=["GET", "POST"])
def login():
    if "user_id" in session:
        return redirect(url_for("dashboard"))
    error = None
    if request.method == "POST":
        username = request.form.get("username", "").strip().lower()
        password = request.form.get("password", "")
        db = get_db()
        user = db.execute(
            "SELECT * FROM users WHERE username = ? AND is_active = 1", (username,)
        ).fetchone()
        if user and check_password_hash(user["password_hash"], password):
            session.permanent = True
            session.permanent = True
            session["user_id"]  = user["id"]
            session["username"] = user["username"]
            session["role"]     = user["role"]
            session["must_change_password"] = bool(user["must_change_password"])
            next_url = request.args.get("next") or url_for("dashboard")
            return redirect(next_url)
        error = "Invalid username or password."
    return render_template("login.html", error=error)

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

# ── Dashboard ────────────────────────────────────────────────
@app.route("/")
@login_required
def dashboard():
    db = get_db()

    # Position summary per product
    positions = db.execute("""
        SELECT
            p.code              AS product,
            p.name              AS product_name,
            -- ON ORDER = ODAs not yet fully shipped (residual per ODA summed)
            COALESCE((
                SELECT SUM(residual) FROM (
                    SELECT o2.id,
                           o2.total_mt - COALESCE((
                               SELECT SUM(COALESCE(c2.actual_mt, c2.nominal_mt))
                               FROM containers c2
                               JOIN shipments s2 ON s2.id = c2.shipment_id
                               WHERE s2.oda_id = o2.id AND c2.is_deleted = 0
                           ), 0) AS residual
                    FROM odas o2
                    WHERE o2.product_id = p.id AND o2.is_deleted = 0 AND (o2.is_closed IS NULL OR o2.is_closed = 0)
                ) sub WHERE residual > 0.001
            ), 0) AS mt_on_order,
            -- IN TRANSIT
            COALESCE(SUM(CASE WHEN c.status IN ('IN_TRANSIT','IN_TRANSIT_ROAD')
                THEN COALESCE(c.actual_mt, c.nominal_mt) ELSE 0 END), 0) AS mt_in_transit,
            -- IN PORT
            COALESCE(SUM(CASE WHEN c.status = 'IN_PORT'
                THEN COALESCE(c.actual_mt, c.nominal_mt) ELSE 0 END), 0) AS mt_in_port,
            -- IN STORAGE = net available per container (gross minus drawn from that container only)
            COALESCE(SUM(CASE WHEN c.status IN ('IN_STORAGE','PARTIALLY_SOLD')
                THEN COALESCE(c.actual_mt, c.nominal_mt) -
                     COALESCE((SELECT SUM(sl.mt_drawn) FROM sale_lots sl WHERE sl.container_id = c.id), 0)
                ELSE 0 END), 0) AS mt_in_storage,
            -- IN ORDER = open ODVs not yet delivered
            COALESCE((
                SELECT SUM(s2.provisional_mt)
                FROM sales s2
                WHERE s2.product_id = p.id
                  AND s2.status IN ('PROVISIONAL','INSTRUCTED')
                  AND s2.is_deleted = 0
            ), 0) AS mt_in_order,
            -- DELIVERED = actual MT drawn via sale_lots, matched by sale product
            COALESCE((
                SELECT SUM(sl.mt_drawn)
                FROM sale_lots sl
                JOIN sales s2 ON s2.id = sl.sale_id
                WHERE s2.product_id = p.id
                  AND s2.is_deleted = 0
            ), 0) AS mt_sold
        FROM products p
        LEFT JOIN odas o        ON o.product_id = p.id AND o.is_deleted = 0
        LEFT JOIN shipments s   ON s.oda_id = o.id AND s.is_deleted = 0
        LEFT JOIN containers c  ON c.shipment_id = s.id AND c.is_deleted = 0
        WHERE p.is_active = 1
        GROUP BY p.id
        ORDER BY p.code
    """).fetchall()

    # Margin summary (CEO / BU_DIRECTOR only)
    margin_summary = None
    margin_by_product = []
    if can("sees_margins"):
        margin_summary = db.execute("""
            SELECT
                COALESCE(SUM(sl.margin_eur_total), 0)   AS realized_total,
                COALESCE(SUM(sl.mt_drawn), 0)           AS sold_mt,
                CASE WHEN COALESCE(SUM(sl.mt_drawn),0) > 0
                     THEN SUM(sl.margin_eur_total) / SUM(sl.mt_drawn)
                     ELSE 0 END                         AS realized_per_mt
            FROM sale_lots sl
            JOIN sales sa ON sa.id = sl.sale_id
            WHERE strftime('%Y', sa.provisional_date) = strftime('%Y', 'now')
        """).fetchone()

        margin_by_product = db.execute("""
            SELECT
                p.code AS product_code,
                COALESCE(SUM(sl.margin_eur_total), 0) AS realized_total,
                COALESCE(SUM(sl.mt_drawn), 0) AS sold_mt,
                CASE WHEN COALESCE(SUM(sl.mt_drawn),0) > 0
                     THEN SUM(sl.margin_eur_total) / SUM(sl.mt_drawn)
                     ELSE 0 END AS realized_per_mt
            FROM sale_lots sl
            JOIN sales sa ON sa.id = sl.sale_id
            JOIN products p ON p.id = sa.product_id
            WHERE strftime('%Y', sa.provisional_date) = strftime('%Y', 'now')
            GROUP BY p.id
            ORDER BY p.code
        """).fetchall()

    # Inbound — next 30 days
    inbound = db.execute("""
        SELECT
            c.container_code,
            c.vessel_name,
            c.eta_date,
            c.port_of_discharge,
            p.code  AS product,
            COALESCE(c.actual_mt, c.nominal_mt) AS actual_mt,
            o.oda_code,
            CAST(julianday(c.eta_date) - julianday('now') AS INTEGER) AS days_to_eta
        FROM containers c
        JOIN shipments s  ON s.id = c.shipment_id
        JOIN odas o       ON o.id = s.oda_id
        JOIN products p   ON p.id = o.product_id
        WHERE c.status IN ('IN_TRANSIT', 'IN_TRANSIT_ROAD')
          AND c.eta_date IS NOT NULL
          AND c.eta_date <= date('now', '+30 days')
          AND c.is_deleted = 0
        ORDER BY c.eta_date ASC
    """).fetchall()

    # Outbound pipeline
    outbound = db.execute("""
        SELECT
            s.sale_code,
            cu.name             AS customer,
            p.code              AS product,
            s.provisional_mt,
            s.actual_mt,
            s.status,
            s.incoterm,
            s.ddt_load_date,
            s.provisional_date
        FROM sales s
        JOIN customers cu ON cu.id = s.customer_id
        JOIN products p   ON p.id = s.product_id
        WHERE s.status IN ('PROVISIONAL','INSTRUCTED','EXECUTED')
          AND s.is_deleted = 0
        ORDER BY s.provisional_date ASC
    """).fetchall()

    # Storage balances
    storage = db.execute("""
        SELECT
            sf.code             AS facility_code,
            sf.name             AS facility,
            p.code              AS product,
            COUNT(DISTINCT c.id) AS num_containers,
            SUM(COALESCE(c.actual_mt, c.nominal_mt)) AS gross_mt,
            SUM(COALESCE(c.actual_mt, c.nominal_mt)) -
                COALESCE((
                    SELECT SUM(sl.mt_drawn) FROM sale_lots sl
                    WHERE sl.container_id = c.id
                ), 0) AS provisional_mt,
            1 AS is_confirmed
        FROM containers c
        JOIN shipments s  ON s.id = c.shipment_id
        JOIN odas o       ON o.id = s.oda_id
        JOIN products p   ON p.id = o.product_id
        JOIN storage_facilities sf ON sf.id = c.storage_facility_id
        WHERE c.status IN ('IN_STORAGE','PARTIALLY_SOLD')
          AND c.is_deleted = 0
        GROUP BY sf.id, p.id
        ORDER BY sf.code, p.code
    """).fetchall()

    # Pending items count (for B's badge)
    pending_count = 0
    if current_role() in ("LOGISTICS_ADMIN", "CEO"):
        row = db.execute("""
            SELECT COUNT(*) AS n FROM (
                SELECT id FROM container_movements WHERE entry_status = 'PENDING_COSTS'
                UNION ALL
                SELECT id FROM sales WHERE status = 'INSTRUCTED' AND is_deleted = 0
            )
        """).fetchone()
        pending_count = row["n"] if row else 0

    return render_template("dashboard.html",
        positions=positions,
        margin_summary=margin_summary,
        margin_by_product=margin_by_product if can("sees_margins") else [],
        inbound=inbound,
        outbound=outbound,
        storage=storage,
        pending_count=pending_count,
    )

# ── Lots / Margin view ───────────────────────────────────────
@app.route("/lots")
@login_required
@role_required("CEO", "BU_DIRECTOR")
def lots():
    db = get_db()
    product_filter  = request.args.get("product_id", "")
    supplier_filter = request.args.get("supplier_id", "")

    where = ["1=1"]
    params = []
    if product_filter:
        where.append("lm.product_code = (SELECT code FROM products WHERE id=?)")
        params.append(product_filter)
    if supplier_filter:
        where.append("lm.supplier_name = (SELECT name FROM suppliers WHERE id=?)")
        params.append(supplier_filter)

    lots = db.execute(f"""
        SELECT lm.* FROM v_lot_margin lm
        WHERE {' AND '.join(where)}
        ORDER BY lm.oda_code DESC
    """, params).fetchall()

    products  = db.execute("SELECT * FROM products  WHERE is_active=1 ORDER BY code").fetchall()
    suppliers = db.execute("SELECT * FROM suppliers WHERE is_active=1 ORDER BY name").fetchall()

    return render_template("lots.html",
        lots=lots, products=products, suppliers=suppliers,
        product_filter=product_filter, supplier_filter=supplier_filter,
    )

# ── Sales pipeline ───────────────────────────────────────────
@app.route("/sales")
@login_required
def sales_pipeline():
    db = get_db()
    # Strip price/margin columns for logistics roles at query level
    sales = db.execute("SELECT * FROM v_sale_pipeline").fetchall()
    return render_template("sales.html", sales=sales)

@app.route("/sales/new", methods=["GET", "POST"])
@login_required
@role_required("CEO", "BU_DIRECTOR", "LOGISTICS_ADMIN")
def sale_new():
    db = get_db()
    products  = db.execute("SELECT * FROM products  WHERE is_active=1 ORDER BY code").fetchall()
    customers = db.execute("SELECT * FROM customers WHERE is_active=1 ORDER BY name").fetchall()
    facilities = db.execute("SELECT * FROM storage_facilities WHERE is_active=1 ORDER BY code").fetchall()
    carriers  = db.execute("SELECT * FROM carriers  WHERE is_active=1 ORDER BY name").fetchall()

    if request.method == "POST":
        step = int(request.form.get("step", 1))

        if step == 1:
            # Validate and store step 1 in session
            session["sale_draft"] = {
                "customer_id":      request.form["customer_id"],
                "product_id":       request.form["product_id"],
                "destination":      request.form["destination"],
                "storage_id":       request.form["storage_facility_id"],
                "incoterm":         request.form["incoterm"],
                "provisional_mt":   request.form["provisional_mt"],
                "price_eur_per_mt": request.form["price_eur_per_mt"],
                "price_usd_per_mt": request.form.get("price_usd_per_mt", ""),
                "provisional_date": request.form["provisional_date"],
            }
            return redirect(url_for("sale_new_step2"))

    return render_template("sale_new_step1.html",
        products=products, customers=customers,
        facilities=facilities, carriers=carriers,
        step=1,
    )

@app.route("/sales/new/step2", methods=["GET", "POST"])
@login_required
@role_required("CEO", "BU_DIRECTOR", "LOGISTICS_ADMIN")
def sale_new_step2():
    db = get_db()
    draft = session.get("sale_draft", {})
    if not draft:
        return redirect(url_for("sale_new"))

    # Transport matrix lookup
    matrix_rate = db.execute("""
        SELECT tm.rate_eur_per_mt, ca.name AS carrier_name, tm.carrier_id
        FROM transport_matrix tm
        JOIN carriers ca ON ca.id = tm.carrier_id
        WHERE tm.storage_facility_id = ?
          AND tm.destination = ?
          AND (tm.product_id = ? OR tm.product_id IS NULL)
          AND tm.valid_from <= date('now')
          AND (tm.valid_to IS NULL OR tm.valid_to >= date('now'))
        ORDER BY tm.product_id DESC
        LIMIT 1
    """, (draft.get("storage_id"), draft.get("destination"), draft.get("product_id"))).fetchone()

    carriers = db.execute("SELECT * FROM carriers WHERE is_active=1 ORDER BY name").fetchall()

    if request.method == "POST":
        session["sale_draft"].update({
            "carrier_id":               request.form.get("carrier_id"),
            "transport_eur":            request.form.get("transport_eur", 0),
            "transport_source":         request.form.get("transport_source", "MATRIX"),
            "transport_override_reason": request.form.get("transport_override_reason", ""),
        })
        return redirect(url_for("sale_new_step3"))

    return render_template("sale_new_step2.html",
        draft=draft, matrix_rate=matrix_rate,
        carriers=carriers, step=2,
    )

@app.route("/sales/new/step3", methods=["GET", "POST"])
@login_required
@role_required("CEO", "BU_DIRECTOR", "LOGISTICS_ADMIN")
def sale_new_step3():
    db = get_db()
    draft = session.get("sale_draft", {})
    if not draft:
        return redirect(url_for("sale_new"))

    # FIFO proposal
    fifo_queue = db.execute("""
        SELECT * FROM v_fifo_queue
        WHERE product_code = (SELECT code FROM products WHERE id = ?)
          AND available_mt > 0
        ORDER BY customs_clearance_date ASC
    """, (draft.get("product_id"),)).fetchall()

    if request.method == "POST":
        # Build sale + sale_lots records
        import uuid
        sale_code = f"SAL-{datetime.now().strftime('%Y-%m%d')}-{str(uuid.uuid4())[:4].upper()}"
        draft = session.get("sale_draft", {})

        price_eur = float(draft["price_eur_per_mt"])
        price_usd = float(draft["price_usd_per_mt"]) if draft.get("price_usd_per_mt") else None
        exch_rate = round(price_usd / price_eur, 6) if price_usd and price_eur else None
        transport_eur = float(draft.get("transport_eur", 0) or 0)
        provisional_mt = float(draft["provisional_mt"])

        db.execute("""
            INSERT INTO sales (
                sale_code, customer_id, product_id, destination,
                storage_facility_id, incoterm,
                price_eur_per_mt, price_usd_per_mt, exchange_rate,
                carrier_id, transport_to_customer_eur,
                transport_source, transport_override_reason,
                provisional_date, provisional_mt, provisional_entered_by,
                status, fifo_assigned
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            sale_code,
            draft["customer_id"], draft["product_id"], draft["destination"],
            draft["storage_id"], draft["incoterm"],
            price_eur, price_usd, exch_rate,
            draft.get("carrier_id"), transport_eur,
            draft.get("transport_source", "MATRIX"),
            draft.get("transport_override_reason", ""),
            draft["provisional_date"], provisional_mt,
            session["username"],
            "PROVISIONAL", 0,
        ))

        # Process lot assignments from form
        lot_assignments = []
        for key in request.form:
            if key.startswith("lot_mt_"):
                shipment_id = key.replace("lot_mt_", "")
                mt = float(request.form[key] or 0)
                if mt > 0:
                    lot_assignments.append((shipment_id, mt))

        sale_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
        transport_per_mt = transport_eur / provisional_mt if provisional_mt else 0

        for shipment_id, mt in lot_assignments:
            # Get cost snapshot
            costs = db.execute("""
                SELECT
                    o.price_eur_per_mt,
                    COALESCE(SUM(c.customs_cost_eur),0) / NULLIF(SUM(c.actual_mt),0)      AS customs_per_mt,
                    COALESCE(SUM(c.transport_to_storage_eur),0) / NULLIF(SUM(c.actual_mt),0) AS transport_st_per_mt
                FROM shipments s
                JOIN odas o ON o.id = s.oda_id
                LEFT JOIN containers c ON c.shipment_id = s.id
                WHERE s.id = ?
                GROUP BY s.id
            """, (shipment_id,)).fetchone()

            if costs:
                purchase   = costs["price_eur_per_mt"] or 0
                customs    = costs["customs_per_mt"] or 0
                transp_st  = costs["transport_st_per_mt"] or 0
                incoterm   = draft.get("incoterm", "DAP")
                transp_cust = transport_per_mt if incoterm != "EXW" else 0.0
                landed     = purchase + customs + transp_st + transp_cust
                margin_mt  = price_eur - landed
                fifo_override = int(request.form.get(f"override_{shipment_id}", 0))
                override_reason = request.form.get(f"override_reason_{shipment_id}", "")

                db.execute("""
                    INSERT INTO sale_lots (
                        sale_id, shipment_id, mt_drawn,
                        fifo_override, fifo_override_reason,
                        purchase_price_eur_per_mt,
                        customs_cost_eur_per_mt,
                        transport_to_storage_eur_per_mt,
                        transport_to_customer_eur_per_mt,
                        landed_cost_eur_per_mt,
                        sale_price_eur_per_mt,
                        margin_eur_per_mt,
                        margin_eur_total
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                """, (
                    sale_id, shipment_id, mt,
                    fifo_override, override_reason,
                    purchase, customs, transp_st, transp_cust,
                    landed, price_eur, margin_mt, margin_mt * mt,
                ))

        if lot_assignments:
            db.execute("UPDATE sales SET fifo_assigned=1 WHERE id=?", (sale_id,))

        db.commit()
        session.pop("sale_draft", None)
        flash(f"Sale {sale_code} created.", "success")
        return redirect(url_for("sales_pipeline"))

    return render_template("sale_new_step3.html",
        draft=draft, fifo_queue=fifo_queue,
        step=3, show_margin=can("sees_margins"),
    )

# ── ODA Status ───────────────────────────────────────────────
@app.route("/oda-status")
@login_required
def oda_status():
    db = get_db()
    products  = db.execute("SELECT * FROM products  WHERE is_active=1 ORDER BY code").fetchall()
    suppliers = db.execute("SELECT * FROM suppliers WHERE is_active=1 ORDER BY name").fetchall()

    filters = {
        "product_id":  request.args.get("product_id", ""),
        "supplier_id": request.args.get("supplier_id", ""),
        "status":      request.args.get("status", ""),
        "sort":        request.args.get("sort", "date_desc"),
    }

    where = ["o.is_deleted=0"]
    params = []
    if filters["product_id"]:
        where.append("o.product_id=?"); params.append(filters["product_id"])
    if filters["supplier_id"]:
        where.append("o.supplier_id=?"); params.append(filters["supplier_id"])

    odas = db.execute(f"""
        SELECT
            o.*,
            s.name  AS supplier_name,
            p.code  AS product_code,
            -- B/L count (distinct bl_numbers across shipments)
            COUNT(DISTINCT sh.bl_number) FILTER (WHERE sh.bl_number IS NOT NULL) AS bl_count,
            -- Container counts
            COUNT(DISTINCT c.id)         AS ctrs_total,
            COUNT(DISTINCT c.id) FILTER (WHERE c.status IN ('IN_STORAGE','PARTIALLY_SOLD','FULLY_SOLD')) AS ctrs_arrived,
            COUNT(DISTINCT c.id) FILTER (WHERE c.customs_clearance_date IS NOT NULL) AS ctrs_cleared,
            COUNT(DISTINCT c.id) FILTER (WHERE c.production_lot IS NOT NULL) AS coas_received,
            -- Closed = all MT sold
            CASE WHEN COUNT(DISTINCT c.id) > 0
                  AND COUNT(DISTINCT c.id) FILTER (WHERE c.status = 'FULLY_SOLD') = COUNT(DISTINCT c.id)
                 THEN 1 ELSE 0 END AS is_closed,
            -- FX rate from most recent shipment
            (SELECT sh2.fx_rate_invoice FROM shipments sh2
             WHERE sh2.oda_id = o.id AND sh2.is_deleted = 0
             AND sh2.fx_rate_invoice IS NOT NULL
             ORDER BY sh2.bl_date DESC LIMIT 1) AS shipment_fx_rate,
            -- Computed EUR price
            CASE WHEN o.currency = 'USD' AND o.price_usd_per_mt IS NOT NULL THEN
                ROUND(o.price_usd_per_mt / COALESCE(
                    (SELECT sh2.fx_rate_invoice FROM shipments sh2
                     WHERE sh2.oda_id = o.id AND sh2.is_deleted = 0
                     AND sh2.fx_rate_invoice IS NOT NULL
                     ORDER BY sh2.bl_date DESC LIMIT 1),
                    o.exchange_rate, 1), 2)
            ELSE o.price_eur_per_mt END AS computed_price_eur
        FROM odas o
        JOIN suppliers s ON s.id = o.supplier_id
        JOIN products p  ON p.id = o.product_id
        LEFT JOIN shipments sh ON sh.oda_id = o.id AND sh.is_deleted=0
        LEFT JOIN containers c ON c.shipment_id = sh.id AND c.is_deleted=0
        WHERE {' AND '.join(where)}
        GROUP BY o.id
        ORDER BY
            CASE WHEN ? = 'date_asc'  THEN o.order_date END ASC,
            CASE WHEN ? = 'date_desc' THEN o.order_date END DESC,
            CASE WHEN ? = 'code_asc'  THEN o.oda_code   END ASC,
            CASE WHEN ? = 'code_desc' THEN o.oda_code   END DESC,
            o.order_date DESC
    """, params + [filters['sort'], filters['sort'], filters['sort'], filters['sort']]).fetchall()

    # Apply status filter after aggregation
    if filters["status"] == "open":
        odas = [o for o in odas if not o["is_closed"]]
    elif filters["status"] == "closed":
        odas = [o for o in odas if o["is_closed"]]

    return render_template("oda_status.html",
        odas=odas, products=products, suppliers=suppliers, filters=filters,
    )


@app.route("/oda-status/<int:oda_id>")
@login_required
def oda_detail(oda_id):
    db = get_db()
    oda = db.execute("""
        SELECT o.*, s.name AS supplier_name, p.code AS product_code
        FROM odas o
        JOIN suppliers s ON s.id = o.supplier_id
        JOIN products p  ON p.id = o.product_id
        WHERE o.id=? AND o.is_deleted=0
    """, (oda_id,)).fetchone()
    if not oda:
        flash("ODA not found.", "error")
        return redirect(url_for("oda_status"))

    shipments = db.execute("""
        SELECT * FROM shipments WHERE oda_id=? AND is_deleted=0
        ORDER BY shipment_number
    """, (oda_id,)).fetchall()

    oda_lines = db.execute("""
        SELECT * FROM oda_lines WHERE oda_id=? ORDER BY line_number
    """, (oda_id,)).fetchall()

    containers = db.execute("""
        SELECT c.*, sh.bl_number, sf.code AS facility_code
        FROM containers c
        JOIN shipments sh ON sh.id = c.shipment_id
        LEFT JOIN storage_facilities sf ON sf.id = c.storage_facility_id
        WHERE sh.oda_id=? AND c.is_deleted=0
        ORDER BY sh.shipment_number, c.container_code
    """, (oda_id,)).fetchall()

    summary = db.execute("""
        SELECT
            COUNT(DISTINCT sh.bl_number) FILTER (WHERE sh.bl_number IS NOT NULL) AS bl_count,
            COUNT(DISTINCT c.id) AS ctrs_total,
            COUNT(DISTINCT c.id) FILTER (WHERE c.status IN ('IN_STORAGE','PARTIALLY_SOLD','FULLY_SOLD','IN_PORT')) AS ctrs_arrived,
            COUNT(DISTINCT c.id) FILTER (WHERE c.customs_clearance_date IS NOT NULL) AS ctrs_cleared,
            COUNT(DISTINCT c.id) FILTER (WHERE c.production_lot IS NOT NULL) AS coas_received
        FROM shipments sh
        LEFT JOIN containers c ON c.shipment_id = sh.id AND c.is_deleted=0
        WHERE sh.oda_id=? AND sh.is_deleted=0
    """, (oda_id,)).fetchone()

    oda = dict(oda)
    oda["is_closed"] = (
        summary["ctrs_total"] > 0 and
        db.execute("""
            SELECT COUNT(*) FROM containers c
            JOIN shipments sh ON sh.id = c.shipment_id
            WHERE sh.oda_id=? AND c.is_deleted=0 AND c.status != 'FULLY_SOLD'
        """, (oda_id,)).fetchone()[0] == 0
    )

    return render_template("oda_detail.html",
        oda=oda, shipments=shipments, oda_lines=oda_lines,
        containers=containers, summary=summary,
    )


@app.route("/shipments/<int:shipment_id>/delete", methods=["POST"])
@login_required
def delete_shipment(shipment_id):
    db = get_db()
    if current_role() not in ("CEO", "LOGISTICS_ADMIN"):
        flash("Not authorized.", "error")
        return redirect(request.referrer or url_for("oda_status"))

    # Check for containers
    containers = db.execute(
        "SELECT COUNT(*) AS n FROM containers WHERE shipment_id=? AND is_deleted=0",
        (shipment_id,)
    ).fetchone()

    if containers["n"] > 0:
        flash(f"Cannot delete — {containers['n']} container(s) linked to this B/L. Delete containers first.", "error")
        return redirect(request.referrer or url_for("oda_status"))

    # Get ODA id for redirect
    shipment = db.execute("SELECT oda_id FROM shipments WHERE id=?", (shipment_id,)).fetchone()
    oda_id = shipment["oda_id"] if shipment else None

    db.execute("UPDATE shipments SET is_deleted=1, updated_at=datetime('now') WHERE id=?", (shipment_id,))
    db.commit()
    flash("B/L deleted.", "success")
    return redirect(url_for("oda_detail", oda_id=oda_id) if oda_id else url_for("oda_status"))


@app.route("/containers/<int:container_id>/delete", methods=["POST"])
@login_required
def delete_container(container_id):
    db = get_db()
    if current_role() not in ("CEO", "LOGISTICS_ADMIN"):
        flash("Not authorized.", "error")
        return redirect(request.referrer or url_for("containers"))

    # Check no sale_lots reference this container
    sl = db.execute(
        "SELECT COUNT(*) AS n FROM sale_lots WHERE container_id=?", (container_id,)
    ).fetchone()
    if sl["n"] > 0:
        flash("Cannot delete — container has sale lot assignments. Reset the sale first.", "error")
        return redirect(request.referrer or url_for("containers"))

    ctr = db.execute("SELECT shipment_id FROM containers WHERE id=?", (container_id,)).fetchone()
    db.execute("UPDATE containers SET is_deleted=1, updated_at=datetime('now') WHERE id=?", (container_id,))
    db.commit()
    flash("Container deleted.", "success")
    return redirect(request.referrer or url_for("containers"))
    oda = db.execute("""
        SELECT o.*, s.name AS supplier_name, p.code AS product_code
        FROM odas o
        JOIN suppliers s ON s.id = o.supplier_id
        JOIN products p  ON p.id = o.product_id
        WHERE o.id=? AND o.is_deleted=0
    """, (oda_id,)).fetchone()
    if not oda:
        flash("ODA not found.", "error")
        return redirect(url_for("oda_status"))

    shipments = db.execute("""
        SELECT * FROM shipments WHERE oda_id=? AND is_deleted=0
        ORDER BY shipment_number
    """, (oda_id,)).fetchall()

    oda_lines = db.execute("""
        SELECT * FROM oda_lines WHERE oda_id=? ORDER BY line_number
    """, (oda_id,)).fetchall()

    containers = db.execute("""
        SELECT c.*, sh.bl_number, sf.code AS facility_code
        FROM containers c
        JOIN shipments sh ON sh.id = c.shipment_id
        LEFT JOIN storage_facilities sf ON sf.id = c.storage_facility_id
        WHERE sh.oda_id=? AND c.is_deleted=0
        ORDER BY sh.shipment_number, c.container_code
    """, (oda_id,)).fetchall()

    summary = db.execute("""
        SELECT
            COUNT(DISTINCT sh.bl_number) FILTER (WHERE sh.bl_number IS NOT NULL) AS bl_count,
            COUNT(DISTINCT c.id) AS ctrs_total,
            COUNT(DISTINCT c.id) FILTER (WHERE c.status IN ('IN_STORAGE','PARTIALLY_SOLD','FULLY_SOLD')) AS ctrs_arrived,
            COUNT(DISTINCT c.id) FILTER (WHERE c.customs_clearance_date IS NOT NULL) AS ctrs_cleared,
            COUNT(DISTINCT c.id) FILTER (WHERE c.production_lot IS NOT NULL) AS coas_received
        FROM shipments sh
        LEFT JOIN containers c ON c.shipment_id = sh.id AND c.is_deleted=0
        WHERE sh.oda_id=? AND sh.is_deleted=0
    """, (oda_id,)).fetchone()

    # Determine open/closed
    oda = dict(oda)
    oda["is_closed"] = (
        summary["ctrs_total"] > 0 and
        db.execute("""
            SELECT COUNT(*) FROM containers c
            JOIN shipments sh ON sh.id = c.shipment_id
            WHERE sh.oda_id=? AND c.is_deleted=0 AND c.status != 'FULLY_SOLD'
        """, (oda_id,)).fetchone()[0] == 0
    )

    return render_template("oda_detail.html",
        oda=oda, shipments=shipments, oda_lines=oda_lines,
        containers=containers, summary=summary,
    )
@app.route("/orders")
@login_required
@role_required("CEO", "BU_DIRECTOR", "LOGISTICS_ADMIN")
def orders():
    db = get_db()
    odas = db.execute("""
        SELECT o.*, s.name AS supplier_name, p.code AS product_code,
               -- Get fx_rate from most recent shipment
               (SELECT sh.fx_rate_invoice FROM shipments sh
                WHERE sh.oda_id = o.id AND sh.is_deleted = 0
                AND sh.fx_rate_invoice IS NOT NULL
                ORDER BY sh.bl_date DESC LIMIT 1) AS shipment_fx_rate,
               -- Computed EUR price
               CASE WHEN o.currency = 'USD' AND o.price_usd_per_mt IS NOT NULL THEN
                   ROUND(o.price_usd_per_mt / COALESCE(
                       (SELECT sh.fx_rate_invoice FROM shipments sh
                        WHERE sh.oda_id = o.id AND sh.is_deleted = 0
                        AND sh.fx_rate_invoice IS NOT NULL
                        ORDER BY sh.bl_date DESC LIMIT 1),
                       o.exchange_rate, 1), 2)
               ELSE o.price_eur_per_mt END AS computed_price_eur
        FROM odas o
        JOIN suppliers s ON s.id = o.supplier_id
        JOIN products p  ON p.id = o.product_id
        WHERE o.is_deleted = 0
        ORDER BY o.order_date DESC
    """).fetchall()
    return render_template("orders.html", odas=odas)

# ── Container schedule (G's view) ────────────────────────────
@app.route("/containers")
@login_required
def containers():
    db = get_db()
    today = datetime.now().date().isoformat()

    product_filter = request.args.get("product_id", "")
    oda_filter     = request.args.get("oda_id", "")
    status_filter  = request.args.get("status", "")

    # Base filters for in_transit
    transit_where = ["c.status IN ('IN_TRANSIT','IN_TRANSIT_ROAD')", "c.is_deleted=0"]
    storage_where = ["c.status IN ('IN_STORAGE','PARTIALLY_SOLD','IN_PORT')", "c.is_deleted=0"]
    params_t = []
    params_s = []

    if product_filter:
        transit_where.append("o.product_id=?"); params_t.append(product_filter)
        storage_where.append("o.product_id=?"); params_s.append(product_filter)
    if oda_filter:
        transit_where.append("o.id=?"); params_t.append(oda_filter)
        storage_where.append("o.id=?"); params_s.append(oda_filter)

    in_transit = db.execute(f"""
        SELECT c.*, p.code AS product_code, o.oda_code,
               COALESCE(c.production_lot, o.oda_code) AS lot_ref,
               CAST(julianday(c.eta_date) - julianday('now') AS INTEGER) AS days_to_eta,
               sf.code AS storage_code, s.bl_number, s.vessel_name AS ship_vessel
        FROM containers c
        JOIN shipments s        ON s.id = c.shipment_id
        JOIN odas o             ON o.id = s.oda_id
        JOIN products p         ON p.id = o.product_id
        LEFT JOIN storage_facilities sf ON sf.id = c.storage_facility_id
        WHERE {' AND '.join(transit_where)}
        ORDER BY c.eta_date ASC NULLS LAST
    """, params_t).fetchall()

    in_storage = db.execute(f"""
        SELECT c.*, p.code AS product_code, o.oda_code,
               sf.name AS facility_name, s.bl_number
        FROM containers c
        JOIN shipments s        ON s.id = c.shipment_id
        JOIN odas o             ON o.id = s.oda_id
        JOIN products p         ON p.id = o.product_id
        LEFT JOIN storage_facilities sf ON sf.id = c.storage_facility_id
        WHERE {' AND '.join(storage_where)}
        ORDER BY c.storage_entry_date DESC
    """, params_s).fetchall()

    products = db.execute("SELECT * FROM products WHERE is_active=1 ORDER BY code").fetchall()
    odas     = db.execute("""
        SELECT o.id, o.oda_code, s.name AS supplier_name
        FROM odas o JOIN suppliers s ON s.id=o.supplier_id
        WHERE o.is_deleted=0 ORDER BY o.order_date DESC
    """).fetchall()

    return render_template("containers.html",
        in_transit=in_transit, in_storage=in_storage, today=today,
        products=products, odas=odas,
        product_filter=product_filter, oda_filter=oda_filter,
    )

@app.route("/containers/<int:container_id>/update_eta", methods=["POST"])
@login_required
def update_eta(container_id):
    db = get_db()
    data = request.get_json()
    db.execute("""
        UPDATE containers
        SET eta_date=?, vessel_name=?, voyage_ref=?,
            port_of_discharge=?,
            eta_last_updated_by=?, eta_last_updated_at=datetime('now'),
            updated_at=datetime('now')
        WHERE id=?
    """, (
        data.get("eta_date"), data.get("vessel_name"),
        data.get("voyage_ref"), data.get("port_of_discharge"),
        session["username"], container_id,
    ))
    db.commit()
    return jsonify({"ok": True})

# ── Movements ────────────────────────────────────────────────
@app.route("/movements")
@login_required
def movements():
    db = get_db()
    pending = db.execute("""
        SELECT cm.*, c.container_code
        FROM container_movements cm
        JOIN containers c ON c.id = cm.container_id
        WHERE cm.entry_status = 'PENDING_COSTS'
        ORDER BY cm.movement_date DESC
    """).fetchall()

    recent = db.execute("""
        SELECT cm.*, c.container_code
        FROM container_movements cm
        JOIN containers c ON c.id = cm.container_id
        WHERE cm.entry_status = 'COMPLETE'
        ORDER BY cm.movement_date DESC
        LIMIT 50
    """).fetchall()

    return render_template("movements.html", pending=pending, recent=recent)

@app.route("/movements/new", methods=["GET", "POST"])
@login_required
@role_required("CEO", "LOGISTICS_ADMIN", "LOGISTICS")
def movement_new():
    db = get_db()
    containers_list = db.execute("""
        SELECT c.id, c.container_code, c.status, p.code AS product_code
        FROM containers c
        JOIN shipments s ON s.id = c.shipment_id
        JOIN odas o      ON o.id = s.oda_id
        JOIN products p  ON p.id = o.product_id
        WHERE c.is_deleted = 0 AND c.status NOT IN ('FULLY_SOLD','SCRAPPED')
        ORDER BY c.container_code
    """).fetchall()
    facilities = db.execute("SELECT * FROM storage_facilities WHERE is_active=1").fetchall()

    if request.method == "POST":
        role = current_role()
        # Phase 1 entry — all logistics roles
        entry_status = "PENDING_COSTS" if role == "LOGISTICS" else "COMPLETE"
        db.execute("""
            INSERT INTO container_movements (
                container_id, movement_type, movement_date, recorded_at,
                from_facility_id, to_facility_id, mt_moved,
                reference_doc, notes, phase1_entered_by,
                entry_status, source
            ) VALUES (?,?,?,datetime('now'),?,?,?,?,?,?,?,?)
        """, (
            request.form["container_id"],
            request.form["movement_type"],
            request.form["movement_date"],
            request.form.get("from_facility_id") or None,
            request.form.get("to_facility_id") or None,
            request.form["mt_moved"],
            request.form.get("reference_doc", ""),
            request.form.get("notes", ""),
            session["username"],
            entry_status,
            "MANUAL",
        ))
        db.commit()
        flash("Movement recorded.", "success")
        return redirect(url_for("movements"))

    return render_template("movement_new.html",
        containers=containers_list, facilities=facilities,
    )

@app.route("/movements/<int:movement_id>/complete_costs", methods=["POST"])
@login_required
@role_required("CEO", "LOGISTICS_ADMIN")
def complete_costs(movement_id):
    db = get_db()
    cost = float(request.form.get("cost_eur", 0) or 0)
    db.execute("""
        UPDATE container_movements
        SET cost_eur=?, entry_status='COMPLETE',
            phase2_entered_by=?, phase2_entered_at=datetime('now'),
            updated_at=datetime('now')
        WHERE id=?
    """, (cost, session["username"], movement_id))
    db.commit()
    flash("Costs recorded.", "success")
    return redirect(url_for("movements"))

# ── Storage statements ───────────────────────────────────────
@app.route("/storage")
@login_required
def storage():
    db = get_db()

    # Live balances from containers (primary source)
    live_balances = db.execute("""
        SELECT
            sf.code AS facility_code,
            sf.name AS facility_name,
            p.code  AS product_code,
            COUNT(DISTINCT c.id) AS num_containers,
            SUM(COALESCE(c.actual_mt, c.nominal_mt)) AS gross_mt,
            SUM(COALESCE(c.actual_mt, c.nominal_mt)) -
                COALESCE((
                    SELECT SUM(sl.mt_drawn) FROM sale_lots sl
                    JOIN containers c2 ON c2.id = sl.container_id
                    WHERE c2.storage_facility_id = sf.id
                      AND c2.shipment_id IN (
                          SELECT id FROM shipments WHERE oda_id IN (
                              SELECT id FROM odas WHERE product_id = p.id
                          )
                      )
                ), 0) AS available_mt
        FROM containers c
        JOIN shipments s  ON s.id = c.shipment_id
        JOIN odas o       ON o.id = s.oda_id
        JOIN products p   ON p.id = o.product_id
        JOIN storage_facilities sf ON sf.id = c.storage_facility_id
        WHERE c.status IN ('IN_STORAGE','PARTIALLY_SOLD')
          AND c.is_deleted = 0
        GROUP BY sf.id, p.id
        ORDER BY sf.code, p.code
    """).fetchall()

    statements = db.execute("""
        SELECT ss.*, sf.name AS facility_name, p.code AS product_code
        FROM storage_statements ss
        JOIN storage_facilities sf ON sf.id = ss.storage_facility_id
        JOIN products p            ON p.id  = ss.product_id
        ORDER BY ss.statement_date DESC
        LIMIT 20
    """).fetchall()

    facilities = db.execute("SELECT * FROM storage_facilities WHERE is_active=1").fetchall()
    products   = db.execute("SELECT * FROM products WHERE is_active=1").fetchall()

    return render_template("storage.html",
        live_balances=live_balances, statements=statements,
        facilities=facilities, products=products,
    )

@app.route("/storage/post_statement", methods=["POST"])
@login_required
@role_required("CEO", "LOGISTICS_ADMIN")
def post_statement():
    db = get_db()
    db.execute("""
        INSERT OR REPLACE INTO storage_statements
            (storage_facility_id, statement_date, product_id, confirmed_mt, posted_by, notes)
        VALUES (?,?,?,?,?,?)
    """, (
        request.form["storage_facility_id"],
        request.form["statement_date"],
        request.form["product_id"],
        float(request.form["confirmed_mt"]),
        session["username"],
        request.form.get("notes", ""),
    ))
    # Mark provisional as confirmed for that date
    db.execute("""
        UPDATE storage_provisional
        SET is_confirmed=1, confirmed_as_of=?
        WHERE storage_facility_id=? AND product_id=? AND balance_date=?
    """, (
        request.form["statement_date"],
        request.form["storage_facility_id"],
        request.form["product_id"],
        request.form["statement_date"],
    ))
    db.commit()
    flash("Storage statement posted.", "success")
    return redirect(url_for("storage"))

# ── Pending items (B's queue) ────────────────────────────────
@app.route("/pending")
@login_required
@role_required("CEO", "LOGISTICS_ADMIN")
def pending():
    db = get_db()
    items = db.execute("SELECT * FROM v_pending_items ORDER BY event_date ASC").fetchall()
    movements_pending = [i for i in items if i["item_type"] == "MOVEMENT"]
    ddts_pending      = [i for i in items if i["item_type"] == "SALE_DDT"]
    return render_template("pending.html",
        movements_pending=movements_pending,
        ddts_pending=ddts_pending,
        invoices_pending=[],
    )

# ── Sale status transitions ──────────────────────────────────
@app.route("/sales/<int:sale_id>/reset", methods=["POST"])
@login_required
def sale_reset(sale_id):
    db = get_db()
    # Only CEO and LOGISTICS_ADMIN can reset
    if current_role() not in ("CEO", "LOGISTICS_ADMIN"):
        flash("Not authorized.", "error")
        return redirect(url_for("sales_pipeline"))
    # Clear DDT data, undo lot assignment, reset status
    db.execute("""
        UPDATE sales SET
            status='PROVISIONAL',
            ddt_number=NULL,
            ddt_load_date=NULL,
            actual_mt=NULL,
            ddt_entered_by=NULL,
            ddt_entered_at=NULL,
            fifo_assigned=0,
            notes=NULL,
            updated_at=datetime('now')
        WHERE id=?
    """, (sale_id,))
    # Undo container status changes from sale_lots
    containers = db.execute(
        "SELECT DISTINCT container_id FROM sale_lots WHERE sale_id=?", (sale_id,)
    ).fetchall()
    for c in containers:
        db.execute("""
            UPDATE containers SET
                status='IN_STORAGE',
                updated_at=datetime('now')
            WHERE id=?
        """, (c["container_id"],))
    # Delete sale_lots
    db.execute("DELETE FROM sale_lots WHERE sale_id=?", (sale_id,))
    db.commit()
    flash("Sale reset to PROVISIONAL — DDT and lot assignment cleared.", "success")
    return redirect(url_for("sales_pipeline"))
@login_required
@role_required("CEO", "LOGISTICS_ADMIN")
def sale_instruct(sale_id):
    db = get_db()
    db.execute("""
        UPDATE sales SET
            status='INSTRUCTED',
            instruction_date=?,
            terminal_release_ref=?,
            carrier_booking_ref=?,
            instructed_mt=?,
            instruction_entered_by=?,
            updated_at=datetime('now')
        WHERE id=?
    """, (
        request.form["instruction_date"],
        request.form.get("terminal_release_ref", ""),
        request.form.get("carrier_booking_ref", ""),
        request.form.get("instructed_mt"),
        session["username"],
        sale_id,
    ))
    db.commit()
    flash("Sale marked as instructed.", "success")
    return redirect(url_for("pending"))

@app.route("/sales/<int:sale_id>/execute", methods=["POST"])
@login_required
@role_required("CEO", "LOGISTICS_ADMIN")
def sale_execute(sale_id):
    db = get_db()
    actual_mt = float(request.form["actual_mt"])
    db.execute("""
        UPDATE sales SET
            status='EXECUTED',
            ddt_number=?,
            ddt_load_date=?,
            ddt_delivery_date=?,
            actual_mt=?,
            ddt_entered_by=?,
            ddt_entered_at=datetime('now'),
            updated_at=datetime('now')
        WHERE id=?
    """, (
        request.form["ddt_number"],
        request.form["ddt_load_date"],
        request.form.get("ddt_delivery_date", ""),
        actual_mt,
        session["username"],
        sale_id,
    ))
    db.commit()
    flash("DDT recorded. Sale moved to EXECUTED.", "success")
    return redirect(url_for("pending"))

    # Invoice route removed — DDT = executed sale, invoicing is out of scope


# ── Storage Value (monthly snapshot) ────────────────────────
@app.route("/storage/value")
@login_required
@role_required("CEO", "BU_DIRECTOR")
def storage_value():
    db = get_db()
    rows = db.execute("""
        SELECT
            sf.code         AS facility_code,
            sf.name         AS facility_name,
            p.code          AS product_code,
            o.oda_code,
            o.price_eur_per_mt AS purchase_price_eur,
            o.price_usd_per_mt AS purchase_price_usd,
            o.currency,
            s.fx_rate_invoice,
            o.supplier_id,
            o.product_id,
            o.paese,
            o.order_date,
            c.storage_entry_date,
            COALESCE(c.actual_mt, c.nominal_mt) AS gross_mt,
            COALESCE(c.actual_mt, c.nominal_mt) - COALESCE((
                SELECT SUM(sl.mt_drawn) FROM sale_lots sl WHERE sl.container_id = c.id
            ), 0) AS available_mt,
            COALESCE(c.customs_cost_eur, 0) /
                NULLIF(COALESCE(c.actual_mt, c.nominal_mt), 0) AS customs_per_mt,
            COALESCE(c.transport_to_storage_eur, 0) /
                NULLIF(COALESCE(c.actual_mt, c.nominal_mt), 0) AS transp_st_per_mt
        FROM containers c
        JOIN shipments s  ON s.id = c.shipment_id
        JOIN odas o       ON o.id = s.oda_id
        JOIN products p   ON p.id = o.product_id
        JOIN storage_facilities sf ON sf.id = c.storage_facility_id
        WHERE c.status IN ('IN_STORAGE', 'PARTIALLY_SOLD')
          AND c.is_deleted = 0
        ORDER BY sf.code, p.code, o.order_date
    """).fetchall()

    from cost_utils import get_delivery_costs
    from collections import defaultdict

    enriched = []
    totals = {"gross_mt": 0, "available_mt": 0, "carrying_value": 0}

    for r in rows:
        r = dict(r)
        dc = get_delivery_costs(db, r["product_id"], r["supplier_id"],
                                r["paese"], r["order_date"])
        commission  = dc.get("commission") or 0.0 if dc else 0.0
        log_in      = dc.get("log_in") or 0.0 if dc else 0.0
        stoccaggio  = dc.get("stoccaggio") or 0.0 if dc else 0.0

        # Compute EUR purchase price using fx_rate_invoice for USD ODAs
        if r.get("currency") == "USD" and r.get("purchase_price_usd") and r.get("fx_rate_invoice"):
            purchase_price = round(r["purchase_price_usd"] / r["fx_rate_invoice"], 4)
        else:
            purchase_price = r.get("purchase_price_eur") or r.get("purchase_price") or 0
        r["purchase_price"] = purchase_price

        cost_per_mt = (
            purchase_price
            + commission + log_in + stoccaggio
            + (r["customs_per_mt"] or 0)
            + (r["transp_st_per_mt"] or 0)
        )
        available = r["available_mt"] or 0
        carrying  = round(cost_per_mt * available, 2)

        r["commission_per_mt"]  = commission
        r["log_in_per_mt"]      = log_in
        r["stoccaggio_per_mt"]  = stoccaggio
        r["cost_per_mt"]        = round(cost_per_mt, 4)
        r["carrying_value_eur"] = carrying

        enriched.append(r)
        totals["gross_mt"]       += r["gross_mt"] or 0
        totals["available_mt"]   += available
        totals["carrying_value"] += carrying

    subtotals = defaultdict(lambda: {"available_mt": 0, "carrying_value": 0})
    for r in enriched:
        key = (r["facility_code"], r["product_code"])
        subtotals[key]["available_mt"]   += r["available_mt"] or 0
        subtotals[key]["carrying_value"] += r["carrying_value_eur"]

    return render_template("storage_value.html",
        rows=enriched,
        subtotals=dict(subtotals),
        totals=totals,
    )



# ── Delete DDT (reset sale to INSTRUCTED) ────────────────────
@app.route("/sales/<int:sale_id>/delete_ddt", methods=["POST"])
@login_required
@role_required("CEO", "LOGISTICS_ADMIN")
def sale_delete_ddt(sale_id):
    db = get_db()
    sale = db.execute("SELECT * FROM sales WHERE id=?", (sale_id,)).fetchone()
    if not sale:
        flash("Sale not found.", "error")
        return redirect(url_for("sales_pipeline"))
    containers = db.execute(
        "SELECT DISTINCT container_id FROM sale_lots WHERE sale_id=?", (sale_id,)
    ).fetchall()
    for c in containers:
        db.execute("UPDATE containers SET status='IN_STORAGE', updated_at=datetime('now') WHERE id=?", (c["container_id"],))
    db.execute("DELETE FROM sale_lots WHERE sale_id=?", (sale_id,))
    db.execute("""UPDATE sales SET status='INSTRUCTED', ddt_number=NULL, ddt_load_date=NULL,
        actual_mt=NULL, ddt_entered_by=NULL, ddt_entered_at=NULL, fifo_assigned=0,
        updated_at=datetime('now') WHERE id=?""", (sale_id,))
    db.commit()
    flash("DDT cleared — sale reset to INSTRUCTED.", "success")
    return redirect(url_for("sales_pipeline"))


# ── Delete ODV (full sale delete) ────────────────────────────
@app.route("/sales/<int:sale_id>/delete", methods=["POST"])
@login_required
@role_required("CEO", "LOGISTICS_ADMIN")
def sale_delete(sale_id):
    db = get_db()
    lots = db.execute("SELECT COUNT(*) AS n FROM sale_lots WHERE sale_id=?", (sale_id,)).fetchone()
    if lots["n"] > 0:
        flash("Cannot delete — sale has lot assignments. Clear DDT first.", "error")
        return redirect(url_for("sales_pipeline"))
    db.execute("UPDATE sales SET is_deleted=1, updated_at=datetime('now') WHERE id=?", (sale_id,))
    db.commit()
    flash("Sale deleted.", "success")
    return redirect(url_for("sales_pipeline"))


# ── Delete B/L (shipment + containers) ───────────────────────
@app.route("/shipments/<int:shipment_id>/delete", methods=["POST"])
@login_required
@role_required("CEO", "LOGISTICS_ADMIN")
def shipment_delete(shipment_id):
    db = get_db()
    blocked = db.execute("""SELECT COUNT(*) AS n FROM sale_lots sl
        JOIN containers c ON c.id = sl.container_id WHERE c.shipment_id=?""", (shipment_id,)).fetchone()
    if blocked["n"] > 0:
        flash("Cannot delete B/L — containers have lot assignments. Reset sales first.", "error")
        return redirect(url_for("oda_status"))
    db.execute("UPDATE containers SET is_deleted=1, updated_at=datetime('now') WHERE shipment_id=?", (shipment_id,))
    db.execute("UPDATE shipments SET is_deleted=1, updated_at=datetime('now') WHERE id=?", (shipment_id,))
    db.commit()
    flash("B/L and its containers deleted.", "success")
    return redirect(request.referrer or url_for("oda_status"))


# ── Delete ODA (cascade to shipments + containers) ────────────
@app.route("/odas/<int:oda_id>/delete", methods=["POST"])
@login_required
@role_required("CEO", "LOGISTICS_ADMIN")
def oda_delete(oda_id):
    db = get_db()
    blocked = db.execute("""SELECT COUNT(*) AS n FROM sale_lots sl
        JOIN containers c ON c.id = sl.container_id
        JOIN shipments s  ON s.id = c.shipment_id WHERE s.oda_id=?""", (oda_id,)).fetchone()
    if blocked["n"] > 0:
        flash("Cannot delete ODA — containers have lot assignments. Reset sales first.", "error")
        return redirect(url_for("oda_status"))
    db.execute("""UPDATE containers SET is_deleted=1, updated_at=datetime('now')
        WHERE shipment_id IN (SELECT id FROM shipments WHERE oda_id=?)""", (oda_id,))
    db.execute("UPDATE shipments SET is_deleted=1, updated_at=datetime('now') WHERE oda_id=?", (oda_id,))
    db.execute("UPDATE odas SET is_deleted=1, updated_at=datetime('now') WHERE id=?", (oda_id,))
    db.commit()
    flash("ODA and all its shipments/containers deleted.", "success")
    return redirect(url_for("oda_status"))



# ── User Guide ───────────────────────────────────────────────
@app.route("/guida")
@login_required
def guida():
    return render_template("guida.html", role=current_role())



# ── Delete Container (ATB/road DDT) ──────────────────────────
@app.route("/containers/<int:container_id>/delete", methods=["POST"])
@login_required
@role_required("CEO", "LOGISTICS_ADMIN")
def container_delete(container_id):
    db = get_db()
    # Block if sale_lots exist
    lots = db.execute(
        "SELECT COUNT(*) AS n FROM sale_lots WHERE container_id=?", (container_id,)
    ).fetchone()
    if lots["n"] > 0:
        flash("Cannot delete — container has lot assignments. Reset sales first.", "error")
        return redirect(request.referrer or url_for("oda_status"))
    # Get shipment_id
    c = db.execute("SELECT shipment_id FROM containers WHERE id=?", (container_id,)).fetchone()
    if not c:
        flash("Container not found.", "error")
        return redirect(url_for("oda_status"))
    shipment_id = c["shipment_id"]
    # Soft delete container
    db.execute("UPDATE containers SET is_deleted=1, updated_at=datetime('now') WHERE id=?", (container_id,))
    # If shipment has no other active containers, delete it too
    remaining = db.execute(
        "SELECT COUNT(*) AS n FROM containers WHERE shipment_id=? AND is_deleted=0 AND id!=?",
        (shipment_id, container_id)
    ).fetchone()
    if remaining["n"] == 0:
        db.execute("UPDATE shipments SET is_deleted=1, updated_at=datetime('now') WHERE id=?", (shipment_id,))
    db.commit()
    flash("Container deleted.", "success")
    return redirect(request.referrer or url_for("oda_status"))



# ── ODA FX Rate Update ────────────────────────────────────────
@app.route("/odas/<int:oda_id>/fx", methods=["POST"])
@login_required
@role_required("CEO", "BU_DIRECTOR", "LOGISTICS_ADMIN")
def oda_fx_update(oda_id):
    from cost_utils import fetch_ecb_rate
    db = get_db()

    invoice_date = request.form.get("invoice_date") or None
    payment_date = request.form.get("payment_date") or None

    # Auto-fetch BCE rates if dates provided and rate not manually overridden
    fx_invoice = float(request.form.get("fx_rate_invoice") or 0) or None
    fx_payment = float(request.form.get("fx_rate_payment") or 0) or None

    if invoice_date and not fx_invoice:
        fx_invoice = fetch_ecb_rate(invoice_date)
    if payment_date and not fx_payment:
        fx_payment = fetch_ecb_rate(payment_date)

    db.execute("""
        UPDATE odas SET invoice_date=?, fx_rate_invoice=?,
        payment_date=?, fx_rate_payment=?,
        updated_at=datetime('now') WHERE id=?
    """, (invoice_date, fx_invoice, payment_date, fx_payment, oda_id))
    db.commit()

    msg = "Tassi di cambio aggiornati."
    if fx_invoice:
        msg += f" Fattura: {fx_invoice:.4f}"
    if fx_payment:
        msg += f" | Pagamento: {fx_payment:.4f}"
    flash(msg, "success")
    return redirect(url_for("oda_detail", oda_id=oda_id))



# ── Shipment FX Rate Update ───────────────────────────────────
@app.route("/shipments/<int:shipment_id>/fx", methods=["POST"])
@login_required
@role_required("CEO", "BU_DIRECTOR", "LOGISTICS_ADMIN")
def shipment_fx_update(shipment_id):
    from cost_utils import fetch_ecb_rate
    db = get_db()

    invoice_date = request.form.get("invoice_date") or None
    payment_date = request.form.get("payment_date") or None
    fx_invoice   = float(request.form.get("fx_rate_invoice") or 0) or None
    fx_payment   = float(request.form.get("fx_rate_payment") or 0) or None

    if invoice_date and not fx_invoice:
        fx_invoice = fetch_ecb_rate(invoice_date)
    if payment_date and not fx_payment:
        fx_payment = fetch_ecb_rate(payment_date)

    db.execute("""
        UPDATE shipments SET invoice_date=?, fx_rate_invoice=?,
        payment_date=?, fx_rate_payment=?, updated_at=datetime('now')
        WHERE id=?
    """, (invoice_date, fx_invoice, payment_date, fx_payment, shipment_id))
    db.commit()

    # Get oda_id to redirect back
    s = db.execute("SELECT oda_id FROM shipments WHERE id=?", (shipment_id,)).fetchone()
    msg = "Cambi aggiornati."
    if fx_invoice: msg += f" Fattura: {fx_invoice:.4f}"
    if fx_payment: msg += f" | Pagamento: {fx_payment:.4f}"
    flash(msg, "success")
    return redirect(url_for("oda_detail", oda_id=s["oda_id"]))



# ── COA Repository ───────────────────────────────────────────
@app.route("/coa-repository")
@login_required
def coa_repository():
    db = get_db()
    product_filter = request.args.get("product_id", "")
    where = ["1=1"]
    params = []
    if product_filter:
        where.append("cd.product_id=?")
        params.append(product_filter)

    docs = db.execute(f"""
        SELECT cd.*, p.code AS product_code, p.name AS product_name,
               s.name AS supplier_name, c.container_code,
               o.oda_code
        FROM coa_documents cd
        LEFT JOIN products p   ON p.id  = cd.product_id
        LEFT JOIN containers c ON c.id  = cd.container_id
        LEFT JOIN shipments sh ON sh.id = cd.shipment_id
        LEFT JOIN odas o       ON o.id  = sh.oda_id
        LEFT JOIN suppliers s  ON s.id  = o.supplier_id
        WHERE {" AND ".join(where)}
        ORDER BY cd.uploaded_at DESC
    """, params).fetchall()

    products = db.execute("SELECT * FROM products WHERE is_active=1 ORDER BY code").fetchall()
    return render_template("coa_repository.html", docs=docs, products=products,
                           product_filter=product_filter)


@app.route("/coa-repository/<int:doc_id>/download")
@login_required
def coa_download(doc_id):
    from flask import send_from_directory
    db = get_db()
    doc = db.execute("SELECT * FROM coa_documents WHERE id=?", (doc_id,)).fetchone()
    if not doc:
        flash("Document not found.", "error")
        return redirect(url_for("coa_repository"))
    import os
    coa_dir = os.path.join(os.path.dirname(__file__), "uploads", "coas")
    return send_from_directory(coa_dir, doc["filename"],
                               download_name=doc["original_name"] or doc["filename"])


# ── Entry point ──────────────────────────────────────────────
if __name__ == "__main__":
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    init_db()
    app.run(host="0.0.0.0", port=5000, debug=False)
