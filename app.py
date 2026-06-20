"""Captive Portal web application.

A Flask service that asks Wi-Fi users to pay a small amount of crypto before
granting network access. Supports multiple EVM chains via Blockscout public
APIs (no API key required for most chains).

Each user/session is assigned a unique deposit address derived from a BIP39
mnemonic using the standard Ethereum derivation path (m/44'/60'/0'/0/{index}).
"""

import io
import ipaddress
import os
import secrets
import time
from urllib.parse import urlparse

import qrcode
from flask import (
    Flask,
    jsonify,
    make_response,
    redirect,
    render_template,
    request,
    send_file,
)

from config import (
    GRACE_DURATION_SECONDS, GRACE_QUOTA_BYTES, PRICE_TOLERANCE_PERCENT, PRICE_LOCK_MODE, DEFAULT_TOKEN,
    PORTAL_TITLE, PORTAL_WELCOME, PORTAL_LEAD, PORTAL_FOOTER, PORTAL_SUPPORT_URL, PORTAL_LOGO_URL,
    QR_FILL_COLOR, QR_BACK_COLOR,
    DEV_MODE, CAPTIVE_DEV_TOKEN, TRUSTED_PROXIES,
    _env_int,
)
from chains import CHAINS, DEFAULT_CHAIN, get_chain_config
from db import (
    init_db, _db_conn,
    get_client_status,
    get_active_pending_payments,
    get_or_create_pending_payment, is_paid, activate_grace, mark_paid,
)
from verification import verify_payment_on_chain
from price_service import PriceService

app = Flask(__name__)
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

_price_service = PriceService(cache_ttl=60)

init_db()


# ---------------------------------------------------------------------------
# Client identification
# ---------------------------------------------------------------------------
def get_client_ip():
    """Return the client IP.

    Trust X-Forwarded-For only when the direct connection comes from a trusted
    proxy to prevent spoofing by remote clients. Validate the forwarded IP.
    """
    remote = request.remote_addr or "unknown"
    forwarded = request.headers.get("X-Forwarded-For", "")
    try:
        remote_net = ipaddress.ip_network(remote, strict=False)
    except ValueError:
        remote_net = None

    if forwarded and remote_net and any(remote_net.subnet_of(proxy) for proxy in TRUSTED_PROXIES):
        candidate = forwarded.split(",")[0].strip()
        try:
            ipaddress.ip_address(candidate)
            return candidate
        except ValueError:
            pass
    return remote


# ---------------------------------------------------------------------------
# Captive portal detection endpoints
# ---------------------------------------------------------------------------
@app.route("/generate_204")
def generate_204():
    """Android/Google captive portal probe."""
    if is_paid(get_client_ip()):
        return "", 204
    return redirect("/", code=302)


@app.route("/hotspot-detect.html")
def apple_detect():
    """Apple iOS/macOS captive portal probe."""
    if is_paid(get_client_ip()):
        return "<HTML><HEAD><TITLE>Success</TITLE></HEAD><BODY>Success</BODY></HTML>"
    return redirect("/", code=302)


@app.route("/connecttest.txt")
def ms_connect_test():
    """Microsoft Windows captive portal probe."""
    if is_paid(get_client_ip()):
        return make_response("Microsoft Connect Test", 200, {"Content-Type": "text/plain"})
    return redirect("/", code=302)


# ---------------------------------------------------------------------------
# Main portal routes
# ---------------------------------------------------------------------------
def _selected_tier_index(cfg):
    """Parse the tier_index query parameter for the current request."""
    try:
        tier_index = int(request.args.get("tier", "0"))
    except (TypeError, ValueError):
        tier_index = 0
    tiers = cfg["tiers"]
    if not (0 <= tier_index < len(tiers)):
        tier_index = 0
    return tier_index


@app.route("/")
def index():
    """Welcome / payment page."""
    client_ip = get_client_ip()
    now = int(time.time())
    client_status = get_client_status(client_ip) or {
        "status": "new",
        "grace_until": 0,
        "paid_until": 0,
        "quota_bytes": 0,
        "used_bytes": 0,
    }

    # Paid or grace still active -> go to success page.
    if (
        client_status["status"] == "paid"
        and client_status["paid_until"] > now
        and client_status["used_bytes"] < client_status["quota_bytes"]
    ):
        return redirect("/success?chain=" + request.args.get("chain", DEFAULT_CHAIN), code=302)
    if (
        client_status["status"] == "grace"
        and client_status["grace_until"] > now
        and client_status["used_bytes"] < client_status["quota_bytes"]
    ):
        return redirect("/success?chain=" + request.args.get("chain", DEFAULT_CHAIN), code=302)

    # Grace/paid expired is shown as "expired" so the user can pay or re-activate grace.
    # sync-auth.sh will update ipset on its next cycle to revoke access.
    if client_status["status"] == "grace" and now >= client_status["grace_until"]:
        client_status["status"] = "expired"
        with _db_conn() as conn:
            conn.execute(
                "UPDATE clients SET status = 'expired' WHERE client_ip = ?",
                (client_ip,),
            )
    if client_status["status"] == "paid" and (
        now >= client_status["paid_until"]
        or client_status["used_bytes"] >= client_status["quota_bytes"]
    ):
        client_status["status"] = "expired"
        with _db_conn() as conn:
            conn.execute(
                "UPDATE clients SET status = 'expired' WHERE client_ip = ?",
                (client_ip,),
            )

    chain_id = request.args.get("chain", DEFAULT_CHAIN)
    if chain_id not in CHAINS:
        chain_id = DEFAULT_CHAIN
    cfg = CHAINS[chain_id]
    tier_index = _selected_tier_index(cfg)
    tier = cfg["tiers"][tier_index]

    token = request.args.get("token", DEFAULT_TOKEN).upper()
    tokens = cfg.get("tokens", {"ETH": {}})
    if token not in tokens:
        token = DEFAULT_TOKEN

    pending = get_or_create_pending_payment(client_ip, chain_id, tier_index, token=token, price_service=_price_service)

    amount_usd = tier.get("amount_usd", 0)
    token_info = tokens.get(token, {"type": "native"})

    needed_symbols = set()
    for t in cfg.get("tiers", []):
        if "amount_usd" in t:
            for sym in tokens:
                needed_symbols.add(sym)
    needed_symbols.add(token)

    prices = _price_service.get_prices(list(needed_symbols))

    price_error = False
    if not prices and any("amount_usd" in t for t in cfg.get("tiers", [])):
        price_error = True

    token_info = tokens.get(token, {"type": "native"})
    default_decimals = token_info.get("decimals", 18)
    amount_token, amount_unit, token_decimals = _price_service.convert_usd_to_token(
        amount_usd, token, prices, decimals=default_decimals
    )

    if amount_unit is None:
        amount_token = "0"
        amount_unit = "0"
        token_decimals = 18
        price_error = True

    token_price = prices.get(token, 0)

    config = {
        "ethAddress": pending["address"],
        "currentChain": chain_id,
        "chainId": cfg["chain_id"],
        "amountWei": str(amount_unit),
        "amountUsd": amount_usd,
        "amountToken": amount_token,
        "token": token,
        "tokenPrice": token_price,
        "tokenDecimals": token_decimals,
        "tokenType": token_info.get("type", "native"),
        "tokenAddress": token_info.get("address", ""),
        "quotaBytes": tier["quota_bytes"],
        "tierIndex": tier_index,
        "clientStatus": client_status["status"],
        "priceTolerance": PRICE_TOLERANCE_PERCENT,
        "priceLockMode": PRICE_LOCK_MODE,
        "priceError": price_error,
        "pollInterval": _env_int("POLL_INTERVAL_MS", 3000, min_val=500),
        "pollMaxInterval": _env_int("POLL_MAX_INTERVAL_MS", 30000, min_val=1000),
        "redirectDelay": _env_int("REDIRECT_DELAY_MS", 1000, min_val=100),
        "lowBytesThreshold": _env_int("LOW_BYTES_THRESHOLD", 52428800, min_val=1048576),
        "lowTimeThreshold": _env_int("LOW_TIME_THRESHOLD", 600, min_val=10),
        "fetchTimeout": _env_int("FETCH_TIMEOUT_MS", 20000, min_val=1000),
    }

    enriched_tiers = []
    for i, t in enumerate(cfg.get("tiers", [])):
        t_usd = t.get("amount_usd", 0)
        t_amount, t_unit, _ = _price_service.convert_usd_to_token(t_usd, token, prices, decimals=default_decimals)
        enriched_tiers.append({
            "amount_usd": t_usd,
            "amount_token": t_amount or "0",
            "amount_unit": str(t_unit or "0"),
            "quota_bytes": t["quota_bytes"],
            "index": i,
        })

    return render_template(
        "index.html",
        eth_address=pending["address"],
        config=config,
        chain_id=chain_id,
        chain_name=cfg["name"],
        chain_chain_id=cfg["chain_id"],
        amount_token=amount_token,
        amount_unit=amount_unit,
        amount_usd=amount_usd,
        token=token,
        token_price=token_price,
        quota_bytes=tier["quota_bytes"],
        tier_index=tier_index,
        tiers=enriched_tiers,
        tokens=tokens,
        client_status=client_status,
        grace_duration=GRACE_DURATION_SECONDS,
        grace_quota=GRACE_QUOTA_BYTES,
        portal_title=PORTAL_TITLE,
        portal_welcome=PORTAL_WELCOME,
        portal_lead=PORTAL_LEAD,
        portal_footer=PORTAL_FOOTER,
        portal_support_url=PORTAL_SUPPORT_URL,
        portal_logo_url=PORTAL_LOGO_URL,
        price_error=price_error,
    )


@app.route("/success")
def success():
    """Page shown after payment is confirmed."""
    client_ip = get_client_ip()
    if not is_paid(client_ip):
        return redirect("/", code=302)
    client_status = get_client_status(client_ip) or {}
    chain_id = request.args.get("chain", DEFAULT_CHAIN)
    if chain_id not in CHAINS:
        chain_id = DEFAULT_CHAIN
    cfg = CHAINS[chain_id]
    return render_template(
        "success.html",
        chain_name=cfg["name"],
        chain_icon=cfg["icon"],
        client_status=client_status,
        status_poll_interval=_env_int("STATUS_POLL_INTERVAL_MS", 3000, min_val=500),
        network_check_interval=_env_int("NETWORK_CHECK_INTERVAL_MS", 3000, min_val=500),
    )


@app.route("/api/health")
def health_check():
    from db import check_db_integrity
    checks = {}
    try:
        with _db_conn() as conn:
            conn.execute("SELECT 1")
        checks["database"] = "ok"
    except Exception:
        checks["database"] = "error"
    ok, msg = check_db_integrity()
    checks["integrity"] = "ok" if ok else f"error: {msg}"
    try:
        with _db_conn() as conn:
            row = conn.execute("SELECT COUNT(*) FROM clients WHERE status IN ('paid','grace')").fetchone()
            checks["active_clients"] = row[0]
    except Exception:
        checks["active_clients"] = -1
    status = 200 if checks.get("database") == "ok" and checks.get("integrity") == "ok" else 503
    return jsonify(checks), status


@app.route("/api/chains")
def list_chains():
    """Return supported chains and their payment tiers with prices."""
    needed_symbols = set()
    for c in CHAINS.values():
        for t in c.get("tiers", []):
            if "amount_usd" in t:
                for sym in c.get("tokens", {"ETH": {}}):
                    needed_symbols.add(sym)
    prices = _price_service.get_prices(list(needed_symbols)) if needed_symbols else {}

    result = {}
    for chain_id, c in CHAINS.items():
        tokens = c.get("tokens", {"ETH": {"type": "native"}})
        enriched_tiers = []
        for t in c.get("tiers", []):
            tier_data = {"quota_bytes": t["quota_bytes"], "index": len(enriched_tiers)}
            if "amount_usd" in t:
                tier_data["amount_usd"] = t["amount_usd"]
                for sym in tokens:
                    sym_info = tokens[sym]
                    sym_decimals = sym_info.get("decimals", 18)
                    _, unit_val, dec = _price_service.convert_usd_to_token(t["amount_usd"], sym, prices, decimals=sym_decimals)
                    tier_data[f"amount_{sym.lower()}"] = unit_val or "0"
                    tier_data[f"decimals_{sym.lower()}"] = dec
            if "amount_wei" in t:
                tier_data["amount_wei"] = t["amount_wei"]
                tier_data["amount_eth"] = t.get("amount_eth", "0")
            enriched_tiers.append(tier_data)
        result[chain_id] = {
            "name": c["name"],
            "chain_id": c["chain_id"],
            "block_time": c["block_time"],
            "recommended": c["recommended"],
            "icon": c["icon"],
            "tokens": {sym: {"type": info.get("type", "native"), "address": info.get("address", "")} for sym, info in tokens.items()},
            "tiers": enriched_tiers,
        }
    return jsonify(result)


@app.route("/api/status")
def api_status():
    """Return current client authorization status."""
    client_ip = get_client_ip()
    chain_id = request.args.get("chain", DEFAULT_CHAIN)
    if chain_id not in CHAINS:
        chain_id = DEFAULT_CHAIN
    status = get_client_status(client_ip) or {
        "status": "new",
        "grace_until": 0,
        "paid_until": 0,
        "quota_bytes": 0,
        "used_bytes": 0,
    }
    remaining_bytes = max(0, status["quota_bytes"] - status["used_bytes"])
    return jsonify(
        {
            "status": status["status"],
            "grace_until": status["grace_until"],
            "paid_until": status["paid_until"],
            "quota_bytes": status["quota_bytes"],
            "used_bytes": status["used_bytes"],
            "remaining_bytes": remaining_bytes,
            "current_chain": chain_id,
        }
    )


@app.route("/api/qr")
def qr_image():
    """Generate the payment QR code locally and return it as a PNG image."""
    client_ip = get_client_ip()
    chain_id = request.args.get("chain", DEFAULT_CHAIN)
    address = request.args.get("address", "")
    token = request.args.get("token", DEFAULT_TOKEN).upper()
    cfg = get_chain_config(chain_id)

    now = int(time.time())
    with _db_conn() as conn:
        row = conn.execute(
            """
            SELECT amount_wei, tier_index
            FROM pending_payments
            WHERE client_ip = ? AND chain_id = ? AND address = ? AND status = 'pending' AND expires_at > ?
            """,
            (client_ip, chain_id, address, now),
        ).fetchone()
    if not row:
        return make_response("Forbidden: address does not belong to active pending payment", 403)

    tokens = cfg.get("tokens", {"ETH": {"type": "native"}})
    token_info = tokens.get(token, {"type": "native"})

    needed_symbols = list(tokens.keys())
    prices = _price_service.get_prices(needed_symbols)

    tier_index = row[1]
    tier = cfg["tiers"][tier_index] if tier_index < len(cfg["tiers"]) else cfg["tiers"][0]
    amount_usd = tier.get("amount_usd", 0)

    qr_decimals = token_info.get("decimals", 18)
    amount_token_str, amount_unit_val, _ = _price_service.convert_usd_to_token(amount_usd, token, prices, decimals=qr_decimals)

    if amount_unit_val is None:
        amount_unit_val = row[0]

    if token_info.get("type") == "erc20":
        token_addr = token_info.get("address", "")
        erc20_amount = int(amount_unit_val) if amount_unit_val else 0
        uri = f"ethereum:{token_addr}@{cfg['chain_id']}?function=transfer(address,uint256)&address={address}&uint256={erc20_amount}"
    else:
        uri = f"ethereum:{address}@{cfg['chain_id']}?value={amount_unit_val}"

    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        box_size=10,
        border=4,
    )
    qr.add_data(uri)
    qr.make(fit=True)

    img = qr.make_image(fill_color=QR_FILL_COLOR, back_color=QR_BACK_COLOR)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return send_file(buf, mimetype="image/png")


def _require_same_origin():
    """Return a 403 response if the request does not come from the same origin."""
    trusted_host = os.environ.get("SERVER_HOST", "localhost")
    allowed = request.scheme + "://" + trusted_host
    origin = request.headers.get("Origin") or request.headers.get("Referer") or ""
    parsed = urlparse(origin)
    if f"{parsed.scheme}://{parsed.netloc}" != allowed:
        return jsonify({"error": "Forbidden: invalid origin"}), 403
    return None


@app.route("/api/activate-grace", methods=["POST"])
def api_activate_grace():
    """Activate the short grace period for the requesting client."""
    forbidden = _require_same_origin()
    if forbidden:
        return forbidden
    client_ip = get_client_ip()
    ok, result = activate_grace(client_ip)
    if not ok:
        return jsonify({"ok": False, "error": result}), 429
    return jsonify({"ok": True, "grace_until": result["grace_until"], "quota_bytes": result["quota_bytes"]})


@app.route("/api/check-payment", methods=["POST"])
def check_payment():
    """Return the current payment status for the requesting client.

    Checks every active pending payment address for this client/chain so that
    switching tiers after a payment still detects the incoming transaction.
    """
    forbidden = _require_same_origin()
    if forbidden:
        return forbidden

    client_ip = get_client_ip()

    # Simple per-IP rate limit: max 5 requests per second.
    now_ts = time.time()
    rl_key = client_ip
    if not hasattr(app, "_rate_limits"):
        app._rate_limits = {}
    # Evict entries older than 60 seconds to prevent unbounded growth.
    stale_keys = [k for k, v in app._rate_limits.items() if now_ts - v[1] > 60]
    for k in stale_keys:
        del app._rate_limits[k]
    rl = app._rate_limits.get(rl_key)
    if rl and now_ts - rl[1] < 1.0:
        if rl[0] >= 5:
            return jsonify({"paid": False, "error": "Rate limit exceeded"}), 429
        app._rate_limits[rl_key] = (rl[0] + 1, rl[1])
    else:
        app._rate_limits[rl_key] = (1, now_ts)

    chain_id = request.args.get("chain", DEFAULT_CHAIN)

    if chain_id not in CHAINS:
        return jsonify({"paid": False, "error": "Unsupported chain"}), 400

    if is_paid(client_ip):
        client_status = get_client_status(client_ip) or {}
        return jsonify(
            {
                "paid": True,
                "status": client_status.get("status", "paid"),
            }
        )

    pending_list = get_active_pending_payments(client_ip, chain_id)
    if not pending_list:
        return jsonify(
            {"paid": False, "error": "No pending payment found"}
        ), 404

    last_message = ""
    for pending in pending_list:
        ok, tx_hash, quota_bytes, last_message = verify_payment_on_chain(
            pending["address"], chain_id, client_ip=client_ip, created_at=pending["created_at"],
            price_service=_price_service,
        )
        if ok:
            mark_paid(client_ip, tx_hash, chain_id, "blockscout", quota_bytes, pending["derivation_index"])
            return jsonify(
                {
                    "paid": True,
                    "status": "paid",
                    "tx_hash": tx_hash,
                    "quota_bytes": quota_bytes,
                }
            )

    pending = pending_list[0]
    return jsonify(
        {
            "paid": False,
            "address": pending["address"],
            "amount_wei": pending["amount_wei"],
            "message": last_message,
        }
    )


# Register simulate-payment only in dev mode so it cannot be enabled by accident.
if DEV_MODE:

    @app.route("/api/simulate-payment", methods=["POST"])
    def simulate_payment():
        """Development helper: mark the client as paid without a real tx.

        Restricted to a dev token to avoid exposing a backdoor when DEV_MODE is on.
        """
        dev_token = request.headers.get("X-Dev-Token", "")
        if not CAPTIVE_DEV_TOKEN or not secrets.compare_digest(dev_token, CAPTIVE_DEV_TOKEN):
            return jsonify({"paid": False, "error": "Forbidden: invalid or missing X-Dev-Token; set CAPTIVE_DEV_TOKEN"}), 403

        client_ip = get_client_ip()
        chain_id = request.args.get("chain", DEFAULT_CHAIN)
        if chain_id not in CHAINS:
            return jsonify({"paid": False, "error": "Unsupported chain"}), 400
        cfg = CHAINS[chain_id]
        tier_index = _selected_tier_index(cfg)
        tier = cfg["tiers"][tier_index]
        if is_paid(client_ip):
            return jsonify({"paid": True, "client_ip": client_ip, "quota_bytes": tier["quota_bytes"]})
        fake_hash = "0x" + secrets.token_hex(32)
        mark_paid(client_ip, fake_hash, chain_id, "dev", tier["quota_bytes"])
        return jsonify(
            {"paid": True, "client_ip": client_ip, "quota_bytes": tier["quota_bytes"]}
        )


@app.errorhandler(500)
def handle_500(e):
    return jsonify({"error": "Internal server error"}), 500


# ---------------------------------------------------------------------------
# Entry point for local development only. Production uses gunicorn.
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    # DEV_MODE defaults to localhost to avoid exposing the Werkzeug dev server on
    # all interfaces; production binds to 0.0.0.0.  Debug is always disabled so the
    # interactive Werkzeug debugger (which allows remote code execution) is never on.
    default_host = "127.0.0.1" if DEV_MODE else "0.0.0.0"
    host = os.environ.get("FLASK_HOST", default_host)
    port = _env_int("FLASK_PORT", 5000, min_val=1, max_val=65535)
    app.run(host=host, port=port, debug=False, use_reloader=DEV_MODE)
