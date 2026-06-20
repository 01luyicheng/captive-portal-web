"""Captive Portal web application.

A Flask service that asks Wi-Fi users to pay a small amount of crypto before
granting network access. Supports multiple EVM chains via Blockscout public
APIs (no API key required for most chains).

Each user/session is assigned a unique deposit address derived from a BIP39
mnemonic using the standard Ethereum derivation path (m/44'/60'/0'/0/{index}).
"""

import io
import collections
import ipaddress
import json
import os
import secrets
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import qrcode
import requests
from bip32 import BIP32
from eth_account import Account
from flask import (
    Flask,
    jsonify,
    make_response,
    redirect,
    render_template,
    request,
    send_file,
)
from mnemonic import Mnemonic

from price_service import PriceService

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
def _env_int(key, default, min_val=None, max_val=None):
    try:
        val = int(os.environ.get(key, str(default)))
    except (ValueError, TypeError):
        val = default
    if min_val is not None:
        val = max(min_val, val)
    if max_val is not None:
        val = min(max_val, val)
    return val

REQUIRED_CONFIRMATIONS = _env_int("REQUIRED_CONFIRMATIONS", 3, min_val=1)
ACCESS_DURATION = _env_int("ACCESS_DURATION", 86400, min_val=60)
PAYMENT_PENDING_DURATION = _env_int("PAYMENT_PENDING_DURATION", 3600, min_val=60)

GRACE_DURATION_SECONDS = _env_int("GRACE_DURATION_SECONDS", 300, min_val=10)
GRACE_QUOTA_BYTES = _env_int("GRACE_QUOTA_BYTES", 104857600, min_val=1048576)
GRACE_MAX_PER_24H = _env_int("GRACE_MAX_PER_24H", 1, min_val=0)
GRACE_COOLDOWN_SECONDS = _env_int("GRACE_COOLDOWN_SECONDS", 3600, min_val=0)

DB_BUSY_TIMEOUT = _env_int("DB_BUSY_TIMEOUT", 5000, min_val=1000)
PENDING_CLEANUP_DAYS = _env_int("PENDING_CLEANUP_DAYS", 7, min_val=1)
PAYMENTS_CLEANUP_DAYS = _env_int("PAYMENTS_CLEANUP_DAYS", 90, min_val=1)
TX_TIMESTAMP_TOLERANCE = _env_int("TX_TIMESTAMP_TOLERANCE", 300, min_val=0)

try:
    PRICE_TOLERANCE_PERCENT = max(0, min(50, int(os.environ.get("PRICE_TOLERANCE_PERCENT", "20"))))
except (ValueError, TypeError):
    PRICE_TOLERANCE_PERCENT = 20
PRICE_LOCK_MODE = os.environ.get("PRICE_LOCK_MODE", "lock")
try:
    PRICE_LOCK_DURATION = max(60, int(os.environ.get("PRICE_LOCK_DURATION", "900")))
except (ValueError, TypeError):
    PRICE_LOCK_DURATION = 900
DEFAULT_TOKEN = os.environ.get("DEFAULT_TOKEN", "ETH")
FALLBACK_CURRENCY = os.environ.get("FALLBACK_CURRENCY", "usd")

PORTAL_TITLE = os.environ.get("PORTAL_TITLE", "Wi-Fi 支付 portal")
PORTAL_WELCOME = os.environ.get("PORTAL_WELCOME", "欢迎连接 Wi-Fi")
PORTAL_LEAD = os.environ.get("PORTAL_LEAD", "支付少量加密货币即可使用本无线网络。")
PORTAL_FOOTER = os.environ.get("PORTAL_FOOTER", "")
PORTAL_SUPPORT_URL = os.environ.get("PORTAL_SUPPORT_URL", "")
PORTAL_LOGO_URL = os.environ.get("PORTAL_LOGO_URL", "")
QR_FILL_COLOR = os.environ.get("QR_FILL_COLOR", "black")
QR_BACK_COLOR = os.environ.get("QR_BACK_COLOR", "white")

_price_service = PriceService(cache_ttl=60)

# DEV_MODE enables the "simulate payment" helper. Disable in production.
DEV_MODE = os.environ.get("CAPTIVE_PORTAL_DEV", "false").lower() in ("1", "true", "yes")

# Persistent SQLite database.
DB_PATH = os.environ.get("CAPTIVE_DB", "/var/lib/captive-portal/payments.db")

# Development helper token. Required when DEV_MODE is enabled; requests to
# /api/simulate-payment must include an X-Dev-Token header matching this value.
CAPTIVE_DEV_TOKEN = os.environ.get("CAPTIVE_DEV_TOKEN", "").strip()

# Trusted proxy networks (comma-separated IPs or CIDRs). X-Forwarded-For is
# only trusted when the direct remote address falls into one of these.
_TRUSTED_PROXY_RAW = os.environ.get("TRUSTED_PROXIES", "127.0.0.1,::1").split(",")
TRUSTED_PROXIES = []
for _raw in _TRUSTED_PROXY_RAW:
    _raw = _raw.strip()
    if not _raw:
        continue
    try:
        TRUSTED_PROXIES.append(ipaddress.ip_network(_raw, strict=False))
    except ValueError:
        pass

# In-memory cache for Blockscout verification results to avoid repeated slow
# remote calls. Key: (chain_id, address), TTL: 5 seconds, bounded LRU.
VERIFY_CACHE_SECONDS = 5
VERIFY_CACHE_MAX_SIZE = 1000


class _VerifyCache:
    """Thread-safe bounded LRU cache with TTL eviction."""

    def __init__(self, ttl_seconds, max_size):
        self.ttl = ttl_seconds
        self.max_size = max_size
        self._store = collections.OrderedDict()
        self._lock = threading.Lock()

    def get(self, key, now):
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            ts, value = entry
            if ts <= now - self.ttl:
                del self._store[key]
                return None
            self._store.move_to_end(key)
            return value

    def set(self, key, now, value):
        with self._lock:
            expired = [k for k, (ts, _) in self._store.items() if ts <= now - self.ttl]
            for k in expired:
                del self._store[k]
            self._store[key] = (now, value)
            self._store.move_to_end(key)
            while len(self._store) > self.max_size:
                self._store.popitem(last=False)


_verify_cache = _VerifyCache(VERIFY_CACHE_SECONDS, VERIFY_CACHE_MAX_SIZE)

# Supported EVM chains. Each chain provides multiple payment tiers; the fiat
# value of the smallest tier is roughly the same across chains while keeping
# gas fees tiny on L2s.
_DEFAULT_CHAINS = {
    "base": {
        "name": "Base",
        "chain_id": 8453,
        "blockscout_api": "https://base.blockscout.com/api/v2",
        "block_time": 2,
        "recommended": True,
        "icon": "blue_circle",
        "tokens": {
            "ETH": {"type": "native"},
            "USDC": {"type": "erc20", "address": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", "decimals": 6},
        },
        "tiers": [
            {"amount_usd": 0.50, "quota_bytes": 104857600},
            {"amount_usd": 1.50, "quota_bytes": 536870912},
            {"amount_usd": 2.50, "quota_bytes": 1073741824},
        ],
    },
    "polygon": {
        "name": "Polygon PoS",
        "chain_id": 137,
        "blockscout_api": "https://polygon.blockscout.com/api/v2",
        "block_time": 2,
        "recommended": True,
        "icon": "purple_circle",
        "tokens": {
            "ETH": {"type": "native"},
            "USDC": {"type": "erc20", "address": "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359", "decimals": 6},
            "USDT": {"type": "erc20", "address": "0xc2132D05D31c914a87C6611C10748AEb04B58e8F", "decimals": 6},
        },
        "tiers": [
            {"amount_usd": 0.50, "quota_bytes": 104857600},
            {"amount_usd": 1.50, "quota_bytes": 536870912},
            {"amount_usd": 2.50, "quota_bytes": 1073741824},
        ],
    },
    "arbitrum": {
        "name": "Arbitrum One",
        "chain_id": 42161,
        "blockscout_api": "https://arbitrum.blockscout.com/api/v2",
        "block_time": 0.25,
        "recommended": False,
        "icon": "blue_diamond",
        "tokens": {
            "ETH": {"type": "native"},
            "USDC": {"type": "erc20", "address": "0xaf88d065e77c8cC2239327C5EDb3A432268e5831", "decimals": 6},
            "USDT": {"type": "erc20", "address": "0xFd086bC7CD5C481DCC9C85ebE478A1C0b69FCbb9", "decimals": 6},
        },
        "tiers": [
            {"amount_usd": 0.50, "quota_bytes": 104857600},
            {"amount_usd": 1.50, "quota_bytes": 536870912},
            {"amount_usd": 2.50, "quota_bytes": 1073741824},
        ],
    },
    "optimism": {
        "name": "Optimism",
        "chain_id": 10,
        "blockscout_api": "https://optimism.blockscout.com/api/v2",
        "block_time": 2,
        "recommended": False,
        "icon": "red_circle",
        "tokens": {
            "ETH": {"type": "native"},
            "USDC": {"type": "erc20", "address": "0x0b2C639c533813f4Aa9D7837CAf62653d097Ff85", "decimals": 6},
            "USDT": {"type": "erc20", "address": "0x94b008aA00579c1307B0EF2c499aD98a8ce58e58", "decimals": 6},
        },
        "tiers": [
            {"amount_usd": 0.50, "quota_bytes": 104857600},
            {"amount_usd": 1.50, "quota_bytes": 536870912},
            {"amount_usd": 2.50, "quota_bytes": 1073741824},
        ],
    },
    "bsc": {
        "name": "BNB Smart Chain",
        "chain_id": 56,
        "blockscout_api": "https://bnb.blockscout.com/api/v2",
        "block_time": 3,
        "recommended": False,
        "icon": "yellow_diamond",
        "tokens": {
            "ETH": {"type": "native"},
            "USDC": {"type": "erc20", "address": "0x8AC76a51cc950d9822D68b83fE1Ad97B32Cd580d", "decimals": 18},
            "USDT": {"type": "erc20", "address": "0x55d398326f99059fF775485246999027B3197955", "decimals": 18},
        },
        "tiers": [
            {"amount_usd": 0.50, "quota_bytes": 104857600},
            {"amount_usd": 1.50, "quota_bytes": 536870912},
            {"amount_usd": 2.50, "quota_bytes": 1073741824},
        ],
    },
    "ethereum": {
        "name": "Ethereum Mainnet",
        "chain_id": 1,
        "blockscout_api": "https://eth.blockscout.com/api/v2",
        "block_time": 12,
        "recommended": False,
        "icon": "black_diamond",
        "tokens": {
            "ETH": {"type": "native"},
            "USDC": {"type": "erc20", "address": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48", "decimals": 6},
            "USDT": {"type": "erc20", "address": "0xdAC17F958D2ee523a2206206994597C13D831ec7", "decimals": 6},
        },
        "tiers": [
            {"amount_usd": 1.00, "quota_bytes": 104857600},
            {"amount_usd": 3.00, "quota_bytes": 536870912},
            {"amount_usd": 5.00, "quota_bytes": 1073741824},
        ],
    },
}

_CHAINS_CONFIG_PATH = os.environ.get("CAPTIVE_CHAINS_CONFIG", "/etc/captive-portal/chains.json")


def _load_chains():
    try:
        with open(_CHAINS_CONFIG_PATH) as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return _DEFAULT_CHAINS
        for chain_id, chain_cfg in data.items():
            if not isinstance(chain_cfg, dict):
                return _DEFAULT_CHAINS
            for key in ("name", "chain_id", "blockscout_api", "tiers"):
                if key not in chain_cfg:
                    return _DEFAULT_CHAINS
            if not isinstance(chain_cfg["tiers"], list) or not chain_cfg["tiers"]:
                return _DEFAULT_CHAINS
            for tier in chain_cfg["tiers"]:
                if not isinstance(tier, dict) or "quota_bytes" not in tier:
                    return _DEFAULT_CHAINS
                if "amount_usd" not in tier and "amount_wei" not in tier:
                    return _DEFAULT_CHAINS
        return data
    except (FileNotFoundError, json.JSONDecodeError, KeyError, TypeError):
        return _DEFAULT_CHAINS


CHAINS = _load_chains()

DEFAULT_CHAIN = os.environ.get("DEFAULT_CHAIN", "base")
if DEFAULT_CHAIN not in CHAINS:
    DEFAULT_CHAIN = next(iter(CHAINS.keys()))

# ---------------------------------------------------------------------------
# HD wallet setup
# ---------------------------------------------------------------------------
MNEMONIC = os.environ.get("CAPTIVE_HD_SEED", "").strip()
if not MNEMONIC:
    raise RuntimeError("CAPTIVE_HD_SEED environment variable is required")

_MNEMO = Mnemonic("english")
if not _MNEMO.check(MNEMONIC):
    raise RuntimeError("CAPTIVE_HD_SEED is not a valid BIP39 mnemonic")

_HD_ROOT = BIP32.from_seed(_MNEMO.to_seed(MNEMONIC))

# BIP44 path: m/44'/60'/0'/0/{index}
_HD_BASE_PATH = [
    0x8000002C,  # 44'
    0x8000003C,  # 60'
    0x80000000,  # 0'
    0,           # 0
]


def derive_address(index):
    """Derive a unique EVM deposit address from the HD seed."""
    priv_key = _HD_ROOT.get_privkey_from_path(_HD_BASE_PATH + [index])
    return Account.from_key(priv_key).address


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------
def _db_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.execute(f"PRAGMA busy_timeout={DB_BUSY_TIMEOUT}")
    return conn


def _column_exists(conn, table, column):
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(r[1] == column for r in rows)


def _migrate_payments_unique_constraint(conn):
    """Ensure payments has (chain_id, tx_hash) unique and no tx_hash single-column unique."""
    cur = conn.execute("PRAGMA index_list(payments)")
    indexes = cur.fetchall()

    tx_hash_unique_idx = None
    composite_unique_idx = None
    for idx in indexes:
        # idx: (seq, name, unique, origin, partial)
        if not idx[2]:
            continue
        name = idx[1]
        info = conn.execute(f"PRAGMA index_info({name})").fetchall()
        cols = tuple(r[2] for r in info)
        if cols == ("tx_hash",):
            tx_hash_unique_idx = name
        elif cols == ("chain_id", "tx_hash"):
            composite_unique_idx = name

    if tx_hash_unique_idx is None and composite_unique_idx is not None:
        return

    conn.execute("SAVEPOINT migrate_payments")
    try:
        cols = conn.execute("PRAGMA table_info(payments)").fetchall()
        col_defs = []
        col_names = []
        for c in cols:
            # c: (cid, name, type, notnull, dflt_value, pk)
            notnull = "NOT NULL" if c[3] else ""
            default = f"DEFAULT {c[4]}" if c[4] is not None else ""
            pk = "PRIMARY KEY AUTOINCREMENT" if c[5] else ""
            col_defs.append(f"{c[1]} {c[2]} {notnull} {default} {pk}".strip())
            col_names.append(c[1])

        conn.execute(
            """
            CREATE TABLE _payments_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_ip TEXT NOT NULL,
                tx_hash TEXT NOT NULL,
                chain_id TEXT NOT NULL,
                source TEXT NOT NULL,
                paid_at INTEGER NOT NULL,
                expires_at INTEGER NOT NULL,
                quota_bytes INTEGER NOT NULL DEFAULT 0,
                derivation_index INTEGER NOT NULL DEFAULT -1,
                UNIQUE(chain_id, tx_hash)
            )
            """
        )
        col_list = ", ".join(col_names)
        conn.execute(f"INSERT INTO _payments_new ({col_list}) SELECT {col_list} FROM payments")
        conn.execute("DROP TABLE payments")
        conn.execute("ALTER TABLE _payments_new RENAME TO payments")
        conn.execute("RELEASE migrate_payments")
    except Exception:
        conn.execute("ROLLBACK TO migrate_payments")
        conn.execute("RELEASE migrate_payments")
        raise


def init_db():
    """Create the payments tables if they do not exist."""
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    with _db_conn() as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_ip TEXT NOT NULL,
                tx_hash TEXT NOT NULL,
                chain_id TEXT NOT NULL,
                source TEXT NOT NULL,
                paid_at INTEGER NOT NULL,
                expires_at INTEGER NOT NULL,
                quota_bytes INTEGER NOT NULL DEFAULT 0,
                UNIQUE(chain_id, tx_hash)
            )
            """
        )
        if not _column_exists(conn, "payments", "quota_bytes"):
            conn.execute("ALTER TABLE payments ADD COLUMN quota_bytes INTEGER NOT NULL DEFAULT 0")
        if not _column_exists(conn, "payments", "derivation_index"):
            conn.execute("ALTER TABLE payments ADD COLUMN derivation_index INTEGER NOT NULL DEFAULT -1")
        _migrate_payments_unique_constraint(conn)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_payments_client_ip ON payments(client_ip)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_payments_tx_hash ON payments(tx_hash)"
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS derivation_counter (
                id INTEGER PRIMARY KEY,
                next_index INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        conn.execute(
            "INSERT OR IGNORE INTO derivation_counter (id, next_index) VALUES (1, 0)"
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS pending_payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_ip TEXT NOT NULL,
                chain_id TEXT NOT NULL,
                address TEXT NOT NULL,
                derivation_index INTEGER NOT NULL UNIQUE,
                amount_wei TEXT NOT NULL,
                quota_bytes INTEGER NOT NULL DEFAULT 0,
                tier_index INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at INTEGER NOT NULL,
                expires_at INTEGER NOT NULL
            )
            """
        )
        if not _column_exists(conn, "pending_payments", "quota_bytes"):
            conn.execute("ALTER TABLE pending_payments ADD COLUMN quota_bytes INTEGER NOT NULL DEFAULT 0")
        if not _column_exists(conn, "pending_payments", "tier_index"):
            conn.execute("ALTER TABLE pending_payments ADD COLUMN tier_index INTEGER NOT NULL DEFAULT 0")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_pending_client_ip ON pending_payments(client_ip)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_pending_address ON pending_payments(address)"
        )
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_pending_active_unique
            ON pending_payments(client_ip, chain_id, tier_index)
            WHERE status = 'pending'
            """
        )
        conn.execute(
            """
            UPDATE derivation_counter
            SET next_index = MAX(
                COALESCE((SELECT MAX(derivation_index) FROM pending_payments), -1),
                COALESCE((SELECT MAX(derivation_index) FROM payments), -1)
            ) + 1
            WHERE id = 1
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS clients (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_ip TEXT UNIQUE NOT NULL,
                first_seen INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'new',
                grace_until INTEGER NOT NULL DEFAULT 0,
                paid_until INTEGER NOT NULL DEFAULT 0,
                quota_bytes INTEGER NOT NULL DEFAULT 0,
                used_bytes INTEGER NOT NULL DEFAULT 0,
                last_ipset_bytes INTEGER NOT NULL DEFAULT 0,
                grace_activated_count INTEGER NOT NULL DEFAULT 0,
                last_grace_activated_at INTEGER NOT NULL DEFAULT 0,
                reset_ipset_at INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        if not _column_exists(conn, "clients", "grace_activated_count"):
            conn.execute("ALTER TABLE clients ADD COLUMN grace_activated_count INTEGER NOT NULL DEFAULT 0")
        if not _column_exists(conn, "clients", "last_grace_activated_at"):
            conn.execute("ALTER TABLE clients ADD COLUMN last_grace_activated_at INTEGER NOT NULL DEFAULT 0")
        if not _column_exists(conn, "clients", "reset_ipset_at"):
            conn.execute("ALTER TABLE clients ADD COLUMN reset_ipset_at INTEGER NOT NULL DEFAULT 0")
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_clients_client_ip ON clients(client_ip)"
        )

        # Cleanup old records once at startup.
        now = int(time.time())
        conn.execute(
            "DELETE FROM pending_payments WHERE expires_at < ?",
            (now - PENDING_CLEANUP_DAYS * 86400,),
        )
        conn.execute(
            "DELETE FROM payments WHERE expires_at < ?",
            (now - PAYMENTS_CLEANUP_DAYS * 86400,),
        )


init_db()


def _next_derivation_index(conn):
    """Atomically reserve and return the next derivation index."""
    conn.execute(
        "UPDATE derivation_counter SET next_index = next_index + 1 WHERE id = 1"
    )
    row = conn.execute(
        "SELECT next_index - 1 FROM derivation_counter WHERE id = 1"
    ).fetchone()
    return row[0]


def _ensure_client_row(conn, client_ip):
    """Ensure a clients row exists; return its current status."""
    now = int(time.time())
    conn.execute(
            """
            INSERT OR IGNORE INTO clients
                (client_ip, first_seen, status, grace_until, paid_until, quota_bytes, used_bytes, last_ipset_bytes, grace_activated_count, last_grace_activated_at, reset_ipset_at)
            VALUES (?, ?, 'new', 0, 0, 0, 0, 0, 0, 0, 0)
            """,
            (client_ip, now),
        )
    row = conn.execute("SELECT status FROM clients WHERE client_ip = ?", (client_ip,)).fetchone()
    return row[0] if row else "new"


def get_client_status(client_ip):
    """Return the current client status, or None if no row exists."""
    with _db_conn() as conn:
        row = conn.execute(
            """
            SELECT status, grace_until, paid_until, quota_bytes, used_bytes
            FROM clients WHERE client_ip = ?
            """,
            (client_ip,),
        ).fetchone()
    if not row:
        return None
    return {
        "status": row[0],
        "grace_until": row[1],
        "paid_until": row[2],
        "quota_bytes": row[3],
        "used_bytes": row[4],
    }


def get_active_pending_payment(client_ip, chain_id, tier_index=0):
    """Return the active pending payment row for a client/chain/tier, if any."""
    now = int(time.time())
    with _db_conn() as conn:
        row = conn.execute(
            """
            SELECT id, address, derivation_index, amount_wei, quota_bytes, tier_index, status, expires_at
            FROM pending_payments
            WHERE client_ip = ? AND chain_id = ? AND tier_index = ? AND status = 'pending' AND expires_at > ?
            """,
            (client_ip, chain_id, tier_index, now),
        ).fetchone()
    if not row:
        return None
    return {
        "id": row[0],
        "address": row[1],
        "derivation_index": row[2],
        "amount_wei": row[3],
        "quota_bytes": row[4],
        "tier_index": row[5],
        "status": row[6],
        "expires_at": row[7],
    }


def get_active_pending_payments(client_ip, chain_id):
    """Return all active pending payment rows for a client/chain, oldest first."""
    now = int(time.time())
    with _db_conn() as conn:
        rows = conn.execute(
            """
            SELECT id, address, derivation_index, amount_wei, quota_bytes, tier_index, status, expires_at, created_at
            FROM pending_payments
            WHERE client_ip = ? AND chain_id = ? AND status = 'pending' AND expires_at > ?
            ORDER BY created_at ASC, id ASC
            """,
            (client_ip, chain_id, now),
        ).fetchall()
    return [
        {
            "id": r[0],
            "address": r[1],
            "derivation_index": r[2],
            "amount_wei": r[3],
            "quota_bytes": r[4],
            "tier_index": r[5],
            "status": r[6],
            "expires_at": r[7],
            "created_at": r[8],
        }
        for r in rows
    ]


def get_or_create_pending_payment(client_ip, chain_id, tier_index=0, token=None):
    """Reuse an active pending payment or create a new derived address record."""
    cfg = CHAINS[chain_id]
    tiers = cfg["tiers"]
    if not (0 <= tier_index < len(tiers)):
        tier_index = 0
    tier = tiers[tier_index]

    if token is None:
        token = DEFAULT_TOKEN
    token = token.upper()
    tokens = cfg.get("tokens", {"ETH": {"type": "native"}})
    if token not in tokens:
        token = DEFAULT_TOKEN

    needed_symbols = set()
    for t in tiers:
        if "amount_usd" in t:
            for sym in tokens:
                needed_symbols.add(sym)
    needed_symbols.add(token)
    prices = _price_service.get_prices(list(needed_symbols))

    if "amount_wei" in tier:
        amount_wei = tier["amount_wei"]
    elif "amount_usd" in tier:
        _, unit_val, _ = _price_service.convert_usd_to_token(tier["amount_usd"], token, prices)
        amount_wei = str(unit_val or "0")
    else:
        amount_wei = "0"

    now = int(time.time())
    expires = now + PAYMENT_PENDING_DURATION
    conn = _db_conn()
    try:
        conn.isolation_level = None
        conn.execute("BEGIN IMMEDIATE")

        # Expire stale pending rows for this tier so they do not block new ones.
        conn.execute(
            """
            UPDATE pending_payments
            SET status = 'expired'
            WHERE client_ip = ? AND chain_id = ? AND tier_index = ? AND status = 'pending' AND expires_at <= ?
            """,
            (client_ip, chain_id, tier_index, now),
        )

        # Try to reuse an existing active pending row for this exact tier.
        row = conn.execute(
            """
            SELECT id, address, derivation_index, amount_wei, quota_bytes, tier_index, status, expires_at
            FROM pending_payments
            WHERE client_ip = ? AND chain_id = ? AND tier_index = ? AND status = 'pending' AND expires_at > ?
            """,
            (client_ip, chain_id, tier_index, now),
        ).fetchone()
        if row:
            conn.execute("COMMIT")
            return {
                "id": row[0],
                "address": row[1],
                "derivation_index": row[2],
                "amount_wei": row[3],
                "quota_bytes": row[4],
                "tier_index": row[5],
                "status": row[6],
                "expires_at": row[7],
            }

        index = _next_derivation_index(conn)
        address = derive_address(index)
        try:
            cur = conn.execute(
                """
                INSERT INTO pending_payments
                    (client_ip, chain_id, address, derivation_index, amount_wei, quota_bytes, tier_index, status, created_at, expires_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                """,
                (client_ip, chain_id, address, index, amount_wei, tier["quota_bytes"], tier_index, now, expires),
            )
            conn.execute("COMMIT")
            return {
                "id": cur.lastrowid,
                "address": address,
                "derivation_index": index,
                "amount_wei": amount_wei,
                "quota_bytes": tier["quota_bytes"],
                "tier_index": tier_index,
                "status": "pending",
                "expires_at": expires,
            }
        except sqlite3.IntegrityError:
            # Unique-index race: another worker inserted for this tier.
            conn.execute("ROLLBACK")
            existing = get_active_pending_payment(client_ip, chain_id, tier_index)
            if existing:
                return existing
            raise
    finally:
        conn.isolation_level = ""
        conn.close()


def is_paid(client_ip):
    """Return True if client_ip has an active grace or paid session."""
    now = int(time.time())
    with _db_conn() as conn:
        row = conn.execute(
            """
            SELECT status, grace_until, paid_until, quota_bytes, used_bytes
            FROM clients WHERE client_ip = ?
            """,
            (client_ip,),
        ).fetchone()
    if not row:
        return False
    status, grace_until, paid_until, quota_bytes, used_bytes = row
    if status == "paid" and paid_until > now and used_bytes < quota_bytes:
        return True
    if status == "grace" and grace_until > now and used_bytes < quota_bytes:
        return True
    return False


def activate_grace(client_ip):
    """Grant a short grace period to a new or expired client."""
    now = int(time.time())
    with _db_conn() as conn:
        status = _ensure_client_row(conn, client_ip)
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """
            SELECT grace_until, paid_until, quota_bytes, used_bytes,
                   grace_activated_count, last_grace_activated_at
            FROM clients WHERE client_ip = ?
            """,
            (client_ip,),
        ).fetchone()
        grace_until, paid_until, quota_bytes, used_bytes, grace_count, last_grace_at = (
            row or (0, 0, 0, 0, 0, 0)
        )

        # H2: DB status is grace but the grace period has already expired.
        if status == "grace" and grace_until <= now:
            conn.execute(
                "UPDATE clients SET status = 'expired' WHERE client_ip = ?",
                (client_ip,),
            )
            status = "expired"

        if status == "paid":
            # If the paid session is already exhausted, mark it expired so grace can start.
            if paid_until <= now or used_bytes >= quota_bytes:
                conn.execute(
                    "UPDATE clients SET status = 'expired' WHERE client_ip = ?",
                    (client_ip,),
                )
                status = "expired"

        if status not in ("new", "expired"):
            conn.commit()
            return False, "当前状态无法激活宽限期"

        # Enforce per-client grace limits for repeat activations.
        if grace_count > 0:
            if now - last_grace_at < GRACE_COOLDOWN_SECONDS:
                conn.commit()
                return False, "宽限期冷却中，请稍后再试"
            if grace_count >= GRACE_MAX_PER_24H and now - last_grace_at < 86400:
                conn.commit()
                return False, "24小时内宽限期激活次数已达上限"

        # Reset counter if the last activation was more than 24 hours ago; otherwise increment.
        if now - last_grace_at >= 86400:
            new_grace_count = 1
        else:
            new_grace_count = grace_count + 1

        grace_until = now + GRACE_DURATION_SECONDS
        conn.execute(
            """
            UPDATE clients
            SET status = 'grace',
                grace_until = ?,
                quota_bytes = ?,
                used_bytes = 0,
                reset_ipset_at = ?,
                grace_activated_count = ?,
                last_grace_activated_at = ?
            WHERE client_ip = ?
            """,
            (grace_until, GRACE_QUOTA_BYTES, now, new_grace_count, now, client_ip),
        )
        conn.commit()
    return True, {"grace_until": grace_until, "quota_bytes": GRACE_QUOTA_BYTES}


def mark_paid(client_ip, tx_hash, chain_id, source, quota_bytes, derivation_index=-1):
    """Record payment and authorize the client, stacking quotas on repeat payments."""
    now = int(time.time())
    conn = _db_conn()
    try:
        conn.isolation_level = None
        conn.execute("BEGIN IMMEDIATE")

        try:
            conn.execute(
                """
                INSERT INTO payments (client_ip, tx_hash, chain_id, source, paid_at, expires_at, quota_bytes, derivation_index)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    client_ip,
                    tx_hash.lower(),
                    chain_id,
                    source,
                    now,
                    now + ACCESS_DURATION,
                    quota_bytes,
                    derivation_index,
                ),
            )
        except sqlite3.IntegrityError:
            # Already recorded (chain_id, tx_hash) unique conflict.
            existing = conn.execute(
                "SELECT client_ip FROM payments WHERE chain_id = ? AND tx_hash = ?",
                (chain_id, tx_hash.lower()),
            ).fetchone()
            if not existing:
                conn.execute("ROLLBACK")
                raise
            # Duplicate payment: roll back and do not stack quota again.
            conn.execute("ROLLBACK")
            return

        conn.execute(
            """
            UPDATE pending_payments
            SET status = 'paid'
            WHERE client_ip = ? AND chain_id = ? AND derivation_index = ? AND status = 'pending'
            """,
            (client_ip, chain_id, derivation_index),
        )
        _ensure_client_row(conn, client_ip)
        conn.execute(
            """
            UPDATE clients
            SET status = 'paid',
                paid_until = MAX(paid_until, ?) + ?,
                quota_bytes = CASE WHEN status = 'grace' THEN ? ELSE quota_bytes + ? END,
                used_bytes = CASE WHEN status = 'grace' THEN 0 ELSE used_bytes END,
                reset_ipset_at = ?
            WHERE client_ip = ?
            """,
            (now, ACCESS_DURATION, quota_bytes, quota_bytes, now, client_ip),
        )
        conn.execute("COMMIT")
    finally:
        conn.isolation_level = ""
        conn.close()


def revoke(client_ip):
    """Remove a client record and its payment records (useful for testing or admin actions)."""
    with _db_conn() as conn:
        conn.execute("DELETE FROM payments WHERE client_ip = ?", (client_ip,))
        conn.execute("DELETE FROM pending_payments WHERE client_ip = ?", (client_ip,))
        conn.execute("DELETE FROM clients WHERE client_ip = ?", (client_ip,))


# ---------------------------------------------------------------------------
# Client identification
# ---------------------------------------------------------------------------
def get_client_ip():
    """Return the client IP.

    Trust X-Forwarded-For only when the direct connection comes from a trusted
    proxy to prevent spoofing by remote clients. Validate the forwarded IP.
    """
    remote = request.remote_addr or "unknown"
    forwarded = request.headers.get("X-Forwarded-For", "")
    try:
        remote_net = ipaddress.ip_network(remote, strict=False)
    except ValueError:
        remote_net = None

    if forwarded and remote_net and any(remote_net.subnet_of(proxy) for proxy in TRUSTED_PROXIES):
        candidate = forwarded.split(",")[0].strip()
        try:
            ipaddress.ip_address(candidate)
            return candidate
        except ValueError:
            pass
    return remote


# ---------------------------------------------------------------------------
# Payment verification
# ---------------------------------------------------------------------------
def get_chain_config(chain_id):
    """Return chain config or default chain if unknown."""
    return CHAINS.get(chain_id, CHAINS.get(DEFAULT_CHAIN, next(iter(CHAINS.values()))))


def _check_payment_items(items, tier_map, address, created_at, check_status=True):
    """Scan Blockscout items for a matching payment. Returns tuple or None.

    tier_map keys are now (token_symbol, amount_str) tuples.
    For price tolerance, we check if the received amount is within PRICE_TOLERANCE_PERCENT of expected.
    """
    for tx in items:
        to_addr = tx.get("to", {})
        if isinstance(to_addr, dict):
            to_addr = to_addr.get("hash", "")
        if to_addr.lower() != address.lower():
            continue
        if check_status and tx.get("status", "") != "ok":
            continue
        try:
            confirmations = int(tx.get("confirmations", "0") or "0")
        except (TypeError, ValueError):
            continue
        if confirmations < REQUIRED_CONFIRMATIONS:
            continue
        tx_timestamp = tx.get("timestamp", "")
        if tx_timestamp:
            try:
                dt = datetime.fromisoformat(tx_timestamp.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                tx_time = dt.timestamp()
                if tx_time < created_at - TX_TIMESTAMP_TOLERANCE:
                    continue
            except (ValueError, OverflowError):
                pass
        value = str(tx.get("value", "0"))
        token_label = tx.get("token", {}).get("symbol", "ETH") if tx.get("token") else "ETH"

        for (sym, expected_amount), quota in tier_map.items():
            if sym.upper() != token_label.upper():
                continue
            try:
                expected_val = int(expected_amount)
                received_val = int(value)
                if expected_val == 0:
                    continue
                tolerance = PRICE_TOLERANCE_PERCENT / 100.0
                if abs(received_val - expected_val) / expected_val <= tolerance:
                    return True, tx.get("hash"), quota, "Payment verified"
            except (ValueError, TypeError):
                if value == expected_amount:
                    return True, tx.get("hash"), quota, "Payment verified"
    return None


def verify_payment_on_chain(address, chain_id, client_ip=None, created_at=None):
    """Check Blockscout for an incoming payment to the unique deposit address.

    Returns (ok: bool, tx_hash: str|None, quota_bytes: int, message: str).
    The accepted amount can match any tier configured for the chain.
    Iterates through paginated Blockscout responses up to a page limit.
    """
    cfg = get_chain_config(chain_id)
    api_base = cfg["blockscout_api"]

    tokens = cfg.get("tokens", {"ETH": {"type": "native"}})
    needed_symbols = list(tokens.keys())
    prices = _price_service.get_prices(needed_symbols)

    tier_map = {}
    for tier in cfg["tiers"]:
        amount_usd = tier.get("amount_usd", 0)
        if amount_usd > 0:
            for sym, t_info in tokens.items():
                _, unit_val, _ = _price_service.convert_usd_to_token(amount_usd, sym, prices)
                if unit_val:
                    tier_map[(sym, unit_val)] = tier["quota_bytes"]
        if "amount_wei" in tier:
            tier_map[("ETH", tier["amount_wei"])] = tier.get("quota_bytes", 0)

    now = int(time.time())
    if created_at is None:
        created_at = now

    # Simple in-memory cache for Blockscout verification results to protect the
    # portal from slow repeated Blockscout calls for the same address.
    cache_key = (chain_id, address)
    cached = _verify_cache.get(cache_key, now)
    if cached is not None:
        return cached

    start_time = time.monotonic()

    def fetch_pages(base_url, checker=_check_payment_items):
        page_params = {}
        max_pages = 3
        for _ in range(max_pages):
            elapsed = time.monotonic() - start_time
            remaining = 15 - elapsed
            if remaining < 1:
                break
            try:
                resp = requests.get(
                    base_url,
                    params=page_params,
                    timeout=min(5, remaining),
                    headers={"Accept": "application/json"},
                )
                resp.raise_for_status()
                data = resp.json()
            except requests.RequestException as exc:
                return False, None, 0, f"Network error while contacting Blockscout: {exc}"

            result = checker(data.get("items") or [], tier_map, address, created_at)
            if result:
                return result

            next_params = data.get("next_page_params")
            if not next_params or not isinstance(next_params, dict):
                break
            page_params = next_params
        return None

    base_url = f"{api_base}/addresses/{address}/transactions"
    result = fetch_pages(base_url)
    if not result:
        internal_url = f"{api_base}/addresses/{address}/internal-transactions"
        result = fetch_pages(internal_url, checker=lambda items, tier_map, address, created_at: _check_payment_items(items, tier_map, address, created_at, check_status=False))

    if not result:
        result = (
            False,
            None,
            0,
            "Waiting for incoming transaction matching any tier amount and enough confirmations",
        )

    _verify_cache.set(cache_key, now, result)
    return result


# ---------------------------------------------------------------------------
# Captive portal detection endpoints
# ---------------------------------------------------------------------------
@app.route("/generate_204")
def generate_204():
    """Android/Google captive portal probe."""
    if is_paid(get_client_ip()):
        return "", 204
    return redirect("/", code=302)


@app.route("/hotspot-detect.html")
def apple_detect():
    """Apple iOS/macOS captive portal probe."""
    if is_paid(get_client_ip()):
        return "<HTML><HEAD><TITLE>Success</TITLE></HEAD><BODY>Success</BODY></HTML>"
    return redirect("/", code=302)


@app.route("/connecttest.txt")
def ms_connect_test():
    """Microsoft Windows captive portal probe."""
    if is_paid(get_client_ip()):
        return make_response("Microsoft Connect Test", 200, {"Content-Type": "text/plain"})
    return redirect("/", code=302)


# ---------------------------------------------------------------------------
# Main portal routes
# ---------------------------------------------------------------------------
def _selected_tier_index(cfg):
    """Parse the tier_index query parameter for the current request."""
    try:
        tier_index = int(request.args.get("tier", "0"))
    except (TypeError, ValueError):
        tier_index = 0
    tiers = cfg["tiers"]
    if not (0 <= tier_index < len(tiers)):
        tier_index = 0
    return tier_index


@app.route("/")
def index():
    """Welcome / payment page."""
    client_ip = get_client_ip()
    now = int(time.time())
    client_status = get_client_status(client_ip) or {
        "status": "new",
        "grace_until": 0,
        "paid_until": 0,
        "quota_bytes": 0,
        "used_bytes": 0,
    }

    # Paid or grace still active -> go to success page.
    if (
        client_status["status"] == "paid"
        and client_status["paid_until"] > now
        and client_status["used_bytes"] < client_status["quota_bytes"]
    ):
        return redirect("/success?chain=" + request.args.get("chain", DEFAULT_CHAIN), code=302)
    if (
        client_status["status"] == "grace"
        and client_status["grace_until"] > now
        and client_status["used_bytes"] < client_status["quota_bytes"]
    ):
        return redirect("/success?chain=" + request.args.get("chain", DEFAULT_CHAIN), code=302)

    # Grace/paid expired is shown as "expired" so the user can pay or re-activate grace.
    if client_status["status"] == "grace" and now >= client_status["grace_until"]:
        client_status["status"] = "expired"
        with _db_conn() as conn:
            conn.execute(
                "UPDATE clients SET status = 'expired' WHERE client_ip = ?",
                (client_ip,),
            )
    if client_status["status"] == "paid" and (
        now >= client_status["paid_until"]
        or client_status["used_bytes"] >= client_status["quota_bytes"]
    ):
        client_status["status"] = "expired"

    chain_id = request.args.get("chain", DEFAULT_CHAIN)
    if chain_id not in CHAINS:
        chain_id = DEFAULT_CHAIN
    cfg = CHAINS[chain_id]
    tier_index = _selected_tier_index(cfg)
    tier = cfg["tiers"][tier_index]

    token = request.args.get("token", DEFAULT_TOKEN).upper()
    tokens = cfg.get("tokens", {"ETH": {}})
    if token not in tokens:
        token = DEFAULT_TOKEN

    pending = get_or_create_pending_payment(client_ip, chain_id, tier_index, token=token)

    amount_usd = tier.get("amount_usd", 0)
    token_info = tokens.get(token, {"type": "native"})

    needed_symbols = set()
    for t in cfg.get("tiers", []):
        if "amount_usd" in t:
            for sym in tokens:
                needed_symbols.add(sym)
    needed_symbols.add(token)

    prices = _price_service.get_prices(list(needed_symbols))

    price_error = False
    if not prices and any("amount_usd" in t for t in cfg.get("tiers", [])):
        price_error = True

    amount_token, amount_unit, token_decimals = _price_service.convert_usd_to_token(
        amount_usd, token, prices
    )

    if amount_unit is None:
        amount_token = "0"
        amount_unit = "0"
        token_decimals = 18
        price_error = True

    token_price = prices.get(token, 0)

    config = {
        "ethAddress": pending["address"],
        "currentChain": chain_id,
        "chainId": cfg["chain_id"],
        "amountWei": str(amount_unit),
        "amountUsd": amount_usd,
        "amountToken": amount_token,
        "token": token,
        "tokenPrice": token_price,
        "tokenDecimals": token_decimals,
        "tokenType": token_info.get("type", "native"),
        "tokenAddress": token_info.get("address", ""),
        "quotaBytes": tier["quota_bytes"],
        "tierIndex": tier_index,
        "clientStatus": client_status["status"],
        "priceTolerance": PRICE_TOLERANCE_PERCENT,
        "priceLockMode": PRICE_LOCK_MODE,
        "priceError": price_error,
        "pollInterval": _env_int("POLL_INTERVAL_MS", 3000, min_val=500),
        "pollMaxInterval": _env_int("POLL_MAX_INTERVAL_MS", 30000, min_val=1000),
        "redirectDelay": _env_int("REDIRECT_DELAY_MS", 1000, min_val=100),
        "lowBytesThreshold": _env_int("LOW_BYTES_THRESHOLD", 52428800, min_val=1048576),
        "lowTimeThreshold": _env_int("LOW_TIME_THRESHOLD", 600, min_val=10),
        "fetchTimeout": _env_int("FETCH_TIMEOUT_MS", 20000, min_val=1000),
    }

    enriched_tiers = []
    for i, t in enumerate(cfg.get("tiers", [])):
        t_usd = t.get("amount_usd", 0)
        t_amount, t_unit, _ = _price_service.convert_usd_to_token(t_usd, token, prices)
        enriched_tiers.append({
            "amount_usd": t_usd,
            "amount_token": t_amount or "0",
            "amount_unit": str(t_unit or "0"),
            "quota_bytes": t["quota_bytes"],
            "index": i,
        })

    return render_template(
        "index.html",
        eth_address=pending["address"],
        config=config,
        chain_id=chain_id,
        chain_name=cfg["name"],
        chain_chain_id=cfg["chain_id"],
        amount_token=amount_token,
        amount_unit=amount_unit,
        amount_usd=amount_usd,
        token=token,
        token_price=token_price,
        quota_bytes=tier["quota_bytes"],
        tier_index=tier_index,
        tiers=enriched_tiers,
        tokens=tokens,
        client_status=client_status,
        grace_duration=GRACE_DURATION_SECONDS,
        grace_quota=GRACE_QUOTA_BYTES,
        portal_title=PORTAL_TITLE,
        portal_welcome=PORTAL_WELCOME,
        portal_lead=PORTAL_LEAD,
        portal_footer=PORTAL_FOOTER,
        portal_support_url=PORTAL_SUPPORT_URL,
        portal_logo_url=PORTAL_LOGO_URL,
        price_error=price_error,
    )


@app.route("/success")
def success():
    """Page shown after payment is confirmed."""
    client_ip = get_client_ip()
    if not is_paid(client_ip):
        return redirect("/", code=302)
    client_status = get_client_status(client_ip) or {}
    chain_id = request.args.get("chain", DEFAULT_CHAIN)
    if chain_id not in CHAINS:
        chain_id = DEFAULT_CHAIN
    cfg = CHAINS[chain_id]
    return render_template(
        "success.html",
        chain_name=cfg["name"],
        chain_icon=cfg["icon"],
        client_status=client_status,
        status_poll_interval=_env_int("STATUS_POLL_INTERVAL_MS", 3000, min_val=500),
        network_check_interval=_env_int("NETWORK_CHECK_INTERVAL_MS", 3000, min_val=500),
    )


@app.route("/api/health")
def health_check():
    checks = {}
    try:
        with _db_conn() as conn:
            conn.execute("SELECT 1")
        checks["database"] = "ok"
    except Exception:
        checks["database"] = "error"
    return jsonify(checks), 200 if all(v == "ok" for v in checks.values()) else 503


@app.route("/api/chains")
def list_chains():
    """Return supported chains and their payment tiers with prices."""
    needed_symbols = set()
    for c in CHAINS.values():
        for t in c.get("tiers", []):
            if "amount_usd" in t:
                for sym in c.get("tokens", {"ETH": {}}):
                    needed_symbols.add(sym)
    prices = _price_service.get_prices(list(needed_symbols)) if needed_symbols else {}

    result = {}
    for chain_id, c in CHAINS.items():
        tokens = c.get("tokens", {"ETH": {"type": "native"}})
        enriched_tiers = []
        for t in c.get("tiers", []):
            tier_data = {"quota_bytes": t["quota_bytes"], "index": len(enriched_tiers)}
            if "amount_usd" in t:
                tier_data["amount_usd"] = t["amount_usd"]
                for sym in tokens:
                    _, unit_val, dec = _price_service.convert_usd_to_token(t["amount_usd"], sym, prices)
                    tier_data[f"amount_{sym.lower()}"] = unit_val or "0"
                    tier_data[f"decimals_{sym.lower()}"] = dec
            if "amount_wei" in t:
                tier_data["amount_wei"] = t["amount_wei"]
                tier_data["amount_eth"] = t.get("amount_eth", "0")
            enriched_tiers.append(tier_data)
        result[chain_id] = {
            "name": c["name"],
            "chain_id": c["chain_id"],
            "block_time": c["block_time"],
            "recommended": c["recommended"],
            "icon": c["icon"],
            "tokens": {sym: {"type": info.get("type", "native"), "address": info.get("address", "")} for sym, info in tokens.items()},
            "tiers": enriched_tiers,
        }
    return jsonify(result)


@app.route("/api/status")
def api_status():
    """Return current client authorization status."""
    client_ip = get_client_ip()
    chain_id = request.args.get("chain", DEFAULT_CHAIN)
    if chain_id not in CHAINS:
        chain_id = DEFAULT_CHAIN
    status = get_client_status(client_ip) or {
        "status": "new",
        "grace_until": 0,
        "paid_until": 0,
        "quota_bytes": 0,
        "used_bytes": 0,
    }
    remaining_bytes = max(0, status["quota_bytes"] - status["used_bytes"])
    return jsonify(
        {
            "status": status["status"],
            "grace_until": status["grace_until"],
            "paid_until": status["paid_until"],
            "quota_bytes": status["quota_bytes"],
            "used_bytes": status["used_bytes"],
            "remaining_bytes": remaining_bytes,
            "current_chain": chain_id,
        }
    )


@app.route("/api/qr")
def qr_image():
    """Generate the payment QR code locally and return it as a PNG image."""
    client_ip = get_client_ip()
    chain_id = request.args.get("chain", DEFAULT_CHAIN)
    address = request.args.get("address", "")
    amount_unit = request.args.get("amount_unit")
    token = request.args.get("token", DEFAULT_TOKEN).upper()
    cfg = get_chain_config(chain_id)

    now = int(time.time())
    with _db_conn() as conn:
        row = conn.execute(
            """
            SELECT amount_wei, tier_index
            FROM pending_payments
            WHERE client_ip = ? AND chain_id = ? AND address = ? AND status = 'pending' AND expires_at > ?
            """,
            (client_ip, chain_id, address, now),
        ).fetchone()
    if not row:
        return make_response("Forbidden: address does not belong to active pending payment", 403)

    tokens = cfg.get("tokens", {"ETH": {"type": "native"}})
    token_info = tokens.get(token, {"type": "native"})

    needed_symbols = list(tokens.keys())
    prices = _price_service.get_prices(needed_symbols)

    tier_index = row[1]
    tier = cfg["tiers"][tier_index] if tier_index < len(cfg["tiers"]) else cfg["tiers"][0]
    amount_usd = tier.get("amount_usd", 0)

    amount_token_str, amount_unit_val, _ = _price_service.convert_usd_to_token(amount_usd, token, prices)

    if amount_unit_val is None:
        amount_unit_val = row[0]

    if token_info.get("type") == "erc20":
        token_addr = token_info.get("address", "")
        erc20_amount = int(amount_unit_val) if amount_unit_val else 0
        uri = f"ethereum:{token_addr}@{cfg['chain_id']}?function=transfer(address,uint256)&address={address}&uint256={erc20_amount}"
    else:
        uri = f"ethereum:{address}@{cfg['chain_id']}?value={amount_unit_val}"

    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        box_size=10,
        border=4,
    )
    qr.add_data(uri)
    qr.make(fit=True)

    img = qr.make_image(fill_color=QR_FILL_COLOR, back_color=QR_BACK_COLOR)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return send_file(buf, mimetype="image/png")


def _require_same_origin():
    """Return a 403 response if the request does not come from the same origin."""
    allowed = request.scheme + "://" + request.host
    origin = request.headers.get("Origin") or request.headers.get("Referer") or ""
    parsed = urlparse(origin)
    if f"{parsed.scheme}://{parsed.netloc}" != allowed:
        return jsonify({"error": "Forbidden: invalid origin"}), 403
    return None


@app.route("/api/activate-grace", methods=["POST"])
def api_activate_grace():
    """Activate the short grace period for the requesting client."""
    forbidden = _require_same_origin()
    if forbidden:
        return forbidden
    client_ip = get_client_ip()
    ok, result = activate_grace(client_ip)
    if not ok:
        return jsonify({"ok": False, "error": result}), 429
    return jsonify({"ok": True, "grace_until": result["grace_until"], "quota_bytes": result["quota_bytes"]})


@app.route("/api/check-payment", methods=["POST"])
def check_payment():
    """Return the current payment status for the requesting client.

    Checks every active pending payment address for this client/chain so that
    switching tiers after a payment still detects the incoming transaction.
    """
    forbidden = _require_same_origin()
    if forbidden:
        return forbidden

    client_ip = get_client_ip()
    chain_id = request.args.get("chain", DEFAULT_CHAIN)

    if chain_id not in CHAINS:
        return jsonify({"paid": False, "error": "Unsupported chain"}), 400

    if is_paid(client_ip):
        client_status = get_client_status(client_ip) or {}
        return jsonify(
            {
                "paid": True,
                "status": client_status.get("status", "paid"),
            }
        )

    pending_list = get_active_pending_payments(client_ip, chain_id)
    if not pending_list:
        return jsonify(
            {"paid": False, "error": "No pending payment found"}
        ), 404

    last_message = ""
    for pending in pending_list:
        ok, tx_hash, quota_bytes, last_message = verify_payment_on_chain(
            pending["address"], chain_id, client_ip=client_ip, created_at=pending["created_at"]
        )
        if ok:
            mark_paid(client_ip, tx_hash, chain_id, "blockscout", quota_bytes, pending["derivation_index"])
            return jsonify(
                {
                    "paid": True,
                    "status": "paid",
                    "tx_hash": tx_hash,
                    "quota_bytes": quota_bytes,
                }
            )

    pending = pending_list[0]
    return jsonify(
        {
            "paid": False,
            "address": pending["address"],
            "amount_wei": pending["amount_wei"],
            "message": last_message,
        }
    )


# Register simulate-payment only in dev mode so it cannot be enabled by accident.
if DEV_MODE:

    @app.route("/api/simulate-payment", methods=["POST"])
    def simulate_payment():
        """Development helper: mark the client as paid without a real tx.

        Restricted to a dev token to avoid exposing a backdoor when DEV_MODE is on.
        """
        dev_token = request.headers.get("X-Dev-Token", "")
        if not CAPTIVE_DEV_TOKEN or not secrets.compare_digest(dev_token, CAPTIVE_DEV_TOKEN):
            return jsonify({"paid": False, "error": "Forbidden: invalid or missing X-Dev-Token; set CAPTIVE_DEV_TOKEN"}), 403

        client_ip = get_client_ip()
        chain_id = request.args.get("chain", DEFAULT_CHAIN)
        if chain_id not in CHAINS:
            return jsonify({"paid": False, "error": "Unsupported chain"}), 400
        cfg = CHAINS[chain_id]
        tier_index = _selected_tier_index(cfg)
        tier = cfg["tiers"][tier_index]
        if is_paid(client_ip):
            return jsonify({"paid": True, "client_ip": client_ip, "quota_bytes": tier["quota_bytes"]})
        fake_hash = "0x" + secrets.token_hex(32)
        mark_paid(client_ip, fake_hash, chain_id, "dev", tier["quota_bytes"])
        return jsonify(
            {"paid": True, "client_ip": client_ip, "quota_bytes": tier["quota_bytes"]}
        )


@app.errorhandler(500)
def handle_500(e):
    return jsonify({"error": "Internal server error"}), 500


# ---------------------------------------------------------------------------
# Entry point for local development only. Production uses gunicorn.
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    # DEV_MODE defaults to localhost to avoid exposing the Werkzeug dev server on
    # all interfaces; production binds to 0.0.0.0.  Debug is always disabled so the
    # interactive Werkzeug debugger (which allows remote code execution) is never on.
    default_host = "127.0.0.1" if DEV_MODE else "0.0.0.0"
    host = os.environ.get("FLASK_HOST", default_host)
    port = _env_int("FLASK_PORT", 5000, min_val=1, max_val=65535)
    app.run(host=host, port=port, debug=False, use_reloader=DEV_MODE)
