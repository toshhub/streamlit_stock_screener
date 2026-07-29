"""Shared context and URL helpers for every interactive stock chart."""

from __future__ import annotations

import math
import re
from datetime import datetime
from urllib.parse import urlencode


_OVERLAY_QUERY_FIELDS = {
    "buyDate": "buy_date",
    "exitDate": "exit_date",
    "windowStart": "window_start",
    "windowEnd": "window_end",
    "buyPrice": "buy_price",
    "targetPrice": "target_price",
    "stopPrice": "stop_price",
    "exitPrice": "exit_price",
    "exitReason": "exit_reason",
    "alertDate": "alert_date",
    "alertPrice": "alert_marker_price",
}


def _market(value):
    clean = str(value or "INDIA").strip().upper()
    return clean if clean in {"INDIA", "US"} else "INDIA"


def _symbol(value):
    return re.sub(r"\.NS$", "", str(value or "").strip().upper())


def _finite_price(value):
    try:
        price = float(value)
    except (TypeError, ValueError):
        return None
    return price if math.isfinite(price) and price > 0 else None


def _date_value(value):
    text = str(value or "").strip()
    if not text:
        return ""
    candidate = text[:10]
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", candidate):
        try:
            return datetime.strptime(candidate, "%Y-%m-%d").strftime("%Y-%m-%d")
        except ValueError:
            return ""
    return ""


def normalize_chart_alert_markers(markers):
    """Return compact, deterministic markers safe for chart serialization."""
    normalized = []
    seen = set()
    for marker in markers or []:
        if not isinstance(marker, dict):
            continue
        price = _finite_price(
            marker.get("price", marker.get("target_price"))
        )
        if price is None:
            continue
        date = _date_value(
            marker.get(
                "date",
                marker.get(
                    "created_candle_date",
                    marker.get("created_at"),
                ),
            )
        )
        direction = (
            "below"
            if str(marker.get("direction") or "").strip().lower() == "below"
            else "above"
        )
        marker_id = str(marker.get("id") or "").strip()
        identity = (round(price, 8), date, direction)
        if identity in seen:
            continue
        seen.add(identity)
        normalized.append({
            "id": marker_id,
            "date": date,
            "price": round(price, 8),
            "direction": direction,
        })
    return sorted(
        normalized,
        key=lambda marker: (
            marker["price"],
            marker["date"],
            marker["direction"],
            marker["id"],
        ),
    )


def active_alert_markers(alerts, symbol, market):
    """Select every active alert belonging to one chart symbol and market."""
    expected_symbol = _symbol(symbol)
    expected_market = _market(market)
    matching = [
        alert
        for alert in alerts or []
        if isinstance(alert, dict)
        and str(alert.get("status") or "").strip().lower() == "active"
        and _symbol(alert.get("symbol")) == expected_symbol
        and _market(alert.get("market")) == expected_market
    ]
    return normalize_chart_alert_markers(matching)


def chart_alert_context(alerts, symbol, market, trade_overlay=None):
    """Combine a selected alert overlay with the user's active alert markers."""
    overlay = dict(trade_overlay or {})
    markers = active_alert_markers(alerts, symbol, market)
    selected_price = _finite_price(overlay.get("alertPrice"))
    if selected_price is not None and any(
        math.isclose(
            selected_price,
            marker["price"],
            rel_tol=0,
            abs_tol=1e-8,
        )
        for marker in markers
    ):
        # The active-alert collection already owns this marker. Removing the
        # legacy single-alert fields prevents duplicate lines and arrows.
        overlay.pop("alertDate", None)
        overlay.pop("alertPrice", None)
    return overlay, markers


def interactive_chart_query(
    symbol,
    market,
    *,
    ma_periods=None,
    pe_ratio=None,
    trade_overlay=None,
    embedded=False,
    initial_range=None,
):
    """Build one canonical query string for every interactive-chart caller."""
    params = {
        "interactive_chart": str(symbol or "").strip(),
        "market": _market(market),
    }
    periods = [
        str(period).strip()
        for period in (ma_periods or [])
        if str(period).strip()
    ]
    if periods:
        params["ma"] = ",".join(periods)
    if pe_ratio is not None:
        try:
            if math.isfinite(float(pe_ratio)):
                params["pe"] = str(pe_ratio)
        except (TypeError, ValueError):
            pass
    if embedded:
        params["embedded"] = "1"
    if initial_range:
        params["range"] = str(initial_range)
    for overlay_key, query_key in _OVERLAY_QUERY_FIELDS.items():
        value = (trade_overlay or {}).get(overlay_key)
        if value is not None and str(value).strip():
            params[query_key] = str(value)
    return "?" + urlencode(params)
