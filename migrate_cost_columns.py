"""
Migration: add commission_eur_per_mt, log_in_eur_per_mt, stoccaggio_eur_per_mt
to sale_lots table.

Run once:
    python3 migrate_cost_columns.py

Safe to run multiple times — uses ALTER TABLE IF NOT EXISTS pattern via try/except.
"""

import sqlite3, os

DB_PATH = os.environ.get("DB_PATH", os.path.join(os.path.dirname(__file__), "db", "bu_control.db"))

COLUMNS = [
    ("commission_eur_per_mt",  "REAL"),
    ("log_in_eur_per_mt",      "REAL"),
    ("stoccaggio_eur_per_mt",  "REAL"),
]

def migrate():
    db = sqlite3.connect(DB_PATH)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA foreign_keys=ON")

    # Get existing columns
    existing = {row[1] for row in db.execute("PRAGMA table_info(sale_lots)")}

    added = []
    for col, col_type in COLUMNS:
        if col not in existing:
            db.execute(f"ALTER TABLE sale_lots ADD COLUMN {col} {col_type}")
            added.append(col)
            print(f"  + Added column: sale_lots.{col}")
        else:
            print(f"  ✓ Already exists: sale_lots.{col}")

    db.commit()
    db.close()

    if added:
        print(f"\n✅ Migration complete — {len(added)} column(s) added.")
    else:
        print("\n✅ Nothing to do — all columns already present.")

if __name__ == "__main__":
    print(f"Migrating: {DB_PATH}\n")
    migrate()
