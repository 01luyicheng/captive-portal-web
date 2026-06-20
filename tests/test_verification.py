"""Tests for verification.py."""

import time
import unittest
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


if __name__ == "__main__":
    unittest.main()
