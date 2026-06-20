# Captive Portal Web

A Wi-Fi captive portal that requires cryptocurrency payment for network access. Users connect to the hotspot, scan a QR code or use a browser wallet extension to pay a small amount of ETH on any supported EVM chain, and gain internet access.

## Features

- Multi-chain support: Base, Polygon, Arbitrum, Optimism, BSC, Ethereum
- Multiple payment tiers with configurable amounts and quotas
- Grace period for temporary free access (configurable)
- Browser wallet integration (MetaMask, Rabby, etc.) via EIP-1193
- QR code payment with EIP-681 compatible URIs
- Mobile wallet deep links
- Automatic captive portal detection for iOS, Android, Windows, Firefox
- Per-client quota tracking via iptables/ipset
- SQLite database with WAL mode for concurrent access
- Configurable via environment variables and JSON config files

## Quick Start

```bash
sudo bash scripts/setup.sh
sudo eth-wifi-start
```

## Configuration

All configuration is via environment variables. Set them in `/etc/captive-portal/env` or in `scripts/captive-portal.service`.

### Core

| Variable | Default | Description |
|----------|---------|-------------|
| `CAPTIVE_HD_SEED` | (required) | BIP39 mnemonic for HD wallet derivation |
| `CAPTIVE_DB` | `/var/lib/captive-portal/payments.db` | SQLite database path |
| `CAPTIVE_PORTAL_DEV` | `false` | Enable dev mode (simulate-payment endpoint) |
| `CAPTIVE_DEV_TOKEN` | (empty) | Dev token for simulate-payment API |
| `CAPTIVE_CHAINS_CONFIG` | `/etc/captive-portal/chains.json` | Chain config JSON file path |

### Payment

| Variable | Default | Description |
|----------|---------|-------------|
| `REQUIRED_CONFIRMATIONS` | `3` | Block confirmations required |
| `ACCESS_DURATION` | `86400` | Payment validity in seconds (1 day) |
| `PAYMENT_PENDING_DURATION` | `3600` | Pending payment expiry in seconds (1 hour) |
| `TX_TIMESTAMP_TOLERANCE` | `300` | Max age of accepted tx in seconds (5 min) |

### Grace Period

| Variable | Default | Description |
|----------|---------|-------------|
| `GRACE_DURATION_SECONDS` | `300` | Grace period duration (5 min) |
| `GRACE_QUOTA_BYTES` | `104857600` | Grace period quota (100 MB) |
| `GRACE_MAX_PER_24H` | `1` | Max grace activations per 24 hours |
| `GRACE_COOLDOWN_SECONDS` | `3600` | Cooldown between grace activations |

### Database

| Variable | Default | Description |
|----------|---------|-------------|
| `DB_BUSY_TIMEOUT` | `5000` | SQLite busy timeout in ms |
| `PENDING_CLEANUP_DAYS` | `7` | Delete pending payments older than N days |
| `PAYMENTS_CLEANUP_DAYS` | `90` | Delete confirmed payments older than N days |

### Frontend

| Variable | Default | Description |
|----------|---------|-------------|
| `POLL_INTERVAL_MS` | `3000` | Payment check poll interval (ms) |
| `POLL_MAX_INTERVAL_MS` | `30000` | Max poll interval with backoff (ms) |
| `REDIRECT_DELAY_MS` | `1000` | Delay before page redirect (ms) |
| `FETCH_TIMEOUT_MS` | `20000` | Blockscout API fetch timeout (ms) |
| `LOW_BYTES_THRESHOLD` | `52428800` | Show renewal hint below this bytes (50 MB) |
| `LOW_TIME_THRESHOLD` | `600` | Show renewal hint below this seconds (10 min) |
| `STATUS_POLL_INTERVAL_MS` | `3000` | Success page status poll interval (ms) |
| `NETWORK_CHECK_INTERVAL_MS` | `3000` | Success page network check interval (ms) |

### Network

| Variable | Default | Description |
|----------|---------|-------------|
| `TRUSTED_PROXIES` | `127.0.0.1,::1` | Trusted proxy IPs for X-Forwarded-For |
| `PORTAL_IP` | `10.88.0.1` | Portal bind IP |
| `PORTAL_PORT` | `5000` | Portal bind port |
| `GUNICORN_WORKERS` | `2` | Number of gunicorn workers |
| `PHY_IF` | `wlx30b49ed56fdd` | Physical wireless interface |
| `AP_IF` | `wlan0-ap` | Virtual AP interface name |
| `AP_IP` | `10.88.0.1/24` | AP interface IP |
| `AP_NET` | `10.88.0.0/24` | AP subnet |

## Chain Configuration

Chain configs (payment amounts, tiers, quotas) can be customized via a JSON file. Copy `scripts/chains.example.json` to `/etc/captive-portal/chains.json` and modify.

```json
{
  "base": {
    "name": "Base",
    "chain_id": 8453,
    "blockscout_api": "https://base.blockscout.com/api/v2",
    "block_time": 2,
    "recommended": true,
    "icon": "blue_circle",
    "tiers": [
      {"amount_eth": "0.00001", "amount_wei": "10000000000000", "quota_bytes": 104857600}
    ]
  }
}
```

If the file is missing or invalid, the built-in defaults are used.

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/` | GET | Payment page |
| `/success` | GET | Success page (paid/grace users) |
| `/api/chains` | GET | List supported chains and tiers |
| `/api/status` | GET | Current client authorization status |
| `/api/qr` | GET | Generate payment QR code (PNG) |
| `/api/check-payment` | POST | Check payment status on-chain |
| `/api/activate-grace` | POST | Activate grace period |
| `/api/health` | GET | Health check (DB connectivity) |
| `/api/simulate-payment` | POST | Dev mode: simulate payment |

## Architecture

```
User Device -> Wi-Fi AP (hostapd) -> iptables/ipset -> Internet
                     |                    |
                     v                    v
              dnsmasq (DNS)       captive-portal (Flask)
              - captive portal          - payment page
                detection               - payment verification
              - DHCP                    - SQLite DB
                                        - HD wallet derivation
              sync-auth.sh (systemd)
              - syncs DB -> ipset
              - tracks byte usage
              - enforces quotas
```

## Development

```bash
source .venv/bin/activate
CAPTIVE_PORTAL_DEV=true CAPTIVE_DEV_TOKEN=test python3 app.py
```

Then POST to `/api/simulate-payment` with header `X-Dev-Token: test` to simulate payment.

## License

MIT
