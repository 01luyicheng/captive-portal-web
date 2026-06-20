#!/usr/bin/env bash
set -euo pipefail

# Synchronize authorized clients from the captive portal SQLite database into
# the ipsets "paid_ips" and "grace_ips" used by iptables to authorize internet
# access and to track per-client quota consumption.
#
# Run this as root or as a user with CAP_NET_ADMIN (e.g. the systemd service
# runs as user captive-portal with AmbientCapabilities=CAP_NET_ADMIN).

DB_PATH="${CAPTIVE_DB:-/var/lib/captive-portal/payments.db}"
SYNC_INTERVAL="${SYNC_INTERVAL:-5}"

# Parameterized DB helper: mark a client as expired only if it is still in the
# expected status. This avoids SQL injection and accidental status overwrites.
_expire_client_status() {
    local db_path="$1" client_ip="$2" old_status="$3"
    python3 -c "import sqlite3,sys; conn=sqlite3.connect(sys.argv[1]); conn.execute('PRAGMA busy_timeout=5000'); conn.execute('''UPDATE clients SET status='expired' WHERE client_ip=? AND status=?''', (sys.argv[2], sys.argv[3])); conn.commit(); conn.close()" "$db_path" "$client_ip" "$old_status"
}

# CAP_NET_ADMIN is required for ipset operations. Root implicitly has it;
# the systemd service grants it via AmbientCapabilities.
if ! ipset list >/dev/null 2>&1; then
    echo "This script requires root privileges or the CAP_NET_ADMIN capability." >&2
    exit 1
fi

# Ensure the ipsets exist.
ipset create paid_ips hash:ip counters timeout 0 2>/dev/null || true
ipset create grace_ips hash:ip counters timeout 0 2>/dev/null || true

# Parse an ipset list output into "<ip> <bytes>" lines.
_parse_ipset() {
    local set_name="$1"
    ipset list "${set_name}" 2>/dev/null | awk '
        /^Members:/ { in_members=1; next }
        in_members && NF >= 4 {
            ip=$1
            bytes=0
            for (i=2; i<=NF; i++) {
                if ($i == "bytes") {
                    bytes=$(i+1)
                    break
                }
            }
            if (ip ~ /^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$/) {
                print ip, bytes
            }
        }
    '
}

sync_once() {
    if [ ! -r "${DB_PATH}" ]; then
        return
    fi

    # Ensure the DB schema includes the counter reset coordination column.
    sqlite3 "${DB_PATH}" "ALTER TABLE clients ADD COLUMN reset_ipset_at INTEGER NOT NULL DEFAULT 0" 2>/dev/null || true

    local now
    now=$(date +%s)

    # ------------------------------------------------------------------
    # 0) Process pending ipset counter resets requested by the portal.
    #    Removing and re-adding an IP zeros its counter, avoiding a stale
    #    delta when last_ipset_bytes is reset.
    # ------------------------------------------------------------------
    local client_ip status grace_until paid_until quota_bytes used_bytes reset_ipset_at target
    while IFS='|' read -r client_ip status grace_until paid_until quota_bytes used_bytes reset_ipset_at; do
        [ -n "${client_ip}" ] || continue
        [ "${reset_ipset_at:-0}" -gt 0 ] || continue

        target=""
        if [ "${status}" = "paid" ] && [ "${paid_until}" -gt "${now}" ] && [ "${used_bytes}" -lt "${quota_bytes}" ]; then
            target="paid_ips"
        elif [ "${status}" = "grace" ] && [ "${grace_until}" -gt "${now}" ] && [ "${used_bytes}" -lt "${quota_bytes}" ]; then
            target="grace_ips"
        fi

        ipset del paid_ips "${client_ip}" 2>/dev/null || true
        ipset del grace_ips "${client_ip}" 2>/dev/null || true
        if [ -n "${target}" ]; then
            ipset add "${target}" "${client_ip}" 2>/dev/null || true
        fi

        python3 -c "import sqlite3,sys; conn=sqlite3.connect(sys.argv[1]); conn.execute('PRAGMA busy_timeout=5000'); conn.execute('UPDATE clients SET last_ipset_bytes=0, reset_ipset_at=0 WHERE client_ip=?', (sys.argv[2],)); conn.commit(); conn.close()" "${DB_PATH}" "${client_ip}"
    done < <(sqlite3 "${DB_PATH}" "SELECT client_ip, status, IFNULL(grace_until,0), IFNULL(paid_until,0), IFNULL(quota_bytes,0), IFNULL(used_bytes,0), IFNULL(reset_ipset_at,0) FROM clients;" 2>/dev/null)

    # ------------------------------------------------------------------
    # 1) Read counters from both ipsets and accumulate into clients.used_bytes.
    # If an IP exists in both sets, prefer paid_ips and ignore grace_ips so
    # the same traffic is not counted twice.
    # ------------------------------------------------------------------
    declare -A ipset_bytes
    local ip bytes
    while read -r ip bytes; do
        [ -n "${ip}" ] || continue
        [ -n "${ipset_bytes[${ip}]:-}" ] && continue
        ipset_bytes["${ip}"]="${bytes}"
    done < <(_parse_ipset paid_ips; _parse_ipset grace_ips)

    # Update counters using a parameterized Python script to avoid SQL injection.
    for ip in "${!ipset_bytes[@]}"; do
        bytes=${ipset_bytes[$ip]}
        python3 -c "import sqlite3,sys; conn=sqlite3.connect(sys.argv[1]); conn.execute('PRAGMA busy_timeout=5000'); conn.execute('UPDATE clients SET used_bytes=used_bytes+MAX(0,?-last_ipset_bytes), last_ipset_bytes=? WHERE client_ip=?', (sys.argv[2], sys.argv[2], sys.argv[3])); conn.commit(); conn.close()" "${DB_PATH}" "${bytes}" "${ip}"
    done

    # ------------------------------------------------------------------
    # 2) Rebuild ipset membership incrementally based on the clients table.
    # This avoids flushing the sets (which would drop counters and briefly
    # disconnect active clients).
    # ------------------------------------------------------------------
    local client_ip status grace_until paid_until quota_bytes used_bytes reset_ipset_at
    local ok target in_paid in_grace

    while IFS='|' read -r client_ip status grace_until paid_until quota_bytes used_bytes reset_ipset_at; do
        [ -n "${client_ip}" ] || continue

        ok=0
        target=""
        if [ "${status}" = "paid" ] && [ "${paid_until}" -gt "${now}" ] && [ "${used_bytes}" -lt "${quota_bytes}" ]; then
            ok=1
            target="paid_ips"
        elif [ "${status}" = "grace" ] && [ "${grace_until}" -gt "${now}" ] && [ "${used_bytes}" -lt "${quota_bytes}" ]; then
            ok=1
            target="grace_ips"
        fi

        in_paid=0
        in_grace=0
        ipset test paid_ips "${client_ip}" >/dev/null 2>&1 && in_paid=1
        ipset test grace_ips "${client_ip}" >/dev/null 2>&1 && in_grace=1

        if [ "${ok}" = "1" ]; then
            if [ "${target}" = "paid_ips" ]; then
                if [ "${in_paid}" != "1" ]; then
                    ipset add paid_ips "${client_ip}" 2>/dev/null || true
                fi
                if [ "${in_grace}" = "1" ]; then
                    ipset del grace_ips "${client_ip}" 2>/dev/null || true
                fi
            else
                if [ "${in_grace}" != "1" ]; then
                    ipset add grace_ips "${client_ip}" 2>/dev/null || true
                fi
                if [ "${in_paid}" = "1" ]; then
                    ipset del paid_ips "${client_ip}" 2>/dev/null || true
                fi
            fi
        else
            if [ "${in_paid}" = "1" ]; then
                ipset del paid_ips "${client_ip}" 2>/dev/null || true
            fi
            if [ "${in_grace}" = "1" ]; then
                ipset del grace_ips "${client_ip}" 2>/dev/null || true
            fi
            if [ "${status}" = "paid" ] || [ "${status}" = "grace" ]; then
                _expire_client_status "${DB_PATH}" "${client_ip}" "${status}" 2>/dev/null || true
            fi
        fi
    done < <(sqlite3 "${DB_PATH}" "SELECT client_ip, status, IFNULL(grace_until,0), IFNULL(paid_until,0), IFNULL(quota_bytes,0), IFNULL(used_bytes,0), IFNULL(reset_ipset_at,0) FROM clients;" 2>/dev/null)
}

if [ "${1:-}" = "--daemon" ]; then
    _shutdown=0
    trap '_shutdown=1' TERM INT
    SYNC_COUNT=0
    while [ "${_shutdown}" -eq 0 ]; do
        sync_once
        SYNC_COUNT=$((SYNC_COUNT + 1))
        if [ $((SYNC_COUNT % 30)) -eq 0 ]; then
            sqlite3 "${DB_PATH}" "PRAGMA wal_checkpoint(PASSIVE)" 2>/dev/null || true
        fi
        if [ $((SYNC_COUNT % 300)) -eq 0 ]; then
            NOW=$(date +%s)
            sqlite3 "${DB_PATH}" "DELETE FROM pending_payments WHERE expires_at < $((NOW - 604800))" 2>/dev/null || true
            sqlite3 "${DB_PATH}" "DELETE FROM payments WHERE expires_at < $((NOW - 7776000))" 2>/dev/null || true
        fi
        sleep "${SYNC_INTERVAL}"
    done
else
    sync_once
fi
