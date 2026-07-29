import base64
import hashlib
import html
import json
import math
import re
from pathlib import Path
from urllib.parse import quote

from matplotlib.figure import Figure
import pandas as pd
from chart_context import (
    interactive_chart_query,
    normalize_chart_alert_markers,
)
from stock_data import (
    SCREENING_HISTORY_YEARS,
    earliest_stock_date,
    latest_stock_date,
    load_stock_dataframe,
    rolling_history_start,
)
from market_snapshots import valuation_chart_payload
import streamlit as st
import streamlit.components.v1 as components

from config import CHARTS_DIR
from price_alerts import create_price_alert
from screener import required_ma_periods


RESULTS_TABLE_RENDERER_VERSION = 6


MA_COLORS = [
    "#2563eb",
    "#dc2626",
    "#16a34a",
    "#9333ea",
    "#ea580c",
    "#0891b2",
    "#be123c",
]

INTERACTIVE_CHART_DEFAULT_MAS = [50, 200]

_CURSOR_ALERT_COMPONENT = components.declare_component(
    "cursor_alert_chart",
    path=str(Path(__file__).parent / "cursor_alert_component"),
)

_ALERT_TABLE_COMPONENT = components.declare_component(
    "alert_table_actions",
    path=str(Path(__file__).parent / "alert_table_component"),
)


def load_price_data(path):
    df = load_stock_dataframe(
        path,
        start=rolling_history_start(SCREENING_HISTORY_YEARS),
    )
    if "Date" in df.columns:
        df["Date"] = pd.to_datetime(df["Date"], errors="coerce")
        df = df.sort_values("Date")
    else:
        df["Date"] = range(1, len(df) + 1)

    df["Close"] = pd.to_numeric(df["Close"], errors="coerce")
    return df.dropna(subset=["Close"])


def _symbol_key(value):
    return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())


def _chart_source_from_path(chart_path):
    stem = Path(chart_path).stem
    parts = stem.rsplit("_", 1)
    if len(parts) == 2 and re.fullmatch(r"[0-9a-f]{12}", parts[1]):
        return parts[0]
    return stem


def _row_chart_matches_symbol(row_symbol, chart_path, chart_source=None):
    expected = _symbol_key(row_symbol)
    if not expected or not chart_path:
        return False

    source = chart_source or _chart_source_from_path(chart_path)
    return _symbol_key(source) == expected


def _chart_data_hash(chart_df):
    signature_columns = [
        column
        for column in ["Date", "Open", "High", "Low", "Close"]
        if column in chart_df.columns
    ]
    signature_df = chart_df[signature_columns].copy()
    if "Date" in signature_df.columns:
        signature_df["Date"] = pd.to_datetime(signature_df["Date"], errors="coerce").dt.strftime("%Y-%m-%d")
    raw = signature_df.to_json(orient="records", date_format="iso", default_handler=str)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _chart_context_fingerprint(json_path, chart_df, filter_set, max_points, max_years, pe_ratio, swing_annotations, date_markers, window_start_date, window_end_date):
    try:
        stat = json_path.stat()
        file_signature = {"mtime_ns": stat.st_mtime_ns, "size": stat.st_size}
    except OSError:
        file_signature = {}

    payload = {
        "style_version": 6,
        "source": str(json_path),
        "file": file_signature,
        "data_hash": _chart_data_hash(chart_df),
        "filter_set": filter_set,
        "max_points": max_points,
        "max_years": max_years,
        "pe_ratio": pe_ratio,
        "swing_annotations": swing_annotations or [],
        "date_markers": date_markers or [],
        "window_start_date": window_start_date,
        "window_end_date": window_end_date,
    }
    raw = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def _shade_trend_regions(ax, chart_df, short_trend_column, long_trend_column):
    if (
        not short_trend_column
        or not long_trend_column
        or short_trend_column not in chart_df.columns
        or long_trend_column not in chart_df.columns
        or len(chart_df) < 2
    ):
        return

    short_values = chart_df[short_trend_column]
    long_values = chart_df[long_trend_column]
    regimes = short_values >= long_values
    valid = regimes.notna() & short_values.notna() & long_values.notna()
    if not valid.any():
        return

    dates = chart_df["Date"].reset_index(drop=True)
    regimes = regimes.reset_index(drop=True)
    valid = valid.reset_index(drop=True)

    start_index = None
    current_regime = None
    for index, is_valid in enumerate(valid):
        if not is_valid:
            if start_index is not None and index - start_index > 1:
                color = "#dcfce7" if current_regime else "#fee2e2"
                ax.axvspan(dates.iloc[start_index], dates.iloc[index - 1], color=color, alpha=0.34, linewidth=0)
            start_index = None
            current_regime = None
            continue

        regime = bool(regimes.iloc[index])
        if start_index is None:
            start_index = index
            current_regime = regime
        elif regime != current_regime:
            color = "#dcfce7" if current_regime else "#fee2e2"
            ax.axvspan(dates.iloc[start_index], dates.iloc[index - 1], color=color, alpha=0.34, linewidth=0)
            start_index = index
            current_regime = regime

    if start_index is not None and len(dates) - start_index > 1:
        color = "#dcfce7" if current_regime else "#fee2e2"
        ax.axvspan(dates.iloc[start_index], dates.iloc[-1], color=color, alpha=0.34, linewidth=0)


def create_stock_chart(
    json_path,
    filter_set,
    output_dir=CHARTS_DIR,
    max_points=None,
    max_years=5,
    pe_ratio=None,
    swing_annotations=None,
    date_markers=None,
    window_start_date=None,
    window_end_date=None,
):
    json_path = Path(json_path)
    df = load_price_data(json_path)
    ma_periods = required_ma_periods(filter_set)
    if df.empty:
        return None

    for period in ma_periods:
        df[f"SMA{period}"] = df["Close"].rolling(period).mean()

    if window_start_date is not None or window_end_date is not None:
        chart_df = df
        if window_start_date is not None:
            chart_df = chart_df[chart_df["Date"] >= pd.Timestamp(window_start_date)]
        if window_end_date is not None:
            chart_df = chart_df[chart_df["Date"] <= pd.Timestamp(window_end_date)]
    elif max_years:
        last_available_date = df["Date"].dropna().iloc[-1]
        start_date = last_available_date - pd.DateOffset(years=max_years)
        chart_df = df[df["Date"] >= start_date]
    else:
        chart_df = df
    if max_points:
        chart_df = chart_df.tail(max_points)
    if chart_df.empty:
        return None

    output_dir.mkdir(parents=True, exist_ok=True)
    fingerprint = _chart_context_fingerprint(
        json_path,
        chart_df,
        filter_set,
        max_points,
        max_years,
        pe_ratio,
        swing_annotations,
        date_markers,
        window_start_date,
        window_end_date,
    )
    out_file = output_dir / f"{json_path.stem}_{fingerprint}.png"

    fig = Figure(figsize=(11, 6), facecolor="#f8fafc")
    ax = fig.subplots()
    ax.set_facecolor("#ffffff")

    last_date = chart_df["Date"].iloc[-1]
    x_lim_right = chart_df["Date"].iloc[-1] + pd.Timedelta(days=(chart_df["Date"].iloc[-1] - chart_df["Date"].iloc[0]).days * 0.12)

    short_trend_period = min(ma_periods) if len(ma_periods) >= 2 else None
    long_trend_period = max(ma_periods) if len(ma_periods) >= 2 else None
    short_trend_column = f"SMA{short_trend_period}" if short_trend_period else None
    long_trend_column = f"SMA{long_trend_period}" if long_trend_period else None
    _shade_trend_regions(ax, chart_df, short_trend_column, long_trend_column)

    close_min = chart_df["Close"].min()
    ax.fill_between(chart_df["Date"], chart_df["Close"], close_min, color="#0f172a", alpha=0.045, linewidth=0)
    ax.plot(chart_df["Date"], chart_df["Close"], label="Close", color="#0f172a", linewidth=2.15, zorder=4)

    for index, period in enumerate(ma_periods):
        ax.plot(
            chart_df["Date"],
            chart_df[f"SMA{period}"],
            label=f"SMA{period}",
            color=MA_COLORS[index % len(MA_COLORS)],
            linewidth=1.55 if period != long_trend_period else 2.0,
            alpha=0.92,
            zorder=3,
        )

    # Annotate latest values at the right edge, stacked vertically to avoid overlap
    annotation_entries = []
    latest_close = chart_df["Close"].iloc[-1]
    annotation_entries.append((latest_close, "Close", "#111827"))
    for index, period in enumerate(ma_periods):
        last_ma = chart_df[f"SMA{period}"].iloc[-1]
        if pd.notna(last_ma):
            annotation_entries.append((last_ma, f"SMA{period}", MA_COLORS[index % len(MA_COLORS)]))

    # Sort by y-value so we can stagger vertical offsets
    annotation_entries.sort(key=lambda entry: entry[0])

    n = len(annotation_entries)
    vertical_spacing = 15  # points between each label
    start_offset = -((n - 1) * vertical_spacing) / 2.0  # center the group

    for i, (y_value, label_text, col) in enumerate(annotation_entries):
        y_offset = start_offset + i * vertical_spacing
        ax.annotate(
            f"{y_value:.2f}",
            (last_date, y_value),
            textcoords="offset points",
            xytext=(8, y_offset),
            ha="left",
            va="center",
            color=col,
            fontsize=8.5,
            fontweight="bold",
            bbox=dict(boxstyle="round,pad=0.2", facecolor="white", edgecolor=col, alpha=0.85),
        )

    ax.set_xlim(chart_df["Date"].iloc[0], x_lim_right)

    if swing_annotations:
        latest_by_type = {
            "H": [swing for swing in reversed(swing_annotations) if swing["type"] == "H"][:3],
            "L": [swing for swing in reversed(swing_annotations) if swing["type"] == "L"][:3],
        }
        chart_dates = set(chart_df["Date"])
        for swing_type, swings in latest_by_type.items():
            color = "#dc2626" if swing_type == "H" else "#16a34a"
            marker = "v" if swing_type == "H" else "^"
            for label_index, swing in enumerate(swings, start=1):
                if swing["date"] not in chart_dates:
                    continue
                label = f"{swing_type}{label_index}"
                ax.scatter([swing["date"]], [swing["price"]], color=color, marker=marker, s=58, zorder=5)
                ax.annotate(
                    label,
                    (swing["date"], swing["price"]),
                    textcoords="offset points",
                    xytext=(0, 9 if swing_type == "H" else -15),
                    ha="center",
                    color=color,
                    fontsize=9,
                    fontweight="bold",
                )

    if date_markers:
        marker_styles = {
            "Start": {"color": "#16a34a", "x_offset": 0, "y_offset": 18, "ha": "center", "va": "bottom"},
            "End": {"color": "#dc2626", "x_offset": 0, "y_offset": -20, "ha": "center", "va": "top"},
            "BUY": {"color": "#16a34a", "x_offset": -10, "y_offset": 18, "ha": "right", "va": "bottom"},
            "TARGET": {"color": "#15803d", "x_offset": 10, "y_offset": -20, "ha": "left", "va": "top"},
            "STOP": {"color": "#dc2626", "x_offset": 10, "y_offset": -20, "ha": "left", "va": "top"},
            "END": {"color": "#dc2626", "x_offset": 10, "y_offset": -20, "ha": "left", "va": "top"},
        }
        marker_dates = pd.to_datetime(chart_df["Date"], errors="coerce")
        chart_min = marker_dates.min()
        chart_max = marker_dates.max()
        for marker in date_markers:
            marker_date = pd.to_datetime(marker.get("date"), errors="coerce")
            if pd.isna(marker_date) or marker_date < chart_min or marker_date > chart_max:
                continue
            label = marker.get("label", "")
            row_index = (marker_dates - marker_date).abs().idxmin()
            row = chart_df.loc[row_index]
            marker_price = marker.get("price", row["Close"])
            try:
                marker_price = float(marker_price)
            except (TypeError, ValueError):
                marker_price = float(row["Close"])
            style = marker_styles.get(label, {
                "color": "#7c3aed", "x_offset": 0, "y_offset": 18,
                "ha": "center", "va": "bottom",
            })
            ax.scatter(
                [row["Date"]],
                [marker_price],
                color=style["color"],
                marker="^" if label in {"Start", "BUY"} else "v",
                s=86,
                edgecolors="white",
                linewidths=0.9,
                zorder=6,
            )
            ax.annotate(
                label,
                (row["Date"], marker_price),
                textcoords="offset points",
                xytext=(style["x_offset"], style["y_offset"]),
                ha=style["ha"],
                va=style["va"],
                color=style["color"],
                fontsize=9,
                fontweight="bold",
                arrowprops=dict(arrowstyle="->", color=style["color"], lw=1.2),
                bbox=dict(boxstyle="round,pad=0.22", facecolor="white", edgecolor=style["color"], alpha=0.9),
            )

    if pe_ratio not in (None, ""):
        ax.text(
            0.012,
            0.91,
            f"PE: {pe_ratio}",
            transform=ax.transAxes,
            ha="left",
            va="top",
            fontsize=9.5,
            fontweight="bold",
            color="#334155",
            bbox=dict(boxstyle="round,pad=0.35", facecolor="#ffffff", edgecolor="#94a3b8", alpha=0.9),
        )

    ax.set_title(json_path.stem, loc="left", fontsize=14, fontweight="bold", color="#0f172a", pad=14)
    ax.set_xlabel("Date")
    ax.set_ylabel("Price")
    ax.grid(True, axis="y", color="#cbd5e1", alpha=0.45, linewidth=0.8)
    ax.grid(True, axis="x", color="#e2e8f0", alpha=0.3, linewidth=0.6)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.spines["left"].set_color("#cbd5e1")
    ax.spines["bottom"].set_color("#cbd5e1")
    ax.tick_params(colors="#475569", labelsize=9)
    ax.legend(loc="upper left", bbox_to_anchor=(0, 1.005), frameon=False, ncol=min(4, len(ma_periods) + 1))
    fig.tight_layout()
    fig.savefig(out_file, dpi=120)

    return str(out_file)


def image_to_data_uri(path):
    path = Path(path)
    with open(path, "rb") as image_file:
        encoded = base64.b64encode(image_file.read()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def normalize_interactive_ma_periods(periods):
    normalized = []
    for period in periods or []:
        try:
            value = int(float(period))
        except (TypeError, ValueError):
            continue
        if 1 <= value <= 1000 and value not in normalized:
            normalized.append(value)
    return sorted(normalized) or list(INTERACTIVE_CHART_DEFAULT_MAS)


def interactive_chart_payload(
    json_path,
    ma_periods=None,
    max_points=None,
    history_years=None,
):
    json_path = Path(json_path)
    ma_periods = normalize_interactive_ma_periods(ma_periods)
    latest_available = latest_stock_date(json_path)
    display_start = (
        rolling_history_start(history_years, as_of=latest_available)
        if history_years is not None
        else None
    )
    # Read a small warm-up period so moving averages are already valid at the
    # left edge, but send only the requested display window to the browser.
    read_start = display_start
    if display_start is not None and ma_periods:
        read_start = display_start - pd.Timedelta(days=max(ma_periods) * 2)
    df = load_stock_dataframe(json_path, start=read_start)
    required_columns = ["Date", "Open", "High", "Low", "Close"]
    missing_columns = [column for column in required_columns if column not in df.columns]
    if missing_columns:
        raise ValueError(f"Missing chart data: {', '.join(missing_columns)}")

    df["Date"] = pd.to_datetime(df["Date"], errors="coerce")
    for column in ["Open", "High", "Low", "Close", "Volume"]:
        if column in df.columns:
            df[column] = pd.to_numeric(df[column], errors="coerce")
    df = df.dropna(subset=required_columns).sort_values("Date").reset_index(drop=True)
    if df.empty:
        raise ValueError("No valid candle data is available for this stock.")

    for period in ma_periods:
        df[f"SMA{period}"] = df["Close"].rolling(period).mean()

    if display_start is not None:
        chart_df = df[df["Date"] >= display_start].copy()
    else:
        chart_df = df.copy()
    if max_points is not None:
        chart_df = df.tail(max(100, int(max_points))).copy()
        if display_start is not None:
            chart_df = chart_df[chart_df["Date"] >= display_start].copy()
    if chart_df.empty:
        raise ValueError("No candles are available in the requested chart window.")
    candles = [
        {
            "time": row.Date.strftime("%Y-%m-%d"),
            "open": round(float(row.Open), 4),
            "high": round(float(row.High), 4),
            "low": round(float(row.Low), 4),
            "close": round(float(row.Close), 4),
        }
        for row in chart_df.itertuples()
    ]

    moving_averages = {}
    for period in ma_periods:
        column = f"SMA{period}"
        moving_averages[column] = [
            {
                "time": row.Date.strftime("%Y-%m-%d"),
                "value": round(float(getattr(row, column)), 4),
            }
            for row in chart_df.itertuples()
            if pd.notna(getattr(row, column))
        ]

    volume = []
    if "Volume" in chart_df.columns:
        volume = [
            {
                "time": row.Date.strftime("%Y-%m-%d"),
                "value": max(0, int(row.Volume)) if pd.notna(row.Volume) else 0,
                "color": "rgba(22, 163, 74, 0.32)"
                if float(row.Close) >= float(row.Open)
                else "rgba(220, 38, 38, 0.30)",
            }
            for row in chart_df.itertuples()
        ]

    oldest_available = earliest_stock_date(json_path)
    has_earlier_history = bool(
        oldest_available is not None
        and pd.Timestamp(oldest_available) < pd.Timestamp(chart_df["Date"].iloc[0])
    )
    return {
        "candles": candles,
        "movingAverages": moving_averages,
        "volume": volume,
        "maPeriods": ma_periods,
        "pointCount": len(candles),
        "firstDate": candles[0]["time"],
        "lastDate": candles[-1]["time"],
        "historyYears": history_years,
        "hasEarlierHistory": has_earlier_history,
        "oldestAvailableDate": (
            oldest_available.strftime("%Y-%m-%d")
            if oldest_available is not None
            else candles[0]["time"]
        ),
    }


def historical_pe_valuation_state(current_pe, valuation_medians):
    pe_medians = (
        valuation_medians.get("Median PE", {})
        if isinstance(valuation_medians, dict)
        else {}
    )
    historical_values = []
    if isinstance(pe_medians, dict):
        for period in ("3 Years", "5 Years", "10 Years"):
            try:
                value = float(pe_medians.get(period))
                if pd.notna(value):
                    historical_values.append(value)
            except (TypeError, ValueError):
                pass
    if len(historical_values) != 3:
        return ""
    try:
        numeric_pe = float(current_pe)
        if not math.isfinite(numeric_pe) or numeric_pe <= 0:
            return "unfavorable"
    except (TypeError, ValueError):
        return "unfavorable"
    return (
        "favorable"
        if sum(numeric_pe < median for median in historical_values) >= 2
        else "unfavorable"
    )


def has_positive_current_pe(current_pe):
    try:
        numeric_pe = float(current_pe)
        return math.isfinite(numeric_pe) and numeric_pe > 0
    except (TypeError, ValueError):
        return False


def _attach_monthly_prices(json_path, valuation_rows):
    if not valuation_rows:
        return valuation_rows
    valuation_dates = pd.to_datetime(
        [row.get("time") for row in valuation_rows],
        errors="coerce",
    )
    valid_dates = valuation_dates[valuation_dates.notna()]
    if valid_dates.empty:
        return valuation_rows
    try:
        price_df = load_stock_dataframe(
            json_path,
            start=valid_dates.min() - pd.Timedelta(days=2),
        )
    except (OSError, ValueError):
        return valuation_rows
    if price_df.empty or "Date" not in price_df.columns or "Close" not in price_df.columns:
        return valuation_rows

    prices = price_df[["Date", "Close"]].copy()
    prices["Date"] = pd.to_datetime(prices["Date"], errors="coerce")
    prices["Close"] = pd.to_numeric(prices["Close"], errors="coerce")
    prices = prices.dropna(subset=["Date", "Close"]).sort_values("Date")
    if prices.empty:
        return valuation_rows
    valuation_frame = pd.DataFrame({
        "valuation_index": range(len(valuation_rows)),
        "Date": valuation_dates,
    }).dropna(subset=["Date"]).sort_values("Date")
    aligned = pd.merge_asof(
        valuation_frame,
        prices,
        on="Date",
        direction="forward",
        tolerance=pd.Timedelta(days=7),
    )
    price_by_index = {
        int(row.valuation_index): round(float(row.Close), 4)
        for row in aligned.itertuples()
        if pd.notna(row.Close)
    }
    return [
        {**row, "price": price_by_index.get(index)}
        for index, row in enumerate(valuation_rows)
    ]


def interactive_stock_chart_html(
    symbol,
    json_path,
    ma_periods=None,
    pe_ratio=None,
    match_position=None,
    match_total=None,
    has_previous=False,
    has_next=False,
    initial_range="252",
    growth_metrics=None,
    valuation_medians=None,
    trade_overlay=None,
    alert_markers=None,
    alert_market="INDIA",
    history_years=SCREENING_HISTORY_YEARS,
    restore_visible_range=None,
    watchlists=None,
):
    payload = interactive_chart_payload(
        json_path,
        ma_periods=ma_periods,
        history_years=history_years,
    )
    monthly_valuations = _attach_monthly_prices(
        json_path,
        valuation_chart_payload(symbol, alert_market),
    )
    payload["monthlyValuations"] = monthly_valuations
    if isinstance(restore_visible_range, dict):
        visible_from = str(restore_visible_range.get("from") or "")
        visible_to = str(restore_visible_range.get("to") or "")
        if visible_from and visible_to:
            payload["restoreVisibleRange"] = {
                "from": visible_from,
                "to": visible_to,
            }
    normalized_overlay = {}
    if isinstance(trade_overlay, dict):
        for key in (
            "buyDate",
            "exitDate",
            "windowStart",
            "windowEnd",
            "alertDate",
        ):
            value = trade_overlay.get(key)
            if value:
                parsed = pd.to_datetime(value, errors="coerce", dayfirst=False)
                if pd.notna(parsed):
                    normalized_overlay[key] = parsed.strftime("%Y-%m-%d")
        for key in (
            "buyPrice",
            "targetPrice",
            "stopPrice",
            "exitPrice",
            "alertPrice",
        ):
            try:
                value = float(trade_overlay.get(key))
                if math.isfinite(value):
                    normalized_overlay[key] = value
            except (TypeError, ValueError):
                pass
        if trade_overlay.get("exitReason"):
            normalized_overlay["exitReason"] = str(trade_overlay["exitReason"])
    payload["tradeOverlay"] = normalized_overlay
    payload["alertMarkers"] = normalize_chart_alert_markers(alert_markers)
    payload_json = json.dumps(payload, separators=(",", ":")).replace("</", "<\\/")
    safe_symbol = html.escape(str(symbol))
    safe_alert_market = html.escape(str(alert_market or "INDIA").strip().upper(), quote=True)
    screener_chart_link_html = ""
    if str(alert_market or "").strip().upper() == "INDIA":
        screener_href = f"https://www.screener.in/company/{quote(str(symbol).upper(), safe='')}/"
        screener_chart_link_html = (
            f'<a class="chart-screener-link" href="{html.escape(screener_href, quote=True)}" '
            'target="_blank" rel="noopener noreferrer" title="Open on Screener.in" '
            'aria-label="Open company on Screener.in">S</a>'
        )
    latest_close = None
    if payload.get("candles"):
        try:
            latest_close = float(payload["candles"][-1].get("close"))
        except (TypeError, ValueError):
            latest_close = None
    latest_close_value = f"{latest_close:.8g}" if latest_close is not None else ""
    pe_badge_html = ""
    current_pe = None
    try:
        numeric_pe = float(pe_ratio)
        if pd.notna(numeric_pe):
            current_pe = numeric_pe
            pe_badge_html = (
                f'<span class="chart-pe-badge" title="Price-to-Earnings ratio">'
                f"PE {numeric_pe:,.2f}</span>"
            )
    except (TypeError, ValueError):
        pass
    selected_range = str(initial_range or "252").lower()
    if selected_range not in {"126", "252", "756", "all"}:
        selected_range = "252"
    valuation_state = historical_pe_valuation_state(current_pe, valuation_medians)
    valuation_state_class = f" valuation-{valuation_state}" if valuation_state else ""
    valuation_state_html = ""
    if valuation_state == "favorable":
        valuation_state_html = (
            '<span class="chart-valuation-status">Below historical median</span>'
        )
    elif valuation_state == "unfavorable":
        valuation_label = (
            "Above historical median"
            if has_positive_current_pe(pe_ratio)
            else "Current P/E unavailable"
        )
        valuation_state_html = (
            f'<span class="chart-valuation-status">{valuation_label}</span>'
        )
    fundamentals_drawer_html = ""
    cards = []
    growth_sections = (
        ("Compounded Sales Growth", "Sales growth", "sales"),
        ("Compounded Profit Growth", "Profit growth", "profit"),
        ("Stock Price CAGR", "Stock price CAGR", "price"),
        ("Return on Equity", "Return on equity", "roe"),
    )
    if isinstance(growth_metrics, dict) and growth_metrics:
        for source_title, display_title, color_class in growth_sections:
            section_values = growth_metrics.get(source_title, {})
            if not isinstance(section_values, dict):
                continue
            rows = []
            has_usable_value = False
            for period, value in section_values.items():
                value_text = "—"
                try:
                    numeric_value = float(value)
                    if pd.notna(numeric_value):
                        value_text = f"{numeric_value:g}%"
                        has_usable_value = True
                except (TypeError, ValueError):
                    pass
                rows.append(
                    '<div class="growth-metric-row">'
                    f"<span>{html.escape(str(period))}</span>"
                    f"<strong>{html.escape(value_text)}</strong>"
                    "</div>"
                )
            if rows and has_usable_value:
                cards.append(
                    f'<article class="growth-card growth-card--{color_class}">'
                    f"<h3>{html.escape(display_title)}</h3>"
                    f"{''.join(rows)}</article>"
                )
    valuation_sections = (
        ("Median PE", "Median P/E", "median-pe"),
        (
            "Median Market Cap to Sales",
            "Median Market Cap / Sales",
            "median-sales",
        ),
    )
    if isinstance(valuation_medians, dict):
        for source_title, display_title, color_class in valuation_sections:
            section_values = valuation_medians.get(source_title, {})
            if not isinstance(section_values, dict):
                continue
            rows = []
            for period in ("10 Years", "5 Years", "3 Years"):
                try:
                    numeric_value = float(section_values.get(period))
                    if not pd.notna(numeric_value):
                        continue
                except (TypeError, ValueError):
                    continue
                rows.append(
                    '<div class="growth-metric-row">'
                    f"<span>{html.escape(period)}</span>"
                    f"<strong>{numeric_value:g}</strong>"
                    "</div>"
                )
            if rows:
                cards.append(
                    f'<article class="growth-card growth-card--{color_class}">'
                    f"<h3>{html.escape(display_title)}</h3>"
                    f"{''.join(rows)}</article>"
                )
    if cards:
        fundamentals_drawer_html = (
            '<div class="fundamentals-drawer" id="fundamentals-drawer">'
            '<button class="fundamentals-toggle" id="fundamentals-toggle" type="button" '
            'aria-controls="fundamentals-panel" aria-expanded="false" '
            'title="Open fundamentals">'
            '<span class="fundamentals-toggle__icon" aria-hidden="true">ƒ</span>'
            '<span>Fundamentals</span>'
            "</button>"
            '<button class="fundamentals-scrim" id="fundamentals-scrim" type="button" '
            'aria-label="Close fundamentals"></button>'
            '<aside class="fundamentals-panel" id="fundamentals-panel" '
            'aria-label="Growth and valuation metrics" aria-hidden="true" inert>'
            '<div class="fundamentals-panel__header">'
            '<div><span class="growth-snapshot__eyebrow">Fundamentals</span>'
            '<h2>Growth &amp; valuation snapshot</h2>'
            '<span class="growth-snapshot__source">Source: Screener.in</span></div>'
            '<button class="fundamentals-close" id="fundamentals-close" type="button" '
            'aria-label="Minimize fundamentals" title="Minimize fundamentals">&times;</button>'
            "</div>"
            f'<div class="growth-grid">{"".join(cards)}</div>'
            "</aside></div>"
        )
    valuation_drawer_html = ""
    if monthly_valuations:
        valuation_drawer_html = (
            '<div class="valuation-drawer" id="valuation-drawer">'
            '<button class="valuation-toggle" id="valuation-toggle" type="button" '
            'aria-controls="valuation-panel" aria-expanded="false" '
            'title="Open monthly valuation chart"><span aria-hidden="true">◫</span>'
            '<span>Valuation</span></button>'
            '<button class="valuation-scrim" id="valuation-scrim" type="button" '
            'aria-label="Close valuation chart"></button>'
            '<aside class="valuation-panel" id="valuation-panel" aria-hidden="true" inert>'
            '<div class="valuation-panel__header"><div>'
            '<span class="growth-snapshot__eyebrow">Screener.in history</span>'
            '<h2 id="valuation-chart-title">PE Ratio</h2>'
            '<span class="growth-snapshot__source">Monthly, stored locally</span>'
            '</div><button class="fundamentals-close" id="valuation-close" type="button" '
            'aria-label="Close valuation chart">&times;</button></div>'
            '<div class="valuation-controls" aria-label="Valuation chart controls">'
            '<div class="valuation-metrics">'
            '<button class="is-active" type="button" data-valuation-metric="pe">PE Ratio</button>'
            '<button type="button" data-valuation-metric="sales">Market Cap / Sales</button>'
            '</div><button class="valuation-price-toggle is-active" '
            'id="valuation-price-toggle" type="button" aria-pressed="true">'
            '<span aria-hidden="true">✓</span> Price</button>'
            '<div class="valuation-ranges">'
            '<button type="button" data-valuation-months="1">1M</button>'
            '<button type="button" data-valuation-months="6">6M</button>'
            '<button type="button" data-valuation-months="12">1Yr</button>'
            '<button type="button" data-valuation-months="36">3Yr</button>'
            '<button class="is-active" type="button" data-valuation-months="60">5Yr</button>'
            '<button type="button" data-valuation-months="120">10Yr</button>'
            '</div></div>'
            '<div class="valuation-chart-wrap"><svg id="valuation-chart" role="img" '
            f'tabindex="0" aria-label="{safe_symbol} monthly valuation chart"></svg>'
            '<div class="valuation-tooltip" id="valuation-tooltip" hidden></div></div>'
            '<div class="valuation-legend"><span id="valuation-bar-legend">■ TTM EPS</span>'
            '<span id="valuation-median-legend">┄ Median PE</span>'
            '<span id="valuation-line-legend">━ PE ratio</span>'
            '<span id="valuation-price-legend">━ Price</span></div>'
            '</aside></div>'
        )
    price_alert_html = (
        '<div class="chart-price-alert-form">'
        f'<button class="chart-cursor-alert" id="price-alert-at-cursor" type="button" '
        f'data-symbol="{html.escape(str(symbol), quote=True)}" data-market="{safe_alert_market}" '
        f'data-current-price="{latest_close_value}" aria-disabled="true" '
        'aria-label="Add price alert at cursor" title="Move the cursor to a price, then click to add an alert">'
        '<span aria-hidden="true">+</span></button></div>'
    )
    watchlist_controls_html = ""
    if watchlists is not None:
        watchlist_options = []
        for watchlist in watchlists:
            watchlist_id = str(watchlist.get("id", "") or "").strip()
            watchlist_name = str(
                watchlist.get("name", "") or "Untitled watchlist"
            ).strip()
            if not watchlist_id:
                continue
            watchlist_options.append(
                f'<option value="{html.escape(watchlist_id, quote=True)}">'
                f"{html.escape(watchlist_name)}</option>"
            )
        if watchlist_options:
            watchlist_controls_html = (
                '<section class="chart-control-section chart-watchlist-section">'
                '<span class="chart-section-label">Add to watchlist</span>'
                '<div class="chart-watchlist-actions">'
                '<select id="chart-watchlist-select" '
                'aria-label="Choose watchlist">'
                f'{"".join(watchlist_options)}</select>'
                '<button id="chart-watchlist-add" type="button" '
                f'data-symbol="{html.escape(str(symbol), quote=True)}" '
                f'data-market="{safe_alert_market}">Add stock</button>'
                "</div></section>"
            )
        else:
            watchlist_controls_html = (
                '<section class="chart-control-section chart-watchlist-section">'
                '<span class="chart-section-label">Add to watchlist</span>'
                '<span class="chart-watchlist-empty">'
                "Create a watchlist first</span></section>"
            )
    previous_disabled = "" if has_previous else "disabled"
    next_disabled = "" if has_next else "disabled"
    match_counter = (
        f"{int(match_position)} / {int(match_total)}"
        if match_position and match_total
        else "Not in table"
    )
    initial_match_index = (
        int(match_position) - 1
        if match_position and match_total
        else -1
    )
    chart_bottom_bar_html = (
        '<nav class="chart-bottom-bar" aria-label="Interactive chart navigation">'
        '<button type="button" class="chart-bottom-button" id="matched-prev" '
        f'aria-label="Previous stock" title="Previous stock" {previous_disabled}>'
        '&lsaquo;</button>'
        '<label class="chart-symbol-search"><span>Stock</span>'
        f'<input id="chart-symbol-input" type="text" value="{html.escape(str(symbol), quote=True)}" '
        'autocomplete="off" autocapitalize="characters" spellcheck="false" '
        'aria-label="Type a stock symbol from this table" list="chart-symbol-options">'
        '<datalist id="chart-symbol-options"></datalist></label>'
        f'<span class="chart-match-counter" id="chart-match-counter">{match_counter}</span>'
        '<button type="button" class="chart-bottom-button" id="matched-next" '
        f'aria-label="Next stock" title="Next stock" {next_disabled}>'
        '&rsaquo;</button>'
        '<button type="button" class="chart-bottom-button chart-close" id="chart-close" '
        'aria-label="Close interactive chart" title="Close interactive chart">&times;</button>'
        '<button type="button" class="chart-bottom-button chart-fullscreen" id="chart-fullscreen" '
        'aria-label="Enter fullscreen landscape chart" title="Enter fullscreen">'
        '<svg class="fullscreen-enter-icon" viewBox="0 0 24 24" aria-hidden="true">'
        '<path d="M4 9V4h5M15 4h5v5M20 15v5h-5M9 20H4v-5"/></svg>'
        '<svg class="fullscreen-exit-icon" viewBox="0 0 24 24" aria-hidden="true">'
        '<path d="M9 4v5H4M20 9h-5V4M15 20v-5h5M4 15h5v5"/></svg>'
        '</button></nav>'
    )

    return f"""
    <!doctype html>
    <html>
    <head>
      <meta charset="utf-8">
      <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
      <style>
        :root {{
          --ink: #10243e;
          --muted: #64748b;
          --brand: #176b87;
          --border: #dce6ee;
          --surface: #ffffff;
          --surface-soft: #f5f8fb;
        }}
        * {{ box-sizing: border-box; }}
        html, body {{ height: 100%; }}
        body {{
          margin: 0;
          background: var(--surface-soft);
          color: var(--ink);
          font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
        }}
        .chart-shell {{
          position: relative;
          overflow: hidden;
          display: grid;
          grid-template-rows: auto minmax(300px, 1fr) auto auto;
          height: 100vh;
          min-height: 0;
          padding: 8px;
        }}
        .chart-header {{
          display: grid;
          grid-template-columns: minmax(240px, 1fr) auto auto;
          align-items: stretch;
          gap: 8px;
          padding: 8px;
          border: 1px solid var(--border);
          border-bottom: 0;
          border-radius: 14px 14px 0 0;
          background: linear-gradient(135deg, #f7fafc 0%, #eef6f8 100%);
        }}
        .chart-title {{
          display: grid;
          grid-template-columns: minmax(170px, auto) minmax(280px, 1fr);
          align-items: center;
          gap: 14px;
          min-width: 0;
          padding: 9px 12px;
          border: 1px solid #cfe0e8;
          border-radius: 10px;
          background: linear-gradient(135deg, #ffffff 0%, #edf8f9 100%);
          box-shadow: 0 2px 8px rgba(16, 36, 62, 0.05);
        }}
        .chart-title__identity {{ min-width: 0; }}
        .chart-title.valuation-favorable {{
          border-color: #78c68f;
          background: linear-gradient(135deg, #f5fff7 0%, #dcf7e4 100%);
          box-shadow: 0 2px 10px rgba(21, 128, 61, 0.10);
        }}
        .chart-title.valuation-unfavorable {{
          border-color: #df9999;
          background: linear-gradient(135deg, #fffafa 0%, #ffe5e5 100%);
          box-shadow: 0 2px 10px rgba(185, 28, 28, 0.09);
        }}
        .chart-title__row {{ display: flex; align-items: center; gap: 8px; min-width: 0; }}
        .chart-title strong {{
          display: block;
          overflow: hidden;
          font-size: 17px;
          letter-spacing: -0.02em;
          text-overflow: ellipsis;
          white-space: nowrap;
        }}
        .chart-title span {{ color: var(--muted); font-size: 11px; }}
        .chart-title .chart-pe-badge {{
          display: inline-flex;
          align-items: center;
          min-height: 24px;
          padding: 3px 8px;
          border: 1px solid #86c99a;
          border-radius: 999px;
          background: #e8f8ed;
          color: #15703a;
          font-size: 10px;
          font-weight: 800;
          line-height: 1;
          white-space: nowrap;
        }}
        .chart-valuation-status {{
          display: inline-flex;
          align-items: center;
          min-height: 22px;
          padding: 3px 7px;
          border-radius: 999px;
          font-size: 8px !important;
          font-weight: 800;
          line-height: 1;
          white-space: nowrap;
        }}
        .valuation-favorable .chart-valuation-status {{
          background: #15803d;
          color: #ffffff;
        }}
        .valuation-unfavorable .chart-valuation-status {{
          background: #b91c1c;
          color: #ffffff;
        }}
        .chart-subtitle {{
          display: block;
          margin-top: 4px;
          overflow: hidden;
          text-overflow: ellipsis;
          white-space: nowrap;
        }}
        .chart-control-section {{
          display: flex;
          min-width: 0;
          padding: 7px 9px;
          border: 1px solid #d7e2e9;
          border-radius: 10px;
          background: rgba(255, 255, 255, 0.94);
          box-shadow: 0 2px 8px rgba(16, 36, 62, 0.045);
          flex-direction: column;
          justify-content: center;
          gap: 5px;
        }}
        .chart-section-label {{
          color: #6a7e90;
          font-size: 8px;
          font-weight: 850;
          letter-spacing: 0.08em;
          line-height: 1;
          text-transform: uppercase;
          white-space: nowrap;
        }}
        .chart-match-navigation {{
          display: inline-flex;
          align-items: center;
          gap: 4px;
          flex: 0 0 auto;
        }}
        .chart-match-nav {{
          display: grid;
          place-items: center;
          width: 28px;
          height: 28px;
          padding: 0;
          border: 1px solid #78a9b9;
          border-radius: 8px;
          background: #e9f6f8;
          color: #10536a;
          cursor: pointer;
          font-size: 20px;
          font-weight: 850;
          line-height: 1;
          touch-action: manipulation;
        }}
        .chart-match-nav:hover:not(:disabled) {{ background: #d6eef2; border-color: #4f91a3; }}
        .chart-match-nav:disabled {{ cursor: not-allowed; opacity: 0.32; }}
        .chart-close {{
          display: grid;
          place-items: center;
          width: 28px;
          height: 28px;
          margin-left: 2px;
          padding: 0;
          border: 1px solid #d9a4a4;
          border-radius: 8px;
          background: #fff5f5;
          color: #a72f2f;
          cursor: pointer;
          font-size: 19px;
          font-weight: 800;
          line-height: 1;
          touch-action: manipulation;
        }}
        .chart-close:hover {{ border-color: #c96f6f; background: #ffe8e8; color: #8f2020; }}
        .chart-match-counter {{
          min-width: 39px;
          color: #52667a !important;
          font-size: 10px !important;
          font-weight: 750;
          text-align: center;
          white-space: nowrap;
        }}
        .chart-control-divider {{
          width: 1px;
          height: 22px;
          margin: 0 2px;
          background: #d9e3e9;
        }}
        .chart-bottom-bar {{
          display: flex;
          align-items: center;
          gap: 6px;
          min-width: 0;
          padding: 6px 8px;
          border: 1px solid var(--border);
          border-top: 0;
          background: #f8fbfd;
        }}
        .chart-bottom-button {{
          display: grid;
          place-items: center;
          flex: 0 0 auto;
          width: 32px;
          height: 32px;
          padding: 0;
          border: 1px solid #9bbbc8;
          border-radius: 8px;
          background: #ffffff;
          color: #10536a;
          cursor: pointer;
          font-size: 21px;
          font-weight: 850;
          line-height: 1;
          touch-action: manipulation;
        }}
        .chart-bottom-button:hover:not(:disabled) {{
          border-color: #4f91a3;
          background: #e9f6f8;
        }}
        .chart-bottom-button:disabled {{ cursor: not-allowed; opacity: 0.3; }}
        .chart-symbol-search {{
          display: grid;
          grid-template-columns: auto minmax(90px, 220px);
          align-items: center;
          gap: 6px;
          min-width: 0;
          color: var(--muted);
          font-size: 9px;
          font-weight: 850;
          letter-spacing: 0.07em;
          text-transform: uppercase;
        }}
        .chart-symbol-search input {{
          width: 100%;
          height: 32px;
          padding: 4px 9px;
          border: 1px solid #7fb1c2;
          border-radius: 8px;
          outline: none;
          background: #e9f6f8;
          color: var(--ink);
          font-size: 12px;
          font-weight: 850;
          text-transform: uppercase;
        }}
        .chart-symbol-search input:focus {{
          border-color: var(--brand);
          box-shadow: 0 0 0 3px rgba(23, 107, 135, 0.14);
        }}
        .chart-symbol-search input.is-not-in-table {{
          border-color: #d99a9a;
          background: #fff5f5;
        }}
        .chart-bottom-bar .chart-match-counter {{
          min-width: 55px;
        }}
        .chart-fullscreen {{
          margin-left: auto;
          border-color: #7894a6;
          color: #243e55;
        }}
        .chart-fullscreen svg {{
          width: 18px;
          height: 18px;
          fill: none;
          stroke: currentColor;
          stroke-width: 2;
          stroke-linecap: round;
          stroke-linejoin: round;
        }}
        .fullscreen-exit-icon {{ display: none; }}
        .chart-fullscreen.is-fullscreen .fullscreen-enter-icon {{ display: none; }}
        .chart-fullscreen.is-fullscreen .fullscreen-exit-icon {{ display: block; }}
        :fullscreen .chart-shell {{
          width: 100vw;
          height: 100dvh;
          padding: 0;
          grid-template-rows: auto minmax(0, 1fr) auto;
          background: #ffffff;
        }}
        :fullscreen .chart-footer {{ display: none; }}
        :fullscreen .chart-header {{ border-radius: 0; }}
        :fullscreen .chart-bottom-bar {{ border-bottom: 0; }}
        .chart-toolbar {{
          display: flex;
          align-items: stretch;
          gap: 8px;
        }}
        .chart-watchlist-actions {{
          display: flex;
          align-items: center;
          gap: 5px;
        }}
        .chart-watchlist-actions select {{
          min-width: 110px;
          max-width: 170px;
          height: 30px;
          padding: 0 25px 0 8px;
          border: 1px solid #bdcdd6;
          border-radius: 7px;
          background: #ffffff;
          color: #263c52;
          font: inherit;
          font-size: 10px;
        }}
        .chart-watchlist-actions button {{
          height: 30px;
          padding: 0 9px;
          border: 1px solid #70a68b;
          border-radius: 7px;
          background: #ecf8f1;
          color: #17613d;
          cursor: pointer;
          font-size: 10px;
          font-weight: 800;
          white-space: nowrap;
        }}
        .chart-watchlist-actions button:hover {{
          border-color: #3f8967;
          background: #dcf2e5;
        }}
        .chart-watchlist-actions button:disabled {{
          cursor: default;
          opacity: 0.68;
        }}
        .chart-watchlist-empty {{
          color: #718397;
          font-size: 10px;
          white-space: nowrap;
        }}
        .chart-actions {{
          display: flex;
          align-items: center;
          gap: 5px;
          justify-content: flex-end;
        }}
        .chart-action {{
          min-width: 31px;
          height: 29px;
          padding: 0 8px;
          border: 1px solid #cad8e2;
          border-radius: 7px;
          background: #ffffff;
          color: #27445d;
          cursor: pointer;
          font-size: 11px;
          font-weight: 750;
          touch-action: manipulation;
        }}
        .chart-action:hover {{ border-color: #70a8b7; background: #edf7f9; color: #10536a; }}
        .chart-action.active {{
          border-color: #78a9b9;
          background: #e9f6f8;
          color: #10536a;
          box-shadow: inset 0 0 0 1px rgba(23, 107, 135, 0.08);
        }}
        .chart-action.primary {{ border-color: var(--brand); background: var(--brand); color: #ffffff; }}
        .chart-legend {{
          display: flex;
          align-items: center;
          align-content: center;
          flex-wrap: wrap;
          gap: 10px;
          min-height: 58px;
          padding: 8px 10px;
          overflow: hidden;
          border: 1px solid rgba(120, 166, 151, 0.52);
          border-radius: 9px;
          background: rgba(255, 255, 255, 0.72);
          color: #334a63;
          font-size: 11px;
          font-variant-numeric: tabular-nums;
          line-height: 1.25;
        }}
        .legend-date {{ color: var(--muted); font-weight: 750; }}
        .legend-ohlc b {{ margin-left: 4px; color: var(--ink); }}
        .legend-gain {{ font-weight: 750; }}
        .legend-gain b {{ margin-left: 4px; }}
        .legend-gain.is-positive {{ color: #15803d; }}
        .legend-gain.is-negative {{ color: #b91c1c; }}
        .legend-gain.is-neutral {{ color: var(--muted); }}
        .legend-ma {{ font-weight: 750; }}
        #chart {{
          position: relative;
          width: 100%;
          height: 100%;
          min-width: 0;
          min-height: 300px;
          border: 1px solid var(--border);
          background: var(--surface);
        }}
        .chart-price-alert-form {{
          position: absolute;
          inset: 0;
          z-index: 12;
          pointer-events: none;
        }}
        .chart-cursor-alert {{
          position: absolute;
          top: 50%;
          right: 5px;
          display: grid;
          place-items: center;
          width: 25px;
          height: 25px;
          padding: 0;
          transform: translateY(-50%);
          border: 1px solid #b7791f;
          border-radius: 50%;
          background: #fff7df;
          color: #925b0b;
          box-shadow: 0 2px 8px rgba(71, 48, 10, 0.22);
          cursor: pointer;
          font-size: 20px;
          font-weight: 700;
          line-height: 1;
          text-decoration: none;
          opacity: 0;
          pointer-events: none;
          transition: opacity 0.12s ease, transform 0.12s ease;
        }}
        .chart-cursor-alert.is-ready {{ opacity: 1; pointer-events: auto; }}
        .chart-cursor-alert.is-ready:hover {{
          transform: translateY(-50%) scale(1.08);
          background: #ffedb3;
        }}
        .chart-cursor-alert:focus-visible {{ outline: 3px solid rgba(23, 107, 135, 0.28); }}
        .chart-loading {{
          position: absolute;
          inset: 0;
          z-index: 2;
          display: grid;
          place-items: center;
          background: #ffffff;
          color: var(--muted);
          font-size: 13px;
        }}
        .chart-footer {{
          display: flex;
          align-items: center;
          justify-content: space-between;
          gap: 12px;
          padding: 8px 12px;
          border: 1px solid var(--border);
          border-top: 0;
          border-radius: 0 0 14px 14px;
          background: #ffffff;
          color: var(--muted);
          font-size: 10px;
        }}
        .chart-footer a {{ color: var(--brand); font-weight: 700; text-decoration: none; }}
        .chart-screener-link {{
          display:inline-grid; place-items:center; width:24px; height:24px;
          border:1px solid #b9c9d4; border-radius:7px; background:#fff;
          color:#176b87; font-size:11px; font-weight:900; text-decoration:none;
        }}
        .fundamentals-drawer {{
          position: absolute;
          inset: 0;
          z-index: 20;
          pointer-events: none;
        }}
        .valuation-drawer {{ position: absolute; inset: 0; z-index: 21; pointer-events: none; }}
        .valuation-toggle {{
          position: absolute; top: 70%; left: 8px; z-index: 3;
          display: flex; align-items: center; gap: 6px; min-height: 108px;
          padding: 10px 7px; border: 1px solid #8b82db; border-radius: 0 10px 10px 0;
          background: linear-gradient(180deg, #6558d9, #4f46b8); color: #fff;
          box-shadow: 0 8px 22px rgba(79,70,184,.25); cursor: pointer;
          font-size: 10px; font-weight: 800; letter-spacing: .04em;
          writing-mode: vertical-rl; pointer-events: auto;
        }}
        .valuation-scrim {{ position:absolute; inset:8px; border:0; border-radius:14px;
          background:rgba(15,35,52,.30); opacity:0; pointer-events:none; }}
        .valuation-panel {{ position:absolute; inset:8px auto 8px 8px; z-index:2;
          width:min(760px,calc(100% - 16px)); padding:14px; overflow:auto;
          border:1px solid #d8d5f2; border-radius:14px; background:#fff;
          box-shadow:12px 0 34px rgba(16,36,62,.18); pointer-events:none;
          transform:translateX(calc(-100% - 18px)); transition:transform .24s ease; }}
        .valuation-panel__header {{ display:flex; justify-content:space-between; gap:10px;
          padding-bottom:10px; border-bottom:1px solid #e5e7eb; }}
        .valuation-panel h2 {{ margin:1px 0 0; color:#17334c; font-size:15px; }}
        .valuation-drawer.is-open .valuation-toggle {{
          border-color:#7c72d4;
          background:linear-gradient(180deg,#7668e8,#4f46b8);
          opacity:1;
          pointer-events:auto;
        }}
        .valuation-drawer.is-open .valuation-scrim {{ opacity:1; pointer-events:auto; }}
        .valuation-drawer.is-open .valuation-panel {{ pointer-events:auto; transform:translateX(0); }}
        .valuation-chart-wrap {{ min-height:300px; margin-top:12px; }}
        #valuation-chart {{ width:100%; height:300px; overflow:visible; touch-action:none;
          outline:none; }}
        #valuation-chart:focus-visible {{ outline:2px solid #6558ff; outline-offset:3px; }}
        .valuation-controls {{ display:flex; align-items:center; justify-content:space-between;
          gap:10px; margin-top:12px; flex-wrap:wrap; }}
        .valuation-metrics, .valuation-ranges {{ display:flex; border:1px solid #d4d8df;
          border-radius:7px; overflow:hidden; background:#fff; }}
        .valuation-controls button {{ min-height:34px; padding:6px 11px; border:0;
          border-right:1px solid #e4e7ec; background:#fff; color:#44556a;
          font-size:11px; font-weight:750; cursor:pointer; }}
        .valuation-controls button:last-child {{ border-right:0; }}
        .valuation-controls button.is-active {{ color:#6257ed; background:#f1efff; }}
        .valuation-controls .valuation-price-toggle {{
          border:1px solid #d4d8df; border-radius:7px;
        }}
        .valuation-price-toggle span {{ display:inline-block; width:12px; }}
        .valuation-chart-wrap {{ position:relative; }}
        .valuation-legend {{ display:flex; justify-content:center; flex-wrap:wrap; gap:18px;
          color:#53657a; font-size:11px; font-weight:700; }}
        #valuation-bar-legend {{ color:#6abbe7; }}
        #valuation-line-legend {{ color:#6558ff; }}
        #valuation-price-legend {{ color:#d97706; }}
        #valuation-median-legend {{ color:#8b94a3; }}
        .valuation-tooltip {{ position:absolute; z-index:4; min-width:145px; padding:8px 10px;
          border:1px solid #dce2ea; border-radius:8px; background:rgba(255,255,255,.97);
          box-shadow:0 8px 24px rgba(31,48,68,.16); color:#334155;
          font-size:12px; line-height:1.55; pointer-events:none; }}
        .valuation-tooltip__date {{ color:#697586; font-size:11px; }}
        .valuation-tooltip strong {{ color:#202939; font-size:14px; }}
        .fundamentals-toggle {{
          position: absolute;
          top: 50%;
          left: 8px;
          z-index: 3;
          display: flex;
          align-items: center;
          gap: 6px;
          min-height: 112px;
          padding: 10px 7px;
          border: 1px solid #8ab5c2;
          border-radius: 0 10px 10px 0;
          background: linear-gradient(180deg, #176b87, #10536a);
          box-shadow: 0 8px 22px rgba(16, 53, 76, 0.25);
          color: #ffffff;
          cursor: pointer;
          font-size: 10px;
          font-weight: 800;
          letter-spacing: 0.04em;
          pointer-events: auto;
          transform: translateY(-50%);
          transition: opacity 0.18s ease, transform 0.18s ease;
          writing-mode: vertical-rl;
        }}
        .fundamentals-toggle:hover {{
          background: linear-gradient(180deg, #168297, #10536a);
          transform: translateY(-50%) translateX(2px);
        }}
        .fundamentals-toggle:focus-visible,
        .fundamentals-close:focus-visible {{
          outline: 3px solid rgba(23, 107, 135, 0.24);
          outline-offset: 2px;
        }}
        .fundamentals-toggle__icon {{
          display: grid;
          place-items: center;
          width: 20px;
          height: 20px;
          border-radius: 6px;
          background: rgba(255, 255, 255, 0.16);
          font-family: Georgia, serif;
          font-size: 13px;
          writing-mode: horizontal-tb;
        }}
        .fundamentals-scrim {{
          position: absolute;
          inset: 8px;
          z-index: 1;
          padding: 0;
          border: 0;
          border-radius: 14px;
          background: rgba(15, 35, 52, 0.30);
          cursor: default;
          opacity: 0;
          pointer-events: none;
          transition: opacity 0.2s ease;
        }}
        .fundamentals-panel {{
          position: absolute;
          top: 8px;
          left: 8px;
          bottom: 8px;
          z-index: 2;
          width: min(430px, calc(100% - 16px));
          padding: 14px;
          overflow-x: hidden;
          overflow-y: auto;
          border: 1px solid #cbdce5;
          border-radius: 14px;
          background: #f6f9fb;
          box-shadow: 12px 0 34px rgba(16, 36, 62, 0.18);
          pointer-events: none;
          transform: translateX(calc(-100% - 18px));
          transition: transform 0.24s ease;
        }}
        .fundamentals-drawer.is-open .fundamentals-toggle {{
          border-color: #69a7b8;
          background: linear-gradient(180deg, #168297, #10536a);
          opacity: 1;
          pointer-events: auto;
          transform: translateY(-50%);
        }}
        .fundamentals-drawer.is-open .fundamentals-scrim {{
          opacity: 1;
          pointer-events: auto;
        }}
        .fundamentals-drawer.is-open .fundamentals-panel {{
          pointer-events: auto;
          transform: translateX(0);
        }}
        .fundamentals-panel__header {{
          position: sticky;
          top: -14px;
          z-index: 1;
          display: flex;
          align-items: flex-start;
          justify-content: space-between;
          gap: 10px;
          margin: -14px -14px 12px;
          padding: 14px;
          border-bottom: 1px solid #d8e4ea;
          background: rgba(246, 249, 251, 0.96);
          backdrop-filter: blur(10px);
        }}
        .growth-snapshot__eyebrow {{
          color: #15803d;
          font-size: 8px;
          font-weight: 850;
          letter-spacing: 0.09em;
          text-transform: uppercase;
        }}
        .fundamentals-panel h2 {{
          margin: 1px 0 0;
          color: #17334c;
          font-size: 13px;
          letter-spacing: -0.01em;
        }}
        .growth-snapshot__source {{
          display: block;
          margin-top: 3px;
          color: #718397;
          font-size: 8px;
          white-space: nowrap;
        }}
        .fundamentals-close {{
          display: grid;
          place-items: center;
          width: 32px;
          height: 32px;
          flex: 0 0 32px;
          padding: 0;
          border: 1px solid #cbd8e1;
          border-radius: 9px;
          background: #ffffff;
          color: #40586d;
          cursor: pointer;
          font-size: 20px;
          font-weight: 750;
          line-height: 1;
        }}
        .fundamentals-close:hover {{
          border-color: #8aaab8;
          background: #eaf4f6;
          color: #10536a;
        }}
        .growth-grid {{
          display: grid;
          grid-template-columns: repeat(2, minmax(0, 1fr));
          gap: 7px;
        }}
        .growth-card {{
          position: relative;
          min-width: 0;
          padding: 9px 10px;
          overflow: hidden;
          border: 1px solid #d9e4ea;
          border-radius: 10px;
          background: #ffffff;
          box-shadow: 0 2px 8px rgba(16, 36, 62, 0.045);
        }}
        .growth-card::before {{
          position: absolute;
          inset: 0 auto 0 0;
          width: 3px;
          background: #16a34a;
          content: "";
        }}
        .growth-card--profit::before {{ background: #0891b2; }}
        .growth-card--price::before {{ background: #ea8a1f; }}
        .growth-card--roe::before {{ background: #7c3aed; }}
        .growth-card--median-pe::before {{ background: #dc2626; }}
        .growth-card--median-sales::before {{ background: #2563eb; }}
        .growth-card h3 {{
          margin: 0 0 6px;
          overflow: hidden;
          color: #17334c;
          font-size: 10px;
          font-weight: 800;
          text-overflow: ellipsis;
          white-space: nowrap;
        }}
        .growth-metric-row {{
          display: flex;
          align-items: center;
          justify-content: space-between;
          gap: 8px;
          min-height: 18px;
          color: #64748b;
          font-size: 9px;
          font-variant-numeric: tabular-nums;
        }}
        .growth-metric-row strong {{
          color: #10243e;
          font-size: 10px;
        }}
        @media (max-width: 980px) {{
          .chart-header {{
            grid-template-columns: minmax(220px, 1fr) auto;
          }}
          .chart-toolbar {{
            grid-column: 1 / -1;
          }}
          .chart-toolbar .chart-control-section {{
            flex: 1 1 0;
          }}
        }}
        @media (max-width: 640px) {{
          .chart-shell {{
            grid-template-rows: auto minmax(0, 1fr) auto auto;
            padding: 0;
          }}
          .chart-header {{
            grid-template-columns: 1fr;
            gap: 6px;
            padding: 6px;
          }}
          .chart-title {{
            grid-template-columns: 1fr;
            gap: 7px;
            padding: 8px 9px;
          }}
          .chart-title strong {{ font-size: 15px; }}
          .chart-navigation-section {{ grid-column: 1; }}
          .chart-match-navigation {{ width: 100%; }}
          .chart-match-counter {{ flex: 1 1 auto; }}
          .chart-toolbar {{
            display: grid;
            grid-column: 1;
            grid-template-columns: minmax(0, 1fr);
            gap: 6px;
          }}
          .chart-watchlist-actions {{
            display: grid;
            grid-template-columns: minmax(0, 1fr) auto;
          }}
          .chart-watchlist-actions select {{
            width: 100%;
            max-width: none;
          }}
          .chart-control-section {{ padding: 7px; }}
          .chart-range-actions {{
            display: grid;
            grid-template-columns: repeat(4, minmax(0, 1fr));
            width: 100%;
            max-width: none;
          }}
          .chart-action {{
            width: 100%;
            height: 30px;
            padding-inline: 4px;
          }}
          .chart-legend {{
            min-height: 44px;
            padding: 6px 8px;
            font-size: 10px;
          }}
          #chart {{ min-height: 0; }}
          .chart-footer {{
            align-items: flex-start;
            flex-direction: column;
            gap: 2px;
            padding: 6px 8px;
            font-size: 9px;
          }}
          .fundamentals-toggle {{ left: 0; }}
          .valuation-toggle {{ left: 0; top: 72%; }}
          .fundamentals-scrim {{ inset: 0; border-radius: 0; }}
          .fundamentals-panel {{
            top: 8px;
            left: 34px;
            bottom: 8px;
            width: calc(100% - 42px);
            border-radius: 12px;
          }}
          .valuation-scrim {{ inset:0; border-radius:0; }}
          .valuation-panel {{
            inset:8px 8px 8px 34px;
            width:auto;
            border-radius:12px;
          }}
          .valuation-controls {{ align-items:stretch; }}
          .valuation-metrics {{ width:100%; }}
          .valuation-metrics button {{ flex:1; }}
          .valuation-ranges {{ width:100%; }}
          .valuation-ranges button {{ flex:1; padding-inline:6px; }}
          .valuation-chart-wrap, #valuation-chart {{
            min-height:240px;
            height:clamp(240px,44dvh,420px);
          }}
          .growth-grid {{ grid-template-columns: 1fr; }}
          .growth-card {{ padding: 8px 9px; }}
        }}
        @media (max-width: 640px) and (orientation: portrait) {{
          html, body {{
            width: 100%;
            overflow: hidden;
          }}
          .chart-shell {{
            display: flex;
            flex-direction: column;
            width: 100%;
            min-height: 100dvh;
            height: 100dvh;
            padding: 0;
            overflow-x: hidden;
            overflow-y: auto;
            overscroll-behavior: contain;
          }}
          .chart-header,
          .chart-toolbar {{
            display: contents;
          }}
          .chart-title {{
            order: 1;
            flex: 0 0 auto;
            gap: 3px;
            padding: 4px;
            border-width: 0 0 1px;
            border-radius: 0;
            box-shadow: none;
          }}
          .chart-title .chart-section-label,
          .chart-subtitle {{
            display: none;
          }}
          .chart-title__row {{ gap: 4px; }}
          .chart-title strong {{ font-size: 13px; }}
          .chart-title .chart-pe-badge,
          .chart-valuation-status {{
            min-height: 19px;
            padding: 2px 5px;
            font-size: 8px !important;
          }}
          .chart-legend {{
            min-height: 31px;
            max-height: 36px;
            gap: 3px 7px;
            padding: 3px 5px;
            border-radius: 6px;
            font-size: 9px;
            line-height: 1.12;
          }}
          .chart-navigation-section {{
            order: 2;
            flex: 0 0 30px;
          }}
          .chart-toolbar > .chart-control-section:not(.chart-watchlist-section) {{
            order: 3;
            flex: 0 0 30px;
          }}
          .chart-navigation-section,
          .chart-toolbar > .chart-control-section:not(.chart-watchlist-section) {{
            min-height: 30px;
            padding: 2px 4px;
            border-width: 0 0 1px;
            border-radius: 0;
            box-shadow: none;
          }}
          .chart-navigation-section .chart-section-label,
          .chart-toolbar > .chart-control-section:not(.chart-watchlist-section) > .chart-section-label {{
            display: none;
          }}
          .chart-match-nav,
          .chart-close {{
            width: 27px;
            height: 27px;
          }}
          .chart-bottom-bar {{
            order: 5;
            gap: 4px;
            padding: 4px;
          }}
          .chart-bottom-button {{
            width: 29px;
            height: 29px;
          }}
          .chart-symbol-search {{
            flex: 1 1 auto;
            grid-template-columns: minmax(76px, 1fr);
          }}
          .chart-symbol-search span {{ display: none; }}
          .chart-symbol-search input {{
            height: 29px;
            padding-inline: 7px;
            font-size: 11px;
          }}
          .chart-bottom-bar .chart-match-counter {{
            min-width: 39px;
            font-size: 9px !important;
          }}
          .chart-action {{
            height: 27px;
            font-size: 9px;
          }}
          #chart {{
            order: 4;
            flex: 0 0 max(240px, calc(100dvh - 193px));
            width: 100%;
            min-height: 240px;
            border-right: 0;
            border-left: 0;
          }}
          #chart canvas {{
            display: block;
          }}
          .chart-watchlist-section {{
            order: 6;
            flex: 0 0 auto;
            margin: 6px;
          }}
          .chart-footer {{
            order: 7;
          }}
          .fundamentals-toggle,
          .valuation-toggle {{
            left: -1px;
            border-left: 0;
          }}
        }}
        @media (orientation: landscape) and (max-height: 600px) {{
          body {{ overflow:hidden; }}
          .chart-shell {{
            height:100dvh;
            padding:0;
            grid-template-rows:auto minmax(0,1fr) auto;
          }}
          .chart-header {{
            grid-template-columns:minmax(190px,1fr) auto auto;
            gap:4px;
            padding:3px;
            border-radius:0;
          }}
          .chart-title {{
            grid-template-columns:minmax(120px,auto) minmax(220px,1fr);
            gap:6px;
            padding:4px 7px;
            border-radius:7px;
          }}
          .chart-title strong {{ font-size:13px; }}
          .chart-title .chart-section-label,
          .chart-subtitle {{ display:none; }}
          .chart-title__row {{ gap:4px; }}
          .chart-title .chart-pe-badge,
          .chart-valuation-status {{ min-height:20px; padding:2px 5px; font-size:8px !important; }}
          .chart-control-section {{
            padding:3px 5px;
            border-radius:7px;
            flex-direction:row;
            align-items:center;
            gap:3px;
          }}
          .chart-watchlist-section {{
            max-width: 260px;
          }}
          .chart-watchlist-actions select {{
            max-width: 120px;
            height: 26px;
          }}
          .chart-watchlist-actions button {{
            height: 26px;
            padding-inline: 6px;
          }}
          .chart-control-section .chart-section-label {{ display:none; }}
          .chart-match-nav, .chart-close {{ width:25px; height:25px; }}
          .chart-toolbar {{ gap:4px; }}
          .chart-action {{ height:25px; min-width:28px; padding-inline:6px; font-size:9px; }}
          .chart-legend {{ min-height:34px; padding:3px 6px; gap:6px; font-size:9px; }}
          #chart {{ min-height:0; }}
          .chart-footer {{ display:none; }}
          .fundamentals-toggle {{ top:42%; left:0; min-height:78px; padding:7px 5px; }}
          .valuation-toggle {{ top:70%; left:0; min-height:78px; padding:7px 5px; }}
          .valuation-panel {{ inset:0 auto 0 0; width:100%; border-radius:0; }}
          .valuation-chart-wrap, #valuation-chart {{ min-height:180px; height:calc(100dvh - 118px); }}
        }}
        @media (prefers-reduced-motion: reduce) {{
          .fundamentals-toggle,
          .fundamentals-scrim,
          .fundamentals-panel,
          .chart-cursor-alert {{ transition: none; }}
        }}
      </style>
    </head>
    <body>
      <main class="chart-shell">
        <header class="chart-header">
          <div class="chart-title{valuation_state_class}">
            <div class="chart-title__identity">
              <span class="chart-section-label">Selected stock</span>
              <div class="chart-title__row">
                <strong>{safe_symbol}</strong>
                {pe_badge_html}
                {valuation_state_html}
                {screener_chart_link_html}
              </div>
              <span class="chart-subtitle">Interactive candlestick chart · {payload["pointCount"]:,} candles loaded on demand</span>
            </div>
            <div class="chart-legend" id="chart-legend">Loading latest OHLC and MA values…</div>
          </div>
          <div class="chart-toolbar" aria-label="Chart controls">
            {watchlist_controls_html}
            <section class="chart-control-section">
              <span class="chart-section-label">Time range</span>
              <div class="chart-actions chart-range-actions">
                <button class="chart-action" type="button" data-range="126">6M</button>
                <button class="chart-action" type="button" data-range="252">1Y</button>
                <button class="chart-action" type="button" data-range="756">3Y</button>
                <button class="chart-action" type="button" data-range="all">All</button>
              </div>
            </section>
          </div>
        </header>
        <section id="chart" aria-label="{safe_symbol} interactive stock chart">
          {price_alert_html}
          <div class="chart-loading" id="chart-loading">Loading interactive chart…</div>
        </section>
        {chart_bottom_bar_html}
        <footer class="chart-footer">
          <span>Scroll or pinch to zoom · drag to pan · tap and hold on mobile to inspect values</span>
          <a href="https://www.tradingview.com/lightweight-charts/" target="_blank" rel="noopener">Charts by TradingView</a>
        </footer>
        {fundamentals_drawer_html}
        {valuation_drawer_html}
      </main>
      <script src="https://unpkg.com/lightweight-charts@5.0.9/dist/lightweight-charts.standalone.production.js"></script>
      <script>
        (function() {{
          const payload = {payload_json};
          const container = document.getElementById("chart");
          const loading = document.getElementById("chart-loading");
          const legend = document.getElementById("chart-legend");
          const fundamentalsDrawer = document.getElementById("fundamentals-drawer");
          const fundamentalsToggle = document.getElementById("fundamentals-toggle");
          const fundamentalsPanel = document.getElementById("fundamentals-panel");
          const fundamentalsClose = document.getElementById("fundamentals-close");
          const fundamentalsScrim = document.getElementById("fundamentals-scrim");
          const priceAlertButton = document.getElementById("price-alert-at-cursor");
          const watchlistSelect = document.getElementById("chart-watchlist-select");
          const watchlistAddButton = document.getElementById("chart-watchlist-add");
          const valuationDrawer = document.getElementById("valuation-drawer");
          const valuationToggle = document.getElementById("valuation-toggle");
          const valuationPanel = document.getElementById("valuation-panel");
          const valuationClose = document.getElementById("valuation-close");
          const valuationScrim = document.getElementById("valuation-scrim");
          const valuationPriceToggle = document.getElementById("valuation-price-toggle");
          let valuationMetric = "pe";
          let valuationMonths = 60;
          let valuationPriceEnabled = true;
          let valuationCursorIndex = null;

          function drawValuationChart() {{
            const svg = document.getElementById("valuation-chart");
            const tooltip = document.getElementById("valuation-tooltip");
            const allRows = (payload.monthlyValuations || []).filter(r => r && r.time);
            if (!svg || !allRows.length) return;
            const newest = new Date(allRows[allRows.length - 1].time + "T00:00:00");
            const cutoff = new Date(
              newest.getFullYear(), newest.getMonth() - Number(valuationMonths), newest.getDate()
            );
            const rows = allRows.filter(r => new Date(r.time + "T00:00:00") >= cutoff);
            const isPe = valuationMetric === "pe";
            const lineKey = isPe ? "pe" : "marketCapToSales";
            const barKey = isPe ? "eps" : "sales";
            const lineLabel = isPe ? "PE ratio" : "Market Cap / Sales";
            const barLabel = isPe ? "TTM EPS" : "TTM Sales";
            const width = Math.max(320, svg.clientWidth || 700);
            const height = Math.max(260, svg.clientHeight || 320);
            const pad = {{l:52,r:54,t:18,b:42}}, plotW=width-pad.l-pad.r, plotH=height-pad.t-pad.b;
            const finite = (value) => Number.isFinite(Number(value)) ? Number(value) : null;
            const lineValues = rows.map(r => finite(r[lineKey])).filter(v => v !== null);
            const barValues = rows.map(r => finite(r[barKey])).filter(v => v !== null);
            const priceValues = rows.map(r => finite(r.price)).filter(v => v !== null);
            if (!lineValues.length && !barValues.length) {{
              svg.innerHTML = '<text x="50%" y="50%" text-anchor="middle" fill="#64748b">No valuation history available</text>';
              return;
            }}
            const lineMin = Math.min(0, ...lineValues), lineMax = Math.max(1, ...lineValues) * 1.08;
            const barMin = Math.min(0, ...barValues), barMaxRaw = Math.max(1, ...barValues);
            const barMax = barMaxRaw === barMin ? barMin + 1 : barMaxRaw * 1.08;
            const lineY = value => pad.t + plotH - (value-lineMin)/(lineMax-lineMin)*plotH;
            const barY = value => pad.t + plotH - (value-barMin)/(barMax-barMin)*plotH;
            const priceMinRaw = priceValues.length ? Math.min(...priceValues) : 0;
            const priceMaxRaw = priceValues.length ? Math.max(...priceValues) : 1;
            const pricePadding = Math.max(
              (priceMaxRaw-priceMinRaw)*.08,
              Math.abs(priceMaxRaw)*.02,
              .01
            );
            const priceMin = priceMinRaw-pricePadding;
            const priceMax = priceMaxRaw+pricePadding;
            const priceY = value => pad.t + plotH - (value-priceMin)/(priceMax-priceMin)*plotH;
            const zeroY = barY(0);
            const step = plotW / Math.max(1, rows.length);
            const tickCount = 4;
            const grid = Array.from({{length:tickCount+1}}, (_,i) => {{
              const y=pad.t+plotH*i/tickCount;
              const left=(barMax-(barMax-barMin)*i/tickCount).toFixed(1).replace(".0","");
              const right=(lineMax-(lineMax-lineMin)*i/tickCount).toFixed(1).replace(".0","");
              return `<line x1="${{pad.l}}" y1="${{y}}" x2="${{width-pad.r}}" y2="${{y}}" stroke="#e5e7eb"/>`
                + `<text x="${{pad.l-8}}" y="${{y+4}}" text-anchor="end" fill="#64748b" font-size="10">${{left}}</text>`
                + `<text x="${{width-pad.r+8}}" y="${{y+4}}" fill="#64748b" font-size="10">${{right}}</text>`;
            }}).join("");
            const bars = rows.map((r,i) => {{
              const value=finite(r[barKey]);
              if (value === null) return "";
              const y=barY(value), h=Math.max(1,Math.abs(zeroY-y));
              return `<rect x="${{(pad.l+step*i+1).toFixed(1)}}" y="${{Math.min(y,zeroY).toFixed(1)}}" `
                + `width="${{Math.max(1,step-2).toFixed(1)}}" height="${{h.toFixed(1)}}" fill="rgba(101,184,230,.58)">`
                + `<title>${{r.time}} · ${{barLabel}} ${{value.toFixed(2)}}</title></rect>`;
            }}).join("");
            let path = "", drawing = false;
            rows.forEach((r,i) => {{
              const value=finite(r[lineKey]);
              if (value === null) {{ drawing=false; return; }}
              const x=pad.l+step*(i+.5), y=lineY(value);
              path += `${{drawing ? "L" : "M"}}${{x.toFixed(1)}},${{y.toFixed(1)}} `;
              drawing=true;
            }});
            let pricePath = "", priceDrawing = false;
            if (valuationPriceEnabled) rows.forEach((r,i) => {{
              const value=finite(r.price);
              if (value === null) {{ priceDrawing=false; return; }}
              const x=pad.l+step*(i+.5), y=priceY(value);
              pricePath += `${{priceDrawing ? "L" : "M"}}${{x.toFixed(1)}},${{y.toFixed(1)}} `;
              priceDrawing=true;
            }});
            const sortedLineValues = [...lineValues].sort((a,b) => a-b);
            const medianMiddle = Math.floor(sortedLineValues.length/2);
            const median = !sortedLineValues.length ? null :
              (sortedLineValues.length % 2
                ? sortedLineValues[medianMiddle]
                : (sortedLineValues[medianMiddle-1]+sortedLineValues[medianMiddle])/2);
            const medianLine = median === null ? "" :
              `<line x1="${{pad.l}}" y1="${{lineY(median)}}" x2="${{width-pad.r}}" y2="${{lineY(median)}}" `
              + `stroke="#a5acb8" stroke-width="1.5" stroke-dasharray="6 6"><title>Median ${{median.toFixed(2)}}</title></line>`;
            const labelIndexes = Array.from(new Set([0, Math.floor((rows.length-1)/3),
              Math.floor((rows.length-1)*2/3), rows.length-1])).filter(i => i >= 0);
            const dateLabels = labelIndexes.map(i => {{
              const x=pad.l+step*(i+.5);
              const date=new Date(rows[i].time+"T00:00:00");
              const label=date.toLocaleDateString(undefined,{{month:"short",year:"numeric"}});
              return `<text x="${{x}}" y="${{height-12}}" text-anchor="middle" fill="#64748b" font-size="10">${{label}}</text>`;
            }}).join("");
            svg.setAttribute("viewBox",`0 0 ${{width}} ${{height}}`);
            svg.innerHTML = `${{grid}}${{bars}}${{medianLine}}`
              + `<path d="${{path}}" fill="none" stroke="#6558ff" stroke-width="2.4" stroke-linejoin="round"/>`
              + (valuationPriceEnabled && pricePath
                ? `<path d="${{pricePath}}" fill="none" stroke="#d97706" stroke-width="2" stroke-linejoin="round"/>`
                : "")
              + `<line id="valuation-crosshair" x1="0" y1="${{pad.t}}" x2="0" y2="${{pad.t+plotH}}" `
              + `stroke="#aab2bf" stroke-width="1" visibility="hidden"/>`
              + `<circle id="valuation-cursor-dot" r="5" fill="#5145e5" stroke="#fff" stroke-width="2" visibility="hidden"/>`
              + `<circle id="valuation-price-cursor-dot" r="4" fill="#d97706" stroke="#fff" stroke-width="2" visibility="hidden"/>`
              + `<rect x="${{pad.l}}" y="${{pad.t}}" width="${{plotW}}" height="${{plotH}}" fill="transparent"/>`
              + `${{dateLabels}}`
              + `<text x="13" y="${{pad.t+plotH/2}}" fill="#64748b" font-size="10" text-anchor="middle" `
              + `transform="rotate(-90 13 ${{pad.t+plotH/2}})">${{barLabel}}</text>`
              + `<text x="${{width-12}}" y="${{pad.t+plotH/2}}" fill="#64748b" font-size="10" text-anchor="middle" `
              + `transform="rotate(90 ${{width-12}} ${{pad.t+plotH/2}})">${{lineLabel}}</text>`;
            const medianLegend = document.getElementById("valuation-median-legend");
            if (medianLegend) {{
              const medianName = isPe ? "Median PE" : "Median MCap / Sales";
              medianLegend.textContent = `┄ ${{medianName}}${{median === null ? "" : " = " + median.toFixed(1)}}`;
            }}
            const priceLegend = document.getElementById("valuation-price-legend");
            if (priceLegend) priceLegend.hidden = !valuationPriceEnabled;

            function showValuationCursor(index, clientX=null, clientY=null) {{
              index = Math.max(0,Math.min(rows.length-1,index));
              valuationCursorIndex = index;
              const row=rows[index], lineValue=finite(row[lineKey]), barValue=finite(row[barKey]);
              const priceValue=finite(row.price);
              const x=pad.l+step*(index+.5);
              const crosshair=svg.querySelector("#valuation-crosshair");
              const dot=svg.querySelector("#valuation-cursor-dot");
              const priceDot=svg.querySelector("#valuation-price-cursor-dot");
              if (crosshair) {{
                crosshair.setAttribute("x1",x); crosshair.setAttribute("x2",x);
                crosshair.setAttribute("visibility","visible");
              }}
              if (dot && lineValue !== null) {{
                dot.setAttribute("cx",x); dot.setAttribute("cy",lineY(lineValue));
                dot.setAttribute("visibility","visible");
              }} else if (dot) dot.setAttribute("visibility","hidden");
              if (priceDot && valuationPriceEnabled && priceValue !== null) {{
                priceDot.setAttribute("cx",x); priceDot.setAttribute("cy",priceY(priceValue));
                priceDot.setAttribute("visibility","visible");
              }} else if (priceDot) priceDot.setAttribute("visibility","hidden");
              if (!tooltip) return;
              const date=new Date(row.time+"T00:00:00");
              const dateLabel=date.toLocaleDateString(undefined,{{day:"numeric",month:"short",year:"2-digit"}});
              tooltip.innerHTML = `<div class="valuation-tooltip__date">${{dateLabel}}</div>`
                + `<div><strong>${{lineLabel}}: ${{lineValue === null ? "—" : lineValue.toFixed(2)}}</strong></div>`
                + `<div>${{barLabel}}: ${{barValue === null ? "—" : barValue.toFixed(2)}}</div>`
                + (valuationPriceEnabled
                  ? `<div>Price: ${{priceValue === null ? "—" : priceValue.toFixed(2)}}</div>`
                  : "");
              tooltip.hidden=false;
              const wrap=svg.parentElement, wrapRect=wrap.getBoundingClientRect();
              const svgRect=svg.getBoundingClientRect();
              const anchorX=clientX === null
                ? svgRect.left + x/width*svgRect.width
                : clientX;
              const anchorY=clientY === null
                ? svgRect.top + (lineValue === null ? pad.t+plotH/2 : lineY(lineValue))/height*svgRect.height
                : clientY;
              const maxLeft=Math.max(8,wrapRect.width-tooltip.offsetWidth-8);
              const left=Math.max(8,Math.min(maxLeft,anchorX-wrapRect.left+12));
              let top=anchorY-wrapRect.top-tooltip.offsetHeight-12;
              if (top < 8) top=anchorY-wrapRect.top+14;
              tooltip.style.left=left+"px";
              tooltip.style.top=Math.max(8,Math.min(top,wrapRect.height-tooltip.offsetHeight-8))+"px";
            }}
            function hideValuationCursor() {{
              const crosshair=svg.querySelector("#valuation-crosshair");
              const dot=svg.querySelector("#valuation-cursor-dot");
              const priceDot=svg.querySelector("#valuation-price-cursor-dot");
              if (crosshair) crosshair.setAttribute("visibility","hidden");
              if (dot) dot.setAttribute("visibility","hidden");
              if (priceDot) priceDot.setAttribute("visibility","hidden");
              if (tooltip) tooltip.hidden=true;
              valuationCursorIndex=null;
            }}
            function cursorIndexFromPointer(event) {{
              const rect=svg.getBoundingClientRect();
              const x=(event.clientX-rect.left)*width/rect.width;
              return Math.round((x-pad.l)/step-.5);
            }}
            svg.onpointerdown = event => {{
              event.preventDefault();
              if (svg.setPointerCapture) svg.setPointerCapture(event.pointerId);
              showValuationCursor(cursorIndexFromPointer(event),event.clientX,event.clientY);
            }};
            svg.onpointermove = event => {{
              if (event.pointerType === "touch" && event.buttons === 0) return;
              showValuationCursor(cursorIndexFromPointer(event),event.clientX,event.clientY);
            }};
            svg.onpointerleave = event => {{
              if (event.pointerType !== "touch") hideValuationCursor();
            }};
            svg.onkeydown = event => {{
              if (!["ArrowLeft","ArrowRight"].includes(event.key)) return;
              event.preventDefault();
              const next=valuationCursorIndex === null
                ? rows.length-1
                : valuationCursorIndex+(event.key === "ArrowRight" ? 1 : -1);
              showValuationCursor(next);
            }};
          }}
          document.querySelectorAll("[data-valuation-metric]").forEach(button => {{
            button.addEventListener("click", () => {{
              valuationMetric = button.dataset.valuationMetric;
              document.querySelectorAll("[data-valuation-metric]").forEach(item =>
                item.classList.toggle("is-active", item === button));
              const isPe = valuationMetric === "pe";
              const title = document.getElementById("valuation-chart-title");
              const barLegend = document.getElementById("valuation-bar-legend");
              const medianLegend = document.getElementById("valuation-median-legend");
              const lineLegend = document.getElementById("valuation-line-legend");
              if (title) title.textContent = isPe ? "PE Ratio" : "Market Cap / Sales";
              if (barLegend) barLegend.textContent = isPe ? "■ TTM EPS" : "■ TTM Sales";
              if (medianLegend) medianLegend.textContent = isPe ? "┄ Median PE" : "┄ Median MCap / Sales";
              if (lineLegend) lineLegend.textContent = isPe ? "━ PE ratio" : "━ Market Cap / Sales";
              drawValuationChart();
            }});
          }});
          document.querySelectorAll("[data-valuation-months]").forEach(button => {{
            button.addEventListener("click", () => {{
              valuationMonths = button.dataset.valuationMonths;
              document.querySelectorAll("[data-valuation-months]").forEach(item =>
                item.classList.toggle("is-active", item === button));
              drawValuationChart();
            }});
          }});
          if (valuationPriceToggle) {{
            valuationPriceToggle.addEventListener("click", () => {{
              valuationPriceEnabled = !valuationPriceEnabled;
              valuationPriceToggle.classList.toggle("is-active", valuationPriceEnabled);
              valuationPriceToggle.setAttribute(
                "aria-pressed",
                valuationPriceEnabled ? "true" : "false"
              );
              const check = valuationPriceToggle.querySelector("span");
              if (check) check.textContent = valuationPriceEnabled ? "✓" : "";
              drawValuationChart();
            }});
          }}
          function setValuationOpen(open) {{
            if (!valuationDrawer || !valuationPanel) return;
            if (open && fundamentalsDrawer) setFundamentalsOpen(false);
            valuationDrawer.classList.toggle("is-open", open);
            valuationPanel.toggleAttribute("inert", !open);
            valuationPanel.setAttribute("aria-hidden", open ? "false" : "true");
            if (open) requestAnimationFrame(drawValuationChart);
          }}
          if (valuationToggle) valuationToggle.addEventListener("click", () =>
            setValuationOpen(!valuationDrawer.classList.contains("is-open")));
          if (valuationClose) valuationClose.addEventListener("click", () => setValuationOpen(false));
          if (valuationScrim) valuationScrim.addEventListener("click", () => setValuationOpen(false));
          window.addEventListener("resize", () => {{
            if (valuationDrawer && valuationDrawer.classList.contains("is-open")) drawValuationChart();
          }});

          if (priceAlertButton) {{
            priceAlertButton.addEventListener("click", function(event) {{
              event.preventDefault();
              if (priceAlertButton.getAttribute("aria-disabled") === "true") return;
              const price = Number(priceAlertButton.dataset.alertPrice);
              if (!Number.isFinite(price) || price <= 0) return;
              postChartMessage({{
                source: "nse-interactive-chart",
                action: "create-price-alert",
                symbol: priceAlertButton.dataset.symbol || "",
                market: priceAlertButton.dataset.market || "INDIA",
                price: price,
                eventId: String(Date.now()) + "-" + String(price)
              }});
              const marker = priceAlertButton.querySelector("span");
              if (marker) marker.textContent = "✓";
              priceAlertButton.title = "Price alert submitted";
              window.setTimeout(function() {{ if (marker) marker.textContent = "+"; }}, 1200);
            }});
          }}

          function setFundamentalsOpen(isOpen) {{
            if (!fundamentalsDrawer || !fundamentalsToggle || !fundamentalsPanel) return;
            if (isOpen && valuationDrawer) setValuationOpen(false);
            fundamentalsDrawer.classList.toggle("is-open", isOpen);
            fundamentalsToggle.setAttribute("aria-expanded", isOpen ? "true" : "false");
            fundamentalsPanel.setAttribute("aria-hidden", isOpen ? "false" : "true");
            if (isOpen) {{
              fundamentalsPanel.removeAttribute("inert");
              if (fundamentalsClose) fundamentalsClose.focus();
            }} else {{
              fundamentalsPanel.setAttribute("inert", "");
              fundamentalsToggle.focus();
            }}
          }}
          if (fundamentalsToggle) {{
            fundamentalsToggle.addEventListener("click", function() {{
              setFundamentalsOpen(
                !fundamentalsDrawer.classList.contains("is-open")
              );
            }});
          }}
          if (fundamentalsClose) {{
            fundamentalsClose.addEventListener("click", function() {{
              setFundamentalsOpen(false);
            }});
          }}
          if (fundamentalsScrim) {{
            fundamentalsScrim.addEventListener("click", function() {{
              setFundamentalsOpen(false);
            }});
          }}
          document.addEventListener("keydown", function(event) {{
            if (event.key === "Escape" && fundamentalsDrawer && fundamentalsDrawer.classList.contains("is-open")) {{
              setFundamentalsOpen(false);
            }}
          }});

          function postChartMessage(message) {{
            if (window.parent) {{
              try {{
                window.parent.postMessage(message, "*");
              }} catch (error) {{}}
            }}
            if (window.parent && window.parent.parent && window.parent.parent !== window.parent) {{
              try {{
                window.parent.parent.postMessage(message, "*");
              }} catch (error) {{}}
            }}
            if (window.top && window.top !== window.parent && window.top !== window.parent.parent) {{
              try {{
                window.top.postMessage(message, "*");
              }} catch (error) {{}}
            }}
          }}
          function requestMatchedStock(direction) {{
            postChartMessage({{
              source: "nse-interactive-chart",
              action: direction
            }});
          }}
          function rememberChartRange(range) {{
            postChartMessage({{
              source: "nse-interactive-chart",
              action: "range-change",
              range: String(range)
            }});
          }}
          let historyRequestPending = false;
          function requestOlderHistory(loadAll) {{
            if (historyRequestPending || !payload.hasEarlierHistory) return false;
            historyRequestPending = true;
            const currentYears = Number(payload.historyYears) || {SCREENING_HISTORY_YEARS};
            const visibleRange = chart.timeScale().getVisibleRange();
            postChartMessage({{
              source: "nse-interactive-chart",
              action: "load-history",
              targetYears: loadAll ? 10 : Math.min(10, currentYears + 2),
              showAll: Boolean(loadAll),
              visibleFrom: visibleRange ? formatTimeKey(visibleRange.from) : "",
              visibleTo: visibleRange ? formatTimeKey(visibleRange.to) : ""
            }});
            return true;
          }}
          const matchedPrevious = document.getElementById("matched-prev");
          const matchedNext = document.getElementById("matched-next");
          const chartClose = document.getElementById("chart-close");
          const chartFullscreen = document.getElementById("chart-fullscreen");
          const chartSymbolInput = document.getElementById("chart-symbol-input");
          const chartSymbolOptions = document.getElementById("chart-symbol-options");
          const chartMatchCounter = document.getElementById("chart-match-counter");
          let hostSymbols = [];
          let hostSymbolIndex = {initial_match_index};

          function normalizedSymbol(value) {{
            return String(value || "").trim().toUpperCase();
          }}

          function refreshSymbolNavigation(navigateOnMatch) {{
            if (!chartSymbolInput) return;
            const requested = normalizedSymbol(chartSymbolInput.value);
            const matchedIndex = hostSymbols.findIndex(function(item) {{
              return normalizedSymbol(item) === requested;
            }});
            const inTable = matchedIndex >= 0;
            const isDisplayedSymbol = requested === normalizedSymbol(
              "{html.escape(str(symbol), quote=True)}"
            );
            chartSymbolInput.classList.toggle("is-not-in-table", !inTable);
            if (matchedPrevious) {{
              matchedPrevious.disabled = !inTable || !isDisplayedSymbol || matchedIndex <= 0;
            }}
            if (matchedNext) {{
              matchedNext.disabled = (
                !inTable || !isDisplayedSymbol || matchedIndex >= hostSymbols.length - 1
              );
            }}
            if (chartMatchCounter) {{
              chartMatchCounter.textContent = inTable
                ? (matchedIndex + 1) + " / " + hostSymbols.length
                : "Not in table";
            }}
            if (inTable) hostSymbolIndex = matchedIndex;
            if (
              navigateOnMatch
              && inTable
              && normalizedSymbol(hostSymbols[matchedIndex]) !== normalizedSymbol("{html.escape(str(symbol), quote=True)}")
            ) {{
              postChartMessage({{
                source: "nse-interactive-chart",
                action: "symbol-select",
                symbol: hostSymbols[matchedIndex]
              }});
            }}
          }}

          window.addEventListener("message", function(event) {{
            const message = event && event.data;
            if (!message || message.source !== "nse-chart-host") return;
            if (message.action !== "symbols" || !Array.isArray(message.symbols)) return;
            hostSymbols = message.symbols.map(function(item) {{
              return String(item || "").trim();
            }}).filter(Boolean);
            if (chartSymbolOptions) {{
              chartSymbolOptions.innerHTML = hostSymbols.map(function(item) {{
                const escaped = item.replace(/&/g, "&amp;")
                  .replace(/</g, "&lt;").replace(/"/g, "&quot;");
                return '<option value="' + escaped + '"></option>';
              }}).join("");
            }}
            refreshSymbolNavigation(false);
          }});

          if (chartSymbolInput) {{
            chartSymbolInput.addEventListener("focus", function() {{
              chartSymbolInput.select();
            }});
            chartSymbolInput.addEventListener("input", function() {{
              refreshSymbolNavigation(false);
            }});
            chartSymbolInput.addEventListener("change", function() {{
              refreshSymbolNavigation(true);
            }});
            chartSymbolInput.addEventListener("keydown", function(event) {{
              if (event.key !== "Enter") return;
              event.preventDefault();
              refreshSymbolNavigation(true);
            }});
          }}

          async function setFullscreenChart() {{
            const fullscreenTarget = document.documentElement;
            try {{
              if (!document.fullscreenElement) {{
                await fullscreenTarget.requestFullscreen();
                if (screen.orientation && screen.orientation.lock) {{
                  try {{ await screen.orientation.lock("landscape"); }} catch (error) {{}}
                }}
              }} else {{
                if (screen.orientation && screen.orientation.unlock) {{
                  try {{ screen.orientation.unlock(); }} catch (error) {{}}
                }}
                await document.exitFullscreen();
              }}
            }} catch (error) {{
              postChartMessage({{
                source: "nse-interactive-chart",
                action: "fullscreen-unavailable"
              }});
            }}
          }}

          function refreshFullscreenButton() {{
            if (!chartFullscreen) return;
            const isFullscreen = Boolean(document.fullscreenElement);
            chartFullscreen.classList.toggle("is-fullscreen", isFullscreen);
            chartFullscreen.setAttribute(
              "aria-label",
              isFullscreen ? "Exit fullscreen chart" : "Enter fullscreen landscape chart"
            );
            chartFullscreen.title = isFullscreen ? "Return to normal view" : "Enter fullscreen";
          }}

          if (chartFullscreen) {{
            chartFullscreen.addEventListener("click", setFullscreenChart);
            document.addEventListener("fullscreenchange", refreshFullscreenButton);
          }}
          if (matchedPrevious) {{
            matchedPrevious.addEventListener("click", function() {{
              requestMatchedStock("previous");
            }});
          }}
          if (matchedNext) {{
            matchedNext.addEventListener("click", function() {{
              requestMatchedStock("next");
            }});
          }}
          if (chartClose) {{
            chartClose.addEventListener("click", function() {{
              requestMatchedStock("close");
            }});
          }}
          if (!window.LightweightCharts) {{
            loading.textContent = "Interactive chart library could not load. Check the internet connection and retry.";
            return;
          }}

          function minimumBarSpacingForWidth(width) {{
            const usableWidth = Math.max(120, Number(width) - 64);
            const candleCount = Math.max(1, payload.candles.length);
            // Permit every candle to fit on screen, including long histories
            // on portrait mobile, while retaining a practical lower bound.
            return Math.max(0.01, Math.min(0.5, usableWidth / (candleCount + 12)));
          }}

          function minimumChartHeight() {{
            return window.matchMedia("(max-width: 640px)").matches ? 120 : 280;
          }}

          const initialChartWidth = Math.max(240, container.clientWidth);
          const colors = ["#2563eb", "#9333ea", "#ea580c", "#0891b2", "#be123c", "#4f46e5", "#15803d"];
          const chart = LightweightCharts.createChart(container, {{
            width: initialChartWidth,
            height: Math.max(minimumChartHeight(), container.clientHeight),
            layout: {{
              background: {{ type: LightweightCharts.ColorType.Solid, color: "#ffffff" }},
              textColor: "#52667a",
              fontFamily: "Inter, ui-sans-serif, system-ui, sans-serif",
            }},
            grid: {{
              vertLines: {{ color: "#eef3f6" }},
              horzLines: {{ color: "#e7eef3" }},
            }},
            crosshair: {{
              mode: LightweightCharts.CrosshairMode.Normal,
              vertLine: {{ color: "#6b879a", width: 1, labelBackgroundColor: "#176b87" }},
              horzLine: {{ color: "#6b879a", width: 1, labelBackgroundColor: "#176b87" }},
            }},
            rightPriceScale: {{ borderColor: "#dce6ee", scaleMargins: {{ top: 0.08, bottom: 0.24 }} }},
            timeScale: {{
              borderColor: "#dce6ee",
              timeVisible: false,
              rightOffset: 6,
              barSpacing: 7,
              minBarSpacing: minimumBarSpacingForWidth(initialChartWidth),
            }},
            handleScroll: {{ mouseWheel: true, pressedMouseMove: true, horzTouchDrag: true, vertTouchDrag: false }},
            handleScale: {{ axisPressedMouseMove: true, mouseWheel: true, pinch: true }},
          }});

          const candleSeries = chart.addSeries(LightweightCharts.CandlestickSeries, {{
            upColor: "#16a34a",
            downColor: "#dc2626",
            borderUpColor: "#15803d",
            borderDownColor: "#b91c1c",
            wickUpColor: "#15803d",
            wickDownColor: "#b91c1c",
            priceLineColor: "#176b87",
          }});
          candleSeries.setData(payload.candles);
          const candleIndexByTime = new Map(
            payload.candles.map(function(candle, index) {{
              return [String(candle.time), index];
            }})
          );
          const tradeOverlay = payload.tradeOverlay || {{}};
          [
            {{ key: "buyPrice", title: "BUY", color: "#15803d", style: LightweightCharts.LineStyle.Solid }},
            {{ key: "targetPrice", title: "TARGET", color: "#2563eb", style: LightweightCharts.LineStyle.Dashed }},
            {{ key: "stopPrice", title: "STOP", color: "#dc2626", style: LightweightCharts.LineStyle.Dashed }},
          ].forEach(function(level) {{
            const price = Number(tradeOverlay[level.key]);
            if (!Number.isFinite(price)) return;
            candleSeries.createPriceLine({{
              price: price,
              color: level.color,
              lineWidth: level.width || 2,
              lineStyle: level.style,
              axisLabelVisible: true,
              title: level.title,
            }});
          }});

          const tradeMarkers = [];
          if (tradeOverlay.buyDate && candleIndexByTime.has(String(tradeOverlay.buyDate))) {{
            tradeMarkers.push({{
              time: tradeOverlay.buyDate,
              position: "belowBar",
              color: "#15803d",
              shape: "arrowUp",
              text: "BUY",
            }});
          }}
          if (tradeOverlay.exitDate && candleIndexByTime.has(String(tradeOverlay.exitDate))) {{
            const reasonLabel = tradeOverlay.exitReason === "Target"
              ? "TARGET"
              : (tradeOverlay.exitReason === "Stop Loss" ? "STOP" : "END");
            tradeMarkers.push({{
              time: tradeOverlay.exitDate,
              position: "aboveBar",
              color: reasonLabel === "TARGET" ? "#2563eb" : (reasonLabel === "STOP" ? "#dc2626" : "#475569"),
              shape: "arrowDown",
              text: reasonLabel,
            }});
          }}
          const candleTimes = payload.candles.map(function(candle) {{
            return String(candle.time);
          }});
          const chartAlertMarkers = Array.isArray(payload.alertMarkers)
            ? payload.alertMarkers.slice()
            : [];
          const legacyAlertPrice = Number(tradeOverlay.alertPrice);
          if (Number.isFinite(legacyAlertPrice)) {{
            chartAlertMarkers.push({{
              id: "selected-alert",
              date: String(tradeOverlay.alertDate || ""),
              price: legacyAlertPrice,
              direction: "above",
            }});
          }}
          const uniqueAlertMarkers = [];
          const alertMarkerKeys = new Set();
          chartAlertMarkers.forEach(function(marker) {{
            const price = Number(marker.price);
            if (!Number.isFinite(price)) return;
            const date = String(marker.date || "");
            const direction = marker.direction === "below" ? "below" : "above";
            const key = price.toFixed(8) + "|" + date + "|" + direction;
            if (alertMarkerKeys.has(key)) return;
            alertMarkerKeys.add(key);
            uniqueAlertMarkers.push({{
              id: String(marker.id || ""),
              date: date,
              price: price,
              direction: direction,
            }});
          }});
          const alertLinePrices = new Set();
          uniqueAlertMarkers.forEach(function(marker) {{
            const priceKey = marker.price.toFixed(8);
            if (alertLinePrices.has(priceKey)) return;
            alertLinePrices.add(priceKey);
            const isBelow = marker.direction === "below";
            candleSeries.createPriceLine({{
              price: marker.price,
              color: isBelow ? "#c026d3" : "#7c3aed",
              lineWidth: 1,
              lineStyle: LightweightCharts.LineStyle.Dashed,
              axisLabelVisible: true,
              title: isBelow ? "ALERT ↓" : "ALERT ↑",
            }});
          }});
          if (watchlistSelect && watchlistAddButton) {{
            watchlistAddButton.addEventListener("click", function(event) {{
              event.preventDefault();
              const watchlistId = String(watchlistSelect.value || "");
              if (!watchlistId) return;
              postChartMessage({{
                source: "nse-interactive-chart",
                action: "add-to-watchlist",
                watchlistId: watchlistId,
                symbol: watchlistAddButton.dataset.symbol || "",
                market: watchlistAddButton.dataset.market || "INDIA",
                eventId: String(Date.now()) + "-" + watchlistId
              }});
              watchlistAddButton.disabled = true;
              watchlistAddButton.textContent = "Added";
              window.setTimeout(function() {{
                watchlistAddButton.disabled = false;
                watchlistAddButton.textContent = "Add stock";
              }}, 1200);
            }});
          }}
          if (tradeMarkers.length) {{
            if (typeof LightweightCharts.createSeriesMarkers === "function") {{
              LightweightCharts.createSeriesMarkers(candleSeries, tradeMarkers);
            }} else if (typeof candleSeries.setMarkers === "function") {{
              candleSeries.setMarkers(tradeMarkers);
            }}
          }}
          uniqueAlertMarkers.forEach(function(marker) {{
            let alertMarkerTime = marker.date;
            if (alertMarkerTime && !candleIndexByTime.has(alertMarkerTime)) {{
              alertMarkerTime = [...candleTimes].reverse().find(function(time) {{
                return time <= marker.date;
              }}) || "";
            }}
            if (!alertMarkerTime || !candleIndexByTime.has(alertMarkerTime)) return;
            const isBelow = marker.direction === "below";
            const alertAnchorSeries = chart.addSeries(LightweightCharts.LineSeries, {{
              color: "rgba(124,58,237,0)",
              lineWidth: 1,
              priceLineVisible: false,
              lastValueVisible: false,
              crosshairMarkerVisible: false,
            }});
            alertAnchorSeries.setData([{{
              time: alertMarkerTime,
              value: marker.price,
            }}]);
            const alertAnchorMarkers = [{{
              time: alertMarkerTime,
              position: isBelow ? "aboveBar" : "belowBar",
              color: isBelow ? "#c026d3" : "#7c3aed",
              shape: isBelow ? "arrowDown" : "arrowUp",
              text: isBelow ? "A↓" : "A↑",
            }}];
            if (typeof LightweightCharts.createSeriesMarkers === "function") {{
              LightweightCharts.createSeriesMarkers(alertAnchorSeries, alertAnchorMarkers);
            }} else if (typeof alertAnchorSeries.setMarkers === "function") {{
              alertAnchorSeries.setMarkers(alertAnchorMarkers);
            }}
          }});

          const maSeries = [];
          payload.maPeriods.forEach(function(period, index) {{
            const label = "SMA" + period;
            const color = colors[index % colors.length];
            const points = payload.movingAverages[label] || [];
            const series = chart.addSeries(LightweightCharts.LineSeries, {{
              color: color,
              lineWidth: 2,
              priceLineVisible: false,
              lastValueVisible: true,
              crosshairMarkerVisible: false,
            }});
            series.setData(points);
            maSeries.push({{
              label: label,
              color: color,
              series: series,
              values: new Map(points.map(function(point) {{
                return [String(point.time), point.value];
              }})),
            }});
          }});

          if (payload.volume.length) {{
            const volumeSeries = chart.addSeries(LightweightCharts.HistogramSeries, {{
              priceFormat: {{ type: "volume" }},
              priceScaleId: "",
              priceLineVisible: false,
              lastValueVisible: false,
            }});
            volumeSeries.priceScale().applyOptions({{ scaleMargins: {{ top: 0.82, bottom: 0 }} }});
            volumeSeries.setData(payload.volume);
          }}

          function formatPrice(value) {{
            return Number.isFinite(Number(value))
              ? Number(value).toLocaleString(undefined, {{ minimumFractionDigits: 2, maximumFractionDigits: 2 }})
              : "—";
          }}

          function formatTimeKey(time) {{
            if (!time) return "";
            if (typeof time === "string") return time;
            if (typeof time === "object" && time.year && time.month && time.day) {{
              return String(time.year) + "-" +
                String(time.month).padStart(2, "0") + "-" +
                String(time.day).padStart(2, "0");
            }}
            return String(time);
          }}

          function candleAtOrBeforeCursor(param) {{
            if (!param || !payload.candles.length) return null;
            const key = formatTimeKey(param.time);
            const logical = Number(param.logical);
            let index = null;

            if (Number.isFinite(logical)) {{
              const roundedLogical = Math.round(logical);
              const isDirectlyOnCandle = Math.abs(logical - roundedLogical) < 0.001;
              index = isDirectlyOnCandle && candleIndexByTime.has(key)
                ? candleIndexByTime.get(key)
                : Math.floor(logical);
            }} else if (candleIndexByTime.has(key)) {{
              index = candleIndexByTime.get(key);
            }}

            // When the cursor is between candles (or in the right-side empty
            // area), use the candle immediately before its logical position.
            if (index === null || index < 0) return null;
            index = Math.min(index, payload.candles.length - 1);
            return {{ index: index, candle: payload.candles[index] }};
          }}

          function gainFromPreviousCandle(index) {{
            if (index <= 0 || index >= payload.candles.length) return null;
            const previousClose = Number(payload.candles[index - 1].close);
            const currentClose = Number(payload.candles[index].close);
            if (!Number.isFinite(previousClose) || !Number.isFinite(currentClose) || previousClose === 0) {{
              return null;
            }}
            return (currentClose - previousClose) / Math.abs(previousClose) * 100;
          }}

          function formatGain(value) {{
            if (!Number.isFinite(Number(value))) return "—";
            const numeric = Number(value);
            return (numeric > 0 ? "+" : "") + numeric.toFixed(2) + "%";
          }}

          function renderLegend(time, candle, candleIndex, param) {{
            if (!candle) return;
            const gain = gainFromPreviousCandle(candleIndex);
            const gainClass = gain > 0 ? "is-positive" : (gain < 0 ? "is-negative" : "is-neutral");
            let content = '<span class="legend-date">' + String(time || "") + '</span>' +
              '<span class="legend-ohlc">O <b>' + formatPrice(candle.open) + '</b></span>' +
              '<span class="legend-ohlc">H <b>' + formatPrice(candle.high) + '</b></span>' +
              '<span class="legend-ohlc">L <b>' + formatPrice(candle.low) + '</b></span>' +
              '<span class="legend-ohlc">C <b>' + formatPrice(candle.close) + '</b></span>' +
              '<span class="legend-gain ' + gainClass + '" title="Gain versus previous candle close">' +
              'Gain <b>' + formatGain(gain) + '</b></span>';
            maSeries.forEach(function(item) {{
              const point = param && param.seriesData
                ? param.seriesData.get(item.series)
                : null;
              const value = point && Number.isFinite(Number(point.value))
                ? point.value
                : item.values.get(String(time));
              content += '<span class="legend-ma" style="color:' + item.color + '">' +
                item.label + ' ' + formatPrice(value) + '</span>';
            }});
            legend.innerHTML = content;
          }}
          const latestCandleIndex = payload.candles.length - 1;
          if (latestCandleIndex >= 0) {{
            const latestCandle = payload.candles[latestCandleIndex];
            renderLegend(latestCandle.time, latestCandle, latestCandleIndex, null);
          }}

          function updateCursorPriceAlert(param) {{
            if (!priceAlertButton || !param || !param.point) return;
            const price = Number(candleSeries.coordinateToPrice(param.point.y));
            const current = Number(priceAlertButton.dataset.currentPrice);
            if (!Number.isFinite(price) || price <= 0 ||
                (Number.isFinite(current) && Math.abs(price - current) < 0.00000001)) {{
              priceAlertButton.setAttribute("aria-disabled", "true");
              priceAlertButton.classList.remove("is-ready");
              return;
            }}
            const roundedPrice = Number(price.toFixed(8));
            const safeY = Math.max(15, Math.min(container.clientHeight - 28, Number(param.point.y)));
            const direction = Number.isFinite(current) && roundedPrice < current ? "below" : "above";
            priceAlertButton.style.top = safeY + "px";
            priceAlertButton.dataset.alertPrice = String(roundedPrice);
            priceAlertButton.setAttribute("aria-disabled", "false");
            priceAlertButton.classList.add("is-ready");
            priceAlertButton.setAttribute(
              "aria-label",
              "Add alert when price crosses " + direction + " " + formatPrice(roundedPrice)
            );
            priceAlertButton.title = "Add alert at " + formatPrice(roundedPrice);
          }}

          chart.subscribeCrosshairMove(function(param) {{
            updateCursorPriceAlert(param);
            const selected = candleAtOrBeforeCursor(param);
            if (!selected) return;
            renderLegend(selected.candle.time, selected.candle, selected.index, param);
          }});

          function showBars(count) {{
            document.querySelectorAll("[data-range]").forEach(function(button) {{
              button.classList.toggle("active", String(button.dataset.range) === String(count));
            }});
            if (count === "all") {{
              if (requestOlderHistory(true)) return;
              chart.timeScale().fitContent();
              return;
            }}
            const total = payload.candles.length;
            chart.timeScale().setVisibleLogicalRange({{
              from: Math.max(0, total - Number(count)),
              to: total + 4,
            }});
          }}

          document.querySelectorAll("[data-range]").forEach(function(button) {{
            button.addEventListener("click", function() {{
              showBars(button.dataset.range);
              rememberChartRange(button.dataset.range);
            }});
          }});

          chart.timeScale().subscribeVisibleLogicalRangeChange(function(range) {{
            if (!range || Number(range.from) > 12 || !payload.hasEarlierHistory) return;
            requestOlderHistory(false);
          }});

          const resizeObserver = new ResizeObserver(function(entries) {{
            const rect = entries[0].contentRect;
            const chartWidth = Math.max(240, Math.floor(rect.width));
            chart.applyOptions({{
              width: chartWidth,
              height: Math.max(minimumChartHeight(), Math.floor(rect.height)),
              timeScale: {{
                minBarSpacing: minimumBarSpacingForWidth(chartWidth),
              }},
            }});
          }});
          resizeObserver.observe(container);
          loading.remove();
          const tradeWindowStart = candleIndexByTime.get(String(tradeOverlay.windowStart || ""));
          const tradeWindowEnd = candleIndexByTime.get(String(tradeOverlay.windowEnd || ""));
          if (payload.restoreVisibleRange && payload.restoreVisibleRange.from && payload.restoreVisibleRange.to) {{
            chart.timeScale().setVisibleRange(payload.restoreVisibleRange);
          }} else if (Number.isInteger(tradeWindowStart) && Number.isInteger(tradeWindowEnd)) {{
            chart.timeScale().setVisibleLogicalRange({{
              from: Math.max(0, tradeWindowStart),
              to: Math.min(payload.candles.length - 1, tradeWindowEnd) + 1,
            }});
          }} else {{
            showBars({json.dumps(selected_range)});
          }}
        }})();
      </script>
    </body>
    </html>
    """


def render_interactive_stock_chart(
    symbol,
    json_path,
    ma_periods=None,
    pe_ratio=None,
    match_position=None,
    match_total=None,
    has_previous=False,
    has_next=False,
    initial_range="252",
    growth_metrics=None,
    valuation_medians=None,
    trade_overlay=None,
    alert_markers=None,
    alert_market="INDIA",
    height=760,
    watchlists=None,
    watchlist_add_callback=None,
):
    history_key = f"_interactive_history_years_{str(alert_market).upper()}_{symbol}"
    visible_range_key = f"{history_key}_visible_range"
    range_override_key = f"{history_key}_range_override"
    history_years = int(
        st.session_state.get(history_key, SCREENING_HISTORY_YEARS)
    )
    restore_visible_range = st.session_state.pop(visible_range_key, None)
    effective_initial_range = st.session_state.pop(
        range_override_key,
        initial_range,
    )
    chart_html = interactive_stock_chart_html(
        symbol,
        json_path,
        ma_periods=ma_periods,
        pe_ratio=pe_ratio,
        match_position=match_position,
        match_total=match_total,
        has_previous=has_previous,
        has_next=has_next,
        initial_range=effective_initial_range,
        growth_metrics=growth_metrics,
        valuation_medians=valuation_medians,
        trade_overlay=trade_overlay,
        alert_markers=alert_markers,
        alert_market=alert_market,
        history_years=history_years,
        restore_visible_range=restore_visible_range,
        watchlists=watchlists,
    )
    alert_event = _CURSOR_ALERT_COMPONENT(
        chartHtml=chart_html,
        height=int(height),
        default=None,
        key=f"cursor_alert_chart_{str(alert_market).upper()}_{symbol}",
    )
    if not isinstance(alert_event, dict):
        return
    if alert_event.get("action") == "load-history":
        try:
            target_years = max(
                history_years,
                min(10, int(alert_event.get("targetYears", history_years + 2))),
            )
        except (TypeError, ValueError):
            target_years = min(10, history_years + 2)
        if target_years > history_years:
            st.session_state[history_key] = target_years
            if alert_event.get("showAll"):
                st.session_state[range_override_key] = "all"
            else:
                visible_from = str(alert_event.get("visibleFrom") or "")
                visible_to = str(alert_event.get("visibleTo") or "")
                if visible_from and visible_to:
                    st.session_state[visible_range_key] = {
                        "from": visible_from,
                        "to": visible_to,
                    }
            st.rerun()
        return
    if alert_event.get("action") == "add-to-watchlist":
        event_id = str(alert_event.get("eventId") or "")
        processed_key = (
            f"_processed_watchlist_add_"
            f"{str(alert_market).upper()}_{symbol}"
        )
        if (
            event_id
            and st.session_state.get(processed_key) != event_id
            and callable(watchlist_add_callback)
        ):
            st.session_state[processed_key] = event_id
            watchlist_add_callback(alert_event)
        return
    if alert_event.get("action") != "create-price-alert":
        return

    event_id = str(alert_event.get("eventId") or "")
    processed_key = f"_processed_cursor_alert_{str(alert_market).upper()}_{symbol}"
    if not event_id or st.session_state.get(processed_key) == event_id:
        return
    st.session_state[processed_key] = event_id
    try:
        alert, created = create_price_alert(
            alert_event.get("symbol", symbol),
            alert_event.get("market", alert_market),
            alert_event.get("price"),
        )
        direction = "above" if alert.get("direction") == "above" else "below"
        message = (
            f"Alert created: {alert['symbol']} crosses {direction} {alert['target_price']:g}."
            if created
            else "That price alert already exists."
        )
        st.toast(message, icon="🔔")
        if created:
            st.session_state.pop("_cached_price_alerts", None)
            st.session_state.pop("_cached_price_alerts_at", None)
            st.rerun()
    except (TypeError, ValueError, OSError, RuntimeError) as exc:
        st.toast(f"Could not create alert: {exc}", icon="⚠️")


def results_hover_table_html(
    df,
    interactive_market=None,
    interactive_ma_periods=None,
    interactive_symbol_click=False,
    table_title="Screening Results",
    row_actions=False,
    count_label=None,
    component_height=700,
):
    visible_df = df.drop(
        columns=[
            "ChartPath",
            "ChartSource",
            "GrowthMetrics",
            "ValuationMedians",
            "Sales CAGR 3Y",
            "Profit CAGR 3Y",
            "Price CAGR 3Y",
            "ROE 3Y",
            "Interactive Market",
            "Alert Date",
            "Alert Price",
            "Acknowledge URL",
            "Remove URL",
            "Acknowledge Button Key",
            "Remove Button Key",
        ],
        errors="ignore",
    )
    chart_paths = df.get("ChartPath")
    nav_origins = (
        ("3.65rem", "3.45rem", "3.05rem")
        if row_actions
        else ("0rem", "0rem", "0rem")
    )

    styles = """
    <style>
      :root {
        --ink: #10243e;
        --muted: #64748b;
        --brand: #176b87;
        --brand-dark: #10536a;
        --brand-soft: #e9f6f8;
        --border: #dce6ee;
        --component-nav-origin: __NAV_ORIGIN_DESKTOP__;
        --fixed-app-nav-clearance: calc(
          var(--component-nav-origin) + 3.15rem + 0.96rem + 2px + 0.8rem
        );
      }
      * { box-sizing: border-box; }
      body {
        margin: 0;
        padding: 2px;
        max-width: 100%;
        overflow-x: hidden;
        background: transparent;
        color: #334a63;
        font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      }
      .results-table-shell {
        max-width: 100%;
        overflow: hidden;
        border: 1px solid var(--border);
        border-radius: 16px;
        background: #ffffff;
        box-shadow: 0 8px 28px rgba(16, 36, 62, 0.08);
      }
      .results-table-toolbar {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 16px;
        padding: 13px 16px;
        border-bottom: 1px solid var(--border);
        background: linear-gradient(135deg, #f8fbfc, #eef7f9);
      }
      .results-table-toolbar__title {
        color: var(--ink);
        font-size: 14px;
        font-weight: 800;
      }
      .results-table-toolbar__meta {
        color: var(--muted);
        font-size: 11px;
        font-weight: 600;
        text-align: right;
      }
      .results-count {
        display: inline-block;
        margin-left: 7px;
        padding: 3px 8px;
        border-radius: 999px;
        background: var(--brand);
        color: #ffffff;
        font-size: 10px;
        letter-spacing: 0.03em;
        text-transform: uppercase;
      }
      .results-table-wrapper {
        max-height: 430px;
        overflow-x: hidden;
        overflow-y: auto;
        width: 100%;
        -webkit-overflow-scrolling: touch;
      }
      .hover-results-table {
        width: 100%;
        max-width: 100%;
        table-layout: fixed;
        border-collapse: separate;
        border-spacing: 0;
        font-size: 13px;
      }
      .hover-results-table th, .hover-results-table td {
        border-bottom: 1px solid #e5e7eb;
        padding: 11px 13px;
        text-align: left;
        vertical-align: middle;
        min-width: 0;
        overflow: hidden;
        overflow-wrap: anywhere;
      }
      .hover-results-table th {
        position: sticky;
        top: 0;
        z-index: 4;
        background: #102f45;
        color: rgba(255, 255, 255, 0.88);
        font-size: 10px;
        font-weight: 800;
        letter-spacing: 0.065em;
        text-transform: uppercase;
        user-select: none;
        white-space: normal;
      }
      .hover-results-table th:not(:first-child),
      .hover-results-table td:not(:first-child) {
        text-align: right;
        font-variant-numeric: tabular-nums;
      }
      .hover-results-table th.sortable {
        cursor: pointer;
        transition: background 0.15s ease;
      }
      .hover-results-table th.sortable:hover { background: #17445f; }
      .hover-results-table th.sortable::after {
        content: "↕";
        margin-left: 6px;
        color: #82d4db;
        font-size: 10px;
      }
      .hover-results-table th.sortable[data-sort-dir="asc"]::after { content: "↑"; }
      .hover-results-table th.sortable[data-sort-dir="desc"]::after { content: "↓"; }
      .hover-results-table tbody tr:nth-child(even) { background: #f8fbfc; }
      .hover-results-table tbody tr { transition: background 0.14s ease, box-shadow 0.14s ease; }
      .hover-results-table tbody tr:hover {
        background: #edf7f9;
        box-shadow: inset 3px 0 0 var(--brand);
      }
      .hover-results-table tbody tr.valuation-favorable {
        background: #edf9f0;
      }
      .hover-results-table tbody tr.valuation-unfavorable {
        background: #fff1f1;
      }
      .hover-results-table tbody tr.valuation-favorable:hover {
        background: #ddf3e3;
        box-shadow: inset 3px 0 0 #2d9852;
      }
      .hover-results-table tbody tr.valuation-unfavorable:hover {
        background: #ffe2e2;
        box-shadow: inset 3px 0 0 #c75c5c;
      }
      .hover-results-table tbody tr:last-child td { border-bottom: none; }
      .hover-results-table td:first-child { font-weight: 750; }
      .hover-results-table th:first-child,
      .hover-results-table td:first-child { width: 40%; }
      .stock-symbol-cell {
        display: inline-flex;
        align-items: center;
        gap: 4px;
        white-space: nowrap;
        max-width: 100%;
        min-width: 0;
      }
      .stock-hover,
      .stock-symbol-label {
        display: inline-flex;
        align-items: center;
        gap: 6px;
        padding: 4px 9px;
        border: 1px solid #c7e2e7;
        border-radius: 999px;
        background: var(--brand-soft);
        color: var(--brand-dark);
        font-weight: 800;
        transition: transform 0.14s ease, box-shadow 0.14s ease;
        max-width: calc(100% - 52px);
        min-width: 0;
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
      }
      .stock-hover { cursor: pointer; }
      .stock-symbol-label { cursor: default; }
      .stock-hover.valuation-favorable,
      .stock-symbol-label.valuation-favorable {
        border-color: #78c68f;
        background: #e4f7e9;
        color: #126736;
      }
      .stock-hover.valuation-unfavorable,
      .stock-symbol-label.valuation-unfavorable {
        border-color: #dfa0a0;
        background: #ffebeb;
        color: #962d2d;
      }
      .stock-hover::after {
        content: "↗";
        color: var(--brand);
        font-size: 11px;
      }
      .stock-hover:hover {
        transform: translateY(-1px);
        box-shadow: 0 4px 10px rgba(23, 107, 135, 0.14);
      }
      .interactive-chart-link {
        display: inline-grid;
        place-items: center;
        width: 22px;
        height: 22px;
        flex: 0 0 22px;
        padding: 0;
        border: 1px solid #f0b15f;
        border-radius: 6px;
        background: #fff7e8;
        color: #b65d18;
        cursor: pointer;
        text-decoration: none;
        transition: transform 0.14s ease, border-color 0.14s ease, background 0.14s ease, box-shadow 0.14s ease;
        -webkit-tap-highlight-color: transparent;
        touch-action: manipulation;
      }
      .interactive-chart-link:hover,
      .interactive-chart-link:focus {
        transform: translateY(-1px);
        border-color: #df7a2c;
        background: #ffedd2;
        box-shadow: 0 3px 8px rgba(182, 93, 24, 0.18);
        outline: none;
      }
      .interactive-chart-link.active {
        border-color: #df7a2c;
        background: #fbd9ad;
        color: #91420f;
        box-shadow: 0 0 0 3px rgba(223, 122, 44, 0.13);
      }
      .interactive-chart-link svg {
        width: 13px;
        height: 13px;
        pointer-events: none;
      }
      .alert-row-actions {
        display: inline-flex;
        align-items: center;
        justify-content: flex-end;
        gap: 5px;
        width: 100%;
      }
      .alert-row-action {
        display: inline-grid;
        place-items: center;
        min-width: 26px;
        height: 26px;
        padding: 0 7px;
        border: 1px solid #cbd5e1;
        border-radius: 7px;
        background: #ffffff;
        color: #334155;
        font-size: 12px;
        font-weight: 800;
        line-height: 1;
        text-decoration: none;
        cursor: pointer;
        -webkit-tap-highlight-color: transparent;
        touch-action: manipulation;
      }
      .alert-row-action:hover,
      .alert-row-action:focus {
        transform: translateY(-1px);
        outline: none;
      }
      .alert-row-action--acknowledge {
        border-color: #79c99a;
        background: #ecf9f1;
        color: #166534;
      }
      .alert-row-action--remove {
        border-color: #efaaaa;
        background: #fff1f1;
        color: #b42323;
      }
      .interactive-chart-link.interactive-symbol-button {
        width: auto;
        height: auto;
        max-width: 100%;
        min-width: 0;
        flex: 0 1 auto;
        padding: 0;
        border: 0;
        background: transparent;
        box-shadow: none;
      }
      .interactive-symbol-button .stock-symbol-label {
        max-width: 100%;
      }
      .interactive-chart-link.interactive-symbol-button:hover,
      .interactive-chart-link.interactive-symbol-button:focus {
        border: 0;
        background: transparent;
        box-shadow: none;
      }
      .screener-company-link {
        display: inline-grid;
        place-items: center;
        width: 22px;
        height: 22px;
        flex: 0 0 22px;
        padding: 0;
        border: 1px solid #84c99a;
        border-radius: 6px;
        background: #eefaf1;
        color: #17713b;
        cursor: pointer;
        font-size: 13px;
        font-weight: 900;
        line-height: 1;
        text-decoration: none;
        transition: transform 0.14s ease, border-color 0.14s ease, background 0.14s ease, box-shadow 0.14s ease;
        -webkit-tap-highlight-color: transparent;
        touch-action: manipulation;
      }
      .screener-company-link:hover,
      .screener-company-link:focus {
        transform: translateY(-1px);
        border-color: #3c9a5c;
        background: #ddf5e4;
        box-shadow: 0 3px 8px rgba(23, 113, 59, 0.16);
        outline: none;
      }
      .stock-hover .chart-tooltip { display: none; }
      .chart-tooltip img { width: 100%; height: auto; display: block; object-fit: contain; }
      .stock-hover-active {
        border-color: var(--brand) !important;
        background: #d9f1f3 !important;
        box-shadow: 0 0 0 3px rgba(23, 107, 135, 0.10);
      }
      .stock-hover-active.valuation-favorable {
        border-color: #2d9852 !important;
        background: #d3f1dc !important;
        box-shadow: 0 0 0 3px rgba(21, 128, 61, 0.12);
      }
      .stock-hover-active.valuation-unfavorable {
        border-color: #c75c5c !important;
        background: #ffdcdc !important;
        box-shadow: 0 0 0 3px rgba(185, 28, 28, 0.11);
      }

      /* ---- Fixed chart panel below table (all screen sizes) ---- */
      .chart-panel {
        display: block;
        position: sticky;
        bottom: 0;
        z-index: 1000;
        background: #fff;
        margin-top: 12px;
        border: 1px solid var(--border);
        border-radius: 14px;
        padding: 10px;
        max-height: 55vh;
        overflow-y: auto;
        -webkit-overflow-scrolling: touch;
        box-shadow: 0 8px 24px rgba(16, 36, 62, 0.08);
      }
      .chart-panel.interactive-mode {
        position: fixed;
        inset: 0;
        top: var(--fixed-app-nav-clearance);
        z-index: 2000;
        display: flex;
        flex-direction: column;
        width: 100%;
        height: calc(100vh - var(--fixed-app-nav-clearance));
        max-height: calc(100vh - var(--fixed-app-nav-clearance));
        margin: 0;
        overflow: hidden;
        padding: 8px;
        overflow-anchor: none;
        border: 0;
        border-radius: 0;
        box-shadow: none;
      }
      .chart-panel.interactive-mode::before {
        position: fixed;
        top: 0;
        right: 0;
        left: 0;
        height: var(--fixed-app-nav-clearance);
        background: #f5f8fb;
        box-shadow: 0 -2px 0 #f5f8fb;
        content: "";
        pointer-events: none;
      }
      .chart-panel img { width: 100%; height: auto; display: block; max-height: 50vh; object-fit: contain; }
      .chart-panel .panel-placeholder {
        color: var(--muted);
        font-size: 13px;
        text-align: center;
        padding: 18px 0;
      }
      .chart-frame {
        position: relative;
        width: 100%;
        min-height: 160px;
        touch-action: pan-y;
        user-select: none;
      }
      .chart-title-row {
        align-items: center;
        color: #334155;
        display: flex;
        font-size: 13px;
        font-weight: 700;
        gap: 8px;
        justify-content: space-between;
        margin-bottom: 6px;
        padding: 0 46px;
        text-align: center;
      }
      .chart-symbol-title {
        flex: 1;
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
      }
      .chart-counter {
        color: #64748b;
        font-size: 12px;
        font-weight: 600;
        white-space: nowrap;
      }
      .chart-nav-btn {
        align-items: center;
        background: rgba(15, 23, 42, 0.78);
        border: none;
        border-radius: 999px;
        color: #ffffff;
        cursor: pointer;
        display: flex;
        font-size: 28px;
        font-weight: 700;
        height: 44px;
        justify-content: center;
        line-height: 1;
        opacity: 0.92;
        position: absolute;
        top: 50%;
        transform: translateY(-50%);
        width: 44px;
        z-index: 3;
        -webkit-tap-highlight-color: transparent;
        touch-action: manipulation;
      }
      .chart-nav-btn:hover,
      .chart-nav-btn:focus { background: rgba(15, 23, 42, 0.95); outline: none; }
      .chart-nav-btn:disabled { cursor: not-allowed; opacity: 0.28; }
      .chart-nav-prev { left: 6px; }
      .chart-nav-next { right: 6px; }
      .chart-image-wrap { padding: 0 46px; }
      .chart-help-text { color: #64748b; font-size: 12px; margin-top: 5px; text-align: center; }
      .interactive-panel-header {
        position: relative;
        flex: 0 0 auto;
        z-index: 20;
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 10px;
        margin: -2px -2px 0;
        padding: 5px 5px 8px;
        border-bottom: 1px solid #e2eaf0;
        border-radius: 8px 8px 0 0;
        background: rgba(255, 255, 255, 0.97);
        box-shadow: 0 4px 10px rgba(16, 36, 62, 0.05);
        backdrop-filter: blur(8px);
      }
      .interactive-panel-title {
        display: flex;
        align-items: center;
        gap: 7px;
        min-width: 0;
        color: var(--ink);
        font-size: 13px;
        font-weight: 800;
      }
      .interactive-panel-title span:first-child {
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
      }
      .interactive-mode-badge {
        flex: 0 0 auto;
        padding: 2px 6px;
        border-radius: 999px;
        background: #fff0dc;
        color: #a95214;
        font-size: 9px;
        letter-spacing: 0.04em;
        text-transform: uppercase;
      }
      .interactive-chart-embed {
        display: block;
        flex: 1 1 auto;
        width: 100%;
        height: auto;
        min-height: 0;
        border: 1px solid #e0e8ee;
        border-radius: 10px;
        background: #f5f8fb;
        overflow-anchor: none;
      }
      .interactive-panel-help {
        flex: 0 0 auto;
        padding: 6px 4px 1px;
        color: #64748b;
        font-size: 10px;
        text-align: center;
      }

      /* ---- Mobile portrait: smaller fonts and bigger touch-friendly controls ---- */
      @media screen and (max-width: 600px) and (orientation: portrait) {
        :root {
          --component-nav-origin: __NAV_ORIGIN_PORTRAIT__;
          --fixed-app-nav-clearance: calc(
            var(--component-nav-origin) + 2.8rem + 0.76rem + 2px + 0.7rem
          );
        }
        body { padding: 0; }
        .results-table-shell { border-radius: 10px; }
        .results-table-toolbar { align-items: flex-start; flex-direction: column; gap: 4px; padding: 8px; }
        .results-table-toolbar__meta { font-size: 9px; text-align: left; }
        .hover-results-table { font-size: 10px; }
        .hover-results-table th, .hover-results-table td { padding: 4px 2px; }
        .hover-results-table th { font-size: 8px; letter-spacing: 0.015em; }
        .hover-results-table th.sortable::after { margin-left: 2px; }
        .hover-results-table th:first-child,
        .hover-results-table td:first-child { width: 43%; }
        .stock-symbol-cell { gap: 2px; }
        .stock-hover, .stock-symbol-label { gap: 2px; padding: 3px 5px; max-width: calc(100% - 48px); }
        .interactive-chart-link, .screener-company-link { height: 20px; width: 20px; flex-basis: 20px; }
        .alert-row-actions { gap: 3px; }
        .alert-row-action { min-width: 22px; height: 22px; padding: 0 5px; font-size: 10px; }
        .chart-panel { max-height: 42vh; padding: 6px; }
        .chart-panel img { max-height: 34vh; }
        .chart-title-row { font-size: 12px; padding: 0 38px; }
        .chart-counter { font-size: 11px; }
        .chart-nav-btn { height: 38px; width: 38px; font-size: 24px; }
        .chart-nav-prev { left: 2px; }
        .chart-nav-next { right: 2px; }
        .chart-image-wrap { padding: 0 34px; }
        .chart-help-text { font-size: 11px; }
        .chart-panel.interactive-mode {
          position: fixed;
          inset: var(--fixed-app-nav-clearance) 0 0;
          z-index: 9999;
          height: calc(100dvh - var(--fixed-app-nav-clearance));
          max-height: calc(100dvh - var(--fixed-app-nav-clearance));
          overflow: hidden;
          padding: 0;
          border: 0;
          border-radius: 0;
          background: #fff;
        }
        .interactive-panel-header { display: none; }
        .interactive-chart-embed {
          height: auto;
          min-height: 0;
          border-width: 0;
          border-radius: 0;
        }
        .interactive-panel-help { font-size: 9px; }
      }
      /* Mobile landscape */
      @media screen and (orientation: landscape) and (max-height: 600px) {
        :root {
          --component-nav-origin: __NAV_ORIGIN_LANDSCAPE__;
          --fixed-app-nav-clearance: calc(
            var(--component-nav-origin) + 2.35rem + 0.56rem + 2px + 0.55rem
          );
        }
        .hover-results-table { font-size: 12px; }
        .hover-results-table th, .hover-results-table td { padding: 5px 6px; }
        .chart-panel.interactive-mode {
          position: fixed; inset: var(--fixed-app-nav-clearance) 0 0; z-index: 9999;
          height: calc(100dvh - var(--fixed-app-nav-clearance));
          max-height: calc(100dvh - var(--fixed-app-nav-clearance));
          overflow: hidden; padding: 0; border: 0;
          border-radius: 0; background: #fff;
        }
        .interactive-panel-header { padding: 2px 4px; }
        .interactive-chart-embed { min-height: 0; border: 0; border-radius: 0; }
        .interactive-panel-help { display: none; }
      }
    </style>
    <script>
      (function() {
        var activeRow = null;
        var activeInteractiveButton = null;
        var activeIndex = -1;
        var activeInteractiveRange = '252';
        var touchStartX = 0;
        var touchStartY = 0;

        function getChartItems() {
          return Array.from(document.querySelectorAll('.stock-hover'));
        }

        function getInteractiveItems() {
          return Array.from(document.querySelectorAll('.interactive-chart-link'));
        }

        function escapeHtml(value) {
          return String(value || '')
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;');
        }

        function setComponentFrameHeight(height) {
          var requestedHeight = Math.max(220, Math.round(Number(height) || 0));
          try {
            if (window.frameElement) {
              window.frameElement.style.height = requestedHeight + 'px';
            }
          } catch (error) {}
          try {
            window.parent.postMessage({
              isStreamlitMessage: true,
              type: 'streamlit:setFrameHeight',
              height: requestedHeight
            }, '*');
          } catch (error) {}
        }

        function setActiveRow(el) {
          if (activeRow && activeRow !== el) {
            activeRow.classList.remove('stock-hover-active');
          }
          if (el) {
            el.classList.add('stock-hover-active');
          }
          activeRow = el;
          activeIndex = el ? getChartItems().indexOf(el) : -1;
        }

        function setActiveInteractiveButton(el) {
          if (activeInteractiveButton && activeInteractiveButton !== el) {
            activeInteractiveButton.classList.remove('active');
          }
          if (el) {
            el.classList.add('active');
          }
          activeInteractiveButton = el;
        }

        function bindSwipeNavigation(frame) {
          if (!frame) return;

          frame.addEventListener('touchstart', function(e) {
            if (!e.changedTouches || !e.changedTouches.length) return;
            touchStartX = e.changedTouches[0].clientX;
            touchStartY = e.changedTouches[0].clientY;
          }, { passive: true });

          frame.addEventListener('touchend', function(e) {
            if (!e.changedTouches || !e.changedTouches.length) return;
            var touchEndX = e.changedTouches[0].clientX;
            var touchEndY = e.changedTouches[0].clientY;
            var deltaX = touchEndX - touchStartX;
            var deltaY = touchEndY - touchStartY;
            var minSwipeDistance = 45;

            if (Math.abs(deltaX) < minSwipeDistance || Math.abs(deltaX) < Math.abs(deltaY) * 1.2) {
              return;
            }

            e.preventDefault();
            e.stopPropagation();
            showChartByOffset(deltaX < 0 ? 1 : -1);
          }, { passive: false });
        }

        function revealInteractiveHeader(panel, behavior) {
          if (!panel) return;
          try {
            window.parent.postMessage({
              source: 'alert-table-chart',
              action: 'reveal',
              behavior: behavior || 'auto'
            }, '*');
          } catch (error) {}
          try {
            if (window.frameElement) {
              window.frameElement.scrollIntoView({
                behavior: behavior || 'auto',
                block: 'start',
                inline: 'nearest'
              });
            }
          } catch (error) {}
          var header = panel.querySelector('.interactive-panel-header');
          if (!header) return;
          header.scrollIntoView({
            behavior: behavior || 'auto',
            block: 'start',
            inline: 'nearest'
          });
        }

        function renderPanel(el) {
          var src = el.getAttribute('data-chart-src');
          var symbol = el.getAttribute('data-symbol') || el.textContent.trim() || 'Chart';
          var panel = document.getElementById('chart-panel');
          var items = getChartItems();
          var index = items.indexOf(el);
          if (!panel || !src || index < 0) return;

          var prevDisabled = index <= 0 ? 'disabled' : '';
          var nextDisabled = index >= items.length - 1 ? 'disabled' : '';
          var escapedSymbol = escapeHtml(symbol);

          panel.classList.remove('interactive-mode');
          setActiveInteractiveButton(null);
          panel.innerHTML = '' +
            '<div class="chart-frame">' +
              '<div class="chart-title-row">' +
                '<span class="chart-symbol-title">' + escapedSymbol + '</span>' +
                '<span class="chart-counter">' + (index + 1) + ' / ' + items.length + '</span>' +
              '</div>' +
              '<button type="button" class="chart-nav-btn chart-nav-prev" data-chart-nav="prev" aria-label="Previous chart" ' + prevDisabled + '>&lsaquo;</button>' +
              '<button type="button" class="chart-nav-btn chart-nav-next" data-chart-nav="next" aria-label="Next chart" ' + nextDisabled + '>&rsaquo;</button>' +
              '<div class="chart-image-wrap"><img src="' + src + '" alt="' + escapedSymbol + ' chart"></div>' +
              '<div class="chart-help-text">Swipe chart or use arrows to move through results. Tap another symbol anytime to jump.</div>' +
            '</div>';

          panel.querySelectorAll('[data-chart-nav]').forEach(function(btn) {
            btn.addEventListener('click', function(e) {
              e.preventDefault();
              e.stopPropagation();
              showChartByOffset(btn.getAttribute('data-chart-nav') === 'next' ? 1 : -1);
            });
          });

          bindSwipeNavigation(panel.querySelector('.chart-frame'));
          panel.scrollIntoView({behavior: 'smooth', block: 'nearest'});
        }

        function renderInteractivePanel(button) {
          var src = button.getAttribute('data-interactive-src');
          var symbol = button.getAttribute('data-symbol') || 'Chart';
          var panel = document.getElementById('chart-panel');
          if (!panel || !src) return;

          var items = getInteractiveItems();
          var index = items.indexOf(button);
          if (index < 0) return;
          var compactLandscape = window.matchMedia(
            '(orientation: landscape) and (max-height: 600px)'
          ).matches;
          var viewportHeight = window.visualViewport
            ? window.visualViewport.height
            : window.innerHeight;
          try {
            if (window.top && window.top.innerHeight) {
              viewportHeight = Math.min(viewportHeight, window.top.innerHeight);
            }
          } catch (error) {
            // Sandboxed components may not read the parent viewport.
          }
          panel.classList.add('interactive-mode');
          var navClearance = parseFloat(
            window.getComputedStyle(panel).top
          ) || 0;
          var componentFrameHeight = viewportHeight;
          var availableEmbedHeight = Math.max(
            compactLandscape ? 240 : 420,
            Math.floor(
              componentFrameHeight
              - navClearance
              - (compactLandscape ? 2 : 70)
            )
          );
          setComponentFrameHeight(componentFrameHeight);
          var embeddedSrc = src + (src.indexOf('?') >= 0 ? '&' : '?') +
            'embedded=1' +
            '&embed_height=' + encodeURIComponent(availableEmbedHeight) +
            '&compact_landscape=' + (compactLandscape ? '1' : '0') +
            '&position=' + encodeURIComponent(index + 1) +
            '&total=' + encodeURIComponent(items.length) +
            '&has_previous=' + (index > 0 ? '1' : '0') +
            '&has_next=' + (index < items.length - 1 ? '1' : '0') +
            '&range=' + encodeURIComponent(activeInteractiveRange);
          var escapedSymbol = escapeHtml(symbol);
          panel.innerHTML = '' +
            '<div class="interactive-panel-header">' +
              '<div class="interactive-panel-title">' +
                '<span>' + escapedSymbol + '</span>' +
                '<span class="interactive-mode-badge">Interactive</span>' +
              '</div>' +
            '</div>' +
            '<iframe class="interactive-chart-embed" src="' + escapeHtml(embeddedSrc) + '" ' +
              'title="' + escapedSymbol + ' interactive chart" loading="eager" ' +
              'allow="fullscreen; screen-orientation" allowfullscreen></iframe>' +
            '<div class="interactive-panel-help">Pinch or scroll to zoom · drag to pan · use the chart controls for 6M, 1Y, 3Y or all data.</div>';

          var embeddedFrame = panel.querySelector('.interactive-chart-embed');
          var hostSymbolMessage = {
            source: 'nse-chart-host',
            action: 'symbols',
            symbols: items.map(function(item) {
              return item.getAttribute('data-symbol') || '';
            }),
            currentIndex: index
          };
          function deliverSymbolsToChart(attempt) {
            if (!embeddedFrame) return;
            try {
              embeddedFrame.contentWindow.postMessage(hostSymbolMessage, '*');
              var nestedChartFrame = embeddedFrame.contentDocument
                ? embeddedFrame.contentDocument.querySelector('iframe')
                : null;
              if (nestedChartFrame && nestedChartFrame.contentWindow) {
                nestedChartFrame.contentWindow.postMessage(hostSymbolMessage, '*');
                return;
              }
            } catch (error) {}
            if (attempt < 20) {
              window.setTimeout(function() {
                deliverSymbolsToChart(attempt + 1);
              }, 100);
            }
          }
          requestAnimationFrame(function() {
            revealInteractiveHeader(panel, 'smooth');
          });
          if (embeddedFrame) {
            embeddedFrame.addEventListener('load', function() {
              deliverSymbolsToChart(0);
              requestAnimationFrame(function() {
                revealInteractiveHeader(panel, 'auto');
              });
            }, { once: true });
          }
        }

        // ---- Panel-based chart display (all screen sizes) ----
        function showChart(el, forceOpen) {
          if (activeRow === el && !forceOpen) {
            el.classList.remove('stock-hover-active');
            clearPanel();
            setActiveRow(null);
            return;
          }
          setActiveInteractiveButton(null);
          setActiveRow(el);
          renderPanel(el);
        }

        function showInteractiveChart(button) {
          if (activeInteractiveButton === button) {
            setActiveInteractiveButton(null);
            clearPanel();
            return;
          }
          setActiveRow(null);
          setActiveInteractiveButton(button);
          renderInteractivePanel(button);
        }

        function showInteractiveByOffset(offset) {
          var items = getInteractiveItems();
          if (!items.length || !activeInteractiveButton) return;
          var currentIndex = items.indexOf(activeInteractiveButton);
          var nextIndex = Math.max(0, Math.min(items.length - 1, currentIndex + offset));
          if (nextIndex === currentIndex) return;
          setActiveInteractiveButton(items[nextIndex]);
          renderInteractivePanel(items[nextIndex]);
        }

        function showChartByOffset(offset) {
          var items = getChartItems();
          if (!items.length) return;
          var currentIndex = activeIndex >= 0 ? activeIndex : 0;
          var nextIndex = Math.max(0, Math.min(items.length - 1, currentIndex + offset));
          if (nextIndex === currentIndex && activeRow) return;
          showChart(items[nextIndex], true);
        }

        function clearPanel() {
          var panel = document.getElementById('chart-panel');
          if (panel) {
            panel.classList.remove('interactive-mode');
            panel.innerHTML = '<div class="panel-placeholder">📈 Select a stock for the fast chart or use its candle icon for the interactive chart</div>';
            setComponentFrameHeight(
              Number(panel.getAttribute('data-default-height')) || 700
            );
          }
        }

        function triggerStreamlitAction(buttonKey) {
          if (!buttonKey) return false;
          try {
            window.parent.postMessage({
              source: 'alert-table-action',
              actionKey: String(buttonKey)
            }, '*');
            return true;
          } catch (error) {
            return false;
          }
        }

        function bindEvents() {
          document.querySelectorAll('.stock-hover').forEach(function(el) {
            // Click loads chart into fixed panel
            el.addEventListener('click', function(e) {
              e.stopPropagation();
              showChart(el, false);
            });
          });

          document.querySelectorAll('.interactive-chart-link').forEach(function(button) {
            button.addEventListener('click', function(e) {
              e.preventDefault();
              e.stopPropagation();
              showInteractiveChart(button);
            });
          });

          document.querySelectorAll('[data-streamlit-action-key]').forEach(function(button) {
            button.addEventListener('click', function(e) {
              e.preventDefault();
              e.stopPropagation();
              var confirmation = button.getAttribute('data-confirm-message');
              if (confirmation && !window.confirm(confirmation)) return;
              button.disabled = true;
              if (!triggerStreamlitAction(
                button.getAttribute('data-streamlit-action-key')
              )) {
                button.disabled = false;
              }
            });
          });

          window.addEventListener('message', function(event) {
            var message = event && event.data;
            if (!message || message.source !== 'nse-interactive-chart') return;
            if (message.action === 'previous') {
              showInteractiveByOffset(-1);
            } else if (message.action === 'next') {
              showInteractiveByOffset(1);
            } else if (message.action === 'symbol-select') {
              var requestedSymbol = String(message.symbol || '').trim().toUpperCase();
              var matchingButton = getInteractiveItems().find(function(item) {
                return String(item.getAttribute('data-symbol') || '').trim().toUpperCase() === requestedSymbol;
              });
              if (matchingButton) {
                setActiveInteractiveButton(matchingButton);
                renderInteractivePanel(matchingButton);
              }
            } else if (message.action === 'range-change') {
              var requestedRange = String(message.range || '').toLowerCase();
              if (['126', '252', '756', 'all'].indexOf(requestedRange) >= 0) {
                activeInteractiveRange = requestedRange;
              }
            } else if (message.action === 'close') {
              setActiveInteractiveButton(null);
              clearPanel();
            }
          });

          var panel = document.getElementById('chart-panel');
          if (panel) {
            panel.addEventListener('click', function(e) {
              e.stopPropagation();
            });
          }

          document.addEventListener('keydown', function(e) {
            if (!activeRow && !activeInteractiveButton) return;
            if (activeInteractiveButton && e.key === 'ArrowLeft') {
              e.preventDefault();
              showInteractiveByOffset(-1);
            } else if (activeInteractiveButton && e.key === 'ArrowRight') {
              e.preventDefault();
              showInteractiveByOffset(1);
            } else if (activeRow && e.key === 'ArrowLeft') {
              e.preventDefault();
              showChartByOffset(-1);
            } else if (activeRow && e.key === 'ArrowRight') {
              e.preventDefault();
              showChartByOffset(1);
            } else if (e.key === 'Escape') {
              setActiveInteractiveButton(null);
              setActiveRow(null);
              clearPanel();
            }
          });

          // Click anywhere else deselects
          document.addEventListener('click', function() {
            if (activeRow || activeInteractiveButton) {
              setActiveInteractiveButton(null);
              setActiveRow(null);
              clearPanel();
            }
          });
        }

        if (document.readyState === 'loading') {
          document.addEventListener('DOMContentLoaded', bindEvents);
        } else {
          bindEvents();
        }
      })();
    </script>
    """
    styles = (
        styles
        .replace("__NAV_ORIGIN_DESKTOP__", nav_origins[0])
        .replace("__NAV_ORIGIN_PORTRAIT__", nav_origins[1])
        .replace("__NAV_ORIGIN_LANDSCAPE__", nav_origins[2])
    )

    def display_column_label(column):
        label = str(column)
        diff_match = re.fullmatch(r"DiffSMA(\d+)", label)
        if diff_match:
            return f"Price vs SMA {diff_match.group(1)}"
        roc_match = re.fullmatch(r"RocSMA(\d+)", label)
        if roc_match:
            return f"SMA {roc_match.group(1)} ROC"
        return label

    header_cells = "".join(
        (
            f'<th class="sortable" onclick="toggleSymbolSort({index})">'
            f"{html.escape(display_column_label(column))}</th>"
            if column == "Symbol"
            else f"<th>{html.escape(display_column_label(column))}</th>"
            if column == "Actions"
            else
            f"<th class=\"sortable\" onclick=\"sortNumericColumn({index})\">"
            f"{html.escape(display_column_label(column))}</th>"
        )
        for index, column in enumerate(visible_df.columns)
    )
    rows = []
    chart_sources = df.get("ChartSource")
    valuation_medians_series = df.get("ValuationMedians")
    interactive_periods = normalize_interactive_ma_periods(interactive_ma_periods)
    for original_index, (row_index, row) in enumerate(visible_df.iterrows()):
        source_row = df.loc[row_index]
        cells = []
        chart_path = chart_paths.loc[row_index] if chart_paths is not None else None
        chart_source = chart_sources.loc[row_index] if chart_sources is not None else None
        valuation_medians = (
            valuation_medians_series.loc[row_index]
            if valuation_medians_series is not None
            else None
        )
        row_valuation_state = historical_pe_valuation_state(
            row.get("PE Ratio"),
            valuation_medians,
        )
        row_valuation_class = (
            f" valuation-{row_valuation_state}" if row_valuation_state else ""
        )
        chart_html = ""
        data_uri = ""
        if chart_path and _row_chart_matches_symbol(row.get("Symbol"), chart_path, chart_source):
            try:
                data_uri = image_to_data_uri(chart_path)
                chart_html = (
                    f'<span class="chart-tooltip">'
                    f'<img src="{data_uri}" alt="{html.escape(str(row.get("Symbol", "Chart")))} chart">'
                    f'</span>'
                )
            except OSError:
                data_uri = ""

        for column in visible_df.columns:
            value = "" if pd.isna(row[column]) else str(row[column])
            escaped_value = html.escape(value)
            if column == "Actions" and row_actions:
                acknowledge_button_key = source_row.get(
                    "Acknowledge Button Key"
                )
                remove_button_key = source_row.get("Remove Button Key")
                action_items = []
                if (
                    acknowledge_button_key
                    and not pd.isna(acknowledge_button_key)
                ):
                    action_items.append(
                        '<button class="alert-row-action '
                        'alert-row-action--acknowledge" type="button" '
                        f'data-streamlit-action-key="'
                        f'{html.escape(str(acknowledge_button_key), quote=True)}" '
                        'title="Acknowledge alert" '
                        'aria-label="Acknowledge alert">✓</button>'
                    )
                if remove_button_key and not pd.isna(remove_button_key):
                    escaped_symbol_for_prompt = html.escape(
                        str(row.get("Symbol", "this stock")),
                        quote=True,
                    )
                    action_items.append(
                        '<button class="alert-row-action '
                        'alert-row-action--remove" type="button" '
                        f'data-streamlit-action-key="'
                        f'{html.escape(str(remove_button_key), quote=True)}" '
                        'title="Remove alert" '
                        'aria-label="Remove alert" '
                        f'data-confirm-message="Remove the saved price alert for '
                        f'{escaped_symbol_for_prompt}? This action cannot be undone.">'
                        "−</button>"
                    )
                escaped_value = (
                    '<span class="alert-row-actions">'
                    f"{''.join(action_items)}</span>"
                )
            elif column == "Symbol":
                valuation_state = row_valuation_state
                valuation_class = row_valuation_class
                valuation_title = ""
                if valuation_state == "favorable":
                    valuation_title = (
                        "Current PE is below at least two of the 3Y, 5Y, and 10Y medians"
                    )
                elif valuation_state == "unfavorable":
                    valuation_title = (
                        "Current PE is not below at least two of the 3Y, 5Y, and 10Y medians"
                        if has_positive_current_pe(row.get("PE Ratio"))
                        else (
                            "Current PE is unavailable or non-positive; "
                            "historical median PE data is available"
                        )
                    )
                title_attribute = (
                    f' title="{html.escape(valuation_title, quote=True)}"'
                    if valuation_title
                    else ""
                )
                symbol_html = (
                    f'<span class="stock-symbol-label{valuation_class}"'
                    f"{title_attribute}>{escaped_value}</span>"
                )
                if chart_html and data_uri:
                    symbol_html = (
                        f'<span class="stock-hover{valuation_class}" '
                        f'data-symbol="{html.escape(value, quote=True)}" '
                        f'data-chart-src="{html.escape(data_uri, quote=True)}"'
                        f"{title_attribute}>"
                        f'{escaped_value}{chart_html}'
                        f'</span>'
                    )

                interactive_link = ""
                source_symbol = chart_source if chart_source and not pd.isna(chart_source) else value
                row_interactive_market = source_row.get(
                    "Interactive Market",
                    interactive_market,
                )
                if row_interactive_market and source_symbol:
                    pe_ratio = row.get("PE Ratio")
                    alert_date = source_row.get("Alert Date")
                    alert_price = source_row.get("Alert Price")
                    row_overlay = {}
                    if alert_date is not None and not pd.isna(alert_date):
                        row_overlay["alertDate"] = alert_date
                    if alert_price is not None and not pd.isna(alert_price):
                        row_overlay["alertPrice"] = alert_price
                    interactive_href = interactive_chart_query(
                        source_symbol,
                        row_interactive_market,
                        ma_periods=interactive_periods,
                        pe_ratio=(
                            pe_ratio
                            if pe_ratio is not None and not pd.isna(pe_ratio)
                            else None
                        ),
                        trade_overlay=row_overlay,
                    )
                    interactive_attributes = (
                        f'data-interactive-src="{html.escape(interactive_href, quote=True)}" '
                        f'data-symbol="{html.escape(value, quote=True)}" '
                        f'title="Show {html.escape(value, quote=True)} interactive chart" '
                        f'aria-label="Show {html.escape(value, quote=True)} interactive chart"'
                    )
                    if interactive_symbol_click:
                        symbol_html = (
                            f'<button class="interactive-chart-link interactive-symbol-button" '
                            f'type="button" {interactive_attributes}>'
                            f"{symbol_html}</button>"
                        )
                    else:
                        interactive_link = (
                            f'<button class="interactive-chart-link" type="button" '
                            f"{interactive_attributes}>"
                            '<svg viewBox="0 0 16 16" aria-hidden="true">'
                            '<path d="M3 2v4M3 9v5M1.5 6h3v3h-3zM8 1v3M8 8v5M6.5 4h3v4h-3zM13 3v5M13 11v3M11.5 8h3v3h-3z" '
                            'fill="none" stroke="currentColor" stroke-width="1.35" stroke-linecap="round"/>'
                            '</svg></button>'
                        )
                screener_company_link = ""
                if (
                    str(row_interactive_market or "").strip().upper() == "INDIA"
                    and value
                ):
                    screener_href = (
                        "https://www.screener.in/company/"
                        f"{quote(str(value).upper(), safe='')}/"
                    )
                    screener_company_link = (
                        f'<a class="screener-company-link" '
                        f'href="{html.escape(screener_href, quote=True)}" '
                        f'target="_blank" rel="noopener noreferrer" '
                        f'title="Open {html.escape(value, quote=True)} on Screener.in" '
                        f'aria-label="Open {html.escape(value, quote=True)} on Screener.in">'
                        '<span aria-hidden="true">S</span></a>'
                    )
                escaped_value = (
                    f'<span class="stock-symbol-cell">'
                    f"{symbol_html}{interactive_link}{screener_company_link}</span>"
                )
            sort_value = html.escape(value, quote=True)
            cells.append(f'<td data-sort-value="{sort_value}">{escaped_value}</td>')
        rows.append(
            f'<tr class="{row_valuation_class.strip()}" '
            f'data-original-index="{original_index}">{"".join(cells)}</tr>'
        )

    script = r"""
    <script>
      // Per-column sort directions (keyed by columnIndex)
      const numericSortDirections = {};

      function tableRows(table) {
        return Array.from(table.tBodies[0].rows);
      }

      function clearSortIndicators(table) {
        table.querySelectorAll("th.sortable").forEach(header => {
          header.removeAttribute("data-sort-dir");
        });
      }

      function restoreOriginalOrder(table) {
        const tbody = table.tBodies[0];
        tableRows(table)
          .sort((a, b) => Number(a.dataset.originalIndex) - Number(b.dataset.originalIndex))
          .forEach(row => tbody.appendChild(row));
        clearSortIndicators(table);
      }

      function cellSortValue(row, columnIndex) {
        const cell = row.cells[columnIndex];
        return cell.dataset.sortValue || cell.innerText || "";
      }

      function parseNumeric(value) {
        // Remove commas, percentage signs, and whitespace; treat empty as +Infinity (sorts to bottom)
        const cleaned = value.replace(/[,%\s]/g, "").trim();
        if (cleaned === "" || cleaned === "-" || cleaned === "N/A") {
          return Number.POSITIVE_INFINITY;
        }
        const parsed = parseFloat(cleaned);
        return Number.isNaN(parsed) ? Number.POSITIVE_INFINITY : parsed;
      }

      function toggleSymbolSort(columnIndex) {
        const table = document.querySelector(".hover-results-table");
        if (!table || !table.tBodies || !table.tBodies.length) return;
        const header = table.tHead.rows[0].cells[columnIndex];

        // Second tap disables alphabetic sorting and restores the original
        // market-cap-ranked result order.
        if (header && header.getAttribute("data-sort-dir") === "asc") {
          restoreOriginalOrder(table);
          return;
        }

        const tbody = table.tBodies[0];
        tableRows(table)
          .sort((a, b) => cellSortValue(a, columnIndex).localeCompare(
            cellSortValue(b, columnIndex),
            undefined,
            { sensitivity: "base", numeric: true }
          ))
          .forEach(row => tbody.appendChild(row));
        clearSortIndicators(table);
        if (header) header.setAttribute("data-sort-dir", "asc");
      }

      function sortNumericColumn(columnIndex) {
        const table = document.querySelector(".hover-results-table");
        if (!table || !table.tBodies || !table.tBodies.length) return;
        const tbody = table.tBodies[0];
        const rows = tableRows(table);

        // Toggle direction for this specific column (default desc on first click)
        const prev = numericSortDirections[columnIndex] || "desc";
        const dir = prev === "asc" ? "desc" : "asc";
        numericSortDirections[columnIndex] = dir;

        rows.sort((a, b) => {
          const av = parseNumeric(cellSortValue(a, columnIndex));
          const bv = parseNumeric(cellSortValue(b, columnIndex));
          return dir === "asc" ? av - bv : bv - av;
        });
        rows.forEach(row => tbody.appendChild(row));

        clearSortIndicators(table);
        const activeHeader = table.tHead.rows[0].cells[columnIndex];
        if (activeHeader) activeHeader.setAttribute("data-sort-dir", dir);
      }
    </script>
    """

    result_count = len(visible_df)
    count_noun = (
        str(count_label)
        if count_label
        else (
            f"alert{'s' if result_count != 1 else ''}"
            if interactive_symbol_click
            else f"match{'es' if result_count != 1 else ''}"
        )
    )
    interaction_help = (
        "Select a stock name for its interactive chart"
        if interactive_symbol_click
        else (
            "Select a symbol for the fast chart · "
            "Use the candle icon for interactive view"
        )
    )
    table_html = (
        f"<div class='results-table-shell'>"
        f"<div class='results-table-toolbar'>"
        f"<div class='results-table-toolbar__title'>{html.escape(str(table_title))}"
        f"<span class='results-count'>{result_count} {count_noun}</span></div>"
        f"<div class='results-table-toolbar__meta'>Tap Symbol for A–Z; tap again for market-cap order · "
        f"{interaction_help}</div>"
        f"</div>"
        f"<div class='results-table-wrapper'>"
        f"<table class='hover-results-table'><thead><tr>{header_cells}</tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
        f"</div></div>"
        f"<div class='chart-panel' id='chart-panel' "
        f"data-default-height='{int(component_height)}'>"
        f"<div class='panel-placeholder'>📈 {interaction_help}</div></div>"
    )
    return f"{styles}{script}{table_html}"


def sortable_results_table(
    df,
    height=700,
    interactive_market=None,
    interactive_ma_periods=None,
    interactive_symbol_click=False,
    table_title="Screening Results",
    row_actions=False,
    count_label=None,
    component_key=None,
):
    table_html = results_hover_table_html(
        df,
        interactive_market=interactive_market,
        interactive_ma_periods=interactive_ma_periods,
        interactive_symbol_click=interactive_symbol_click,
        table_title=table_title,
        row_actions=row_actions,
        count_label=count_label,
        component_height=height,
    )
    if row_actions:
        return _ALERT_TABLE_COMPONENT(
            table_html=table_html,
            default_height=height,
            key=component_key,
            default=None,
        )
    components.html(
        table_html,
        height=height,
        scrolling=True,
    )
    return None
