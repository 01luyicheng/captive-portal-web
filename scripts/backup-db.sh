#!/usr/bin/env bash
set -euo pipefail

# Backup the captive portal SQLite database.
# Usage: backup-db.sh [backup_dir]
# Default backup dir: /var/lib/captive-portal/backups

DB_PATH="${CAPTIVE_DB:-/var/lib/captive-portal/payments.db}"
BACKUP_DIR="${1:-/var/lib/captive-portal/backups}"
RETENTION_DAYS=30

if [ ! -f "${DB_PATH}" ]; then
    echo "[ERROR] Database not found: ${DB_PATH}" >&2
    exit 1
fi

mkdir -p "${BACKUP_DIR}"

TIMESTAMP=$(date +%Y%m%d_%H%M%S)
BACKUP_FILE="${BACKUP_DIR}/payments-${TIMESTAMP}.db"

# Use SQLite .backup for a consistent snapshot even during writes.
sqlite3 "${DB_PATH}" ".backup '${BACKUP_FILE}'"

if [ -f "${BACKUP_FILE}" ]; then
    echo "[OK] Backup created: ${BACKUP_FILE} ($(du -h "${BACKUP_FILE}" | cut -f1))"
else
    echo "[ERROR] Backup failed" >&2
    exit 1
fi

# Remove backups older than RETENTION_DAYS.
find "${BACKUP_DIR}" -name "payments-*.db" -mtime +${RETENTION_DAYS} -delete 2>/dev/null || true

echo "[OK] Cleanup: removed backups older than ${RETENTION_DAYS} days"
