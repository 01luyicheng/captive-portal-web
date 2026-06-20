"""Real-time cryptocurrency price service.

Fetches prices from CoinGecko (primary) and CoinMarketCap (fallback).
Caches results to respect rate limits.
"""

import logging
import os
import threading
import time

import requests

logger = logging.getLogger(__name__)

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
        self._empty_cache_expiry = {}
        self._max_cache_size = 100

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
                empty_ttl = self._empty_cache_expiry.get(cache_key)
                effective_ttl = empty_ttl - now if empty_ttl and now < empty_ttl else self.cache_ttl
                if now - ts < effective_ttl:
                    return data
                else:
                    self._empty_cache_expiry.pop(cache_key, None)

        prices = self._fetch_coingecko(symbols, vs_currency)
        if not prices and self.cmc_api_key:
            logger.info("CoinGecko returned no data, falling back to CoinMarketCap")
            prices = self._fetch_coinmarketcap(symbols, vs_currency)
        elif not prices:
            logger.warning("All price sources returned no data for symbols: %s", symbols)

        if prices:
            missing = set(s.upper() for s in symbols) - set(prices.keys())
            if missing:
                logger.warning("Partial price data, missing: %s", missing)

        now = time.time()
        with self._lock:
            if prices:
                self._cache[cache_key] = (now, prices)
            else:
                if cache_key not in self._cache:
                    self._cache[cache_key] = (now, {})
                self._empty_cache_expiry[cache_key] = now + 10

            if len(self._cache) > self._max_cache_size:
                keys_to_remove = []
                for k in list(self._cache.keys()):
                    if k not in self._empty_cache_expiry:
                        keys_to_remove.append(k)
                        if len(self._cache) - len(keys_to_remove) <= self._max_cache_size:
                            break
                if len(keys_to_remove) < len(self._cache) - self._max_cache_size:
                    sorted_keys = sorted(self._cache.keys(), key=lambda k: self._cache[k][0])
                    for k in sorted_keys:
                        if len(self._cache) - len(keys_to_remove) > self._max_cache_size:
                            keys_to_remove.append(k)
                for k in keys_to_remove:
                    self._cache.pop(k, None)
                    self._empty_cache_expiry.pop(k, None)

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
        except requests.RequestException as e:
            logger.warning("CoinGecko API error: %s", e)
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
        except requests.RequestException as e:
            logger.warning("CoinMarketCap API error: %s", e)
            return {}

    def convert_usd_to_token(self, usd_amount, token_symbol, prices, decimals=18):
        """Convert a USD amount to token amount.

        Returns (token_amount_str, token_amount_wei_or_smallest_unit, decimals).
        Use decimals from chain token config (18 for native, varies for ERC-20).
        """
        token = token_symbol.upper()
        price = prices.get(token)
        if not price or price <= 0:
            return None, None, None

        token_amount = usd_amount / price
        smallest_unit = int(round(token_amount * 10**decimals))
        fmt = f".{decimals}f"
        return f"{token_amount:{fmt}}", str(smallest_unit), decimals
