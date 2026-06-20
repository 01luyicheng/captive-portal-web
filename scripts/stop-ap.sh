#!/usr/bin/env bash
set -euo pipefail

# Stop the Ethereum-paid Wi-Fi hotspot.

AP_IF="${AP_IF:-wlan0-ap}"

echo "[*] Stopping services..."
systemctl stop eth-wifi-sync 2>/dev/null || true
systemctl stop hostapd@eth-wifi 2>/dev/null || true
systemctl stop dnsmasq 2>/dev/null || true

echo "[*] Removing iptables rules..."
iptables -D INPUT -j ETH_WIFI 2>/dev/null || true
iptables -D FORWARD -j ETH_WIFI 2>/dev/null || true
iptables -F ETH_WIFI 2>/dev/null || true
iptables -X ETH_WIFI 2>/dev/null || true

iptables -t nat -D PREROUTING -j ETH_WIFI_NAT 2>/dev/null || true
iptables -t nat -D POSTROUTING -j ETH_WIFI_NAT 2>/dev/null || true
iptables -t nat -F ETH_WIFI_NAT 2>/dev/null || true
iptables -t nat -X ETH_WIFI_NAT 2>/dev/null || true

# Restore original FORWARD default policy so other forwarding (mihomo TUN,
# containers, etc.) is not permanently broken. Default to ACCEPT if the save
# file is missing for backward compatibility with earlier deployments.
ORIG_POLICY=ACCEPT
if [[ -r /run/eth-wifi-forward-policy ]]; then
    ORIG_POLICY=$(cat /run/eth-wifi-forward-policy)
fi
iptables -P FORWARD "${ORIG_POLICY:-ACCEPT}" 2>/dev/null || true
rm -f /run/eth-wifi-forward-policy

echo "[*] Disabling IP forwarding..."
sysctl -w net.ipv4.ip_forward=0 >/dev/null
sysctl -w net.ipv6.conf.${AP_IF}.disable_ipv6=0 >/dev/null

echo "[*] Flushing authorization ipsets..."
ipset flush paid_ips 2>/dev/null || true
ipset flush grace_ips 2>/dev/null || true
ipset destroy paid_ips 2>/dev/null || true
ipset destroy grace_ips 2>/dev/null || true

echo "[*] Removing AP interface ${AP_IF}..."
ip link set "${AP_IF}" down 2>/dev/null || true
iw dev "${AP_IF}" del 2>/dev/null || true

echo "[*] Hotspot stopped."
