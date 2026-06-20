"""Tests for price_service.py — cryptocurrency price fetching and conversion."""

import time
import unittest
from unittest.mock import MagicMock, patch

from price_service import PriceService


class TestConvertUsdToToken(unittest.TestCase):
    """Tests for PriceService.convert_usd_to_token()."""

    def setUp(self):
        self.ps = PriceService(cache_ttl=60)

    def test_convert_usd_to_token_eth(self):
        prices = {"ETH": 3500.0}
        token_str, wei_str, decimals = self.ps.convert_usd_to_token(100, "ETH", prices, decimals=18)
        expected_wei = str(int(round(100 / 3500.0 * 10**18)))
        self.assertEqual(wei_str, expected_wei)
        self.assertEqual(decimals, 18)
        self.assertIn(".", token_str)

    def test_convert_usd_to_token_usdc(self):
        prices = {"USDC": 1.0}
        token_str, smallest_str, decimals = self.ps.convert_usd_to_token(100, "USDC", prices, decimals=6)
        expected = str(100 * 10**6)
        self.assertEqual(smallest_str, expected)
        self.assertEqual(decimals, 6)

    def test_convert_usd_to_token_zero_price(self):
        prices = {"ETH": 0.0}
        result = self.ps.convert_usd_to_token(100, "ETH", prices, decimals=18)
        self.assertEqual(result, (None, None, None))

    def test_convert_usd_to_token_missing_symbol(self):
        prices = {"ETH": 3500.0}
        result = self.ps.convert_usd_to_token(100, "DOGE", prices, decimals=18)
        self.assertEqual(result, (None, None, None))


class TestGetPrices(unittest.TestCase):
    """Tests for PriceService.get_prices() with mocked HTTP calls."""

    def setUp(self):
        self.ps = PriceService(cache_ttl=60)

    def _mock_response(self, json_data, status_code=200):
        mock = MagicMock()
        mock.json.return_value = json_data
        mock.status_code = status_code
        mock.raise_for_status.return_value = None
        return mock

    @patch("price_service.requests.get")
    def test_cache_hit(self, mock_get):
        mock_get.return_value = self._mock_response(
            {"ethereum": {"usd": 3500.0}}
        )
        r1 = self.ps.get_prices(["ETH"])
        r2 = self.ps.get_prices(["ETH"])
        self.assertEqual(r1, r2)
        self.assertEqual(mock_get.call_count, 1)

    @patch("price_service.requests.get")
    def test_api_failure_returns_empty(self, mock_get):
        import requests
        mock_get.side_effect = requests.ConnectionError("fail")
        result = self.ps.get_prices(["ETH"])
        self.assertEqual(result, {})
