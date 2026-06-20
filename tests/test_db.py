"""Tests for db.py using a temporary SQLite file (shared across calls)."""

import os
import sqlite3
import tempfile
import time
import unittest

import db as db_mod


def _setup_temp_db():
    """Create a temp SQLite file and wire db module to use it."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db_mod.DB_PATH = path
    return path


class TestInitDb(unittest.TestCase):
    """Tests for init_db()."""

    def setUp(self):
        self._db_path = _setup_temp_db()
        db_mod.init_db()

    def tearDown(self):
        os.unlink(self._db_path)

    def test_payments_table_exists(self):
        conn = db_mod._db_conn()
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        table_names = [t[0] for t in tables]
        self.assertIn("payments", table_names)

    def test_clients_table_exists(self):
        conn = db_mod._db_conn()
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        table_names = [t[0] for t in tables]
        self.assertIn("clients", table_names)

    def test_pending_payments_table_exists(self):
        conn = db_mod._db_conn()
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        table_names = [t[0] for t in tables]
        self.assertIn("pending_payments", table_names)

    def test_derivation_counter_table_exists(self):
        conn = db_mod._db_conn()
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        table_names = [t[0] for t in tables]
        self.assertIn("derivation_counter", table_names)

    def test_payments_has_chain_id_tx_hash_unique(self):
        conn = db_mod._db_conn()
        idx_info = conn.execute("PRAGMA index_list(payments)").fetchall()
        for idx in idx_info:
            if idx[2]:  # unique
                info = conn.execute(f"PRAGMA index_info({idx[1]})").fetchall()
                cols = tuple(r[2] for r in info)
                if cols == ("chain_id", "tx_hash"):
                    return
        self.fail("No unique index on (chain_id, tx_hash) in payments table")

    def test_payments_has_derivation_index_column(self):
        conn = db_mod._db_conn()
        cols = conn.execute("PRAGMA table_info(payments)").fetchall()
        col_names = [c[1] for c in cols]
        self.assertIn("derivation_index", col_names)


class TestEnsureClientRow(unittest.TestCase):
    """Tests for _ensure_client_row()."""

    def setUp(self):
        self._db_path = _setup_temp_db()
        db_mod.init_db()

    def tearDown(self):
        os.unlink(self._db_path)

    def test_creates_new_client(self):
        conn = db_mod._db_conn()
        status = db_mod._ensure_client_row(conn, "192.168.1.100")
        self.assertEqual(status, "new")

    def test_existing_client_returns_status(self):
        conn = db_mod._db_conn()
        db_mod._ensure_client_row(conn, "192.168.1.101")
        conn.execute("UPDATE clients SET status = 'paid' WHERE client_ip = '192.168.1.101'")
        status = db_mod._ensure_client_row(conn, "192.168.1.101")
        self.assertEqual(status, "paid")

    def test_idempotent_insert(self):
        conn = db_mod._db_conn()
        db_mod._ensure_client_row(conn, "192.168.1.102")
        db_mod._ensure_client_row(conn, "192.168.1.102")
        row = conn.execute("SELECT COUNT(*) FROM clients WHERE client_ip = '192.168.1.102'").fetchone()
        self.assertEqual(row[0], 1)


class TestIsPaid(unittest.TestCase):
    """Tests for is_paid()."""

    def setUp(self):
        self._db_path = _setup_temp_db()
        db_mod.init_db()

    def tearDown(self):
        os.unlink(self._db_path)

    def test_new_client_not_paid(self):
        self.assertFalse(db_mod.is_paid("10.0.0.1"))

    def test_paid_client_is_paid(self):
        now = int(time.time())
        with db_mod._db_conn() as conn:
            conn.execute(
                "INSERT INTO clients (client_ip, first_seen, status, paid_until, quota_bytes, used_bytes) "
                "VALUES (?, ?, 'paid', ?, ?, 0)",
                ("10.0.0.2", now, now + 86400, 100000),
            )
        self.assertTrue(db_mod.is_paid("10.0.0.2"))

    def test_paid_client_expired_not_paid(self):
        now = int(time.time())
        with db_mod._db_conn() as conn:
            conn.execute(
                "INSERT INTO clients (client_ip, first_seen, status, paid_until, quota_bytes, used_bytes) "
                "VALUES (?, ?, 'paid', ?, ?, 0)",
                ("10.0.0.3", now, now - 100, 100000),
            )
        self.assertFalse(db_mod.is_paid("10.0.0.3"))

    def test_paid_client_quota_exhausted_not_paid(self):
        now = int(time.time())
        with db_mod._db_conn() as conn:
            conn.execute(
                "INSERT INTO clients (client_ip, first_seen, status, paid_until, quota_bytes, used_bytes) "
                "VALUES (?, ?, 'paid', ?, ?, ?)",
                ("10.0.0.4", now, now + 86400, 100000, 100000),
            )
        self.assertFalse(db_mod.is_paid("10.0.0.4"))

    def test_grace_client_is_paid(self):
        now = int(time.time())
        with db_mod._db_conn() as conn:
            conn.execute(
                "INSERT INTO clients (client_ip, first_seen, status, grace_until, quota_bytes, used_bytes) "
                "VALUES (?, ?, 'grace', ?, ?, 0)",
                ("10.0.0.5", now, now + 300, 100000),
            )
        self.assertTrue(db_mod.is_paid("10.0.0.5"))

    def test_grace_client_expired_not_paid(self):
        now = int(time.time())
        with db_mod._db_conn() as conn:
            conn.execute(
                "INSERT INTO clients (client_ip, first_seen, status, grace_until, quota_bytes, used_bytes) "
                "VALUES (?, ?, 'grace', ?, ?, 0)",
                ("10.0.0.6", now, now - 10, 100000),
            )
        self.assertFalse(db_mod.is_paid("10.0.0.6"))


class TestActivateGrace(unittest.TestCase):
    """Tests for activate_grace()."""

    def setUp(self):
        self._db_path = _setup_temp_db()
        db_mod.init_db()

    def tearDown(self):
        os.unlink(self._db_path)

    def test_grace_activation_new_client(self):
        ok, result = db_mod.activate_grace("10.0.0.10")
        self.assertTrue(ok)
        self.assertIn("grace_until", result)
        self.assertIn("quota_bytes", result)
        self.assertGreater(result["grace_until"], 0)
        self.assertGreater(result["quota_bytes"], 0)

    def test_grace_already_active(self):
        ok, _ = db_mod.activate_grace("10.0.0.11")
        self.assertTrue(ok)
        ok2, msg = db_mod.activate_grace("10.0.0.11")
        self.assertFalse(ok2)

    def test_grace_respects_cooldown(self):
        now = int(time.time())
        with db_mod._db_conn() as conn:
            conn.execute(
                "INSERT INTO clients (client_ip, first_seen, status, grace_activated_count, last_grace_activated_at) "
                "VALUES (?, ?, 'new', 1, ?)",
                ("10.0.0.12", now, now - 10),
            )
        ok, msg = db_mod.activate_grace("10.0.0.12")
        self.assertFalse(ok)
        self.assertIn("冷却", str(msg))

    def test_grace_quota_is_positive(self):
        ok, result = db_mod.activate_grace("10.0.0.13")
        self.assertTrue(ok)
        self.assertGreater(result["quota_bytes"], 0)

    def test_grace_sets_client_status(self):
        db_mod.activate_grace("10.0.0.14")
        status = db_mod.get_client_status("10.0.0.14")
        self.assertEqual(status["status"], "grace")


class TestMarkPaid(unittest.TestCase):
    """Tests for mark_paid()."""

    def setUp(self):
        self._db_path = _setup_temp_db()
        db_mod.init_db()

    def tearDown(self):
        os.unlink(self._db_path)

    def test_mark_paid_creates_record(self):
        db_mod.mark_paid("10.0.0.20", "0xabc123", "base", "test", 50000)
        with db_mod._db_conn() as conn:
            row = conn.execute("SELECT * FROM payments WHERE tx_hash = '0xabc123'").fetchone()
            self.assertIsNotNone(row)

    def test_mark_paid_updates_client_status(self):
        db_mod.mark_paid("10.0.0.21", "0xdef456", "base", "test", 50000)
        with db_mod._db_conn() as conn:
            row = conn.execute("SELECT status FROM clients WHERE client_ip = '10.0.0.21'").fetchone()
            self.assertEqual(row[0], "paid")

    def test_duplicate_payment_does_not_crash(self):
        db_mod.mark_paid("10.0.0.22", "0x789xyz", "base", "test", 50000)
        db_mod.mark_paid("10.0.0.23", "0x789xyz", "base", "test", 50000)

    def test_mark_paid_grace_to_paid_transition(self):
        now = int(time.time())
        with db_mod._db_conn() as conn:
            conn.execute(
                "INSERT INTO clients (client_ip, first_seen, status, grace_until, quota_bytes, used_bytes) "
                "VALUES (?, ?, 'grace', ?, ?, 0)",
                ("10.0.0.24", now, now + 300, 100000),
            )
        db_mod.mark_paid("10.0.0.24", "0xgrace2paid", "base", "test", 200000)
        with db_mod._db_conn() as conn:
            row = conn.execute("SELECT status, quota_bytes FROM clients WHERE client_ip = '10.0.0.24'").fetchone()
            self.assertEqual(row[0], "paid")


class TestNextDerivationIndex(unittest.TestCase):
    """Tests for _next_derivation_index()."""

    def setUp(self):
        self._db_path = _setup_temp_db()
        db_mod.init_db()

    def tearDown(self):
        os.unlink(self._db_path)

    def test_first_index_is_zero(self):
        conn = db_mod._db_conn()
        idx = db_mod._next_derivation_index(conn)
        self.assertEqual(idx, 0)

    def test_increments_atomically(self):
        conn = db_mod._db_conn()
        idx1 = db_mod._next_derivation_index(conn)
        idx2 = db_mod._next_derivation_index(conn)
        self.assertEqual(idx2, idx1 + 1)

    def test_multiple_increments(self):
        conn = db_mod._db_conn()
        indices = [db_mod._next_derivation_index(conn) for _ in range(10)]
        for i in range(1, len(indices)):
            self.assertEqual(indices[i], indices[i - 1] + 1)


class TestGetOrCreatePendingPayment(unittest.TestCase):
    """Tests for get_or_create_pending_payment()."""

    def setUp(self):
        self._db_path = _setup_temp_db()
        db_mod.init_db()
        self.mock_price_service = _MockPriceService()

    def tearDown(self):
        os.unlink(self._db_path)

    def test_creates_new_pending_payment(self):
        result = db_mod.get_or_create_pending_payment(
            "10.0.0.30", "base", tier_index=0,
            token="ETH", price_service=self.mock_price_service,
        )
        self.assertIn("address", result)
        self.assertIn("derivation_index", result)
        self.assertEqual(result["status"], "pending")

    def test_reuses_existing_pending_payment(self):
        r1 = db_mod.get_or_create_pending_payment(
            "10.0.0.31", "base", tier_index=0,
            token="ETH", price_service=self.mock_price_service,
        )
        r2 = db_mod.get_or_create_pending_payment(
            "10.0.0.31", "base", tier_index=0,
            token="ETH", price_service=self.mock_price_service,
        )
        self.assertEqual(r1["id"], r2["id"])
        self.assertEqual(r1["address"], r2["address"])

    def test_different_tiers_get_different_addresses(self):
        r1 = db_mod.get_or_create_pending_payment(
            "10.0.0.32", "base", tier_index=0,
            token="ETH", price_service=self.mock_price_service,
        )
        r2 = db_mod.get_or_create_pending_payment(
            "10.0.0.32", "base", tier_index=1,
            token="ETH", price_service=self.mock_price_service,
        )
        self.assertNotEqual(r1["id"], r2["id"])
        self.assertNotEqual(r1["address"], r2["address"])

    def test_different_clients_get_different_addresses(self):
        r1 = db_mod.get_or_create_pending_payment(
            "10.0.0.33", "base", tier_index=0,
            token="ETH", price_service=self.mock_price_service,
        )
        r2 = db_mod.get_or_create_pending_payment(
            "10.0.0.34", "base", tier_index=0,
            token="ETH", price_service=self.mock_price_service,
        )
        self.assertNotEqual(r1["address"], r2["address"])


class TestGetClientStatus(unittest.TestCase):
    """Tests for get_client_status()."""

    def setUp(self):
        self._db_path = _setup_temp_db()
        db_mod.init_db()

    def tearDown(self):
        os.unlink(self._db_path)

    def test_returns_none_for_unknown_client(self):
        self.assertIsNone(db_mod.get_client_status("10.0.0.99"))

    def test_returns_status_for_known_client(self):
        db_mod.activate_grace("10.0.0.98")
        status = db_mod.get_client_status("10.0.0.98")
        self.assertIsNotNone(status)
        self.assertEqual(status["status"], "grace")


class TestRevoke(unittest.TestCase):
    """Tests for revoke()."""

    def setUp(self):
        self._db_path = _setup_temp_db()
        db_mod.init_db()

    def tearDown(self):
        os.unlink(self._db_path)

    def test_revoke_removes_client(self):
        db_mod.activate_grace("10.0.0.50")
        self.assertIsNotNone(db_mod.get_client_status("10.0.0.50"))
        db_mod.revoke("10.0.0.50")
        self.assertIsNone(db_mod.get_client_status("10.0.0.50"))


class _MockPriceService:
    def get_prices(self, symbols):
        return {"ETH": 3000.0, "USDC": 1.0, "USDT": 1.0}

    def convert_usd_to_token(self, usd_amount, token_symbol, prices, decimals=18):
        price = prices.get(token_symbol.upper(), 0)
        if not price:
            return None, None, None
        amount = usd_amount / price
        smallest = int(round(amount * 10 ** decimals))
        return f"{amount:.{decimals}f}", str(smallest), decimals


class TestMigratePaymentsUniqueConstraint(unittest.TestCase):
    """Tests for _migrate_payments_unique_constraint()."""

    def setUp(self):
        self._db_path = _setup_temp_db()
        db_mod._db_conn().execute(
            "PRAGMA journal_mode=WAL"
        )

    def tearDown(self):
        os.unlink(self._db_path)

    def _create_old_schema(self):
        conn = db_mod._db_conn()
        conn.execute(
            """
            CREATE TABLE payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_ip TEXT NOT NULL,
                tx_hash TEXT NOT NULL,
                chain_id TEXT NOT NULL,
                source TEXT NOT NULL,
                paid_at INTEGER NOT NULL,
                expires_at INTEGER NOT NULL,
                quota_bytes INTEGER NOT NULL DEFAULT 0,
                derivation_index INTEGER NOT NULL DEFAULT -1
            )
            """
        )
        conn.execute(
            "CREATE UNIQUE INDEX idx_payments_tx_hash ON payments(tx_hash)"
        )
        conn.commit()

    def test_migration_creates_composite_unique(self):
        self._create_old_schema()
        conn = db_mod._db_conn()
        conn.execute(
            "INSERT INTO payments (client_ip, tx_hash, chain_id, source, paid_at, expires_at) "
            "VALUES ('10.0.0.1', '0xaaa', 'base', 'test', 1000, 2000)"
        )
        conn.commit()

        db_mod._migrate_payments_unique_constraint(conn)

        indexes = conn.execute("PRAGMA index_list(payments)").fetchall()
        composite_found = False
        old_found = False
        for idx in indexes:
            if not idx[2]:
                continue
            info = conn.execute(f"PRAGMA index_info({idx[1]})").fetchall()
            cols = tuple(r[2] for r in info)
            if cols == ("chain_id", "tx_hash"):
                composite_found = True
            elif cols == ("tx_hash",):
                old_found = True
        self.assertTrue(composite_found, "composite unique index missing")
        self.assertFalse(old_found, "old tx_hash unique index still present")

        row = conn.execute(
            "SELECT client_ip, tx_hash, chain_id FROM payments WHERE tx_hash = '0xaaa'"
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], "10.0.0.1")
        self.assertEqual(row[2], "base")

    def test_migration_noop_when_composite_exists(self):
        self._create_old_schema()
        conn = db_mod._db_conn()
        conn.execute(
            "DROP INDEX idx_payments_tx_hash"
        )
        conn.execute(
            "CREATE UNIQUE INDEX idx_payments_chain_tx ON payments(chain_id, tx_hash)"
        )
        conn.execute(
            "INSERT INTO payments (client_ip, tx_hash, chain_id, source, paid_at, expires_at) "
            "VALUES ('10.0.0.2', '0xbbb', 'base', 'test', 1000, 2000)"
        )
        conn.commit()

        db_mod._migrate_payments_unique_constraint(conn)

        row = conn.execute(
            "SELECT client_ip FROM payments WHERE tx_hash = '0xbbb'"
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], "10.0.0.2")


if __name__ == "__main__":
    unittest.main()
