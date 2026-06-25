# BU Control Tool

Italian trading company — chemicals/biofuels distribution
Flask + SQLite operational management system.

## Setup

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Place schema file alongside the app
#    bu_control_schema_v1.2.sql must be in the parent directory of bu_control/

# 3. Run (first run creates DB and seeds demo users)
python app.py
```

Open http://localhost:5000

## Demo users (password: demo1234)

| Username  | Role            | Access |
|-----------|-----------------|--------|
| ceo       | CEO             | Full   |
| li        | BU Director     | Full commercial |
| b         | Logistics Admin | Ops + prices, no margins |
| g         | Logistics       | Container schedule, movements |
| logistics | Logistics       | Movements only |

## DB location

Default: `./db/bu_control.db`
Override: set `DB_PATH` environment variable.

## Production (Hetzner)

```bash
# Install gunicorn
pip install gunicorn

# Run behind Nginx
gunicorn -w 2 -b 127.0.0.1:5000 app:app

# Set production secret key
export SECRET_KEY="your-long-random-secret"
export DB_PATH="/var/data/bu_control.db"
```

## Backup

```bash
# Cron job — daily SQLite backup to Dropbox
0 2 * * * cp /var/data/bu_control.db ~/Dropbox/backups/bu_control_$(date +\%Y\%m\%d).db
```
