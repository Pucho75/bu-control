"""
BU Control — Shared cost lookup utilities

get_delivery_costs(db, product_id, supplier_id, paese, ref_date)
    Returns the best-matching delivery_costs row for the given lane and date.
    Specificity priority: product+supplier+paese > product+supplier > product+paese > product only
    Within each level, takes most recent valid_from <= ref_date.
    Returns a dict with all cost fields, or None if no match.
"""


def get_delivery_costs(db, product_id, supplier_id, paese, ref_date):
    """
    Look up the best delivery_costs row for a given lane and reference date.

    Args:
        db: SQLite connection (with row_factory = sqlite3.Row)
        product_id: int or None
        supplier_id: int or None
        paese: str or None (country code)
        ref_date: str ISO date (e.g. ODA order_date for ODA-anchored costs)

    Returns:
        dict with keys: log_in, commission, porto, dazio, dazio_type,
                        log_ita, stoccaggio
        or None if no row found.
    """
    if not ref_date:
        ref_date = "9999-12-31"

    row = db.execute("""
        SELECT
            dc.log_in,
            dc.log_in_currency,
            dc.commission,
            dc.commission_currency,
            dc.porto,
            dc.dazio,
            dc.dazio_type,
            dc.log_ita,
            dc.stoccaggio
        FROM delivery_costs dc
        WHERE (dc.product_id  = ? OR dc.product_id  IS NULL)
          AND (dc.supplier_id = ? OR dc.supplier_id IS NULL)
          AND (dc.paese       = ? OR dc.paese       IS NULL)
          AND dc.valid_from <= ?
        ORDER BY
            (CASE WHEN dc.product_id  IS NOT NULL THEN 4 ELSE 0 END +
             CASE WHEN dc.supplier_id IS NOT NULL THEN 2 ELSE 0 END +
             CASE WHEN dc.paese       IS NOT NULL THEN 1 ELSE 0 END) DESC,
            dc.valid_from DESC
        LIMIT 1
    """, (product_id, supplier_id, paese, ref_date)).fetchone()

    if not row:
        return None

    return dict(row)


def compute_dazio_eur_per_mt(dc_row, purchase_price_eur_per_mt):
    """
    Compute duty in €/MT from a delivery_costs row.
    dazio_type PCT  → dazio% × purchase_price
    dazio_type EUR_MT → dazio as flat €/MT
    """
    if not dc_row or dc_row.get("dazio") is None:
        return 0.0
    dazio = dc_row["dazio"]
    dazio_type = dc_row.get("dazio_type", "PCT")
    if dazio_type == "PCT":
        return round((dazio / 100.0) * (purchase_price_eur_per_mt or 0), 4)
    else:  # EUR_MT
        return round(dazio, 4)


def fetch_ecb_rate(date_str):
    """Fetch EUR/USD rate from ECB for a given date (YYYY-MM-DD).
    Returns float rate or None if not available (weekend/holiday).
    Falls back to nearest previous business day if no rate found."""
    import urllib.request, csv, io, datetime

    def _fetch(d):
        url = (f"https://data-api.ecb.europa.eu/service/data/EXR/"
               f"D.USD.EUR.SP00.A?startPeriod={d}&endPeriod={d}&format=csvdata")
        try:
            with urllib.request.urlopen(url, timeout=5) as r:
                text = r.read().decode()
            reader = csv.DictReader(io.StringIO(text))
            for row in reader:
                if row.get("OBS_VALUE"):
                    return float(row["OBS_VALUE"])
        except:
            pass
        return None

    # Try requested date, then go back up to 5 days for weekends/holidays
    dt = datetime.date.fromisoformat(date_str)
    for i in range(5):
        rate = _fetch((dt - datetime.timedelta(days=i)).isoformat())
        if rate:
            return rate
    return None
