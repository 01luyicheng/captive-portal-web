#!/usr/bin/env bash
set -euo pipefail

# One-time setup for the Ethereum-paid Wi-Fi hotspot.
# Run as root (or with sudo).

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "${SCRIPT_DIR}")"

echo "[*] Installing system packages..."
apt-get update
apt-get install -y hostapd dnsmasq iw iptables ipset sqlite3 python3-venv python3-pip

echo "[*] Stopping conflicting services..."
systemctl stop hostapd 2>/dev/null || true
systemctl stop dnsmasq 2>/dev/null || true
systemctl mask hostapd 2>/dev/null || true
systemctl disable --now dnsmasq 2>/dev/null || true

echo "[*] Installing hostapd config..."
cp "${SCRIPT_DIR}/hostapd.conf" /etc/hostapd/eth-wifi.conf

echo "[*] Installing dnsmasq config..."
cp "${SCRIPT_DIR}/dnsmasq.conf" /etc/dnsmasq.d/eth-wifi.conf

echo "[*] Installing helper scripts..."
cp "${SCRIPT_DIR}/start-ap.sh" /usr/local/bin/eth-wifi-start
chmod +x /usr/local/bin/eth-wifi-start
cp "${SCRIPT_DIR}/stop-ap.sh" /usr/local/bin/eth-wifi-stop
chmod +x /usr/local/bin/eth-wifi-stop
cp "${SCRIPT_DIR}/sync-auth.sh" /usr/local/bin/eth-wifi-sync
chmod +x /usr/local/bin/eth-wifi-sync

echo "[*] Installing systemd services..."
cp "${SCRIPT_DIR}/captive-portal.service" /etc/systemd/system/
cp "${SCRIPT_DIR}/sync-auth.service" /etc/systemd/system/

echo "[*] Enabling hostapd instance service..."
if [ ! -f /lib/systemd/system/hostapd@.service ] && [ ! -f /etc/systemd/system/hostapd@.service ]; then
    cat >/etc/systemd/system/hostapd@.service <<'EOF'
[Unit]
Description=Advanced IEEE 802.11 AP daemon (%I)
After=network.target

[Service]
Type=forking
PIDFile=/run/hostapd-%I.pid
ExecStart=/usr/sbin/hostapd -B -P /run/hostapd-%I.pid /etc/hostapd/%I.conf
ExecStopPost=/bin/rm -f /run/hostapd-%I.pid
Restart=on-failure
RestartSec=2

[Install]
WantedBy=multi-user.target
EOF
fi

echo "[*] Creating dedicated user and data directory..."
if ! id -u captive-portal >/dev/null 2>&1; then
    useradd --system --no-create-home --shell /usr/sbin/nologin captive-portal
fi
mkdir -p /var/lib/captive-portal
chown captive-portal:captive-portal /var/lib/captive-portal
chmod 750 /var/lib/captive-portal
mkdir -p /etc/captive-portal
chown root:captive-portal /etc/captive-portal
chmod 750 /etc/captive-portal

# Environment file for secrets (e.g. CAPTIVE_HD_SEED).
if [ ! -f /etc/captive-portal/env ]; then
    touch /etc/captive-portal/env
fi
chown root:captive-portal /etc/captive-portal/env
chmod 640 /etc/captive-portal/env

# Keep project code owned by root and read-only for the service user.
chown -R root:root "${PROJECT_DIR}"
chmod -R o+rX "${PROJECT_DIR}"

# Ensure the database parent directory is writable by the portal user.
DB_PATH="${CAPTIVE_DB:-/var/lib/captive-portal/payments.db}"
DB_DIR="$(dirname "${DB_PATH}")"
mkdir -p "${DB_DIR}"
chown captive-portal:captive-portal "${DB_DIR}"

# Pre-create an empty database file so SQLite does not create it world-readable.
if [ ! -e "${DB_PATH}" ]; then
    touch "${DB_PATH}"
    chown captive-portal:captive-portal "${DB_PATH}"
    chmod 640 "${DB_PATH}"
fi

echo "[*] Setting up Python virtual environment for portal..."
cd "${PROJECT_DIR}"
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Generate a master seed if one has not been configured.
if ! grep -qE '^CAPTIVE_HD_SEED="[^"]+"' /etc/captive-portal/env; then
    SEED=$("${PROJECT_DIR}/.venv/bin/python3" -c 'from mnemonic import Mnemonic; print(Mnemonic("english").generate(128))')
    printf 'CAPTIVE_HD_SEED="%s"\n' "${SEED}" >> /etc/captive-portal/env
    chown root:captive-portal /etc/captive-portal/env
    chmod 640 /etc/captive-portal/env

    cat <<'EOF' >&2
[!] WARNING: A new BIP39 master key has been generated and written to
    /etc/captive-portal/env as CAPTIVE_HD_SEED.
    This mnemonic is the master secret for all payment addresses.
    BACK IT UP NOW and store it offline; loss of this key means loss of
    all funds sent to derived addresses.
    Ensure /etc/captive-portal/env remains mode 640 and owned by
    root:captive-portal.
EOF
fi

echo "[*] Reloading systemd and enabling services..."
systemctl daemon-reload
systemctl enable captive-portal.service sync-auth.service
systemctl start captive-portal.service

echo ""
echo "[*] Setup complete. Next steps:"
echo "    1. Run: sudo eth-wifi-start"
echo "    2. On a client device, connect to SSID 'Free-WiFi-Pay-ETH' and pay."
echo ""
echo "    To stop: sudo eth-wifi-stop"
