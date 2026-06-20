"""Tests for verification.py."""

import time
import unittest
from unittest.mock import patch, MagicMock
from datetime import datetime, timezone

from verification import _match_tx, _check_token_transfers


class TestMatchTxNativeEth(unittest.TestCase):
    """Tests for _match_tx with native ETH transfers."""

    def _make_tier_map(self, expected_wei, quota):
        return {("ETH", str(expected_wei)): quota}

    def test_exact_match(self):
        tier_map = self._make_tier_map(500000000000000000, 104857600)
        tx = {
            "to": {"hash": "0xAAAA"},
            "status": "ok",
            "confirmations": "10",
            "value": "500000000000000000",
            "timestamp": "2025-01-01T00:00:00Z",
            "hash": "0xTX1",
        }
        result = _match_tx(tx, tier_map, "0xAAAA", created_at=0)
        self.assertIsNotNone(result)
        self.assertTrue(result[0])
        self.assertEqual(result[1], "0xTX1")

    def test_wrong_address_returns_none(self):
        tier_map = self._make_tier_map(500000000000000000, 104857600)
        tx = {
            "to": {"hash": "0xBBBB"},
            "status": "ok",
            "confirmations": "10",
            "value": "500000000000000000",
            "hash": "0xTX2",
        }
        result = _match_tx(tx, tier_map, "0xAAAA", created_at=0)
        self.assertIsNone(result)

    def test_insufficient_confirmations(self):
        from config import REQUIRED_CONFIRMATIONS
        tier_map = self._make_tier_map(500000000000000000, 104857600)
        tx = {
            "to": {"hash": "0xAAAA"},
            "status": "ok",
            "confirmations": str(REQUIRED_CONFIRMATIONS - 1),
            "value": "500000000000000000",
            "hash": "0xTX3",
        }
        result = _match_tx(tx, tier_map, "0xAAAA", created_at=0)
        self.assertIsNone(result)

    def test_status_not_ok(self):
        tier_map = self._make_tier_map(500000000000000000, 104857600)
        tx = {
            "to": {"hash": "0xAAAA"},
            "status": "error",
            "confirmations": "10",
            "value": "500000000000000000",
            "hash": "0xTX4",
        }
        result = _match_tx(tx, tier_map, "0xAAAA", created_at=0)
        self.assertIsNone(result)

    def test_zero_expected_amount_skipped(self):
        tier_map = {("ETH", "0"): 104857600}
        tx = {
            "to": {"hash": "0xAAAA"},
            "status": "ok",
            "confirmations": "10",
            "value": "100000000000000000",
            "hash": "0xTX5",
        }
        result = _match_tx(tx, tier_map, "0xAAAA", created_at=0)
        self.assertIsNone(result)

    def test_to_addr_as_string(self):
        tier_map = self._make_tier_map(500000000000000000, 104857600)
        tx = {
            "to": "0xAAAA",
            "status": "ok",
            "confirmations": "10",
            "value": "500000000000000000",
            "hash": "0xTX6",
        }
        result = _match_tx(tx, tier_map, "0xAAAA", created_at=0)
        self.assertIsNotNone(result)


class TestMatchTxErc20(unittest.TestCase):
    """Tests for _match_tx with ERC-20 token transfers."""

    def test_usdc_transfer_exact(self):
        tier_map = {("USDC", "10000000"): 104857600}
        tx = {
            "to": {"hash": "0xAAAA"},
            "status": "ok",
            "confirmations": "10",
            "value": "10000000",
            "token": {"symbol": "USDC"},
            "hash": "0xTX7",
        }
        result = _match_tx(tx, tier_map, "0xAAAA", created_at=0)
        self.assertIsNotNone(result)
        self.assertTrue(result[0])

    def test_usdt_transfer_exact(self):
        tier_map = {("USDT", "5000000"): 536870912}
        tx = {
            "to": {"hash": "0xAAAA"},
            "status": "ok",
            "confirmations": "10",
            "value": "5000000",
            "token": {"symbol": "USDT"},
            "hash": "0xTX8",
        }
        result = _match_tx(tx, tier_map, "0xAAAA", created_at=0)
        self.assertIsNotNone(result)
        self.assertTrue(result[0])

    def test_wrong_token_symbol(self):
        tier_map = {("USDC", "10000000"): 104857600}
        tx = {
            "to": {"hash": "0xAAAA"},
            "status": "ok",
            "confirmations": "10",
            "value": "10000000",
            "token": {"symbol": "DAI"},
            "hash": "0xTX9",
        }
        result = _match_tx(tx, tier_map, "0xAAAA", created_at=0)
        self.assertIsNone(result)


class TestPriceTolerance(unittest.TestCase):
    """Tests for price tolerance matching within/outside tolerance."""

    def test_within_price_tolerance(self):
        from config import PRICE_TOLERANCE_PERCENT
        expected = 10000000
        tolerance_val = int(expected * (1 + (PRICE_TOLERANCE_PERCENT - 1) / 100))
        tier_map = {("ETH", str(expected)): 104857600}
        tx = {
            "to": {"hash": "0xAAAA"},
            "status": "ok",
            "confirmations": "10",
            "value": str(tolerance_val),
            "hash": "0xTX10",
        }
        result = _match_tx(tx, tier_map, "0xAAAA", created_at=0)
        self.assertIsNotNone(result)

    def test_outside_price_tolerance(self):
        from config import PRICE_TOLERANCE_PERCENT
        expected = 10000000
        tolerance_val = int(expected * 1.20)
        tier_map = {("ETH", str(expected)): 104857600}
        tx = {
            "to": {"hash": "0xAAAA"},
            "status": "ok",
            "confirmations": "10",
            "value": str(tolerance_val),
            "hash": "0xTX11",
        }
        result = _match_tx(tx, tier_map, "0xAAAA", created_at=0)
        self.assertIsNone(result)

    def test_at_exact_boundary_price_tolerance(self):
        from config import PRICE_TOLERANCE_PERCENT
        expected = 10000000
        tolerance_val = int(expected * (1 + PRICE_TOLERANCE_PERCENT / 100))
        tier_map = {("ETH", str(expected)): 104857600}
        tx = {
            "to": {"hash": "0xAAAA"},
            "status": "ok",
            "confirmations": "10",
            "value": str(tolerance_val),
            "hash": "0xTX12",
        }
        result = _match_tx(tx, tier_map, "0xAAAA", created_at=0)
        self.assertIsNotNone(result)


class TestStablecoinTolerance(unittest.TestCase):
    """Tests for stablecoin tolerance matching within/outside tolerance."""

    def test_exact_stablecoin_amount(self):
        expected = 10000000
        tier_map = {("USDC", str(expected)): 104857600}
        tx = {
            "to": {"hash": "0xAAAA"},
            "status": "ok",
            "confirmations": "10",
            "value": str(expected),
            "token": {"symbol": "USDC"},
            "hash": "0xTX13",
        }
        result = _match_tx(tx, tier_map, "0xAAAA", created_at=0)
        self.assertIsNotNone(result)

    def test_within_stablecoin_tolerance_rejected(self):
        expected = 10000000
        tolerance_val = int(expected * 1.01)
        tier_map = {("USDC", str(expected)): 104857600}
        tx = {
            "to": {"hash": "0xAAAA"},
            "status": "ok",
            "confirmations": "10",
            "value": str(tolerance_val),
            "token": {"symbol": "USDC"},
            "hash": "0xTX13",
        }
        result = _match_tx(tx, tier_map, "0xAAAA", created_at=0)
        self.assertIsNone(result)

    def test_outside_stablecoin_tolerance(self):
        expected = 10000000
        tolerance_val = int(expected * 1.05)
        tier_map = {("USDC", str(expected)): 104857600}
        tx = {
            "to": {"hash": "0xAAAA"},
            "status": "ok",
            "confirmations": "10",
            "value": str(tolerance_val),
            "token": {"symbol": "USDC"},
            "hash": "0xTX14",
        }
        result = _match_tx(tx, tier_map, "0xAAAA", created_at=0)
        self.assertIsNone(result)

    def test_usdt_exact_amount(self):
        expected = 10000000
        tier_map = {("USDT", str(expected)): 104857600}
        tx = {
            "to": {"hash": "0xAAAA"},
            "status": "ok",
            "confirmations": "10",
            "value": str(expected),
            "token": {"symbol": "USDT"},
            "hash": "0xTX15",
        }
        result = _match_tx(tx, tier_map, "0xAAAA", created_at=0)
        self.assertIsNotNone(result)

    def test_usdt_tolerance_rejected(self):
        expected = 10000000
        tolerance_val = int(expected * 1.01)
        tier_map = {("USDT", str(expected)): 104857600}
        tx = {
            "to": {"hash": "0xAAAA"},
            "status": "ok",
            "confirmations": "10",
            "value": str(tolerance_val),
            "token": {"symbol": "USDT"},
            "hash": "0xTX15",
        }
        result = _match_tx(tx, tier_map, "0xAAAA", created_at=0)
        self.assertIsNone(result)


class TestCheckTokenTransfers(unittest.TestCase):
    """Tests for _check_token_transfers()."""

    def test_matching_token_transfer(self):
        tier_map = {("USDC", "10000000"): 104857600}
        items = [
            {
                "to": {"hash": "0xAAAA"},
                "total": {"value": "10000000"},
                "token": {"symbol": "USDC"},
                "tx_hash": "0xTTX1",
                "timestamp": "2025-01-01T00:00:00Z",
            }
        ]
        result = _check_token_transfers(items, tier_map, "0xAAAA", created_at=0)
        self.assertIsNotNone(result)
        self.assertTrue(result[0])

    def test_non_matching_token_transfer(self):
        tier_map = {("USDC", "10000000"): 104857600}
        items = [
            {
                "to": {"hash": "0xAAAA"},
                "total": {"value": "10000000"},
                "token": {"symbol": "DAI"},
                "tx_hash": "0xTTX2",
                "timestamp": "2025-01-01T00:00:00Z",
            }
        ]
        result = _check_token_transfers(items, tier_map, "0xAAAA", created_at=0)
        self.assertIsNone(result)

    def test_wrong_address_token_transfer(self):
        tier_map = {("USDC", "10000000"): 104857600}
        items = [
            {
                "to": {"hash": "0xBBBB"},
                "total": {"value": "10000000"},
                "token": {"symbol": "USDC"},
                "tx_hash": "0xTTX3",
                "timestamp": "2025-01-01T00:00:00Z",
            }
        ]
        result = _check_token_transfers(items, tier_map, "0xAAAA", created_at=0)
        self.assertIsNone(result)

    def test_exact_stablecoin_transfer(self):
        expected = 10000000
        tier_map = {("USDC", str(expected)): 104857600}
        items = [
            {
                "to": {"hash": "0xAAAA"},
                "total": {"value": str(expected)},
                "token": {"symbol": "USDC"},
                "tx_hash": "0xTTX4",
                "timestamp": "2025-01-01T00:00:00Z",
            }
        ]
        result = _check_token_transfers(items, tier_map, "0xAAAA", created_at=0)
        self.assertIsNotNone(result)

    def test_within_stablecoin_tolerance_transfer_rejected(self):
        expected = 10000000
        tolerance_val = int(expected * 1.01)
        tier_map = {("USDC", str(expected)): 104857600}
        items = [
            {
                "to": {"hash": "0xAAAA"},
                "total": {"value": str(tolerance_val)},
                "token": {"symbol": "USDC"},
                "tx_hash": "0xTTX4",
                "timestamp": "2025-01-01T00:00:00Z",
            }
        ]
        result = _check_token_transfers(items, tier_map, "0xAAAA", created_at=0)
        self.assertIsNone(result)


class TestTimestampTolerance(unittest.TestCase):
    """Tests for timestamp tolerance in _match_tx."""

    def _epoch_to_iso(self, epoch):
        """Convert epoch seconds to ISO 8601 string."""
        return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def test_tx_before_created_at_minus_tolerance(self):
        from config import TX_TIMESTAMP_TOLERANCE
        now = int(time.time())
        tier_map = {("ETH", "1000"): 100}
        tx = {
            "to": {"hash": "0xAAAA"},
            "status": "ok",
            "confirmations": "10",
            "value": "1000",
            "timestamp": self._epoch_to_iso(now - TX_TIMESTAMP_TOLERANCE - 100),
            "hash": "0xTX16",
        }
        result = _match_tx(tx, tier_map, "0xAAAA", created_at=now)
        self.assertIsNone(result)

    def test_tx_within_timestamp_tolerance(self):
        from config import TX_TIMESTAMP_TOLERANCE
        now = int(time.time())
        tier_map = {("ETH", "1000"): 100}
        tx = {
            "to": {"hash": "0xAAAA"},
            "status": "ok",
            "confirmations": "10",
            "value": "1000",
            "timestamp": self._epoch_to_iso(now - TX_TIMESTAMP_TOLERANCE + 10),
            "hash": "0xTX17",
        }
        result = _match_tx(tx, tier_map, "0xAAAA", created_at=now)
        self.assertIsNotNone(result)


class _MockPriceService:
    def get_prices(self, symbols):
        return {"ETH": 3500.0, "USDC": 1.0, "USDT": 1.0}

    def convert_usd_to_token(self, usd_amount, token_symbol, prices, decimals=18):
        price = prices.get(token_symbol.upper(), 0)
        if not price:
            return None, None, None
        amount = usd_amount / price
        smallest = int(round(amount * 10 ** decimals))
        return f"{amount:.{decimals}f}", str(smallest), decimals


class TestVerifyCache(unittest.TestCase):
    """Tests for _VerifyCache."""

    def setUp(self):
        from verification import _VerifyCache
        self.cache = _VerifyCache(ttl_seconds=10, max_size=3)

    def test_get_set(self):
        self.cache.set("k1", 100, "v1")
        self.assertEqual(self.cache.get("k1", 101), "v1")

    def test_get_missing_key(self):
        self.assertIsNone(self.cache.get("missing", 100))

    def test_ttl_expiry(self):
        self.cache.set("k1", 100, "v1")
        self.assertEqual(self.cache.get("k1", 105), "v1")
        self.assertIsNone(self.cache.get("k1", 111))

    def test_max_size_eviction(self):
        self.cache.set("k1", 100, "v1")
        self.cache.set("k2", 100, "v2")
        self.cache.set("k3", 100, "v3")
        self.cache.set("k4", 100, "v4")
        self.assertIsNone(self.cache.get("k1", 101))

    def test_lru_refresh_on_get(self):
        self.cache = _VerifyCache(ttl_seconds=10, max_size=3) if False else self.cache
        self.cache.set("k1", 100, "v1")
        self.cache.set("k2", 100, "v2")
        self.cache.set("k3", 100, "v3")
        self.cache.get("k1", 101)
        self.cache.set("k4", 100, "v4")
        self.assertEqual(self.cache.get("k1", 101), "v1")
        self.assertIsNone(self.cache.get("k2", 101))


class TestVerifyPaymentOnChain(unittest.TestCase):
    """Tests for verify_payment_on_chain()."""

    def setUp(self):
        from verification import _VerifyCache, VERIFY_CACHE_SECONDS, VERIFY_CACHE_MAX_SIZE
        self.mock_price = _MockPriceService()
        self._patcher_cache = patch(
            "verification._verify_cache",
            _VerifyCache(VERIFY_CACHE_SECONDS, VERIFY_CACHE_MAX_SIZE),
        )
        self._patcher_cache.start()

    def tearDown(self):
        self._patcher_cache.stop()

    def _mock_response(self, items, next_page_params=None):
        resp = MagicMock()
        resp.json.return_value = {"items": items, "next_page_params": next_page_params}
        resp.raise_for_status = MagicMock()
        return resp

    def _now_iso(self):
        from datetime import datetime, timezone
        return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def test_verify_native_eth_match(self):
        from verification import verify_payment_on_chain
        address = "0xAAAA"
        chain_id = "base"
        eth_wei = str(int(round(0.50 / 3500.0 * 10 ** 18)))
        items = [
            {
                "to": {"hash": address},
                "status": "ok",
                "confirmations": "10",
                "value": eth_wei,
                "hash": "0xTX_NATIVE",
                "timestamp": self._now_iso(),
            }
        ]
        with patch("verification.requests.get", return_value=self._mock_response(items)):
            ok, tx_hash, quota, msg = verify_payment_on_chain(
                address, chain_id, price_service=self.mock_price
            )
        self.assertTrue(ok)
        self.assertEqual(tx_hash, "0xTX_NATIVE")
        self.assertGreater(quota, 0)

    def test_verify_erc20_match(self):
        from verification import verify_payment_on_chain
        address = "0xAAAA"
        chain_id = "base"
        usdc_units = str(int(round(0.50 / 1.0 * 10 ** 6)))
        token_transfer_items = [
            {
                "to": {"hash": address},
                "total": {"value": usdc_units},
                "token": {"symbol": "USDC"},
                "tx_hash": "0xTX_ERC20",
                "timestamp": self._now_iso(),
            }
        ]
        empty_response = self._mock_response([])
        token_response = self._mock_response(token_transfer_items)
        with patch("verification.requests.get", side_effect=[empty_response, empty_response, token_response]):
            ok, tx_hash, quota, msg = verify_payment_on_chain(
                address, chain_id, price_service=self.mock_price
            )
        self.assertTrue(ok)
        self.assertEqual(tx_hash, "0xTX_ERC20")
        self.assertGreater(quota, 0)

    def test_verify_no_match(self):
        from verification import verify_payment_on_chain
        address = "0xAAAA"
        chain_id = "base"
        with patch("verification.requests.get", return_value=self._mock_response([])):
            ok, tx_hash, quota, msg = verify_payment_on_chain(
                address, chain_id, price_service=self.mock_price
            )
        self.assertFalse(ok)
        self.assertIsNone(tx_hash)
        self.assertEqual(quota, 0)

    def test_verify_network_error(self):
        from verification import verify_payment_on_chain
        import requests as req_lib
        address = "0xAAAA"
        chain_id = "base"
        with patch(
            "verification.requests.get",
            side_effect=req_lib.RequestException("connection refused"),
        ):
            ok, tx_hash, quota, msg = verify_payment_on_chain(
                address, chain_id, price_service=self.mock_price
            )
        self.assertFalse(ok)
        self.assertIsNone(tx_hash)
        self.assertEqual(quota, 0)


if __name__ == "__main__":
    unittest.main()
