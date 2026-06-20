"""Tests for wallet.py — BIP39 mnemonic to EVM address derivation."""

import importlib
import os
import re
import unittest

TEST_MNEMONIC = "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about"


class TestDeriveAddress(unittest.TestCase):
    """Tests for derive_address()."""

    def setUp(self):
        os.environ["CAPTIVE_HD_SEED"] = TEST_MNEMONIC
        import wallet as wallet_mod
        importlib.reload(wallet_mod)
        self.wallet_mod = wallet_mod

    def test_derive_address_returns_valid_eth_address(self):
        addr = self.wallet_mod.derive_address(0)
        self.assertTrue(re.match(r"^0x[0-9a-fA-F]{40}$", addr))

    def test_derive_address_deterministic(self):
        a1 = self.wallet_mod.derive_address(5)
        a2 = self.wallet_mod.derive_address(5)
        self.assertEqual(a1, a2)

    def test_derive_address_unique_per_index(self):
        addrs = [self.wallet_mod.derive_address(i) for i in range(5)]
        self.assertEqual(len(set(addrs)), 5)


class TestMissingSeed(unittest.TestCase):
    """Tests that missing CAPTIVE_HD_SEED raises RuntimeError."""

    def test_missing_seed_raises(self):
        os.environ.pop("CAPTIVE_HD_SEED", None)
        import wallet as wallet_mod
        with self.assertRaises(RuntimeError):
            importlib.reload(wallet_mod)


if __name__ == "__main__":
    unittest.main()
