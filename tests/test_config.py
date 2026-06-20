"""Tests for config.py."""

import os
import unittest


class TestEnvInt(unittest.TestCase):
    """Tests for _env_int() helper."""

    def _call(self, key, default, min_val=None, max_val=None):
        from config import _env_int
        return _env_int(key, default, min_val, max_val)

    def test_returns_default_when_env_unset(self):
        key = "TEST_CFG_MISSING_98765"
        os.environ.pop(key, None)
        self.assertEqual(self._call(key, 42), 42)

    def test_returns_int_from_env(self):
        key = "TEST_CFG_VALID_98765"
        os.environ[key] = "99"
        try:
            self.assertEqual(self._call(key, 0), 99)
        finally:
            del os.environ[key]

    def test_returns_default_on_invalid_value(self):
        key = "TEST_CFG_BAD_98765"
        os.environ[key] = "not_a_number"
        try:
            self.assertEqual(self._call(key, 7), 7)
        finally:
            del os.environ[key]

    def test_returns_default_on_empty_string(self):
        key = "TEST_CFG_EMPTY_98765"
        os.environ[key] = ""
        try:
            self.assertEqual(self._call(key, 11), 11)
        finally:
            del os.environ[key]

    def test_min_val_clamps_up(self):
        self.assertEqual(self._call("X", 1, min_val=5), 5)

    def test_max_val_clamps_down(self):
        self.assertEqual(self._call("X", 100, max_val=10), 10)

    def test_min_max_both_apply(self):
        self.assertEqual(self._call("X", 50, min_val=10, max_val=20), 20)

    def test_value_within_bounds_unmodified(self):
        self.assertEqual(self._call("X", 15, min_val=10, max_val=20), 15)


class TestConfigDefaults(unittest.TestCase):
    """Verify critical default values are correct after refactor."""

    def test_price_tolerance_percent_default(self):
        from importlib import reload
        import config as cfg
        os.environ.pop("PRICE_TOLERANCE_PERCENT", None)
        reload(cfg)
        self.assertEqual(cfg.PRICE_TOLERANCE_PERCENT, 5)

    def test_stablecoin_tolerance_percent_default(self):
        from importlib import reload
        import config as cfg
        os.environ.pop("STABLECOIN_TOLERANCE_PERCENT", None)
        reload(cfg)
        self.assertEqual(cfg.STABLECOIN_TOLERANCE_PERCENT, 0)

    def test_price_tolerance_percent_respects_env(self):
        os.environ["PRICE_TOLERANCE_PERCENT"] = "15"
        try:
            from importlib import reload
            import config as cfg
            reload(cfg)
            self.assertEqual(cfg.PRICE_TOLERANCE_PERCENT, 15)
        finally:
            os.environ.pop("PRICE_TOLERANCE_PERCENT", None)

    def test_stablecoin_tolerance_percent_clamped_at_10(self):
        os.environ["STABLECOIN_TOLERANCE_PERCENT"] = "99"
        try:
            from importlib import reload
            import config as cfg
            reload(cfg)
            self.assertEqual(cfg.STABLECOIN_TOLERANCE_PERCENT, 10)
        finally:
            os.environ.pop("STABLECOIN_TOLERANCE_PERCENT", None)

    def test_stablecoin_tolerance_percent_clamped_at_0(self):
        os.environ["STABLECOIN_TOLERANCE_PERCENT"] = "-5"
        try:
            from importlib import reload
            import config as cfg
            reload(cfg)
            self.assertEqual(cfg.STABLECOIN_TOLERANCE_PERCENT, 0)
        finally:
            os.environ.pop("STABLECOIN_TOLERANCE_PERCENT", None)

    def test_required_confirmations_default(self):
        from importlib import reload
        import config as cfg
        os.environ.pop("REQUIRED_CONFIRMATIONS", None)
        reload(cfg)
        self.assertEqual(cfg.REQUIRED_CONFIRMATIONS, 3)

    def test_access_duration_default(self):
        from importlib import reload
        import config as cfg
        os.environ.pop("ACCESS_DURATION", None)
        reload(cfg)
        self.assertEqual(cfg.ACCESS_DURATION, 86400)


if __name__ == "__main__":
    unittest.main()
