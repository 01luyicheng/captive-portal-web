#!/bin/bash
set -euo pipefail

# Speed limiting via tc with IFB for download shaping
# Default: 3Mbps for all clients (free trial)
# Paid clients get full speed by being added to the "fast" class

WLAN="wlx30b49ed56fdd"
IFB="ifb0"
LIMIT_KBPS=3000  # 3Mbps
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

setup_tc() {
    echo "[tc] Setting up traffic shaping on $WLAN..."

    # Egress shaping on WLAN (traffic going TO clients = downloads)
    tc qdisc del dev "$WLAN" root 2>/dev/null || true
    tc qdisc add dev "$WLAN" root handle 1: htb default 10
    tc class add dev "$WLAN" parent 1: classid 1:1 htb rate 1000mbit
    tc class add dev "$WLAN" parent 1:1 classid 1:10 htb rate ${LIMIT_KBPS}kbit ceil ${LIMIT_KBPS}kbit prio 1
    tc class add dev "$WLAN" parent 1:1 classid 1:20 htb rate 1000mbit ceil 1000mbit prio 0

    # Ingress shaping via IFB (mirrors WLAN ingress = traffic FROM clients = uploads)
    modprobe ifb 2>/dev/null || true
    ip link set "$IFB" up 2>/dev/null || true
    tc qdisc del dev "$WLAN" ingress 2>/dev/null || true
    tc qdisc add dev "$WLAN" ingress
    tc filter add dev "$WLAN" parent ffff: protocol ip u32 match u32 0 0 action mirred egress redirect dev "$IFB"

    tc qdisc del dev "$IFB" root 2>/dev/null || true
    tc qdisc add dev "$IFB" root handle 1: htb default 10
    tc class add dev "$IFB" parent 1: classid 1:1 htb rate 1000mbit
    tc class add dev "$IFB" parent 1:1 classid 1:10 htb rate ${LIMIT_KBPS}kbit ceil ${LIMIT_KBPS}kbit prio 1
    tc class add dev "$IFB" parent 1:1 classid 1:20 htb rate 1000mbit ceil 1000mbit prio 0

    echo "[tc] Default 3Mbps limit active on both directions."
}

add_paid_client() {
    IP="$1"
    echo "[tc] Granting full speed to $IP"
    tc filter add dev "$WLAN" parent 1: protocol ip prio 0 u32 match ip dst "$IP" flowid 1:20
    tc filter add dev "$IFB" parent 1: protocol ip prio 0 u32 match ip src "$IP" flowid 1:20
}

remove_paid_client() {
    IP="$1"
    echo "[tc] Removing full speed for $IP"
    tc filter del dev "$WLAN" parent 1: protocol ip prio 0 u32 match ip dst "$IP" flowid 1:20 2>/dev/null || true
    tc filter del dev "$IFB" parent 1: protocol ip prio 0 u32 match ip src "$IP" flowid 1:20 2>/dev/null || true
}

allow() {
    IP="$1"
    add_paid_client "$IP"
    bash "$SCRIPT_DIR/iptables.sh" allow_client "$IP"
}

deny() {
    IP="$1"
    remove_paid_client "$IP"
    bash "$SCRIPT_DIR/iptables.sh" deny_client "$IP"
}

case "${1:-setup}" in
    setup)   setup_tc ;;
    allow)   allow "${2:?IP required}" ;;
    deny)    deny "${2:?IP required}" ;;
    *)       echo "Usage: $0 {setup|allow <IP>|deny <IP>}" ;;
esac
