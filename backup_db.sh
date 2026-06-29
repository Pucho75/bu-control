#!/bin/bash
BACKUP_DIR="/opt/bu_control/backups"
DB_PATH="/opt/bu_control/db/bu_control.db"
DATE=$(date +%Y%m%d_%H%M%S)
mkdir -p $BACKUP_DIR
# SQLite safe backup using .backup command
sqlite3 $DB_PATH ".backup $BACKUP_DIR/bu_control_$DATE.db"
# Keep only last 30 backups
ls -t $BACKUP_DIR/bu_control_*.db | tail -n +31 | xargs -r rm
echo "Backup completed: bu_control_$DATE.db"
