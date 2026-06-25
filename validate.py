"""
validate.py  -  Phase 0 data validation for bu_control.db
Chimitrade S.p.A. / BPL / BU Control  v1.0

Runs a series of checks across all imported data and prints a report.
Exit code 0 = all checks passed, 1 = warnings found, 2 = errors found.

Checks:
  V01  ODAs with no supplier
  V02  ODAs with no price
  V03  ODAs with no order_date
  V04  Shipments with no ODA
  V05  Containers with no shipment
  V06  Containers with no storage facility
  V07  Containers with NULL actual_mt
  V08  Duplicate container codes
  V09  Sales with no customer
  V10  Sales with no product
  V11  Sales with no storage facility
  V12  Sales with no date
  V13  Sales with price = 0
  V14  MT balance per product/facility (inbound vs outbound)
  V15  ODAs with duplicate codes across sources
  V16  Customers appearing also as suppliers (fill purchase candidates)
  V17  Containers linked to non-existent shipments
  V18  Sales with actual_mt > 0 but fifo_assigned = 0

Usage:
    python3 validate.py [--db PATH] [--fix] [--product NAME] [--verbose]

Flags:
    --fix       Auto-fix safe issues (remove fake suppliers, null dates)
    --product   Restrict MT balance check to one product name
    --verbose   Show all rows, not just summary counts
"""

import argparse
import sqlite3
import sys
from pathlib import Path
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

PASS    = "PASS"
WARN    = "WARN"
ERROR   = "ERROR"

@dataclass
class CheckResult:
    code: str
    level: str          # PASS | WARN | ERROR
    message: str
    rows: list = field(default_factory=list)
    fix_applied: bool = False


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------

def v01_odas_no_supplier(db, verbose) -> CheckResult:
    rows = db.execute("""
        SELECT o.oda_code, o.order_date
        FROM odas o
        WHERE o.supplier_id IS NULL AND o.is_deleted=0
    """).fetchall()
    if not rows:
        return CheckResult("V01", PASS, "All ODAs have a supplier")
    msg = f"{len(rows)} ODA(s) have no supplier"
    return CheckResult("V01", WARN, msg,
                       rows=[(r[0], r[1]) for r in rows])


def v02_odas_no_price(db, verbose) -> CheckResult:
    rows = db.execute("""
        SELECT oda_code, order_date, currency
        FROM odas
        WHERE (price_eur_per_mt IS NULL OR price_eur_per_mt = 0)
          AND is_deleted=0
    """).fetchall()
    if not rows:
        return CheckResult("V02", PASS, "All ODAs have a price")
    msg = f"{len(rows)} ODA(s) have no or zero price"
    return CheckResult("V02", WARN, msg,
                       rows=[(r[0], r[1], r[2]) for r in rows])


def v03_odas_no_date(db, verbose) -> CheckResult:
    rows = db.execute("""
        SELECT oda_code FROM odas
        WHERE (order_date IS NULL OR order_date='1900-01-01')
          AND is_deleted=0
    """).fetchall()
    if not rows:
        return CheckResult("V03", PASS, "All ODAs have a valid order date")
    msg = f"{len(rows)} ODA(s) have no or placeholder order date"
    return CheckResult("V03", WARN, msg, rows=[r[0] for r in rows])


def v04_shipments_no_oda(db, verbose) -> CheckResult:
    rows = db.execute("""
        SELECT s.id, s.shipment_number
        FROM shipments s
        LEFT JOIN odas o ON o.id=s.oda_id
        WHERE o.id IS NULL AND s.is_deleted=0
    """).fetchall()
    if not rows:
        return CheckResult("V04", PASS, "All shipments linked to an ODA")
    return CheckResult("V04", ERROR, f"{len(rows)} orphaned shipments",
                       rows=[(r[0], r[1]) for r in rows])


def v05_containers_no_shipment(db, verbose) -> CheckResult:
    rows = db.execute("""
        SELECT c.container_code
        FROM containers c
        LEFT JOIN shipments s ON s.id=c.shipment_id
        WHERE s.id IS NULL AND c.is_deleted=0
    """).fetchall()
    if not rows:
        return CheckResult("V05", PASS, "All containers linked to a shipment")
    return CheckResult("V05", ERROR, f"{len(rows)} containers with no shipment",
                       rows=[r[0] for r in rows])


def v06_containers_no_facility(db, verbose) -> CheckResult:
    rows = db.execute("""
        SELECT container_code, status
        FROM containers
        WHERE storage_facility_id IS NULL
          AND status NOT IN ('IN_TRANSIT','SCRAPPED')
          AND is_deleted=0
    """).fetchall()
    if not rows:
        return CheckResult("V06", PASS, "All in-storage containers have a facility")
    msg = f"{len(rows)} containers missing storage facility"
    return CheckResult("V06", WARN, msg,
                       rows=[(r[0], r[1]) for r in rows])


def v07_containers_no_mt(db, verbose) -> CheckResult:
    rows = db.execute("""
        SELECT container_code FROM containers
        WHERE actual_mt IS NULL AND is_deleted=0
    """).fetchall()
    if not rows:
        return CheckResult("V07", PASS, "All containers have actual_mt")
    msg = f"{len(rows)} containers with NULL actual_mt"
    return CheckResult("V07", WARN, msg, rows=[r[0] for r in rows])


def v08_duplicate_containers(db, verbose) -> CheckResult:
    rows = db.execute("""
        SELECT container_code, COUNT(*) as n
        FROM containers
        WHERE is_deleted=0
        GROUP BY container_code
        HAVING n > 1
    """).fetchall()
    if not rows:
        return CheckResult("V08", PASS, "No duplicate container codes")
    return CheckResult("V08", ERROR,
                       f"{len(rows)} duplicate container codes",
                       rows=[(r[0], r[1]) for r in rows])


def v09_sales_no_customer(db, verbose) -> CheckResult:
    rows = db.execute("""
        SELECT sale_code FROM sales
        WHERE customer_id IS NULL AND is_deleted=0
    """).fetchall()
    if not rows:
        return CheckResult("V09", PASS, "All sales have a customer")
    return CheckResult("V09", ERROR, f"{len(rows)} sales with no customer",
                       rows=[r[0] for r in rows])


def v10_sales_no_product(db, verbose) -> CheckResult:
    rows = db.execute("""
        SELECT sale_code FROM sales
        WHERE product_id IS NULL AND is_deleted=0
    """).fetchall()
    if not rows:
        return CheckResult("V10", PASS, "All sales have a product")
    return CheckResult("V10", ERROR, f"{len(rows)} sales with no product",
                       rows=[r[0] for r in rows])


def v11_sales_no_facility(db, verbose) -> CheckResult:
    rows = db.execute("""
        SELECT sale_code, status FROM sales
        WHERE storage_facility_id IS NULL AND is_deleted=0
    """).fetchall()
    if not rows:
        return CheckResult("V11", PASS, "All sales have a storage facility")
    msg = f"{len(rows)} sales with no storage facility"
    return CheckResult("V11", WARN, msg,
                       rows=[(r[0], r[1]) for r in rows])


def v12_sales_no_date(db, verbose) -> CheckResult:
    rows = db.execute("""
        SELECT sale_code FROM sales
        WHERE (provisional_date IS NULL OR provisional_date='1900-01-01')
          AND is_deleted=0
    """).fetchall()
    if not rows:
        return CheckResult("V12", PASS, "All sales have a date")
    msg = f"{len(rows)} sales with no or placeholder date"
    return CheckResult("V12", WARN, msg, rows=[r[0] for r in rows])


def v13_sales_zero_price(db, verbose) -> CheckResult:
    rows = db.execute("""
        SELECT sale_code, p.name, c.name
        FROM sales s
        JOIN products p ON p.id=s.product_id
        JOIN customers c ON c.id=s.customer_id
        WHERE (s.price_eur_per_mt IS NULL OR s.price_eur_per_mt=0)
          AND s.is_deleted=0
    """).fetchall()
    if not rows:
        return CheckResult("V13", PASS, "All sales have a price")
    msg = f"{len(rows)} sales with zero or missing price"
    return CheckResult("V13", WARN, msg,
                       rows=[(r[0], r[1], r[2]) for r in rows])


def v14_mt_balance(db, verbose, product_filter=None) -> CheckResult:
    """
    Per product + facility: total inbound MT (containers) vs total outbound MT (sales).
    Outbound should never exceed inbound. Flags negative balance.
    """
    prod_clause = "AND p.name=?" if product_filter else ""
    params = (product_filter,) if product_filter else ()

    inbound = db.execute(f"""
        SELECT p.name, sf.name, ROUND(SUM(c.actual_mt),2) as total_in
        FROM containers c
        JOIN shipments s ON s.id=c.shipment_id
        JOIN odas o ON o.id=s.oda_id
        JOIN products p ON p.id=o.product_id
        LEFT JOIN storage_facilities sf ON sf.id=c.storage_facility_id
        WHERE c.is_deleted=0 {prod_clause}
        GROUP BY p.name, sf.name
    """, params).fetchall()

    outbound = db.execute(f"""
        SELECT p.name, sf.name, ROUND(SUM(s.actual_mt),2) as total_out
        FROM sales s
        JOIN products p ON p.id=s.product_id
        LEFT JOIN storage_facilities sf ON sf.id=s.storage_facility_id
        WHERE s.is_deleted=0 {prod_clause}
        GROUP BY p.name, sf.name
    """, params).fetchall()

    in_map  = {(r[0], r[1]): r[2] for r in inbound}
    out_map = {(r[0], r[1]): r[2] for r in outbound}

    all_keys = set(in_map) | set(out_map)
    issues = []
    details = []

    for key in sorted(all_keys):
        total_in  = in_map.get(key, 0) or 0
        total_out = out_map.get(key, 0) or 0
        balance   = round(total_in - total_out, 2)
        details.append((key[0], key[1], total_in, total_out, balance))
        if balance < 0:
            issues.append((key[0], key[1], total_in, total_out, balance))

    if not issues:
        return CheckResult("V14", PASS,
                           f"MT balance OK across {len(all_keys)} product/facility combinations",
                           rows=details)
    msg = f"{len(issues)} product/facility combination(s) have negative MT balance (sold > received)"
    return CheckResult("V14", ERROR, msg, rows=details)


def v15_duplicate_oda_codes(db, verbose) -> CheckResult:
    rows = db.execute("""
        SELECT oda_code, COUNT(*) as n
        FROM odas WHERE is_deleted=0
        GROUP BY oda_code HAVING n > 1
    """).fetchall()
    if not rows:
        return CheckResult("V15", PASS, "No duplicate ODA codes")
    return CheckResult("V15", ERROR,
                       f"{len(rows)} duplicate ODA codes",
                       rows=[(r[0], r[1]) for r in rows])


def v16_customer_supplier_overlap(db, verbose) -> CheckResult:
    """Names appearing in both customers and suppliers — fill purchase candidates."""
    rows = db.execute("""
        SELECT c.name FROM customers c
        JOIN suppliers s ON LOWER(s.name)=LOWER(c.name)
        ORDER BY c.name
    """).fetchall()
    if not rows:
        return CheckResult("V16", PASS, "No customer/supplier name overlaps")
    msg = f"{len(rows)} names appear as both customer and supplier (fill purchase candidates)"
    return CheckResult("V16", WARN, msg, rows=[r[0] for r in rows])


def v17_containers_orphaned_shipment(db, verbose) -> CheckResult:
    rows = db.execute("""
        SELECT c.container_code, c.shipment_id
        FROM containers c
        WHERE c.shipment_id IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM shipments s WHERE s.id=c.shipment_id)
          AND c.is_deleted=0
    """).fetchall()
    if not rows:
        return CheckResult("V17", PASS, "All container shipment references are valid")
    return CheckResult("V17", ERROR,
                       f"{len(rows)} containers reference non-existent shipments",
                       rows=[(r[0], r[1]) for r in rows])


def v18_sales_fifo_pending(db, verbose) -> CheckResult:
    rows = db.execute("""
        SELECT sale_code, p.name, actual_mt
        FROM sales s
        JOIN products p ON p.id=s.product_id
        WHERE s.fifo_assigned=0
          AND s.actual_mt > 0
          AND s.status IN ('INVOICED','EXECUTED')
          AND s.is_deleted=0
        ORDER BY s.provisional_date
    """).fetchall()
    if not rows:
        return CheckResult("V18", PASS, "All executed/invoiced sales have FIFO assigned")
    msg = f"{len(rows)} invoiced sales pending FIFO lot assignment"
    return CheckResult("V18", WARN, msg,
                       rows=[(r[0], r[1], r[2]) for r in rows])


# ---------------------------------------------------------------------------
# Auto-fix
# ---------------------------------------------------------------------------

def apply_fixes(db, results: list[CheckResult]) -> int:
    fixed = 0

    # Fix V03: replace placeholder dates with NULL
    v03 = next((r for r in results if r.code=="V03"), None)
    if v03 and v03.rows:
        db.execute("""
            UPDATE odas SET order_date=NULL
            WHERE order_date='1900-01-01'
        """)
        n = db.execute("SELECT changes()").fetchone()[0]
        if n:
            print(f"  FIX V03: cleared {n} placeholder order_date values")
            fixed += n

    # Fix V12: replace placeholder sale dates with NULL
    v12 = next((r for r in results if r.code=="V12"), None)
    if v12 and v12.rows:
        db.execute("""
            UPDATE sales SET provisional_date=NULL
            WHERE provisional_date='1900-01-01'
        """)
        n = db.execute("SELECT changes()").fetchone()[0]
        if n:
            print(f"  FIX V12: cleared {n} placeholder provisional_date values")
            fixed += n

    db.commit()
    return fixed


# ---------------------------------------------------------------------------
# Report printer
# ---------------------------------------------------------------------------

def print_report(results: list[CheckResult], verbose: bool):
    errors = [r for r in results if r.level == ERROR]
    warns  = [r for r in results if r.level == WARN]
    passes = [r for r in results if r.level == PASS]

    print(f"\n{'='*65}")
    print(f"  VALIDATION REPORT")
    print(f"  PASS: {len(passes)}   WARN: {len(warns)}   ERROR: {len(errors)}")
    print(f"{'='*65}")

    for r in results:
        icon = "✓" if r.level==PASS else ("⚠" if r.level==WARN else "✗")
        fix  = " [FIXED]" if r.fix_applied else ""
        print(f"\n  {icon} {r.code}  [{r.level}]  {r.message}{fix}")

        if r.level == PASS and r.code == "V14" and verbose:
            # Always show MT balance detail when verbose
            print(f"    {'Product':<20} {'Facility':<20} {'In MT':>10} {'Out MT':>10} {'Balance':>10}")
            print(f"    {'-'*64}")
            for row in r.rows:
                print(f"    {str(row[0] or '?'):<20} {str(row[1] or '?'):<20} "
                      f"{row[2]:>10.2f} {row[3]:>10.2f} {row[4]:>10.2f}")

        elif r.level in (WARN, ERROR) and r.rows:
            if r.code == "V14":
                print(f"    {'Product':<20} {'Facility':<20} {'In MT':>10} {'Out MT':>10} {'Balance':>10}")
                print(f"    {'-'*64}")
                for row in r.rows:
                    flag = " ← NEGATIVE" if row[4] < 0 else ""
                    print(f"    {str(row[0] or '?'):<20} {str(row[1] or '?'):<20} "
                          f"{row[2]:>10.2f} {row[3]:>10.2f} {row[4]:>10.2f}{flag}")
            else:
                show = r.rows if verbose else r.rows[:10]
                for row in show:
                    print(f"    {row}")
                if not verbose and len(r.rows) > 10:
                    print(f"    ... and {len(r.rows)-10} more (use --verbose to see all)")

    print(f"\n{'='*65}")
    if errors:
        print(f"  ACTION REQUIRED: {len(errors)} error(s) need attention")
    elif warns:
        print(f"  {len(warns)} warning(s) — review recommended")
    else:
        print(f"  All checks passed ✓")
    print(f"{'='*65}\n")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Validate bu_control.db")
    ap.add_argument("--db",      default=None)
    ap.add_argument("--fix",     action="store_true",
                    help="Auto-fix safe issues")
    ap.add_argument("--product", default=None,
                    help="Restrict MT balance check to one product name")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    script_dir = Path(__file__).parent
    db_path = Path(args.db) if args.db else script_dir / "db" / "bu_control.db"

    if not db_path.exists():
        print(f"ERROR: DB not found: {db_path}", file=sys.stderr)
        sys.exit(1)

    db = sqlite3.connect(str(db_path))
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")

    print(f"Validating: {db_path}")

    results = [
        v01_odas_no_supplier(db, args.verbose),
        v02_odas_no_price(db, args.verbose),
        v03_odas_no_date(db, args.verbose),
        v04_shipments_no_oda(db, args.verbose),
        v05_containers_no_shipment(db, args.verbose),
        v06_containers_no_facility(db, args.verbose),
        v07_containers_no_mt(db, args.verbose),
        v08_duplicate_containers(db, args.verbose),
        v09_sales_no_customer(db, args.verbose),
        v10_sales_no_product(db, args.verbose),
        v11_sales_no_facility(db, args.verbose),
        v12_sales_no_date(db, args.verbose),
        v13_sales_zero_price(db, args.verbose),
        v14_mt_balance(db, args.verbose, args.product),
        v15_duplicate_oda_codes(db, args.verbose),
        v16_customer_supplier_overlap(db, args.verbose),
        v17_containers_orphaned_shipment(db, args.verbose),
        v18_sales_fifo_pending(db, args.verbose),
    ]

    if args.fix:
        print("\nApplying auto-fixes...")
        n = apply_fixes(db, results)
        print(f"  {n} values fixed")
        # Re-run affected checks
        results[2]  = v03_odas_no_date(db, args.verbose)
        results[11] = v12_sales_no_date(db, args.verbose)

    print_report(results, args.verbose)

    db.close()

    errors = [r for r in results if r.level == ERROR]
    warns  = [r for r in results if r.level == WARN]
    sys.exit(2 if errors else (1 if warns else 0))


if __name__ == "__main__":
    main()
