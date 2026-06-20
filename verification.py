"""Payment verification against Blockscout APIs."""

import collections
import logging
import threading
import time
from datetime import datetime, timezone

import requests

from config import (
    PRICE_TOLERANCE_PERCENT,
    REQUIRED_CONFIRMATIONS,
    STABLECOIN_SYMBOLS,
    STABLECOIN_TOLERANCE_PERCENT,
    TX_TIMESTAMP_TOLERANCE,
    VERIFY_CACHE_MAX_SIZE,
    VERIFY_CACHE_SECONDS,
)
from chains import get_chain_config, needed_token_symbols

logger = logging.getLogger(__name__)


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


def _match_tx(tx, tier_map, address, created_at, tx_hash_key="hash", value_key="value", token_key="token", check_status=True):
    """Common matcher: check if a single tx matches any tier. Returns result tuple or None."""
    to_addr = tx.get("to", {})
    if isinstance(to_addr, dict):
        to_addr = to_addr.get("hash", "")
    if to_addr.lower() != address.lower():
        return None

    if check_status and tx.get("status", "") != "ok":
        return None

    if check_status:
        try:
            confirmations = int(tx.get("confirmations", "0") or "0")
        except (TypeError, ValueError):
            return None
        if confirmations < REQUIRED_CONFIRMATIONS:
            return None

    tx_timestamp = tx.get("timestamp", "")
    if tx_timestamp:
        try:
            dt = datetime.fromisoformat(tx_timestamp.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            tx_time = dt.timestamp()
            if tx_time < created_at - TX_TIMESTAMP_TOLERANCE:
                return None
        except (ValueError, OverflowError):
            pass

    value = str(tx.get(value_key, "0"))
    token_data = tx.get(token_key, {})
    token_label = token_data.get("symbol", "ETH") if isinstance(token_data, dict) and token_data else "ETH"

    for (sym, expected_amount), quota in tier_map.items():
        if sym.upper() != token_label.upper():
            continue
        try:
            expected_val = int(expected_amount)
            received_val = int(value)
            if expected_val == 0:
                continue
            tolerance_pct = STABLECOIN_TOLERANCE_PERCENT if sym.upper() in STABLECOIN_SYMBOLS else PRICE_TOLERANCE_PERCENT
            tolerance = tolerance_pct / 100.0
            if abs(received_val - expected_val) / expected_val <= tolerance:
                return True, tx.get(tx_hash_key), quota, "Payment verified"
        except (ValueError, TypeError):
            if value == expected_amount:
                return True, tx.get(tx_hash_key), quota, "Payment verified"
    return None


def _check_payment_items(items, tier_map, address, created_at, check_status=True):
    """Scan Blockscout items for a matching payment. Returns tuple or None."""
    for tx in items:
        result = _match_tx(tx, tier_map, address, created_at, check_status=check_status)
        if result:
            return result
    return None


def _check_token_transfers(items, tier_map, address, created_at):
    """Scan Blockscout token-transfer items for a matching ERC-20 payment.

    Token-transfers have a different response format from regular txs:
    - to.hash is the recipient address
    - total.value is the amount in smallest unit
    - token.symbol is the token symbol
    - No confirmations field (transfers are indexed from confirmed blocks)
    """
    for tx in items:
        to_addr = tx.get("to", {})
        if isinstance(to_addr, dict):
            to_addr = to_addr.get("hash", "")
        if to_addr.lower() != address.lower():
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

        total = tx.get("total", {})
        value = str(total.get("value", "0")) if isinstance(total, dict) else "0"
        token_data = tx.get("token", {})
        token_label = token_data.get("symbol", "") if isinstance(token_data, dict) else ""

        for (sym, expected_amount), quota in tier_map.items():
            if sym.upper() != token_label.upper():
                continue
            try:
                expected_val = int(expected_amount)
                received_val = int(value)
                if expected_val == 0:
                    continue
                tolerance_pct = STABLECOIN_TOLERANCE_PERCENT if sym.upper() in STABLECOIN_SYMBOLS else PRICE_TOLERANCE_PERCENT
                tolerance = tolerance_pct / 100.0
                if abs(received_val - expected_val) / expected_val <= tolerance:
                    return True, tx.get("tx_hash"), quota, "Token transfer verified"
            except (ValueError, TypeError):
                if value == expected_amount:
                    return True, tx.get("tx_hash"), quota, "Token transfer verified"
    return None


def verify_payment_on_chain(address, chain_id, client_ip=None, created_at=None, price_service=None):
    """Check Blockscout for an incoming payment to the unique deposit address.

    Returns (ok: bool, tx_hash: str|None, quota_bytes: int, message: str).
    The accepted amount can match any tier configured for the chain.
    Iterates through paginated Blockscout responses up to a page limit.
    """
    cfg = get_chain_config(chain_id)
    api_base = cfg["blockscout_api"]

    tokens = cfg.get("tokens", {"ETH": {"type": "native"}})
    symbols = list(needed_token_symbols(cfg))
    prices = price_service.get_prices(symbols)

    tier_map = {}
    for tier in cfg["tiers"]:
        amount_usd = tier.get("amount_usd", 0)
        if amount_usd > 0:
            for sym, t_info in tokens.items():
                t_decimals = t_info.get("decimals", 18 if t_info.get("type") == "native" else 6)
                _, unit_val, _ = price_service.convert_usd_to_token(amount_usd, sym, prices, t_decimals)
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
                logger.warning("Blockscout API error for %s: %s", base_url, exc)
                return False, None, 0, "Verification temporarily unavailable. Please try again."

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
        token_url = f"{api_base}/addresses/{address}/token-transfers"
        result = fetch_pages(token_url, checker=_check_token_transfers)

    if not result:
        result = (
            False,
            None,
            0,
            "Waiting for incoming transaction matching any tier amount and enough confirmations",
        )

    _verify_cache.set(cache_key, now, result)
    return result
