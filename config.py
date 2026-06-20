"""Application configuration — env-var parsing, validation, and constants."""

import ipaddress
import logging
import os

logger = logging.getLogger(__name__)


def _env_int(key, default, min_val=None, max_val=None):
    try:
        val = int(os.environ.get(key, str(default)))
    except (ValueError, TypeError):
        val = default
    if min_val is not None:
        val = max(min_val, val)
    if max_val is not None:
        val = min(max_val, val)
    return val


# --- Access / payment durations ---
REQUIRED_CONFIRMATIONS = _env_int("REQUIRED_CONFIRMATIONS", 3, min_val=1)
ACCESS_DURATION = _env_int("ACCESS_DURATION", 86400, min_val=60)
PAYMENT_PENDING_DURATION = _env_int("PAYMENT_PENDING_DURATION", 3600, min_val=60)

# --- Grace period ---
GRACE_DURATION_SECONDS = _env_int("GRACE_DURATION_SECONDS", 300, min_val=10)
GRACE_QUOTA_BYTES = _env_int("GRACE_QUOTA_BYTES", 104857600, min_val=1048576)
GRACE_MAX_PER_24H = _env_int("GRACE_MAX_PER_24H", 1, min_val=0)
GRACE_COOLDOWN_SECONDS = _env_int("GRACE_COOLDOWN_SECONDS", 3600, min_val=0)

# --- Quota limits ---
MAX_ACCESS_DURATION = _env_int("MAX_ACCESS_DURATION", 86400 * 30, min_val=86400)
MAX_QUOTA_BYTES = _env_int("MAX_QUOTA_BYTES", 1073741824 * 10, min_val=1073741824)

# --- Database ---
DB_BUSY_TIMEOUT = _env_int("DB_BUSY_TIMEOUT", 5000, min_val=1000)
PENDING_CLEANUP_DAYS = _env_int("PENDING_CLEANUP_DAYS", 7, min_val=1)
PAYMENTS_CLEANUP_DAYS = _env_int("PAYMENTS_CLEANUP_DAYS", 90, min_val=1)

# --- Verification ---
TX_TIMESTAMP_TOLERANCE = _env_int("TX_TIMESTAMP_TOLERANCE", 300, min_val=0)
try:
    PRICE_TOLERANCE_PERCENT = max(0, min(50, int(os.environ.get("PRICE_TOLERANCE_PERCENT", "5"))))
except (ValueError, TypeError):
    PRICE_TOLERANCE_PERCENT = 5
STABLECOIN_SYMBOLS = {"USDC", "USDT", "DAI", "FRAX"}
STABLECOIN_TOLERANCE_PERCENT = max(0, min(10, int(os.environ.get("STABLECOIN_TOLERANCE_PERCENT", "0"))))
PRICE_LOCK_MODE = os.environ.get("PRICE_LOCK_MODE", "lock")
try:
    PRICE_LOCK_DURATION = max(60, int(os.environ.get("PRICE_LOCK_DURATION", "900")))
except (ValueError, TypeError):
    PRICE_LOCK_DURATION = 900
DEFAULT_TOKEN = os.environ.get("DEFAULT_TOKEN", "ETH")
FALLBACK_CURRENCY = os.environ.get("FALLBACK_CURRENCY", "usd")

# --- Portal UI ---
PORTAL_TITLE = os.environ.get("PORTAL_TITLE", "Wi-Fi 支付 portal")
PORTAL_WELCOME = os.environ.get("PORTAL_WELCOME", "欢迎连接 Wi-Fi")
PORTAL_LEAD = os.environ.get("PORTAL_LEAD", "支付少量加密货币即可使用本无线网络。")
PORTAL_FOOTER = os.environ.get("PORTAL_FOOTER", "")
PORTAL_SUPPORT_URL = os.environ.get("PORTAL_SUPPORT_URL", "")
PORTAL_LOGO_URL = os.environ.get("PORTAL_LOGO_URL", "")
QR_FILL_COLOR = os.environ.get("QR_FILL_COLOR", "black")
QR_BACK_COLOR = os.environ.get("QR_BACK_COLOR", "white")

# --- Dev mode ---
DEV_MODE = os.environ.get("CAPTIVE_PORTAL_DEV", "false").lower() in ("1", "true", "yes")
CAPTIVE_DEV_TOKEN = os.environ.get("CAPTIVE_DEV_TOKEN", "").strip()

# --- Database path ---
DB_PATH = os.environ.get("CAPTIVE_DB", "/var/lib/captive-portal/payments.db")


def _validate_db_path(db_path):
    """Ensure DB_PATH is under the expected directory."""
    expected_dir = os.path.dirname("/var/lib/captive-portal/payments.db")
    real_path = os.path.realpath(db_path)
    real_expected = os.path.realpath(expected_dir)
    if not real_path.startswith(real_expected + os.sep) and real_path != real_expected:
        logger.warning("DB_PATH %s is outside expected directory %s", db_path, expected_dir)


# --- Trusted proxies ---
_TRUSTED_PROXY_RAW = os.environ.get("TRUSTED_PROXIES", "127.0.0.1,::1").split(",")
TRUSTED_PROXIES = []
for _raw in _TRUSTED_PROXY_RAW:
    _raw = _raw.strip()
    if not _raw:
        continue
    try:
        TRUSTED_PROXIES.append(ipaddress.ip_network(_raw, strict=False))
    except ValueError:
        pass

# --- Server host (used for origin checking) ---
SERVER_HOST = os.environ.get("SERVER_HOST", "localhost")

# --- Verify cache ---
VERIFY_CACHE_SECONDS = 5
VERIFY_CACHE_MAX_SIZE = 1000


def log_resolved_config():
    """Log all resolved configuration values at startup."""
    logger.info("REQUIRED_CONFIRMATIONS=%s", REQUIRED_CONFIRMATIONS)
    logger.info("ACCESS_DURATION=%s", ACCESS_DURATION)
    logger.info("PAYMENT_PENDING_DURATION=%s", PAYMENT_PENDING_DURATION)
    logger.info("GRACE_DURATION_SECONDS=%s", GRACE_DURATION_SECONDS)
    logger.info("GRACE_QUOTA_BYTES=%s", GRACE_QUOTA_BYTES)
    logger.info("GRACE_MAX_PER_24H=%s", GRACE_MAX_PER_24H)
    logger.info("GRACE_COOLDOWN_SECONDS=%s", GRACE_COOLDOWN_SECONDS)
    logger.info("MAX_ACCESS_DURATION=%s", MAX_ACCESS_DURATION)
    logger.info("MAX_QUOTA_BYTES=%s", MAX_QUOTA_BYTES)
    logger.info("DB_BUSY_TIMEOUT=%s", DB_BUSY_TIMEOUT)
    logger.info("DB_PATH=%s", DB_PATH)
    logger.info("PENDING_CLEANUP_DAYS=%s", PENDING_CLEANUP_DAYS)
    logger.info("PAYMENTS_CLEANUP_DAYS=%s", PAYMENTS_CLEANUP_DAYS)
    logger.info("TX_TIMESTAMP_TOLERANCE=%s", TX_TIMESTAMP_TOLERANCE)
    logger.info("PRICE_TOLERANCE_PERCENT=%s", PRICE_TOLERANCE_PERCENT)
    logger.info("STABLECOIN_TOLERANCE_PERCENT=%s", STABLECOIN_TOLERANCE_PERCENT)
    logger.info("PRICE_LOCK_MODE=%s", PRICE_LOCK_MODE)
    logger.info("PRICE_LOCK_DURATION=%s", PRICE_LOCK_DURATION)
    logger.info("DEFAULT_TOKEN=%s", DEFAULT_TOKEN)
    logger.info("FALLBACK_CURRENCY=%s", FALLBACK_CURRENCY)
    logger.info("DEV_MODE=%s", DEV_MODE)
    logger.info("SERVER_HOST=%s", SERVER_HOST)
    logger.info("TRUSTED_PROXIES=%s", [str(p) for p in TRUSTED_PROXIES])
    logger.info("QR_FILL_COLOR=%s", QR_FILL_COLOR)
    logger.info("QR_BACK_COLOR=%s", QR_BACK_COLOR)
