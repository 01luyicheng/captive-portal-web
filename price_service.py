"""Real-time cryptocurrency price service.

Fetches prices from CoinGecko (primary) and CoinMarketCap (fallback).
Caches results to respect rate limits.
"""

import os
import threading
import time

import requests

COINGECKO_API = "https://api.coingecko.com/api/v3"
COINMARKETCAP_API = "https://pro-api.coinmarketcap.com/v1"

COINGECKO_IDS = {
    "ETH": "ethereum",
    "MATIC": "matic-network",
    "USDC": "usd-coin",
    "USDT": "tether",
    "DAI": "dai",
    "FRAX": "frax",
    "ARB": "arbitrum",
    "OP": "optimism",
    "BNB": "binancecoin",
}

COINMARKETCAP_IDS = {
    "ETH": "1027",
    "MATIC": "3890",
    "USDC": "3408",
    "USDT": "825",
    "DAI": "4943",
    "FRAX": "6952",
    "ARB": "11841",
    "OP": "11840",
    "BNB": "1839",
}


class PriceService:
    """Thread-safe price cache with CoinGecko + CoinMarketCap fallback."""

    def __init__(self, cache_ttl=60, cmc_api_key=None):
        self.cache_ttl = cache_ttl
        self.cmc_api_key = cmc_api_key or os.environ.get("COINMARKETCAP_API_KEY", "")
        self._cache = {}
        self._lock = threading.Lock()
        self._last_fetch = 0

    def get_prices(self, symbols, vs_currency="usd"):
        """Get prices for a list of token symbols in USD.

        Returns dict like {"ETH": 3500.0, "USDC": 1.0, ...}
        Returns empty dict on failure.
        """
        now = time.time()
        cache_key = (tuple(sorted(symbols)), vs_currency)

        with self._lock:
            if cache_key in self._cache:
                ts, data = self._cache[cache_key]
                if now - ts < self.cache_ttl:
                    return data

        prices = self._fetch_coingecko(symbols, vs_currency)
        if not prices and self.cmc_api_key:
            prices = self._fetch_coinmarketcap(symbols, vs_currency)

        if prices:
            with self._lock:
                self._cache[cache_key] = (now, prices)

        return prices or {}

    def _fetch_coingecko(self, symbols, vs_currency):
        """Fetch from CoinGecko free API."""
        ids = []
        symbol_to_id = {}
        for s in symbols:
            s_upper = s.upper()
            cg_id = COINGECKO_IDS.get(s_upper)
            if cg_id:
                ids.append(cg_id)
                symbol_to_id[cg_id] = s_upper

        if not ids:
            return {}

        try:
            resp = requests.get(
                f"{COINGECKO_API}/simple/price",
                params={"ids": ",".join(ids), "vs_currencies": vs_currency},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()

            prices = {}
            for cg_id, symbol in symbol_to_id.items():
                if cg_id in data and vs_currency in data[cg_id]:
                    prices[symbol] = float(data[cg_id][vs_currency])

            return prices
        except Exception:
            return {}

    def _fetch_coinmarketcap(self, symbols, vs_currency):
        """Fetch from CoinMarketCap paid API."""
        ids = []
        symbol_to_id = {}
        for s in symbols:
            s_upper = s.upper()
            cmc_id = COINMARKETCAP_IDS.get(s_upper)
            if cmc_id:
                ids.append(cmc_id)
                symbol_to_id[cmc_id] = s_upper

        if not ids:
            return {}

        try:
            resp = requests.get(
                f"{COINMARKETCAP_API}/cryptocurrency/quotes/latest",
                params={"id": ",".join(ids), "convert": vs_currency.upper()},
                headers={"X-CMC_PRO_API_KEY": self.cmc_api_key},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()

            prices = {}
            for cmc_id, symbol in symbol_to_id.items():
                if cmc_id in data.get("data", {}):
                    quote = data["data"][cmc_id].get("quote", {})
                    if vs_currency.upper() in quote:
                        prices[symbol] = float(quote[vs_currency.upper()]["price"])

            return prices
        except Exception:
            return {}

    def convert_usd_to_token(self, usd_amount, token_symbol, prices):
        """Convert a USD amount to token amount.

        Returns (token_amount_str, token_amount_wei_or_smallest_unit, decimals).
        For native ETH: returns wei (18 decimals).
        For ERC-20: returns smallest unit (e.g., 6 decimals for USDC).
        """
        token = token_symbol.upper()
        price = prices.get(token)
        if not price or price <= 0:
            return None, None, None

        if token == "ETH":
            token_amount = usd_amount / price
            wei = int(round(token_amount * 10**18))
            return f"{token_amount:.10f}", str(wei), 18
        elif token == "MATIC":
            token_amount = usd_amount / price
            wei = int(round(token_amount * 10**18))
            return f"{token_amount:.10f}", str(wei), 18
        elif token == "BNB":
            token_amount = usd_amount / price
            wei = int(round(token_amount * 10**18))
            return f"{token_amount:.10f}", str(wei), 18
        elif token == "ARB":
            token_amount = usd_amount / price
            wei = int(round(token_amount * 10**18))
            return f"{token_amount:.10f}", str(wei), 18
        elif token == "OP":
            token_amount = usd_amount / price
            wei = int(round(token_amount * 10**18))
            return f"{token_amount:.10f}", str(wei), 18
        else:
            token_amount = usd_amount / price
            wei = int(round(token_amount * 10**6))
            return f"{token_amount:.6f}", str(wei), 6
