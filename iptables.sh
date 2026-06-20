#!/bin/bash
set -euo pipefail

WLAN="wlx30b49ed56fdd"
WAN="enx026445343736"
AP_NET="10.0.0.0/24"
PORTAL_IP="10.0.0.1"
PORTAL_PORT="5000"

setup() {
    # Flush all existing rules (including mangle for stale marks)
    iptables -t nat -F PREROUTING
    iptables -t nat -F POSTROUTING
    iptables -F FORWARD
    iptables -F INPUT
    iptables -t mangle -F PREROUTING

    # NAT
    iptables -t nat -A POSTROUTING -o "$WAN" -j MASQUERADE

    # PREROUTING: portal redirect
    iptables -t nat -A PREROUTING -i "$WLAN" -s "$AP_NET" -p tcp --dport 80 -j DNAT --to-destination "$PORTAL_IP:$PORTAL_PORT"
    iptables -t nat -A PREROUTING -i "$WLAN" -s "$AP_NET" -p tcp --dport 443 -j DNAT --to-destination "$PORTAL_IP:$PORTAL_PORT"
    iptables -t nat -A PREROUTING -i "$WLAN" -s "$AP_NET" -p tcp --dport 53 -j DNAT --to-destination "$PORTAL_IP:53"
    iptables -t nat -A PREROUTING -i "$WLAN" -s "$AP_NET" -p udp --dport 53 -j DNAT --to-destination "$PORTAL_IP:53"

    # FORWARD: only allow marked (authenticated) traffic from WLAN to WAN
    iptables -A FORWARD -i "$WLAN" -o "$WAN" -m mark --mark 1 -j ACCEPT
    iptables -A FORWARD -i "$WAN" -o "$WLAN" -m state --state RELATED,ESTABLISHED -j ACCEPT
    iptables -A FORWARD -i "$WLAN" -o "$WAN" -j DROP

    # INPUT: allow portal and DNS from AP clients
    iptables -A INPUT -i "$WLAN" -s "$AP_NET" -p tcp --dport "$PORTAL_PORT" -j ACCEPT
    iptables -A INPUT -i "$WLAN" -s "$AP_NET" -p tcp --dport 53 -j ACCEPT
    iptables -A INPUT -i "$WLAN" -s "$AP_NET" -p udp --dport 53 -j ACCEPT

    # Enable forwarding
    echo 1 > /proc/sys/net/ipv4/ip_forward

    echo "[iptables] Mark-based captive portal active."
}

allow_client() {
    iptables -t mangle -A PREROUTING -i "$WLAN" -s "$1" -j MARK --set-mark 1
}

deny_client() {
    iptables -t mangle -D PREROUTING -i "$WLAN" -s "$1" -j MARK --set-mark 1 2>/dev/null || true
}

case "${1:-setup}" in
    setup)        setup ;;
    allow_client) allow_client "${2:?IP required}" ;;
    deny_client)  deny_client "${2:?IP required}" ;;
    *)            echo "Usage: $0 {setup|allow_client <IP>|deny_client <IP>}" ;;
esac
