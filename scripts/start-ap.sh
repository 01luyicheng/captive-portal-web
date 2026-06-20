#!/usr/bin/env bash
set -euo pipefail

# Start the Ethereum-paid Wi-Fi hotspot.
# Must be run as root.

PHY_IF="${PHY_IF:-wlx30b49ed56fdd}"
AP_IF="${AP_IF:-wlan0-ap}"
AP_IP="${AP_IP:-10.88.0.1/24}"
AP_NET="${AP_NET:-10.88.0.0/24}"
PORTAL_IP="${PORTAL_IP:-10.88.0.1}"
PORTAL_PORT="${PORTAL_PORT:-5000}"

# Cleanup handler to restore state on failure
_cleanup() {
    local exit_code=$?
    if [ $exit_code -ne 0 ]; then
        echo "[!] Script failed (exit $exit_code). Restoring previous state..."
        # Restore FORWARD policy if it was saved
        if [ -f /run/eth-wifi-forward-policy ]; then
            iptables -P FORWARD "$(cat /run/eth-wifi-forward-policy)" 2>/dev/null || true
        fi
        # Clean up partial iptables chains
        iptables -D INPUT -j ETH_WIFI 2>/dev/null || true
        iptables -D FORWARD -j ETH_WIFI 2>/dev/null || true
        iptables -F ETH_WIFI 2>/dev/null || true
        iptables -X ETH_WIFI 2>/dev/null || true
        iptables -t nat -D PREROUTING -j ETH_WIFI_NAT 2>/dev/null || true
        iptables -t nat -D POSTROUTING -j ETH_WIFI_NAT 2>/dev/null || true
        iptables -t nat -F ETH_WIFI_NAT 2>/dev/null || true
        iptables -t nat -X ETH_WIFI_NAT 2>/dev/null || true
    fi
}
trap _cleanup EXIT

echo "[*] Stopping services if already running..."
systemctl stop eth-wifi-sync 2>/dev/null || true
systemctl stop hostapd@eth-wifi 2>/dev/null || true
systemctl stop dnsmasq 2>/dev/null || true
systemctl stop captive-portal 2>/dev/null || true

echo "[*] Removing old AP interface ${AP_IF}..."
iw dev "${AP_IF}" del 2>/dev/null || true

echo "[*] Adding virtual AP interface ${AP_IF} on ${PHY_IF}..."
if ! iw dev "${PHY_IF}" interface add "${AP_IF}" type __ap; then
    echo "[!] Failed to add virtual AP interface."
    echo "    Make sure ${PHY_IF} is on channel 10 and supports AP mode."
    exit 1
fi

echo "[*] Configuring ${AP_IF} IP..."
ip addr flush dev "${AP_IF}" 2>/dev/null || true
ip addr add "${AP_IP}" dev "${AP_IF}"
ip link set "${AP_IF}" up

echo "[*] Enabling IP forwarding..."
sysctl -w net.ipv4.ip_forward=1 >/dev/null
sysctl -w "net.ipv6.conf.${AP_IF}.disable_ipv6=1" >/dev/null

echo "[*] Saving current FORWARD default policy..."
iptables -S FORWARD | awk '/^-P FORWARD/{print $3}' > /run/eth-wifi-forward-policy

echo "[*] Setting FORWARD default policy to DROP..."
iptables -P FORWARD DROP

echo "[*] Cleaning up old iptables rules..."
# Remove jumps first so custom chains can be deleted.
iptables -D INPUT -j ETH_WIFI 2>/dev/null || true
iptables -D FORWARD -j ETH_WIFI 2>/dev/null || true
iptables -F ETH_WIFI 2>/dev/null || true
iptables -X ETH_WIFI 2>/dev/null || true
iptables -t nat -D PREROUTING -j ETH_WIFI_NAT 2>/dev/null || true
iptables -t nat -D POSTROUTING -j ETH_WIFI_NAT 2>/dev/null || true
iptables -t nat -F ETH_WIFI_NAT 2>/dev/null || true
iptables -t nat -X ETH_WIFI_NAT 2>/dev/null || true

echo "[*] Setting up ipset for authorized clients (paid + grace)..."
ipset destroy paid_ips 2>/dev/null || true
ipset destroy grace_ips 2>/dev/null || true
ipset create paid_ips hash:ip counters timeout 0
ipset create grace_ips hash:ip counters timeout 0

echo "[*] Setting up iptables rules..."
iptables -t nat -D PREROUTING -j ETH_WIFI_NAT 2>/dev/null || true
iptables -t nat -D POSTROUTING -j ETH_WIFI_NAT 2>/dev/null || true
iptables -N ETH_WIFI
iptables -t nat -N ETH_WIFI_NAT

# Drop traffic between wireless clients (client isolation).
iptables -A ETH_WIFI -i "${AP_IF}" -o "${AP_IF}" -j DROP

# Allow DHCP, DNS, ICMP and portal access (unpaid clients need these to pay).
iptables -A ETH_WIFI -i "${AP_IF}" -p udp --dport 67 -j ACCEPT
iptables -A ETH_WIFI -i "${AP_IF}" -p udp --dport 53 -j ACCEPT
iptables -A ETH_WIFI -i "${AP_IF}" -p tcp --dport "${PORTAL_PORT}" -j ACCEPT
iptables -A ETH_WIFI -i "${AP_IF}" -p icmp -j ACCEPT

# Paid/grace clients can forward traffic. Count both upload (src) and download
# (dst) so the ipset counters can be used to track quota consumption.
# dst matches are restricted to -o ${AP_IF} to avoid matching transit traffic.
iptables -A ETH_WIFI -i "${AP_IF}" -m set --match-set paid_ips src -j ACCEPT
iptables -A ETH_WIFI -o "${AP_IF}" -m set --match-set paid_ips dst -j ACCEPT
iptables -A ETH_WIFI -i "${AP_IF}" -m set --match-set grace_ips src -j ACCEPT
iptables -A ETH_WIFI -o "${AP_IF}" -m set --match-set grace_ips dst -j ACCEPT

# Allow established/related traffic for everyone (replies to DNS/portal).
iptables -A ETH_WIFI -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT

# Drop everything else from unpaid clients
iptables -A ETH_WIFI -i "${AP_IF}" -j DROP
# Also block unauthorized outbound traffic to the AP interface.
iptables -A ETH_WIFI -o "${AP_IF}" -j DROP

# Insert into INPUT/FORWARD chains
iptables -I INPUT 1 -i "${AP_IF}" -j ETH_WIFI
iptables -I FORWARD 1 -j ETH_WIFI

# NAT: redirect non-authorized DNS to dnsmasq on the AP interface (bypass mihomo dns-hijack)
iptables -t nat -A ETH_WIFI_NAT -i "${AP_IF}" -p udp --dport 53 -m set ! --match-set paid_ips src -m set ! --match-set grace_ips src -j DNAT --to-destination "${PORTAL_IP}:53"
iptables -t nat -A ETH_WIFI_NAT -i "${AP_IF}" -p tcp --dport 53 -m set ! --match-set paid_ips src -m set ! --match-set grace_ips src -j DNAT --to-destination "${PORTAL_IP}:53"

# NAT: redirect non-authorized HTTP to portal
iptables -t nat -A ETH_WIFI_NAT -i "${AP_IF}" -p tcp --dport 80 -m set ! --match-set paid_ips src -m set ! --match-set grace_ips src -j DNAT --to-destination "${PORTAL_IP}:${PORTAL_PORT}"

# NAT: masquerade authorized client traffic so mihomo TUN can take over
iptables -t nat -A ETH_WIFI_NAT -s "${AP_NET}" ! -d "${AP_NET}" -m set --match-set paid_ips src -j MASQUERADE
iptables -t nat -A ETH_WIFI_NAT -s "${AP_NET}" ! -d "${AP_NET}" -m set --match-set grace_ips src -j MASQUERADE

iptables -t nat -A PREROUTING -j ETH_WIFI_NAT
iptables -t nat -A POSTROUTING -j ETH_WIFI_NAT

echo "[*] Starting captive-portal web service..."
systemctl start captive-portal

echo "[*] Starting hostapd..."
systemctl start hostapd@eth-wifi

echo "[*] Starting dnsmasq..."
systemctl start dnsmasq

echo "[*] Starting authorization sync service..."
systemctl start eth-wifi-sync

echo "[*] Hotspot is up. SSID: Free-WiFi-Pay-ETH"
echo "    Portal: http://${PORTAL_IP}:${PORTAL_PORT}/"
