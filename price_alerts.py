import hashlib
import json
import math
import re
import threading
from contextvars import ContextVar
from datetime import datetime
from pathlib import Path

from config import DAILY_DIR, META_DIR, US_DAILY_DIR


PRICE_ALERTS_FILE = META_DIR / "price_alerts.json"
_ALERTS_LOCK = threading.RLock()
_VALID_MARKETS = {"INDIA", "US"}
_CLOUD_BACKEND = None
_REQUIRE_AUTH_FOR_WRITES = False
_CURRENT_USER_ID = ContextVar("price_alert_user_id", default=None)


def configure_cloud_alerts(backend, require_auth=False):
    """Configure the process-wide cloud backend without storing session identity globally."""
    global _CLOUD_BACKEND, _REQUIRE_AUTH_FOR_WRITES
    _CLOUD_BACKEND = backend
    _REQUIRE_AUTH_FOR_WRITES = bool(require_auth)


def set_current_alert_user(user_id):
    """Set the authenticated user for the current Streamlit session thread."""
    _CURRENT_USER_ID.set(str(user_id).strip() if user_id else None)


def cloud_alerts_enabled():
    return _CLOUD_BACKEND is not None


def _normalize_market(market):
    clean = str(market or "INDIA").strip().upper()
    return clean if clean in _VALID_MARKETS else "INDIA"


def _normalize_symbol(symbol):
    clean = str(symbol or "").strip().upper()
    clean = re.sub(r"\.NS$", "", clean)
    if not clean or not re.fullmatch(r"[A-Z0-9._-]+", clean):
        raise ValueError("Invalid stock symbol.")
    return clean


def _normalize_price(value):
    try:
        price = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("Enter a valid alert price.") from exc
    if not math.isfinite(price) or price <= 0:
        raise ValueError("Alert price must be greater than zero.")
    return round(price, 8)


def _read_alerts_unlocked():
    if not PRICE_ALERTS_FILE.exists():
        return []
    try:
        data = json.loads(PRICE_ALERTS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return data if isinstance(data, list) else []


def _write_alerts_unlocked(alerts):
    PRICE_ALERTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    temp_file = PRICE_ALERTS_FILE.with_suffix(PRICE_ALERTS_FILE.suffix + ".tmp")
    temp_file.write_text(json.dumps(alerts, indent=2), encoding="utf-8")
    temp_file.replace(PRICE_ALERTS_FILE)


def load_price_alerts():
    if _REQUIRE_AUTH_FOR_WRITES and not _CURRENT_USER_ID.get():
        return []
    if _CLOUD_BACKEND is not None:
        user_id = _CURRENT_USER_ID.get()
        if not user_id:
            return []
        return _CLOUD_BACKEND.load_alerts(user_id)
    with _ALERTS_LOCK:
        return [dict(alert) for alert in _read_alerts_unlocked()]


def sort_price_alerts(alerts):
    """Put active alerts first and show the newest relevant date first."""
    def relevant_date(alert):
        if alert.get("status") == "Triggered":
            return (
                str(alert.get("triggered_candle_date") or ""),
                str(alert.get("triggered_at") or ""),
            )
        return (str(alert.get("created_at") or ""), "")

    newest_first = sorted(
        (dict(alert) for alert in alerts),
        key=relevant_date,
        reverse=True,
    )
    return sorted(
        newest_first,
        key=lambda alert: 0 if alert.get("status") == "Active" else 1,
    )


def _stock_file(market, symbol):
    directory = US_DAILY_DIR if _normalize_market(market) == "US" else DAILY_DIR
    return directory / f"{_normalize_symbol(symbol)}.json"


def _load_candles(stock_file):
    path = Path(stock_file)
    if not path.exists():
        return []
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(rows, list):
        return []
    valid_rows = [row for row in rows if isinstance(row, dict) and row.get("Date")]
    return sorted(valid_rows, key=lambda row: str(row.get("Date")))


def latest_stock_price(market, symbol, stock_file=None):
    candles = _load_candles(stock_file or _stock_file(market, symbol))
    if not candles:
        return None, None
    latest = candles[-1]
    try:
        close = float(latest.get("Close"))
    except (TypeError, ValueError):
        return None, str(latest.get("Date") or "")
    if not math.isfinite(close):
        return None, str(latest.get("Date") or "")
    return close, str(latest.get("Date") or "")


def create_price_alert(
    symbol,
    market,
    target_price,
    current_price=None,
    current_candle_date=None,
    stock_file=None,
):
    symbol = _normalize_symbol(symbol)
    market = _normalize_market(market)
    target_price = _normalize_price(target_price)
    if current_price is None or current_candle_date is None:
        latest_price, latest_date = latest_stock_price(market, symbol, stock_file=stock_file)
        if current_price is None:
            current_price = latest_price
        if current_candle_date is None:
            current_candle_date = latest_date
    current_price = _normalize_price(current_price)
    if math.isclose(target_price, current_price, rel_tol=0, abs_tol=1e-8):
        raise ValueError("Alert price must be different from the current price.")

    direction = "above" if target_price > current_price else "below"
    identity = f"{market}|{symbol}|{target_price:.8f}|{direction}"
    alert_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    alert = {
        "id": alert_id,
        "market": market,
        "symbol": symbol,
        "target_price": target_price,
        "direction": direction,
        "status": "Active",
        "reference_price": round(current_price, 8),
        "created_at": now,
        "created_candle_date": str(current_candle_date or ""),
        "last_checked_date": str(current_candle_date or ""),
        "triggered_at": "",
        "triggered_candle_date": "",
        "triggered_price": None,
    }
    if _REQUIRE_AUTH_FOR_WRITES and not _CURRENT_USER_ID.get():
        raise PermissionError("Sign in with Google to create price alerts.")
    if _REQUIRE_AUTH_FOR_WRITES and _CLOUD_BACKEND is None:
        raise RuntimeError("Cloud alert storage is not configured.")
    if _CLOUD_BACKEND is not None:
        user_id = _CURRENT_USER_ID.get()
        if not user_id:
            raise PermissionError("Sign in with Google to create price alerts.")
        return _CLOUD_BACKEND.create_alert(user_id, alert)
    with _ALERTS_LOCK:
        alerts = _read_alerts_unlocked()
        existing = next((item for item in alerts if item.get("id") == alert_id), None)
        if existing:
            return dict(existing), False
        alerts.append(alert)
        _write_alerts_unlocked(alerts)
    return dict(alert), True


def _row_price(row, field):
    try:
        value = float(row.get(field))
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def check_price_alerts_for_symbol(symbol, market, stock_file=None):
    symbol = _normalize_symbol(symbol)
    market = _normalize_market(market)
    candles = _load_candles(stock_file or _stock_file(market, symbol))
    if not candles:
        return []

    if _REQUIRE_AUTH_FOR_WRITES and _CLOUD_BACKEND is None:
        return []
    if _CLOUD_BACKEND is not None:
        alerts = _CLOUD_BACKEND.load_active_alerts(symbol, market)
        triggered, changed_alerts = _evaluate_alerts(alerts, candles, symbol, market)
        if changed_alerts:
            _CLOUD_BACKEND.update_alerts(changed_alerts)
        return triggered

    with _ALERTS_LOCK:
        alerts = _read_alerts_unlocked()
        triggered, changed_alerts = _evaluate_alerts(alerts, candles, symbol, market)
        if changed_alerts:
            _write_alerts_unlocked(alerts)
    return triggered


def remove_price_alerts(alert_ids):
    remove_ids = {str(alert_id) for alert_id in alert_ids if alert_id}
    if not remove_ids:
        return 0
    if _REQUIRE_AUTH_FOR_WRITES and not _CURRENT_USER_ID.get():
        raise PermissionError("Sign in with Google to remove price alerts.")
    if _REQUIRE_AUTH_FOR_WRITES and _CLOUD_BACKEND is None:
        raise RuntimeError("Cloud alert storage is not configured.")
    if _CLOUD_BACKEND is not None:
        user_id = _CURRENT_USER_ID.get()
        if not user_id:
            raise PermissionError("Sign in with Google to remove price alerts.")
        return _CLOUD_BACKEND.remove_alerts(user_id, remove_ids)
    with _ALERTS_LOCK:
        alerts = _read_alerts_unlocked()
        kept = [alert for alert in alerts if str(alert.get("id")) not in remove_ids]
        removed = len(alerts) - len(kept)
        if removed:
            _write_alerts_unlocked(kept)
        return removed


def _evaluate_alerts(alerts, candles, symbol, market):
    """Apply downloaded candles to alert rows and return triggered/changed rows."""
    triggered = []
    changed_rows = []
    for alert in alerts:
        if (
            alert.get("status") != "Active"
            or alert.get("symbol") != symbol
            or alert.get("market") != market
        ):
            continue
        changed = False
        target = _normalize_price(alert.get("target_price"))
        created_date = str(alert.get("created_candle_date") or "")
        candidate_rows = [
            row for row in candles if str(row.get("Date") or "") > created_date
        ]
        hit_row = None
        hit_price = None
        for row in candidate_rows:
            if alert.get("direction") == "above":
                high = _row_price(row, "High")
                if high is not None and high >= target:
                    hit_row, hit_price = row, target
                    break
            else:
                low = _row_price(row, "Low")
                if low is not None and low <= target:
                    hit_row, hit_price = row, target
                    break
        latest_date = str(candles[-1].get("Date") or "")
        if alert.get("last_checked_date") != latest_date:
            alert["last_checked_date"] = latest_date
            changed = True
        if hit_row is not None:
            alert.update({
                "status": "Triggered",
                "triggered_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "triggered_candle_date": str(hit_row.get("Date") or ""),
                "triggered_price": round(float(hit_price), 8),
            })
            triggered.append(dict(alert))
            changed = True
        if changed:
            changed_rows.append(dict(alert))
    return triggered, changed_rows
