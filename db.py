"""Database schema, migrations, and query functions."""

import logging
import sqlite3
import time
from pathlib import Path

from config import (
    DB_BUSY_TIMEOUT,
    DB_PATH,
    PENDING_CLEANUP_DAYS,
    PAYMENTS_CLEANUP_DAYS,
    ACCESS_DURATION,
    MAX_ACCESS_DURATION,
    MAX_QUOTA_BYTES,
    PAYMENT_PENDING_DURATION,
    GRACE_DURATION_SECONDS,
    GRACE_QUOTA_BYTES,
    GRACE_MAX_PER_24H,
    GRACE_COOLDOWN_SECONDS,
    DEFAULT_TOKEN,
)
from chains import CHAINS, DEFAULT_CHAIN

logger = logging.getLogger(__name__)


def _db_conn():
    return sqlite3.connect(DB_PATH, timeout=DB_BUSY_TIMEOUT / 1000)


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
        logger.exception("Migration rollback for payments unique constraint")
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


def _next_derivation_index(conn):
    """Atomically reserve and return the next derivation index."""
    conn.execute(
        "UPDATE derivation_counter SET next_index = next_index + 1 WHERE id = 1"
    )
    row = conn.execute(
        "SELECT next_index - 1 FROM derivation_counter WHERE id = 1"
    ).fetchone()
    idx = row[0]
    if idx > 0x7FFFFFFF:
        raise RuntimeError('BIP44 derivation index exhausted')
    return idx


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


def update_client_status(client_ip, status):
    """Update client status column."""
    with _db_conn() as conn:
        conn.execute(
            "UPDATE clients SET status = ? WHERE client_ip = ?",
            (status, client_ip),
        )


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


def get_or_create_pending_payment(client_ip, chain_id, tier_index=0, token=None, price_service=None):
    """Reuse an active pending payment or create a new derived address record."""
    from chains import get_chain_config, needed_token_symbols
    from wallet import derive_address

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

    symbols = needed_token_symbols(cfg)
    symbols.add(token)
    prices = price_service.get_prices(list(symbols))

    if "amount_wei" in tier:
        amount_wei = tier["amount_wei"]
    elif "amount_usd" in tier:
        token_info = tokens.get(token, {"type": "native"})
        token_decimals = token_info.get("decimals", 18 if token_info.get("type") == "native" else 6)
        _, unit_val, _ = price_service.convert_usd_to_token(tier["amount_usd"], token, prices, token_decimals)
        amount_wei = str(unit_val or "0")
    else:
        amount_wei = "0"

    now = int(time.time())
    expires = now + PAYMENT_PENDING_DURATION
    conn = _db_conn()
    try:
        conn.isolation_level = None
        conn.execute("BEGIN IMMEDIATE")

        conn.execute('SAVEPOINT sp_pending')
        try:
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
                conn.execute('RELEASE sp_pending')
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
            cur = conn.execute(
                """
                INSERT INTO pending_payments
                    (client_ip, chain_id, address, derivation_index, amount_wei, quota_bytes, tier_index, status, created_at, expires_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                """,
                (client_ip, chain_id, address, index, amount_wei, tier["quota_bytes"], tier_index, now, expires),
            )
            conn.execute('RELEASE sp_pending')
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
            conn.execute('ROLLBACK TO sp_pending')
            conn.execute('RELEASE sp_pending')
            logger.warning("IntegrityError race in get_or_create_pending_payment for %s/%s tier=%s", client_ip, chain_id, tier_index)
            existing = get_active_pending_payment(client_ip, chain_id, tier_index)
            if existing:
                conn.execute("COMMIT")
                return existing
            conn.execute("COMMIT")
            raise
    finally:
        conn.isolation_level = None
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
    conn = _db_conn()
    try:
        conn.isolation_level = None
        conn.execute("BEGIN IMMEDIATE")
        status = _ensure_client_row(conn, client_ip)
        # Read after BEGIN IMMEDIATE to get a fresh snapshot and avoid stale-data races.
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
        logger.info("Grace activated for %s: until=%s quota=%s", client_ip, grace_until, GRACE_QUOTA_BYTES)
        return True, {"grace_until": grace_until, "quota_bytes": GRACE_QUOTA_BYTES}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.isolation_level = None
        conn.close()


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
            existing = conn.execute(
                "SELECT client_ip FROM payments WHERE chain_id = ? AND tx_hash = ?",
                (chain_id, tx_hash.lower()),
            ).fetchone()
            if not existing:
                logger.exception("Unexpected IntegrityError in mark_paid for tx %s", tx_hash)
                conn.execute("ROLLBACK")
                raise
            logger.info("Payment already recorded: tx=%s chain=%s", tx_hash, chain_id)
            conn.execute("ROLLBACK")
            return True

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
                paid_until = MIN(MAX(paid_until, ?) + ?, ?),
                quota_bytes = MIN(
                    CASE WHEN status = 'grace' THEN ? ELSE quota_bytes + ? END,
                    ?
                ),
                used_bytes = CASE WHEN status = 'grace' THEN 0 ELSE used_bytes END,
                reset_ipset_at = ?
            WHERE client_ip = ?
            """,
            (now, ACCESS_DURATION, now + MAX_ACCESS_DURATION, quota_bytes, quota_bytes, MAX_QUOTA_BYTES, now, client_ip),
        )
        conn.execute("COMMIT")
        logger.info("Payment recorded for %s: tx=%s chain=%s quota=%s", client_ip, tx_hash, chain_id, quota_bytes)
    finally:
        conn.isolation_level = None
        conn.close()


def revoke(client_ip):
    """Remove a client record and its payment records (useful for testing or admin actions)."""
    with _db_conn() as conn:
        conn.execute("DELETE FROM payments WHERE client_ip = ?", (client_ip,))
        conn.execute("DELETE FROM pending_payments WHERE client_ip = ?", (client_ip,))
        conn.execute("DELETE FROM clients WHERE client_ip = ?", (client_ip,))
