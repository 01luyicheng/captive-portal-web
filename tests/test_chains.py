"""Tests for chains.py."""

import json
import os
import tempfile
import unittest


class TestNeededTokenSymbols(unittest.TestCase):
    """Tests for needed_token_symbols()."""

    def test_base_chain_tokens(self):
        from chains import needed_token_symbols, CHAINS
        cfg = CHAINS["base"]
        symbols = needed_token_symbols(cfg)
        self.assertIn("ETH", symbols)
        self.assertIn("USDC", symbols)

    def test_polygon_chain_tokens(self):
        from chains import needed_token_symbols, CHAINS
        cfg = CHAINS["polygon"]
        symbols = needed_token_symbols(cfg)
        self.assertIn("ETH", symbols)
        self.assertIn("USDC", symbols)
        self.assertIn("USDT", symbols)

    def test_empty_tiers_returns_empty(self):
        from chains import needed_token_symbols
        cfg = {"tokens": {"ETH": {"type": "native"}}, "tiers": []}
        symbols = needed_token_symbols(cfg)
        self.assertEqual(symbols, set())

    def test_no_amount_usd_tiers(self):
        from chains import needed_token_symbols
        cfg = {
            "tokens": {"ETH": {"type": "native"}},
            "tiers": [{"amount_wei": "1000", "quota_bytes": 100}],
        }
        symbols = needed_token_symbols(cfg)
        self.assertEqual(symbols, set())

    def test_no_tokens_defaults_to_eth(self):
        from chains import needed_token_symbols
        cfg = {"tiers": [{"amount_usd": 1.0, "quota_bytes": 100}]}
        symbols = needed_token_symbols(cfg)
        self.assertIn("ETH", symbols)


class TestValidateChainId(unittest.TestCase):
    """Tests for _validate_chain_id()."""

    def test_valid_chain_id(self):
        from chains import _validate_chain_id
        self.assertEqual(_validate_chain_id("base"), "base")

    def test_unknown_chain_returns_default(self):
        from chains import _validate_chain_id, DEFAULT_CHAIN
        self.assertEqual(_validate_chain_id("unknown_chain_xyz"), DEFAULT_CHAIN)

    def test_non_string_returns_default(self):
        from chains import _validate_chain_id, DEFAULT_CHAIN
        self.assertEqual(_validate_chain_id(12345), DEFAULT_CHAIN)

    def test_none_returns_default(self):
        from chains import _validate_chain_id, DEFAULT_CHAIN
        self.assertEqual(_validate_chain_id(None), DEFAULT_CHAIN)

    def test_empty_string_returns_default(self):
        from chains import _validate_chain_id, DEFAULT_CHAIN
        self.assertEqual(_validate_chain_id(""), DEFAULT_CHAIN)


class TestLoadChains(unittest.TestCase):
    """Tests for _load_chains() with various config files."""

    def test_valid_config_file(self):
        from chains import _DEFAULT_CHAINS
        config = {"testchain": {
            "name": "Test",
            "chain_id": 999,
            "blockscout_api": "https://test.blockscout.com/api/v2",
            "block_time": 1,
            "recommended": False,
            "icon": "circle",
            "tokens": {"ETH": {"type": "native"}},
            "tiers": [{"amount_usd": 0.50, "quota_bytes": 100}],
        }}
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(config, f)
            f.flush()
            path = f.name
        try:
            os.environ["CAPTIVE_CHAINS_CONFIG"] = path
            # Need to reload module-level _CHAINS_CONFIG_PATH
            import importlib
            import chains as chains_mod
            chains_mod._CHAINS_CONFIG_PATH = path
            result = chains_mod._load_chains()
            self.assertIn("testchain", result)
            self.assertEqual(result["testchain"]["chain_id"], 999)
        finally:
            del os.environ["CAPTIVE_CHAINS_CONFIG"]
            os.unlink(path)

    def test_missing_file_returns_defaults(self):
        import chains as chains_mod
        chains_mod._CHAINS_CONFIG_PATH = "/nonexistent/path/config.json"
        result = chains_mod._load_chains()
        self.assertIn("base", result)
        self.assertEqual(result["base"]["chain_id"], 8453)

    def test_invalid_json_returns_defaults(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            f.write("not valid json {{{")
            f.flush()
            path = f.name
        try:
            import chains as chains_mod
            chains_mod._CHAINS_CONFIG_PATH = path
            result = chains_mod._load_chains()
            self.assertIn("base", result)
        finally:
            os.unlink(path)

    def test_non_dict_root_returns_defaults(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(["not", "a", "dict"], f)
            f.flush()
            path = f.name
        try:
            import chains as chains_mod
            chains_mod._CHAINS_CONFIG_PATH = path
            result = chains_mod._load_chains()
            self.assertIn("base", result)
        finally:
            os.unlink(path)

    def test_missing_required_keys_returns_defaults(self):
        config = {"testchain": {"name": "Test"}}  # missing chain_id, blockscout_api, tiers
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(config, f)
            f.flush()
            path = f.name
        try:
            import chains as chains_mod
            chains_mod._CHAINS_CONFIG_PATH = path
            result = chains_mod._load_chains()
            self.assertIn("base", result)
        finally:
            os.unlink(path)

    def test_empty_tiers_returns_defaults(self):
        config = {"testchain": {
            "name": "Test",
            "chain_id": 999,
            "blockscout_api": "https://test.blockscout.com/api/v2",
            "tiers": [],
        }}
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(config, f)
            f.flush()
            path = f.name
        try:
            import chains as chains_mod
            chains_mod._CHAINS_CONFIG_PATH = path
            result = chains_mod._load_chains()
            self.assertIn("base", result)
        finally:
            os.unlink(path)

    def test_invalid_blockscout_url_returns_defaults(self):
        config = {"testchain": {
            "name": "Test",
            "chain_id": 999,
            "blockscout_api": "http://evil.com/api/v2",
            "tiers": [{"amount_usd": 1.0, "quota_bytes": 100}],
        }}
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(config, f)
            f.flush()
            path = f.name
        try:
            import chains as chains_mod
            chains_mod._CHAINS_CONFIG_PATH = path
            result = chains_mod._load_chains()
            self.assertIn("base", result)
        finally:
            os.unlink(path)


class TestGetChainConfig(unittest.TestCase):
    def test_known_chain(self):
        from chains import get_chain_config, CHAINS
        cfg = get_chain_config("base")
        self.assertEqual(cfg["name"], "Base")

    def test_unknown_chain_returns_default(self):
        from chains import get_chain_config, CHAINS, DEFAULT_CHAIN
        cfg = get_chain_config("nonexistent")
        self.assertEqual(cfg, CHAINS[DEFAULT_CHAIN])


if __name__ == "__main__":
    unittest.main()
