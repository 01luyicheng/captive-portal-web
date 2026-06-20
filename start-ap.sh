#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

WLAN="wlx30b49ed56fdd"
WAN="enx026445343736"
AP_IP="10.0.0.1"
PORTAL_PORT=5000
PID_FILE="$SCRIPT_DIR/dnsmasq.pid"

wait_for_port() {
    local port=$1
    local i=0
    while ! ss -tln | grep -q ":${port} " && [ $i -lt 30 ]; do
        sleep 0.5
        i=$((i+1))
    done
    if [ $i -ge 30 ]; then
        echo "Warning: port $port did not open within 15s"
    fi
}

is_running() {
    pidof "$1" >/dev/null 2>&1
}

echo "=== Captive Portal WiFi Setup ==="

# 1. Disconnect WiFi from any existing network
echo "[1/7] Disconnecting $WLAN..."
nmcli device disconnect "$WLAN" 2>/dev/null || true

# 2. Assign static IP to the AP interface
echo "[2/7] Configuring $WLAN IP $AP_IP..."
ip addr flush dev "$WLAN" 2>/dev/null || true
ip addr add "${AP_IP}/24" dev "$WLAN"
ip link set "$WLAN" up

# 3. Start hostapd
echo "[3/7] Starting hostapd..."
if is_running hostapd; then
    echo "[3/7] hostapd already running, restarting..."
    pkill hostapd 2>/dev/null || true
    sleep 1
fi
hostapd "$SCRIPT_DIR/hostapd.conf" -B
wait_for_port 8080

# 4. Start dnsmasq with PID file
echo "[4/7] Starting dnsmasq..."
if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
    kill "$(cat "$PID_FILE")" 2>/dev/null || true
    sleep 1
fi
dnsmasq -C "$SCRIPT_DIR/dnsmasq-ap.conf" --no-daemon --pid-file="$PID_FILE" &
wait_for_port 53

# 5. Set up iptables
echo "[5/7] Setting up iptables..."
bash "$SCRIPT_DIR/iptables.sh"

# 6. Set up speed limiting
echo "[6/7] Setting up 3Mbps speed limit..."
bash "$SCRIPT_DIR/speed-limit.sh" setup

# 7. Start the captive portal Flask app
echo "[7/7] Starting captive portal on $AP_IP:$PORTAL_PORT..."
# NOTE: Production deployments should use systemd EnvironmentFile instead of this cat approach.
export CAPTIVE_HD_SEED="$(cat "$SCRIPT_DIR/.secret")"
export CAPTIVE_PORTAL_DEV=false
export CAPTIVE_DB="$SCRIPT_DIR/data/payments.db"
export SERVER_HOST="$AP_IP"
export FLASK_HOST="0.0.0.0"
export FLASK_PORT=5000
export PRICE_TOLERANCE_PERCENT=5

mkdir -p "$SCRIPT_DIR/data"

echo ""
echo "=== Setup Complete ==="
echo "SSID: 可以直连EvoMap的网络"
echo "Portal: http://$AP_IP:$FLASK_PORT"
echo "Free trial: unlimited time, 3Mbps speed"
echo "Paid: full speed via real crypto payment"
echo ""
echo "Press Ctrl+C to stop all services"

cleanup() {
    echo ""
    echo "Shutting down..."

    # Kill Flask
    pkill -f "python.*app.py" 2>/dev/null || true

    # Kill hostapd
    pkill hostapd 2>/dev/null || true

    # Kill dnsmasq by PID file
    if [ -f "$PID_FILE" ]; then
        kill "$(cat "$PID_FILE")" 2>/dev/null || true
        rm -f "$PID_FILE"
    fi

    # Clean up tc
    tc qdisc del dev "$WLAN" root 2>/dev/null || true
    tc qdisc del dev "$WLAN" ingress 2>/dev/null || true
    tc qdisc del dev ifb0 root 2>/dev/null || true
    ip link set ifb0 down 2>/dev/null || true

    # Clean up iptables (full reset)
    iptables -t nat -F PREROUTING 2>/dev/null || true
    iptables -t nat -F POSTROUTING 2>/dev/null || true
    iptables -t nat -X CAPTIVE 2>/dev/null || true
    iptables -F FORWARD 2>/dev/null || true
    iptables -F INPUT 2>/dev/null || true
    iptables -X CAPTIVE 2>/dev/null || true
    iptables -t mangle -F PREROUTING 2>/dev/null || true

    # Disable forwarding
    echo 0 > /proc/sys/net/ipv4/ip_forward 2>/dev/null || true

    # Remove static IP
    ip addr del "${AP_IP}/24" dev "$WLAN" 2>/dev/null || true

    echo "Done."
}
trap cleanup EXIT INT TERM

cd "$SCRIPT_DIR"
.venv/bin/python app.py
