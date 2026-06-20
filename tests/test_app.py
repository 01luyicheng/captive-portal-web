"""Integration tests for app.py Flask application."""

import os
import sqlite3
import tempfile
import time
import unittest

# Set required env vars before importing any app modules
os.environ["CAPTIVE_HD_SEED"] = "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about"

import db as db_mod

# Create temp DB and patch before importing app
_fd, _db_path = tempfile.mkstemp(suffix=".db")
os.close(_fd)
db_mod.DB_PATH = _db_path
db_mod.init_db()

from app import app, get_client_ip

_ip_counter = 0


def _fresh_ip():
    """Generate a unique test IP for isolation."""
    global _ip_counter
    _ip_counter += 1
    return f"10.0.{_ip_counter // 256}.{_ip_counter % 256}"


class _TestBase(unittest.TestCase):
    """Base class that ensures clean DB state for each test."""

    def setUp(self):
        self.client = app.test_client()
        self.ip = _fresh_ip()
        with db_mod._db_conn() as conn:
            conn.execute("DELETE FROM clients")
            conn.execute("DELETE FROM payments")
            conn.execute("DELETE FROM pending_payments")

    def _insert_paid_client(self, ip="127.0.0.1"):
        now = int(time.time())
        with db_mod._db_conn() as conn:
            conn.execute(
                "INSERT INTO clients (client_ip, first_seen, status, paid_until, quota_bytes, used_bytes) "
                "VALUES (?, ?, 'paid', ?, ?, 0)",
                (ip, now, now + 86400, 1000000),
            )

    def _get_status_code(self, resp):
        return resp.status_code


class TestCreateApp(_TestBase):
    """Tests for Flask app factory."""

    def test_app_exists(self):
        self.assertIsNotNone(app)

    def test_app_is_flask(self):
        from flask import Flask
        self.assertIsInstance(app, Flask)

    def test_app_has_rules(self):
        rules = [rule.rule for rule in app.url_map.iter_rules()]
        self.assertIn("/", rules)
        self.assertIn("/api/chains", rules)
        self.assertIn("/api/status", rules)


class TestIndexRoute(_TestBase):
    """Tests for / route."""

    def test_index_returns_200_or_302(self):
        resp = self.client.get("/")
        self.assertIn(resp.status_code, [200, 302])


class TestChainsApi(_TestBase):
    """Tests for /api/chains endpoint."""

    def test_returns_200(self):
        resp = self.client.get("/api/chains")
        self.assertEqual(resp.status_code, 200)

    def test_returns_json(self):
        resp = self.client.get("/api/chains")
        data = resp.get_json()
        self.assertIsInstance(data, dict)

    def test_contains_base_chain(self):
        resp = self.client.get("/api/chains")
        data = resp.get_json()
        self.assertIn("base", data)

    def test_chain_has_tiers(self):
        resp = self.client.get("/api/chains")
        data = resp.get_json()
        self.assertIn("tiers", data["base"])
        self.assertIsInstance(data["base"]["tiers"], list)
        self.assertGreater(len(data["base"]["tiers"]), 0)

    def test_chain_has_name(self):
        resp = self.client.get("/api/chains")
        data = resp.get_json()
        self.assertIn("name", data["base"])
        self.assertEqual(data["base"]["name"], "Base")


class TestStatusApi(_TestBase):
    """Tests for /api/status endpoint."""

    def test_returns_200(self):
        resp = self.client.get("/api/status")
        self.assertEqual(resp.status_code, 200)

    def test_returns_json(self):
        resp = self.client.get("/api/status")
        data = resp.get_json()
        self.assertIsInstance(data, dict)

    def test_has_status_field(self):
        resp = self.client.get("/api/status")
        data = resp.get_json()
        self.assertIn("status", data)

    def test_new_client_has_new_status(self):
        resp = self.client.get("/api/status")
        data = resp.get_json()
        self.assertEqual(data["status"], "new")

    def test_has_quota_fields(self):
        resp = self.client.get("/api/status")
        data = resp.get_json()
        self.assertIn("quota_bytes", data)
        self.assertIn("used_bytes", data)
        self.assertIn("remaining_bytes", data)

    def test_chain_field(self):
        resp = self.client.get("/api/status")
        data = resp.get_json()
        self.assertIn("current_chain", data)


class TestGenerate204(_TestBase):
    """Tests for /generate_204 endpoint."""

    def test_unpaid_returns_302(self):
        resp = self.client.get("/generate_204")
        self.assertEqual(resp.status_code, 302)

    def test_paid_returns_204(self):
        self._insert_paid_client()
        resp = self.client.get("/generate_204")
        self.assertEqual(resp.status_code, 204)


class TestCsrfProtection(_TestBase):
    """Tests for CSRF/origin protection on POST endpoints."""

    def test_activate_grace_post_no_origin_returns_403(self):
        resp = self.client.post("/api/activate-grace")
        self.assertEqual(resp.status_code, 403)

    def test_check_payment_post_no_origin_returns_403(self):
        resp = self.client.post("/api/check-payment")
        self.assertEqual(resp.status_code, 403)

    def test_activate_grace_post_wrong_origin_returns_403(self):
        resp = self.client.post(
            "/api/activate-grace",
            headers={"Origin": "http://evil.com"},
        )
        self.assertEqual(resp.status_code, 403)

    def test_check_payment_post_wrong_origin_returns_403(self):
        resp = self.client.post(
            "/api/check-payment",
            headers={"Origin": "http://evil.com"},
        )
        self.assertEqual(resp.status_code, 403)


class TestHealthCheck(_TestBase):
    """Tests for /api/health endpoint."""

    def test_health_returns_200(self):
        resp = self.client.get("/api/health")
        self.assertEqual(resp.status_code, 200)

    def test_health_returns_json(self):
        resp = self.client.get("/api/health")
        data = resp.get_json()
        self.assertIsInstance(data, dict)
        self.assertIn("database", data)


class TestAppleDetect(_TestBase):
    """Tests for /hotspot-detect.html endpoint."""

    def test_unpaid_returns_302(self):
        resp = self.client.get("/hotspot-detect.html")
        self.assertEqual(resp.status_code, 302)

    def test_paid_returns_200(self):
        self._insert_paid_client()
        resp = self.client.get("/hotspot-detect.html")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"Success", resp.data)


class TestMsConnectTest(_TestBase):
    """Tests for /connecttest.txt endpoint."""

    def test_unpaid_returns_302(self):
        resp = self.client.get("/connecttest.txt")
        self.assertEqual(resp.status_code, 302)

    def test_paid_returns_200(self):
        self._insert_paid_client()
        resp = self.client.get("/connecttest.txt")
        self.assertEqual(resp.status_code, 200)


def tearDownModule():
    os.unlink(_db_path)


if __name__ == "__main__":
    unittest.main()
