"""EVM chain configurations and helpers."""

import json
import logging
import os
import re

logger = logging.getLogger(__name__)

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
_BLOCKSCOUT_URL_PATTERN = re.compile(r'^https://[a-zA-Z0-9.-]+\.blockscout\.com(/|$)')


def _validate_single_chain(chain_id, chain_cfg):
    if not isinstance(chain_cfg, dict):
        return False
    for key in ("name", "chain_id", "blockscout_api", "tiers"):
        if key not in chain_cfg:
            return False
    if not isinstance(chain_cfg["tiers"], list) or not chain_cfg["tiers"]:
        return False
    for tier in chain_cfg["tiers"]:
        if not isinstance(tier, dict) or "quota_bytes" not in tier:
            return False
        if "amount_usd" not in tier and "amount_wei" not in tier:
            return False
    if not _BLOCKSCOUT_URL_PATTERN.match(chain_cfg.get('blockscout_api', '')):
        return False
    return True


def _load_chains():
    try:
        with open(_CHAINS_CONFIG_PATH) as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return _DEFAULT_CHAINS
        loaded = {}
        for chain_id, chain_cfg in data.items():
            if _validate_single_chain(chain_id, chain_cfg):
                loaded[chain_id] = chain_cfg
            else:
                logger.warning("Skipping invalid chain config: %s", chain_id)
        if not loaded:
            logger.warning("No valid chains in config at %s, falling back to defaults", _CHAINS_CONFIG_PATH)
            return _DEFAULT_CHAINS
        return loaded
    except FileNotFoundError:
        logger.info("No custom chain config at %s, using defaults", _CHAINS_CONFIG_PATH)
        return _DEFAULT_CHAINS
    except (json.JSONDecodeError, KeyError, TypeError):
        logger.warning("Invalid chain config at %s, falling back to defaults", _CHAINS_CONFIG_PATH)
        return _DEFAULT_CHAINS


CHAINS = _load_chains()

DEFAULT_CHAIN = os.environ.get("DEFAULT_CHAIN", "base")
if DEFAULT_CHAIN not in CHAINS:
    DEFAULT_CHAIN = next(iter(CHAINS.keys()))


def _validate_chain_id(chain_id):
    if isinstance(chain_id, str) and chain_id in CHAINS:
        return chain_id
    return DEFAULT_CHAIN


def get_chain_config(chain_id):
    """Return chain config or default chain if unknown."""
    return CHAINS.get(chain_id, CHAINS.get(DEFAULT_CHAIN, next(iter(CHAINS.values()))))


def needed_token_symbols(cfg):
    """Collect all token symbols that need price lookups for a chain config."""
    symbols = set()
    tokens = cfg.get("tokens", {"ETH": {"type": "native"}})
    for t in cfg.get("tiers", []):
        if "amount_usd" in t:
            for sym in tokens:
                symbols.add(sym)
    return symbols
