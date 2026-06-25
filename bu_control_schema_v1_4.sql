-- ============================================================
-- BU CONTROL TOOL — SQLite Schema v1.4
-- Italian trading company — chemicals/biofuels distribution
-- ============================================================
-- v1.4 changes vs v1.3:
--   • containers.container_code → nullable (ATB road tankers have no ISO code)
--   • containers: added transport_mode, ddt_number, plate_number, leasing_company_id,
--                 empty_return_deadline, empty_returned_date
--   • shipments: added ddt_number, plate_number, transport_mode, carrier_id (road)
--   • container_leasing_companies → NEW table
--   • leasing_rates → NEW table (free days + demurrage rate, time-versioned)
--   • road_transport_rates → NEW table (carrier + route + rate, time-versioned)
--   • locations → NEW reference table (depots, supplier sites, customer sites)
-- ============================================================

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- ------------------------------------------------------------
-- REFERENCE TABLES
-- ------------------------------------------------------------

CREATE TABLE IF NOT EXISTS products (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    code                TEXT NOT NULL UNIQUE,           -- internal code e.g. "FAME-RME"
    name                TEXT NOT NULL,
    unit                TEXT NOT NULL DEFAULT 'MT',     -- MT standard
    is_active           INTEGER NOT NULL DEFAULT 1,
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Supplier aliases — learned from document imports
-- e.g. "PRINCZ SAICFel Argentina" → stored as "PRINCZ SAICFel"
CREATE TABLE IF NOT EXISTS supplier_aliases (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    supplier_id         INTEGER NOT NULL REFERENCES suppliers(id),
    alias               TEXT NOT NULL,
    source              TEXT NOT NULL DEFAULT 'MANUAL', -- MANUAL | LEARNED
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(alias)
);

CREATE INDEX IF NOT EXISTS idx_supplier_aliases ON supplier_aliases(alias);

-- Product aliases — supplier-specific product name mappings
-- e.g. PRINCZ calls ESO "PRINPLAS700"; learned automatically from ODA imports
CREATE TABLE IF NOT EXISTS product_aliases (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id          INTEGER NOT NULL REFERENCES products(id),
    supplier_id         INTEGER NOT NULL REFERENCES suppliers(id),
    alias               TEXT NOT NULL,                  -- supplier's name for this product
    source              TEXT NOT NULL DEFAULT 'MANUAL', -- MANUAL | LEARNED (auto from ODA confirm)
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(supplier_id, alias)                          -- one alias per supplier is unambiguous
);

CREATE INDEX IF NOT EXISTS idx_product_aliases ON product_aliases(supplier_id, alias);

CREATE TABLE IF NOT EXISTS suppliers (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    code                TEXT UNIQUE,                      -- nullable: auto-generated if not provided
    name                TEXT NOT NULL,
    country             TEXT,
    is_active           INTEGER NOT NULL DEFAULT 1,
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS customers (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    code                TEXT UNIQUE,                      -- nullable: auto-generated if not provided
    name                TEXT NOT NULL,
    country             TEXT,
    is_active           INTEGER NOT NULL DEFAULT 1,
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS storage_facilities (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    code                TEXT NOT NULL UNIQUE,           -- e.g. "WH-IT-01"
    name                TEXT NOT NULL,
    location            TEXT,
    country             TEXT NOT NULL DEFAULT 'IT',
    is_active           INTEGER NOT NULL DEFAULT 1,
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Supplier → default depot mapping
-- Used by customs clearance parser to auto-assign containers to a depot
-- Can be overridden case-by-case at customs clearance entry
CREATE TABLE IF NOT EXISTS supplier_depot_defaults (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    supplier_id             INTEGER NOT NULL REFERENCES suppliers(id),
    storage_facility_id     INTEGER NOT NULL REFERENCES storage_facilities(id),
    product_id              INTEGER REFERENCES products(id), -- NULL = applies to all products
    valid_from              TEXT NOT NULL DEFAULT '2000-01-01', -- ISO date
    valid_to                TEXT,                           -- NULL = currently active
    notes                   TEXT,
    created_at              TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at              TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(supplier_id, product_id, valid_from)
);

-- ------------------------------------------------------------
-- CONTAINER LEASING COMPANIES
-- ------------------------------------------------------------
-- e.g. Den Hartogh, Stolt, Eurotainer — own the isotanks
-- Containers must be returned within free days or demurrage applies

CREATE TABLE IF NOT EXISTS container_leasing_companies (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    name                TEXT NOT NULL,
    code                TEXT UNIQUE,                        -- e.g. DHDU prefix
    contact_email       TEXT,
    notes               TEXT,
    is_active           INTEGER NOT NULL DEFAULT 1,
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Time-versioned demurrage rates per leasing company
CREATE TABLE IF NOT EXISTS leasing_rates (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    leasing_company_id      INTEGER NOT NULL REFERENCES container_leasing_companies(id),
    valid_from              TEXT NOT NULL,                  -- ISO date
    free_days               INTEGER NOT NULL DEFAULT 14,    -- days before demurrage starts
    demurrage_rate_eur_day  REAL NOT NULL,                 -- €/day per container after free days
    notes                   TEXT,
    created_at              TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(leasing_company_id, valid_from)
);

CREATE INDEX IF NOT EXISTS idx_leasing_rates ON leasing_rates(leasing_company_id, valid_from DESC);

-- ------------------------------------------------------------
-- LOCATIONS (reference table for road transport routes)
-- ------------------------------------------------------------
-- Covers: supplier sites, our depots, customer delivery points

CREATE TABLE IF NOT EXISTS locations (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    code                TEXT UNIQUE,                        -- short code e.g. "GE-SILOMAR"
    name                TEXT NOT NULL,
    type                TEXT NOT NULL DEFAULT 'OTHER',      -- DEPOT | SUPPLIER | CUSTOMER | PORT
    address             TEXT,
    city                TEXT,
    country             TEXT NOT NULL DEFAULT 'IT',
    is_active           INTEGER NOT NULL DEFAULT 1,
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ------------------------------------------------------------
-- ROAD TRANSPORT RATES
-- ------------------------------------------------------------
-- Time-versioned rate matrix for road carriers
-- from_location_id NULL = any origin; to_location_id NULL = any destination (variable)

CREATE TABLE IF NOT EXISTS road_transport_rates (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    carrier_id              INTEGER NOT NULL REFERENCES carriers(id),
    from_location_id        INTEGER REFERENCES locations(id),
    to_location_id          INTEGER REFERENCES locations(id), -- NULL = variable (e.g. customer)
    valid_from              TEXT NOT NULL,                   -- ISO date
    rate_eur_per_mt         REAL,                           -- €/MT (preferred for variable dest)
    rate_eur_per_trip       REAL,                           -- €/trip (fixed route)
    notes                   TEXT,
    created_at              TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at              TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_road_transport_rates
    ON road_transport_rates(carrier_id, from_location_id, to_location_id, valid_from DESC);

CREATE TABLE IF NOT EXISTS carriers (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    code                TEXT NOT NULL UNIQUE,
    name                TEXT NOT NULL,
    is_active           INTEGER NOT NULL DEFAULT 1,
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ------------------------------------------------------------
-- DELIVERY COSTS (purchase-side cost stack per lane)
-- ------------------------------------------------------------
-- Replaces the broken DEL COSTS import into transport_matrix.
-- Each row = full cost stack for a product/supplier/country lane
-- valid from a specific date.
-- Two date anchors per cost component (Option 5a):
--   cost_anchor = 'ODA'       → rate looked up using ODA order_date
--   cost_anchor = 'CLEARANCE' → rate looked up using customs_clearance_date
-- Per business rule confirmed 2026-06-18:
--   ODA date:       log_in, log_in_currency, commission, commission_currency,
--                   porto, porto_currency
--   Clearance date: dazio (duty), log_ita, stoccaggio

CREATE TABLE IF NOT EXISTS delivery_costs (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    -- Lane identifiers (any can be NULL = applies to all)
    product_id              INTEGER REFERENCES products(id),
    supplier_id             INTEGER REFERENCES suppliers(id),
    paese                   TEXT,                       -- country of origin
    -- Effective date — new row for each rate change
    valid_from              TEXT NOT NULL,              -- ISO date
    -- Purchase-side costs anchored to ODA date
    log_in                  REAL,                       -- inbound logistics €/MT or lump
    log_in_currency         TEXT DEFAULT 'EUR',
    commission              REAL,                       -- trading commission €/MT
    commission_currency     TEXT DEFAULT 'EUR',
    porto                   REAL,                       -- port cost €/MT or lump
    porto_currency          TEXT DEFAULT 'EUR',
    -- Purchase-side costs anchored to CLEARANCE date
    dazio                   REAL,                       -- customs duty rate (% or €/MT)
    dazio_type              TEXT DEFAULT 'PCT',         -- PCT = percentage | EUR_MT = €/MT
    log_ita                 REAL,                       -- Italian logistics €/container
    stoccaggio              REAL,                       -- storage cost €/MT/month
    -- Audit
    source                  TEXT NOT NULL DEFAULT 'MANUAL', -- MANUAL | EXCEL_IMPORT
    notes                   TEXT,
    created_at              TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at              TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_delivery_costs_lane
    ON delivery_costs(product_id, supplier_id, paese, valid_from DESC);

-- Transport rate matrix: storage × destination × carrier → €/MT
-- Used as default lookup on SALE entry (outbound transport); overridable at sale level
CREATE TABLE IF NOT EXISTS transport_matrix (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    storage_facility_id     INTEGER NOT NULL REFERENCES storage_facilities(id),
    destination             TEXT NOT NULL,              -- city or zone code
    carrier_id              INTEGER NOT NULL REFERENCES carriers(id),
    product_id              INTEGER REFERENCES products(id),  -- NULL = applies to all
    rate_eur_per_mt         REAL NOT NULL,
    valid_from              TEXT NOT NULL,              -- ISO date
    valid_to                TEXT,                       -- NULL = currently valid
    created_at              TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at              TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ------------------------------------------------------------
-- ORDERS
-- ------------------------------------------------------------

-- ODA = Purchase Order. Single ODA can have multiple shipments.
CREATE TABLE IF NOT EXISTS odas (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    oda_code            TEXT NOT NULL UNIQUE,           -- e.g. "2026-0167"
    supplier_id         INTEGER NOT NULL REFERENCES suppliers(id),
    product_id          INTEGER NOT NULL REFERENCES products(id),
    total_mt            REAL NOT NULL,
    price_usd_per_mt    REAL,                           -- NULL if EUR-only deal
    price_eur_per_mt    REAL NOT NULL,
    exchange_rate       REAL,                           -- derived: usd/eur, stored at entry
    currency            TEXT NOT NULL DEFAULT 'EUR',    -- EUR or USD
    order_date          TEXT NOT NULL,                  -- ISO date
    -- Operational fields
    paese               TEXT,                           -- country of origin
    vessel_name         TEXT,                           -- expected vessel at ODA time
    port_of_discharge   TEXT,                           -- expected port
    container_type      TEXT,                           -- ISOTANK | ONE_TON_CUBE
    num_containers      INTEGER,                        -- expected number of containers
    supplier_order_ref  TEXT,                           -- supplier's own reference
    incoterm            TEXT,                           -- e.g. FOB, CIF, DAP
    incoterm_location   TEXT,                           -- city/port only e.g. GENOVA, B.AIRES
    incoterm_full       TEXT,                           -- full string e.g. CIF GENOVA T1
    customs_regime      TEXT,                           -- T1 | T2 | null
    vat_rate            REAL,                           -- 0, 4, 10, 22 etc.
    payment_terms       TEXT,                           -- e.g. ANTICIPATO/ADVANCED
    -- Transport/cost references (rate looked up from delivery_costs at import time)
    transport_to_storage_eur REAL,                      -- total for this ODA (all containers)
    -- Commercial planning
    expected_delivery_month TEXT,                       -- YYYY-MM, for position planning
    -- Audit
    source              TEXT NOT NULL DEFAULT 'MANUAL', -- MANUAL | EXCEL_IMPORT | ODA_PARSER
    notes               TEXT,
    is_deleted          INTEGER NOT NULL DEFAULT 0,     -- soft delete
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ODA delivery lines — commercial schedule rows from the ODA PDF
-- One row per line item: each has its own MT, price, delivery date, container count
CREATE TABLE IF NOT EXISTS oda_lines (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    oda_id              INTEGER NOT NULL REFERENCES odas(id),
    line_number         INTEGER NOT NULL DEFAULT 1,       -- sequential within ODA
    mt_planned          REAL NOT NULL,                    -- MT for this line
    price_per_mt        REAL,                             -- price for this line (may differ per tranche)
    container_type      TEXT,                             -- ISOTANK | ONE_TON_CUBE
    num_containers      INTEGER,                          -- expected containers for this line
    scheduled_date      TEXT,                             -- ISO date — delivery date from ODA
    notes               TEXT,
    source              TEXT NOT NULL DEFAULT 'MANUAL',
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at          TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(oda_id, line_number)
);

-- Shipment = one B/L = one physical vessel loading event within an ODA
-- Multiple shipments per ODA are normal (6 B/Ls for one ODA is fine)
CREATE TABLE IF NOT EXISTS shipments (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    oda_id              INTEGER NOT NULL REFERENCES odas(id),
    shipment_number     INTEGER NOT NULL DEFAULT 1,
    -- Transport mode
    transport_mode      TEXT NOT NULL DEFAULT 'SEA',        -- SEA | ROAD
    -- B/L fields (sea shipments)
    bl_number           TEXT,
    bl_date             TEXT,
    vessel_name         TEXT,
    voyage_ref          TEXT,
    port_of_loading     TEXT,
    port_of_discharge   TEXT,
    -- DDT fields (road shipments)
    ddt_number          TEXT,                               -- e.g. 485/2026
    ddt_date            TEXT,                               -- ISO date
    carrier_id          INTEGER REFERENCES carriers(id),    -- road carrier
    plate_number        TEXT,                               -- truck plate
    from_location_id    INTEGER REFERENCES locations(id),   -- pickup point
    to_location_id      INTEGER REFERENCES locations(id),   -- delivery point
    -- Arrival
    actual_date         TEXT,
    source              TEXT NOT NULL DEFAULT 'MANUAL',     -- MANUAL | BL_PARSER | DDT_PARSER
    notes               TEXT,
    is_deleted          INTEGER NOT NULL DEFAULT 0,
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at          TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(oda_id, shipment_number)
);


-- ------------------------------------------------------------
-- MOVEMENTS (container-level)
-- ------------------------------------------------------------

CREATE TABLE IF NOT EXISTS containers (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    container_code          TEXT UNIQUE,                    -- nullable for ATB road tankers
    shipment_id             INTEGER NOT NULL REFERENCES shipments(id),
    container_type          TEXT NOT NULL,                  -- ISOTANK | ONE_TON_CUBE | ATB
    transport_mode          TEXT NOT NULL DEFAULT 'SEA',    -- SEA | ROAD
    nominal_mt              REAL NOT NULL,                  -- 23 MT isotank, 1 MT cube, actual for ATB
    actual_mt               REAL,                           -- set on customs clearance / discharge
    -- Road transport specific
    ddt_number              TEXT,                           -- DDT reference for road shipments
    plate_number            TEXT,                           -- truck plate (optional, recurring ok)
    -- COA data (per container)
    production_lot          TEXT,                           -- supplier batch from COA
    manufacture_date        TEXT,                           -- ISO date from COA
    expiration_date         TEXT,                           -- ISO date from COA
    coa_date                TEXT,                           -- ISO date of issue of COA
    -- Customs clearance
    customs_clearance_date  TEXT,                           -- ISO date; drives FIFO queue
    customs_cost_eur        REAL,                           -- lump sum for this container
    -- Transport to storage
    transport_to_storage_eur REAL,                          -- lump sum for this container
    transport_carrier_id    INTEGER REFERENCES carriers(id), -- road carrier if applicable
    road_transport_rate_id  INTEGER REFERENCES road_transport_rates(id), -- rate used
    road_transport_override INTEGER NOT NULL DEFAULT 0,     -- 1 if actual cost differs from matrix
    road_transport_override_reason TEXT,
    -- Storage assignment
    storage_facility_id     INTEGER REFERENCES storage_facilities(id),
    storage_entry_date      TEXT,                           -- ISO date
    -- Leasing / empty return tracking (sea isotanks)
    leasing_company_id      INTEGER REFERENCES container_leasing_companies(id),
    empty_return_deadline   TEXT,                           -- ISO date (discharge + free days)
    empty_returned_date     TEXT,                           -- ISO date, set by logistics
    demurrage_cost_eur      REAL,                           -- actual demurrage if any
    -- Vessel tracking (updated by G)
    vessel_name             TEXT,
    voyage_ref              TEXT,
    port_of_discharge       TEXT,
    eta_date                TEXT,
    ata_date                TEXT,
    eta_last_updated_by     TEXT,
    eta_last_updated_at     TEXT,
    -- Status
    status                  TEXT NOT NULL DEFAULT 'IN_TRANSIT',
    -- SEA: IN_TRANSIT | CUSTOMS | IN_STORAGE | PARTIALLY_SOLD | FULLY_SOLD | SCRAPPED
    -- ROAD: IN_TRANSIT_ROAD | IN_STORAGE | PARTIALLY_SOLD | FULLY_SOLD
    -- Audit
    entered_by              TEXT,
    source                  TEXT NOT NULL DEFAULT 'MANUAL',
    is_deleted              INTEGER NOT NULL DEFAULT 0,
    created_at              TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at              TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Derived cost per MT helpers (computed in application layer, not stored):
--   customs_cost_per_mt    = customs_cost_eur / actual_mt
--   transport_to_st_per_mt = transport_to_storage_eur / actual_mt

-- Container movement log — every physical move recorded
CREATE TABLE IF NOT EXISTS container_movements (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    container_id            INTEGER NOT NULL REFERENCES containers(id),
    movement_type           TEXT NOT NULL,
    -- IMPORT | CUSTOMS_CLEARANCE | TRANSFER_IN | TRANSFER_OUT | SALE_OUT | ADJUSTMENT
    movement_date           TEXT NOT NULL,              -- ISO date of actual physical event
    recorded_at             TEXT NOT NULL DEFAULT (datetime('now')), -- wall-clock entry timestamp
    -- Two-phase entry: logistics team enters physical data, B completes costs
    entry_status            TEXT NOT NULL DEFAULT 'PENDING_COSTS',
    -- PENDING_COSTS (logistics team entered) | COMPLETE (B added costs)
    from_facility_id        INTEGER REFERENCES storage_facilities(id),
    to_facility_id          INTEGER REFERENCES storage_facilities(id),
    mt_moved                REAL NOT NULL,
    reference_doc           TEXT,                       -- bill of lading, CMR, DDT ref
    notes                   TEXT,
    -- Phase 1 — logistics team
    phase1_entered_by       TEXT,
    -- Phase 2 — B completes costs
    cost_eur                REAL,                       -- customs or transport cost if applicable
    phase2_entered_by       TEXT,
    phase2_entered_at       TEXT,
    source                  TEXT NOT NULL DEFAULT 'MANUAL',  -- MANUAL | EMAIL_PARSER
    raw_email_id            INTEGER,                    -- FK to email_inbox if parsed
    created_at              TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at              TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ------------------------------------------------------------
-- STORAGE
-- ------------------------------------------------------------

-- Confirmed statements posted by Admin
CREATE TABLE IF NOT EXISTS storage_statements (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    storage_facility_id     INTEGER NOT NULL REFERENCES storage_facilities(id),
    statement_date          TEXT NOT NULL,              -- ISO date of the statement
    product_id              INTEGER NOT NULL REFERENCES products(id),
    confirmed_mt            REAL NOT NULL,
    posted_by               TEXT NOT NULL,              -- admin user
    notes                   TEXT,
    created_at              TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(storage_facility_id, statement_date, product_id)
);

-- Daily provisional balances — materialized by application layer
-- Provisional = last confirmed statement + movements in - movements out
-- Recalculated on every movement insert; stored for performance
CREATE TABLE IF NOT EXISTS storage_provisional (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    storage_facility_id     INTEGER NOT NULL REFERENCES storage_facilities(id),
    product_id              INTEGER NOT NULL REFERENCES products(id),
    balance_date            TEXT NOT NULL,              -- ISO date
    provisional_mt          REAL NOT NULL,
    confirmed_as_of         TEXT,                       -- date of last statement used as base
    is_confirmed            INTEGER NOT NULL DEFAULT 0, -- 1 when statement exists for this date
    calculated_at           TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(storage_facility_id, product_id, balance_date)
);

-- ------------------------------------------------------------
-- SALES
-- ------------------------------------------------------------

CREATE TABLE IF NOT EXISTS sales (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    sale_code               TEXT NOT NULL UNIQUE,       -- internal ref e.g. "SAL-2026-0089"
    customer_id             INTEGER NOT NULL REFERENCES customers(id),
    product_id              INTEGER NOT NULL REFERENCES products(id),
    destination             TEXT,                       -- nullable: Da definire at DDT time
    storage_facility_id     INTEGER REFERENCES storage_facilities(id), -- nullable: Da definire
    incoterm                TEXT NOT NULL DEFAULT 'DAP',

    -- ── PRICING (set at PROVISIONAL, immutable after EXECUTED) ──
    price_usd_per_mt        REAL,
    price_eur_per_mt        REAL NOT NULL,
    exchange_rate           REAL,                       -- derived at entry

    -- ── TRANSPORT TO CUSTOMER (DAP only; zero for EXW) ──
    carrier_id              INTEGER REFERENCES carriers(id),
    transport_to_customer_eur REAL,                     -- total for this sale; 0 if EXW
    transport_source        TEXT NOT NULL DEFAULT 'MATRIX',  -- MATRIX | MANUAL | NA
    transport_override_reason TEXT,                     -- mandatory if MANUAL

    -- ── STATUS MACHINE ──
    -- PROVISIONAL → INSTRUCTED → EXECUTED → INVOICED
    status                  TEXT NOT NULL DEFAULT 'PROVISIONAL',

    -- LAYER 1 — PROVISIONAL
    provisional_date        TEXT NOT NULL,              -- ISO date, when sale was entered
    provisional_mt          REAL NOT NULL,              -- estimated MT
    provisional_entered_by  TEXT NOT NULL,

    -- LAYER 2 — INSTRUCTION
    instruction_date        TEXT,                       -- ISO date
    terminal_release_ref    TEXT,                       -- EXW and DAP
    carrier_booking_ref     TEXT,                       -- DAP only
    instructed_mt           REAL,
    instruction_entered_by  TEXT,

    -- LAYER 3 — DDT (actual execution)
    ddt_number              TEXT,                       -- Documento di Trasporto ref
    ddt_load_date           TEXT,                       -- ISO date — drives storage balance update
    ddt_delivery_date       TEXT,                       -- ISO date — actual delivery
    actual_mt               REAL,                       -- exact MT e.g. 25.89
    ddt_entered_by          TEXT,
    ddt_entered_at          TEXT,                       -- wall-clock timestamp of entry (may differ from load date)

    -- LAYER 4 — INVOICED
    invoice_number          TEXT,
    invoice_date            TEXT,                       -- ISO date
    invoiced_by             TEXT,

    -- ── SOURCE / ODV ──
    odv_ref                 TEXT,                       -- ODV document reference
    source                  TEXT NOT NULL DEFAULT 'MANUAL', -- MANUAL | ODV_PARSER

    -- ── FIFO ASSIGNMENT ──
    fifo_assigned           INTEGER NOT NULL DEFAULT 0, -- 0=pending, 1=assigned

    notes                   TEXT,
    is_deleted              INTEGER NOT NULL DEFAULT 0,
    created_at              TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at              TEXT NOT NULL DEFAULT (datetime('now'))
);

-- FIFO lot assignment lines — one sale can draw from multiple lots
CREATE TABLE IF NOT EXISTS sale_lots (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    sale_id                 INTEGER NOT NULL REFERENCES sales(id),
    shipment_id             INTEGER NOT NULL REFERENCES shipments(id),
    container_id            INTEGER REFERENCES containers(id), -- NULL if lot-level only
    mt_drawn                REAL NOT NULL,
    -- FIFO override
    fifo_override           INTEGER NOT NULL DEFAULT 0,   -- 1 = manually reassigned from FIFO proposal
    fifo_override_reason    TEXT,                         -- optional free text
    -- Costs at draw time (snapshot — immutable after insert)
    purchase_price_eur_per_mt       REAL NOT NULL,
    customs_cost_eur_per_mt         REAL,
    transport_to_storage_eur_per_mt REAL,
    transport_to_customer_eur_per_mt REAL,                -- 0.0 explicitly if EXW (not NULL)
    -- Derived margin (computed and stored for performance)
    landed_cost_eur_per_mt  REAL,                         -- sum of above four
    sale_price_eur_per_mt   REAL NOT NULL,
    margin_eur_per_mt       REAL,                         -- sale_price - landed_cost
    margin_eur_total        REAL,                         -- margin_per_mt * mt_drawn
    created_at              TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(sale_id, shipment_id, container_id)
);

-- ------------------------------------------------------------
-- DEMURRAGE TRACKER VIEW
-- ------------------------------------------------------------
CREATE VIEW IF NOT EXISTS v_demurrage_tracker AS
SELECT
    c.id                        AS container_id,
    c.container_code,
    o.oda_code,
    s.bl_number,
    lc.name                     AS leasing_company,
    c.customs_clearance_date    AS discharge_date,
    lr.free_days,
    c.empty_return_deadline,
    c.empty_returned_date,
    lr.demurrage_rate_eur_day,
    CAST(julianday('now') - julianday(c.customs_clearance_date) AS INTEGER) AS days_since_discharge,
    CAST(julianday('now') - julianday(c.empty_return_deadline) AS INTEGER)  AS days_overdue,
    CASE
        WHEN c.empty_returned_date IS NOT NULL THEN COALESCE(c.demurrage_cost_eur, 0)
        WHEN c.empty_return_deadline IS NOT NULL
             AND julianday('now') > julianday(c.empty_return_deadline) THEN
            CAST(julianday('now') - julianday(c.empty_return_deadline) AS INTEGER)
            * COALESCE(lr.demurrage_rate_eur_day, 0)
        ELSE 0
    END                         AS estimated_demurrage_eur,
    CASE
        WHEN c.empty_returned_date IS NOT NULL THEN 'RETURNED'
        WHEN c.customs_clearance_date IS NULL THEN 'NOT_CLEARED'
        WHEN c.empty_return_deadline IS NULL THEN 'NO_DEADLINE'
        WHEN julianday('now') > julianday(c.empty_return_deadline) THEN 'OVERDUE'
        WHEN julianday(c.empty_return_deadline) - julianday('now') <= 3 THEN 'DUE_SOON'
        ELSE 'OK'
    END                         AS return_status
FROM containers c
JOIN shipments s  ON s.id  = c.shipment_id
JOIN odas o       ON o.id  = s.oda_id
LEFT JOIN container_leasing_companies lc ON lc.id = c.leasing_company_id
LEFT JOIN leasing_rates lr ON lr.id = (
    SELECT id FROM leasing_rates
    WHERE leasing_company_id = c.leasing_company_id
      AND valid_from <= COALESCE(c.customs_clearance_date, date('now'))
    ORDER BY valid_from DESC LIMIT 1
)
WHERE c.transport_mode = 'SEA'
  AND c.is_deleted = 0
ORDER BY
    CASE WHEN c.empty_returned_date IS NOT NULL THEN 1 ELSE 0 END,
    days_overdue DESC NULLS LAST;

-- ------------------------------------------------------------
-- FIFO QUEUE VIEW
-- ------------------------------------------------------------
-- Available inventory ordered by customs_clearance_date ASC
-- Application layer queries this to propose lot assignment on sale entry

CREATE VIEW IF NOT EXISTS v_fifo_queue AS
SELECT
    c.id                        AS container_id,
    c.container_code,
    c.container_type,
    s.id                        AS shipment_id,
    c.production_lot,
    o.oda_code,
    COALESCE(c.production_lot, o.oda_code) AS effective_lot_ref,
    o.product_id,
    p.code                      AS product_code,
    c.customs_clearance_date,
    COALESCE(c.actual_mt, c.nominal_mt) AS total_mt,
    c.storage_facility_id,
    -- Remaining MT = actual_mt (or nominal) minus all mt_drawn in sale_lots
    COALESCE(c.actual_mt, c.nominal_mt) - COALESCE((
        SELECT SUM(sl.mt_drawn)
        FROM sale_lots sl
        WHERE sl.container_id = c.id
    ), 0)                       AS available_mt,
    c.status
FROM containers c
JOIN shipments s ON s.id = c.shipment_id
JOIN odas o ON o.id = s.oda_id
JOIN products p ON p.id = o.product_id
WHERE c.status NOT IN ('FULLY_SOLD', 'SCRAPPED')
  AND c.is_deleted = 0
  AND c.customs_clearance_date IS NOT NULL
ORDER BY o.product_id, c.customs_clearance_date ASC;

-- ------------------------------------------------------------
-- LOT MARGIN VIEW
-- ------------------------------------------------------------

CREATE VIEW IF NOT EXISTS v_lot_margin AS
SELECT
    o.oda_code,
    s.id                        AS shipment_id,
    s.shipment_number,
    COALESCE(s.bl_number, o.oda_code || '-' || s.shipment_number) AS effective_lot_ref,
    p.code                      AS product_code,
    su.name                     AS supplier_name,
    s.vessel_name,
    s.bl_date,
    o.price_eur_per_mt          AS purchase_price_eur_per_mt,
    o.exchange_rate,
    -- Total MT (use actual if available, fall back to nominal)
    SUM(COALESCE(c.actual_mt, c.nominal_mt))    AS total_mt,
    COUNT(DISTINCT c.id)                         AS num_containers,
    -- Sold MT
    COALESCE(SUM(sl_agg.mt_sold), 0)            AS sold_mt,
    -- Unsold MT
    SUM(COALESCE(c.actual_mt, c.nominal_mt)) - COALESCE(SUM(sl_agg.mt_sold), 0) AS unsold_mt,
    -- Realized margin
    COALESCE(SUM(sl_agg.margin_eur_total), 0)   AS realized_margin_eur,
    -- Carrying value of unsold MT
    (SUM(COALESCE(c.actual_mt, c.nominal_mt)) - COALESCE(SUM(sl_agg.mt_sold), 0))
        * (o.price_eur_per_mt
           + COALESCE(SUM(c.customs_cost_eur) / NULLIF(SUM(COALESCE(c.actual_mt, c.nominal_mt)), 0), 0)
           + COALESCE(SUM(c.transport_to_storage_eur) / NULLIF(SUM(COALESCE(c.actual_mt, c.nominal_mt)), 0), 0)
          )                     AS unsold_carrying_value_eur
FROM shipments s
JOIN odas o   ON o.id  = s.oda_id  AND o.is_deleted = 0
JOIN products p   ON p.id  = o.product_id
JOIN suppliers su ON su.id = o.supplier_id
LEFT JOIN containers c ON c.shipment_id = s.id AND c.is_deleted = 0
LEFT JOIN (
    SELECT
        sl.container_id,
        SUM(sl.mt_drawn)         AS mt_sold,
        SUM(sl.margin_eur_total) AS margin_eur_total
    FROM sale_lots sl
    GROUP BY sl.container_id
) sl_agg ON sl_agg.container_id = c.id
WHERE s.is_deleted = 0
GROUP BY s.id
ORDER BY o.order_date DESC, s.shipment_number;

-- ------------------------------------------------------------
-- EMAIL PARSER INBOX (raw, for audit)
-- ------------------------------------------------------------

CREATE TABLE IF NOT EXISTS email_inbox (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    received_at         TEXT NOT NULL,
    sender              TEXT,
    subject             TEXT,
    raw_body            TEXT NOT NULL,
    parsed_status       TEXT NOT NULL DEFAULT 'PENDING',
    -- PENDING | PARSED | FAILED | MANUAL_REVIEW
    parsed_at           TEXT,
    parsed_by           TEXT,                           -- 'CLAUDE_API' | user
    error_message       TEXT,
    created_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ------------------------------------------------------------
-- USERS & ROLES
-- ------------------------------------------------------------

CREATE TABLE IF NOT EXISTS users (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    username            TEXT NOT NULL UNIQUE,
    email               TEXT NOT NULL UNIQUE,
    role                TEXT NOT NULL,
    -- CEO | BU_DIRECTOR | LOGISTICS_ADMIN | LOGISTICS | READ_ONLY
    -- LOGISTICS_ADMIN = B: prices visible, no margins, invoicing + storage statements
    -- LOGISTICS = team: movements only, no prices
    password_hash       TEXT NOT NULL,
    is_active           INTEGER NOT NULL DEFAULT 1,
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ------------------------------------------------------------
-- AUDIT LOG (immutable append-only)
-- ------------------------------------------------------------

CREATE TABLE IF NOT EXISTS audit_log (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    table_name          TEXT NOT NULL,
    record_id           INTEGER NOT NULL,
    action              TEXT NOT NULL,                  -- INSERT | UPDATE | DELETE
    user_id             INTEGER REFERENCES users(id),
    changed_fields      TEXT,                           -- JSON diff
    old_values          TEXT,                           -- JSON snapshot
    new_values          TEXT,                           -- JSON snapshot
    created_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ------------------------------------------------------------
-- INDEXES
-- ------------------------------------------------------------

CREATE INDEX IF NOT EXISTS idx_containers_shipment    ON containers(shipment_id);
CREATE INDEX IF NOT EXISTS idx_containers_clearance   ON containers(customs_clearance_date);
CREATE INDEX IF NOT EXISTS idx_containers_storage     ON containers(storage_facility_id);
CREATE INDEX IF NOT EXISTS idx_shipments_oda          ON shipments(oda_id);
CREATE INDEX IF NOT EXISTS idx_sale_lots_sale         ON sale_lots(sale_id);
CREATE INDEX IF NOT EXISTS idx_sale_lots_shipment     ON sale_lots(shipment_id);
CREATE INDEX IF NOT EXISTS idx_movements_container    ON container_movements(container_id);
CREATE INDEX IF NOT EXISTS idx_movements_date         ON container_movements(movement_date);
CREATE INDEX IF NOT EXISTS idx_movements_status       ON container_movements(entry_status);
CREATE INDEX IF NOT EXISTS idx_sales_status           ON sales(status);
CREATE INDEX IF NOT EXISTS idx_sales_product          ON sales(product_id);
CREATE INDEX IF NOT EXISTS idx_sales_ddt_load         ON sales(ddt_load_date);
CREATE INDEX IF NOT EXISTS idx_storage_prov           ON storage_provisional(storage_facility_id, product_id, balance_date);
CREATE INDEX IF NOT EXISTS idx_audit_table_record     ON audit_log(table_name, record_id);

-- ------------------------------------------------------------
-- SALE PIPELINE VIEW
-- ------------------------------------------------------------
-- Full sale lifecycle visible to CEO / BU_DIRECTOR
-- Application layer strips price/margin columns for LOGISTICS role

CREATE VIEW IF NOT EXISTS v_sale_pipeline AS
SELECT
    s.id,
    s.sale_code,
    c.name                      AS customer,
    p.code                      AS product,
    s.incoterm,
    s.destination,
    sf.code                     AS storage_facility,
    s.status,
    -- MT progression
    s.provisional_mt,
    s.instructed_mt,
    s.actual_mt,
    -- Key dates
    s.provisional_date,
    s.instruction_date,
    s.ddt_load_date,
    s.ddt_delivery_date,
    s.ddt_number,
    s.invoice_number,
    s.invoice_date,
    -- Pricing (hidden from LOGISTICS role at application layer)
    s.price_eur_per_mt,
    s.price_usd_per_mt,
    s.exchange_rate,
    s.transport_to_customer_eur,
    s.transport_source,
    -- FIFO
    s.fifo_assigned
FROM sales s
JOIN customers c   ON c.id = s.customer_id
JOIN products p    ON p.id = s.product_id
LEFT JOIN storage_facilities sf ON sf.id = s.storage_facility_id
WHERE s.is_deleted = 0
ORDER BY s.provisional_date DESC;

-- ------------------------------------------------------------
-- B'S PENDING ITEMS VIEW
-- ------------------------------------------------------------
-- Open items queue for LOGISTICS_ADMIN (B):
-- movements awaiting cost completion + sales awaiting DDT or invoice

CREATE VIEW IF NOT EXISTS v_pending_items AS
-- Movements pending cost entry
SELECT
    'MOVEMENT'                  AS item_type,
    cm.id                       AS item_id,
    c.container_code            AS reference,
    cm.movement_type            AS sub_type,
    cm.movement_date            AS event_date,
    cm.entry_status             AS status,
    NULL                        AS sale_code
FROM container_movements cm
JOIN containers c ON c.id = cm.container_id
WHERE cm.entry_status = 'PENDING_COSTS'

UNION ALL

-- Sales awaiting DDT
SELECT
    'SALE_DDT'                  AS item_type,
    s.id                        AS item_id,
    s.sale_code                 AS reference,
    s.incoterm                  AS sub_type,
    s.instruction_date          AS event_date,
    s.status                    AS status,
    s.sale_code
FROM sales s
WHERE s.status = 'INSTRUCTED'
  AND s.is_deleted = 0

ORDER BY event_date ASC;

-- ------------------------------------------------------------
-- MARKET PRICE REFERENCE
-- ------------------------------------------------------------
-- Weekly/daily reference prices per product
-- Enables mark-to-market on open positions
-- Source: MANUAL for now; future: API feed

CREATE TABLE IF NOT EXISTS market_prices (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id          INTEGER NOT NULL REFERENCES products(id),
    price_date          TEXT NOT NULL,                  -- ISO date
    price_eur_per_mt    REAL NOT NULL,
    source              TEXT NOT NULL DEFAULT 'MANUAL', -- MANUAL | API
    notes               TEXT,
    entered_by          TEXT,
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(product_id, price_date)
);

CREATE INDEX IF NOT EXISTS idx_market_prices_product_date ON market_prices(product_id, price_date DESC);

-- MTM position view — requires latest market price per product
CREATE VIEW IF NOT EXISTS v_mtm_position AS
SELECT
    p.code                      AS product_code,
    mp.price_date               AS market_price_date,
    mp.price_eur_per_mt         AS market_price,
    -- Unsold MT
    COALESCE(SUM(
        CASE WHEN c.status IN ('IN_STORAGE','PARTIALLY_SOLD')
             THEN c.actual_mt ELSE 0 END
    ), 0)                       AS unsold_mt,
    -- Weighted avg landed cost (purchase + customs + transport to storage)
    CASE WHEN SUM(c.actual_mt) > 0 THEN
        (SUM(o.price_eur_per_mt * c.actual_mt)
       + SUM(COALESCE(c.customs_cost_eur, 0))
       + SUM(COALESCE(c.transport_to_storage_eur, 0)))
       / SUM(c.actual_mt)
    ELSE 0 END                  AS avg_landed_cost_per_mt,
    -- MTM values
    COALESCE(SUM(
        CASE WHEN c.status IN ('IN_STORAGE','PARTIALLY_SOLD')
             THEN c.actual_mt ELSE 0 END
    ), 0) * mp.price_eur_per_mt AS mtm_value_eur,
    -- Unrealized P&L = MTM value - carrying value
    COALESCE(SUM(
        CASE WHEN c.status IN ('IN_STORAGE','PARTIALLY_SOLD')
             THEN c.actual_mt ELSE 0 END
    ), 0) * (mp.price_eur_per_mt -
        CASE WHEN SUM(c.actual_mt) > 0 THEN
            (SUM(o.price_eur_per_mt * c.actual_mt)
           + SUM(COALESCE(c.customs_cost_eur, 0))
           + SUM(COALESCE(c.transport_to_storage_eur, 0)))
           / SUM(c.actual_mt)
        ELSE 0 END
    )                           AS unrealized_pnl_eur
FROM products p
LEFT JOIN odas o        ON o.product_id = p.id
LEFT JOIN shipments s   ON s.oda_id = o.id
LEFT JOIN containers c  ON c.shipment_id = s.id AND c.is_deleted = 0
LEFT JOIN (
    SELECT product_id, price_eur_per_mt, price_date
    FROM market_prices mp2
    WHERE mp2.price_date = (
        SELECT MAX(mp3.price_date) FROM market_prices mp3
        WHERE mp3.product_id = mp2.product_id
    )
) mp ON mp.product_id = p.id
WHERE p.is_active = 1
GROUP BY p.id, mp.price_eur_per_mt, mp.price_date;

-- ------------------------------------------------------------
-- END OF SCHEMA
-- ------------------------------------------------------------
