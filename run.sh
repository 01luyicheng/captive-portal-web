#!/usr/bin/env bash
set -euo pipefail

# Default: production-like mode (simulate-payment disabled, gunicorn server).
# Enable dev/test mode by passing --dev or setting CAPTIVE_PORTAL_DEV=true.
# NOTE: CAPTIVE_PORTAL_DEV=true requires CAPTIVE_DEV_TOKEN to be set; requests
# to /api/simulate-payment must include the matching X-Dev-Token header.
DEV_MODE="false"

for arg in "$@"; do
    case "$arg" in
        --dev)
            DEV_MODE="true"
            ;;
    esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Use a local virtual environment if available, otherwise rely on system Python.
if [ -d "$SCRIPT_DIR/.venv" ]; then
    source "$SCRIPT_DIR/.venv/bin/activate"
fi

export CAPTIVE_PORTAL_DEV="$DEV_MODE"
export FLASK_APP=app.py

if [ "$DEV_MODE" = "true" ]; then
    echo "Starting Captive Portal in DEV mode (Flask dev server, simulate-payment enabled) on http://127.0.0.1:5000"
    python3 app.py
else
    echo "Starting Captive Portal in PROD mode (gunicorn) on http://${PORTAL_IP:-10.88.0.1}:${PORTAL_PORT:-5000}"
    # -w 2 is suitable for low-traffic captive portals on small boards.
    # --preload loads the app in the master process before forking workers,
    # which, together with SQLite WAL mode, helps avoid multi-worker write conflicts.
    # Bind to localhost if you put Nginx/Caddy in front, or 0.0.0.0 if exposed directly.
    # CAPTIVE_PORTAL_DEV=true requires CAPTIVE_DEV_TOKEN; requests to /api/simulate-payment
    # must include the matching X-Dev-Token header.
    GUNICORN_WORKERS="${GUNICORN_WORKERS:-2}"
    exec gunicorn -w "${GUNICORN_WORKERS}" -b "${PORTAL_IP:-10.88.0.1}:${PORTAL_PORT:-5000}" --preload --access-logfile - --error-logfile - app:app
fi
