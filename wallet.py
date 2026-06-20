"""HD wallet setup — BIP39 mnemonic to EVM address derivation."""

import logging
import os

from bip32 import BIP32
from eth_account import Account
from mnemonic import Mnemonic

logger = logging.getLogger(__name__)

MNEMONIC = os.environ.get("CAPTIVE_HD_SEED", "").strip()
if not MNEMONIC:
    raise RuntimeError("CAPTIVE_HD_SEED environment variable is required")

_MNEMO = Mnemonic("english")
if not _MNEMO.check(MNEMONIC):
    raise RuntimeError("CAPTIVE_HD_SEED is not a valid BIP39 mnemonic")

_HD_ROOT = BIP32.from_seed(_MNEMO.to_seed(MNEMONIC))

# BIP44 path: m/44'/60'/0'/0/{index}
_HD_BASE_PATH = [
    0x8000002C,  # 44'
    0x8000003C,  # 60'
    0x80000000,  # 0'
    0,           # 0
]


def derive_address(index):
    """Derive a unique EVM deposit address from the HD seed."""
    priv_key = _HD_ROOT.get_privkey_from_path(_HD_BASE_PATH + [index])
    return Account.from_key(priv_key).address
