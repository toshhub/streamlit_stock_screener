import base64
import hashlib
import html
import json
import math
import re
from pathlib import Path
from urllib.parse import quote, urlencode

from matplotlib.figure import Figure
import pandas as pd
import streamlit.components.v1 as components

from config import CHARTS_DIR
from screener import required_ma_periods


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


def load_price_data(path):
    df = pd.DataFrame(json.loads(path.read_text()))
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


def _chart_context_fingerprint(json_path, chart_df, filter_set, max_points, max_years, pe_ratio, swing_annotations, date_markers):
    try:
        stat = json_path.stat()
        file_signature = {"mtime_ns": stat.st_mtime_ns, "size": stat.st_size}
    except OSError:
        file_signature = {}

    payload = {
        "style_version": 4,
        "source": str(json_path),
        "file": file_signature,
        "data_hash": _chart_data_hash(chart_df),
        "filter_set": filter_set,
        "max_points": max_points,
        "max_years": max_years,
        "pe_ratio": pe_ratio,
        "swing_annotations": swing_annotations or [],
        "date_markers": date_markers or [],
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
):
    json_path = Path(json_path)
    df = load_price_data(json_path)
    ma_periods = required_ma_periods(filter_set)
    if df.empty:
        return None

    for period in ma_periods:
        df[f"SMA{period}"] = df["Close"].rolling(period).mean()

    last_available_date = df["Date"].dropna().iloc[-1]
    if max_years:
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
            "Start": {"color": "#16a34a", "offset": 18, "va": "bottom"},
            "End": {"color": "#dc2626", "offset": -20, "va": "top"},
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
            marker_price = row["Close"]
            style = marker_styles.get(label, {"color": "#7c3aed", "offset": 18, "va": "bottom"})
            ax.scatter(
                [row["Date"]],
                [marker_price],
                color=style["color"],
                marker="^" if label == "Start" else "v",
                s=86,
                edgecolors="white",
                linewidths=0.9,
                zorder=6,
            )
            ax.annotate(
                label,
                (row["Date"], marker_price),
                textcoords="offset points",
                xytext=(0, style["offset"]),
                ha="center",
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
    return sorted(normalized)[:7] or list(INTERACTIVE_CHART_DEFAULT_MAS)


def interactive_chart_payload(json_path, ma_periods=None, max_points=None):
    json_path = Path(json_path)
    df = pd.DataFrame(json.loads(json_path.read_text(encoding="utf-8")))
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

    ma_periods = normalize_interactive_ma_periods(ma_periods)
    for period in ma_periods:
        df[f"SMA{period}"] = df["Close"].rolling(period).mean()

    if max_points is None:
        chart_df = df.copy()
    else:
        chart_df = df.tail(max(100, int(max_points))).copy()
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

    return {
        "candles": candles,
        "movingAverages": moving_averages,
        "volume": volume,
        "maPeriods": ma_periods,
        "pointCount": len(candles),
        "firstDate": candles[0]["time"],
        "lastDate": candles[-1]["time"],
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
):
    payload = interactive_chart_payload(json_path, ma_periods=ma_periods)
    payload_json = json.dumps(payload, separators=(",", ":")).replace("</", "<\\/")
    safe_symbol = html.escape(str(symbol))
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
    growth_cards_html = ""
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
        growth_cards_html = (
            '<section class="growth-snapshot" aria-label="Growth and valuation metrics">'
            '<div class="growth-snapshot__heading">'
            '<div><span class="growth-snapshot__eyebrow">Fundamentals</span>'
            '<h2>Growth &amp; valuation snapshot</h2></div>'
            '<span class="growth-snapshot__source">Source: Screener.in</span>'
            "</div>"
            f'<div class="growth-grid">{"".join(cards)}</div>'
            "</section>"
        )
    match_navigation_html = ""
    if match_position and match_total:
        previous_disabled = "" if has_previous else "disabled"
        next_disabled = "" if has_next else "disabled"
        match_navigation_html = (
            '<section class="chart-control-section chart-navigation-section">'
            '<span class="chart-section-label">Browse matches</span>'
            '<div class="chart-match-navigation" aria-label="Matched stock navigation">'
            f'<button type="button" class="chart-match-nav" id="matched-prev" '
            f'aria-label="Previous matched stock" title="Previous matched stock" '
            f'{previous_disabled}>&lsaquo;</button>'
            f'<span class="chart-match-counter">{int(match_position)} / {int(match_total)}</span>'
            f'<button type="button" class="chart-match-nav" id="matched-next" '
            f'aria-label="Next matched stock" title="Next matched stock" '
            f'{next_disabled}>&rsaquo;</button>'
            '<span class="chart-control-divider" aria-hidden="true"></span>'
            '<button type="button" class="chart-close" id="chart-close" '
            'aria-label="Close interactive chart" title="Close interactive chart">&times;</button>'
            "</div></section>"
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
          display: grid;
          grid-template-rows: auto auto minmax(300px, 1fr) auto auto;
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
          min-width: 0;
          padding: 9px 12px;
          border: 1px solid #cfe0e8;
          border-radius: 10px;
          background: linear-gradient(135deg, #ffffff 0%, #edf8f9 100%);
          box-shadow: 0 2px 8px rgba(16, 36, 62, 0.05);
        }}
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
        .chart-toolbar {{
          display: flex;
          align-items: stretch;
          gap: 8px;
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
          gap: 10px;
          min-height: 36px;
          padding: 7px 14px;
          overflow-x: auto;
          border: 1px solid var(--border);
          border-bottom: 0;
          background: #f9fbfc;
          color: #334a63;
          font-size: 11px;
          font-variant-numeric: tabular-nums;
          white-space: nowrap;
        }}
        .legend-date {{ color: var(--muted); font-weight: 750; }}
        .legend-ohlc b {{ margin-left: 4px; color: var(--ink); }}
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
        .growth-snapshot {{
          padding: 10px 0 0;
          background: var(--surface-soft);
        }}
        .growth-snapshot__heading {{
          display: flex;
          align-items: flex-end;
          justify-content: space-between;
          gap: 10px;
          margin-bottom: 7px;
          padding: 0 2px;
        }}
        .growth-snapshot__eyebrow {{
          color: #15803d;
          font-size: 8px;
          font-weight: 850;
          letter-spacing: 0.09em;
          text-transform: uppercase;
        }}
        .growth-snapshot h2 {{
          margin: 1px 0 0;
          color: #17334c;
          font-size: 13px;
          letter-spacing: -0.01em;
        }}
        .growth-snapshot__source {{
          color: #718397;
          font-size: 8px;
          white-space: nowrap;
        }}
        .growth-grid {{
          display: grid;
          grid-template-columns: repeat(4, minmax(0, 1fr));
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
            grid-template-rows: auto auto minmax(280px, 1fr) auto;
            padding: 0;
          }}
          .chart-header {{
            grid-template-columns: 1fr;
            gap: 6px;
            padding: 6px;
          }}
          .chart-title {{ padding: 8px 9px; }}
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
            min-height: 34px;
            padding: 6px 8px;
            font-size: 10px;
          }}
          #chart {{ min-height: 280px; }}
          .chart-footer {{
            align-items: flex-start;
            flex-direction: column;
            gap: 2px;
            padding: 6px 8px;
            font-size: 9px;
          }}
          .growth-grid {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
          .growth-snapshot {{ padding-top: 7px; }}
          .growth-card {{ padding: 8px 9px; }}
        }}
      </style>
    </head>
    <body>
      <main class="chart-shell">
        <header class="chart-header">
          <div class="chart-title{valuation_state_class}">
            <span class="chart-section-label">Selected stock</span>
            <div class="chart-title__row">
              <strong>{safe_symbol}</strong>
              {pe_badge_html}
              {valuation_state_html}
            </div>
            <span class="chart-subtitle">Interactive candlestick chart · {payload["pointCount"]:,} candles</span>
          </div>
          {match_navigation_html}
          <div class="chart-toolbar" aria-label="Chart controls">
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
        <div class="chart-legend" id="chart-legend">Move or tap the crosshair to inspect OHLC and MA values.</div>
        <section id="chart" aria-label="{safe_symbol} interactive stock chart">
          <div class="chart-loading" id="chart-loading">Loading interactive chart…</div>
        </section>
        {growth_cards_html}
        <footer class="chart-footer">
          <span>Scroll or pinch to zoom · drag to pan · tap and hold on mobile to inspect values</span>
          <a href="https://www.tradingview.com/lightweight-charts/" target="_blank" rel="noopener">Charts by TradingView</a>
        </footer>
      </main>
      <script src="https://unpkg.com/lightweight-charts@5.0.9/dist/lightweight-charts.standalone.production.js"></script>
      <script>
        (function() {{
          const payload = {payload_json};
          const container = document.getElementById("chart");
          const loading = document.getElementById("chart-loading");
          const legend = document.getElementById("chart-legend");

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
          const matchedPrevious = document.getElementById("matched-prev");
          const matchedNext = document.getElementById("matched-next");
          const chartClose = document.getElementById("chart-close");
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

          const colors = ["#2563eb", "#9333ea", "#ea580c", "#0891b2", "#be123c", "#4f46e5", "#15803d"];
          const chart = LightweightCharts.createChart(container, {{
            width: Math.max(240, container.clientWidth),
            height: Math.max(280, container.clientHeight),
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
              minBarSpacing: 1.2,
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

          const maSeries = [];
          payload.maPeriods.forEach(function(period, index) {{
            const label = "SMA" + period;
            const color = colors[index % colors.length];
            const series = chart.addSeries(LightweightCharts.LineSeries, {{
              color: color,
              lineWidth: 2,
              priceLineVisible: false,
              lastValueVisible: true,
              crosshairMarkerVisible: false,
            }});
            series.setData(payload.movingAverages[label] || []);
            maSeries.push({{ label: label, color: color, series: series }});
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

          function renderLegend(time, candle, param) {{
            if (!candle) return;
            let content = '<span class="legend-date">' + String(time || "") + '</span>' +
              '<span class="legend-ohlc">O <b>' + formatPrice(candle.open) + '</b></span>' +
              '<span class="legend-ohlc">H <b>' + formatPrice(candle.high) + '</b></span>' +
              '<span class="legend-ohlc">L <b>' + formatPrice(candle.low) + '</b></span>' +
              '<span class="legend-ohlc">C <b>' + formatPrice(candle.close) + '</b></span>';
            maSeries.forEach(function(item) {{
              const point = param ? param.seriesData.get(item.series) : null;
              const value = point && point.value;
              content += '<span class="legend-ma" style="color:' + item.color + '">' +
                item.label + ' ' + formatPrice(value) + '</span>';
            }});
            legend.innerHTML = content;
          }}

          chart.subscribeCrosshairMove(function(param) {{
            if (!param || !param.time || !param.seriesData) return;
            renderLegend(param.time, param.seriesData.get(candleSeries), param);
          }});

          function showBars(count) {{
            document.querySelectorAll("[data-range]").forEach(function(button) {{
              button.classList.toggle("active", String(button.dataset.range) === String(count));
            }});
            if (count === "all") {{
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

          const resizeObserver = new ResizeObserver(function(entries) {{
            const rect = entries[0].contentRect;
            chart.applyOptions({{
              width: Math.max(240, Math.floor(rect.width)),
              height: Math.max(280, Math.floor(rect.height)),
            }});
          }});
          resizeObserver.observe(container);
          loading.remove();
          showBars({json.dumps(selected_range)});
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
    height=760,
):
    components.html(
        interactive_stock_chart_html(
            symbol,
            json_path,
            ma_periods=ma_periods,
            pe_ratio=pe_ratio,
            match_position=match_position,
            match_total=match_total,
            has_previous=has_previous,
            has_next=has_next,
            initial_range=initial_range,
            growth_metrics=growth_metrics,
            valuation_medians=valuation_medians,
        ),
        height=height,
        scrolling=False,
    )


def results_hover_table_html(df, interactive_market=None, interactive_ma_periods=None):
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
        ],
        errors="ignore",
    )
    chart_paths = df.get("ChartPath")

    styles = """
    <style>
      :root {
        --ink: #10243e;
        --muted: #64748b;
        --brand: #176b87;
        --brand-dark: #10536a;
        --brand-soft: #e9f6f8;
        --border: #dce6ee;
      }
      * { box-sizing: border-box; }
      body {
        margin: 0;
        padding: 2px;
        background: transparent;
        color: #334a63;
        font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      }
      .results-table-shell {
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
        overflow: auto;
        -webkit-overflow-scrolling: touch;
      }
      .hover-results-table {
        width: 100%;
        min-width: 560px;
        border-collapse: separate;
        border-spacing: 0;
        font-size: 13px;
      }
      .hover-results-table th, .hover-results-table td {
        border-bottom: 1px solid #e5e7eb;
        padding: 11px 13px;
        text-align: left;
        vertical-align: middle;
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
        white-space: nowrap;
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
      .hover-results-table tbody tr:last-child td { border-bottom: none; }
      .hover-results-table td:first-child { font-weight: 750; }
      .stock-symbol-cell {
        display: inline-flex;
        align-items: center;
        gap: 4px;
        white-space: nowrap;
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
        position: relative;
        max-height: none;
        overflow: visible;
        padding: 8px;
        scroll-margin-top: 8px;
        overflow-anchor: none;
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
        position: sticky;
        top: 0;
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
        width: 100%;
        height: 1100px;
        border: 1px solid #e0e8ee;
        border-radius: 10px;
        background: #f5f8fb;
        overflow-anchor: none;
      }
      .interactive-panel-help {
        padding: 6px 4px 1px;
        color: #64748b;
        font-size: 10px;
        text-align: center;
      }

      /* ---- Mobile portrait: smaller fonts and bigger touch-friendly controls ---- */
      @media screen and (max-width: 600px) and (orientation: portrait) {
        .hover-results-table { font-size: 11px; }
        .hover-results-table th, .hover-results-table td { padding: 4px 5px; }
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
          max-height: none;
          overflow: visible;
          padding: 0;
        }
        .interactive-panel-header { padding: 3px 4px 6px; }
        .interactive-chart-embed {
          height: 1100px;
          border-width: 0;
          border-radius: 0;
        }
        .interactive-panel-help { font-size: 9px; }
      }
      /* Mobile landscape */
      @media screen and (max-width: 600px) and (orientation: landscape) {
        .hover-results-table { font-size: 12px; }
        .hover-results-table th, .hover-results-table td { padding: 5px 6px; }
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
          var embeddedSrc = src + (src.indexOf('?') >= 0 ? '&' : '?') +
            'embedded=1' +
            '&position=' + encodeURIComponent(index + 1) +
            '&total=' + encodeURIComponent(items.length) +
            '&has_previous=' + (index > 0 ? '1' : '0') +
            '&has_next=' + (index < items.length - 1 ? '1' : '0') +
            '&range=' + encodeURIComponent(activeInteractiveRange);
          var escapedSymbol = escapeHtml(symbol);
          panel.classList.add('interactive-mode');
          panel.innerHTML = '' +
            '<div class="interactive-panel-header">' +
              '<div class="interactive-panel-title">' +
                '<span>' + escapedSymbol + '</span>' +
                '<span class="interactive-mode-badge">Interactive</span>' +
              '</div>' +
            '</div>' +
            '<iframe class="interactive-chart-embed" src="' + escapeHtml(embeddedSrc) + '" ' +
              'title="' + escapedSymbol + ' interactive chart" loading="eager"></iframe>' +
            '<div class="interactive-panel-help">Pinch or scroll to zoom · drag to pan · use the chart controls for 6M, 1Y, 3Y or all data.</div>';

          var embeddedFrame = panel.querySelector('.interactive-chart-embed');
          requestAnimationFrame(function() {
            revealInteractiveHeader(panel, 'smooth');
          });
          if (embeddedFrame) {
            embeddedFrame.addEventListener('load', function() {
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

          window.addEventListener('message', function(event) {
            var message = event && event.data;
            if (!message || message.source !== 'nse-interactive-chart') return;
            if (message.action === 'previous') {
              showInteractiveByOffset(-1);
            } else if (message.action === 'next') {
              showInteractiveByOffset(1);
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

    # Columns that support click-to-sort: all except Symbol (text column)
    _sort_exempt = {"Symbol"}

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
            f"<th class=\"sortable\" onclick=\"sortNumericColumn({index})\">"
            f"{html.escape(display_column_label(column))}</th>"
            if column not in _sort_exempt
            else f"<th>{html.escape(display_column_label(column))}</th>"
        )
        for index, column in enumerate(visible_df.columns)
    )
    rows = []
    chart_sources = df.get("ChartSource")
    valuation_medians_series = df.get("ValuationMedians")
    interactive_periods = normalize_interactive_ma_periods(interactive_ma_periods)
    interactive_ma_query = ",".join(str(period) for period in interactive_periods)
    for row_index, row in visible_df.iterrows():
        cells = []
        chart_path = chart_paths.loc[row_index] if chart_paths is not None else None
        chart_source = chart_sources.loc[row_index] if chart_sources is not None else None
        valuation_medians = (
            valuation_medians_series.loc[row_index]
            if valuation_medians_series is not None
            else None
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
            if column == "Symbol":
                valuation_state = historical_pe_valuation_state(
                    row.get("PE Ratio"),
                    valuation_medians,
                )
                valuation_class = (
                    f" valuation-{valuation_state}" if valuation_state else ""
                )
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
                if interactive_market and source_symbol:
                    interactive_params = {
                        "interactive_chart": str(source_symbol),
                        "market": str(interactive_market),
                        "ma": interactive_ma_query,
                    }
                    pe_ratio = row.get("PE Ratio")
                    if pe_ratio is not None and not pd.isna(pe_ratio):
                        interactive_params["pe"] = str(pe_ratio)
                    interactive_href = "?" + urlencode(interactive_params)
                    interactive_link = (
                        f'<button class="interactive-chart-link" type="button" '
                        f'data-interactive-src="{html.escape(interactive_href, quote=True)}" '
                        f'data-symbol="{html.escape(value, quote=True)}" '
                        f'title="Show {html.escape(value, quote=True)} interactive chart" '
                        f'aria-label="Show {html.escape(value, quote=True)} interactive chart">'
                        '<svg viewBox="0 0 16 16" aria-hidden="true">'
                        '<path d="M3 2v4M3 9v5M1.5 6h3v3h-3zM8 1v3M8 8v5M6.5 4h3v4h-3zM13 3v5M13 11v3M11.5 8h3v3h-3z" '
                        'fill="none" stroke="currentColor" stroke-width="1.35" stroke-linecap="round"/>'
                        '</svg></button>'
                    )
                screener_company_link = ""
                if (
                    str(interactive_market or "").strip().upper() == "INDIA"
                    and value
                ):
                    screener_href = (
                        "https://www.screener.in/company/"
                        f"{quote(str(value).upper(), safe='')}/consolidated/"
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
            cells.append(f"<td>{escaped_value}</td>")
        rows.append(f"<tr>{''.join(cells)}</tr>")

    script = r"""
    <script>
      // Per-column sort directions (keyed by columnIndex)
      const numericSortDirections = {};

      function parseNumeric(value) {
        // Remove commas, percentage signs, and whitespace; treat empty as +Infinity (sorts to bottom)
        const cleaned = value.replace(/[,%\s]/g, "").trim();
        if (cleaned === "" || cleaned === "-" || cleaned === "N/A") {
          return Number.POSITIVE_INFINITY;
        }
        const parsed = parseFloat(cleaned);
        return Number.isNaN(parsed) ? Number.POSITIVE_INFINITY : parsed;
      }

      function sortNumericColumn(columnIndex) {
        const table = document.querySelector(".hover-results-table");
        if (!table || !table.tBodies || !table.tBodies.length) return;
        const tbody = table.tBodies[0];
        const rows = Array.from(tbody.rows);

        // Toggle direction for this specific column (default desc on first click)
        const prev = numericSortDirections[columnIndex] || "desc";
        const dir = prev === "asc" ? "desc" : "asc";
        numericSortDirections[columnIndex] = dir;

        rows.sort((a, b) => {
          const av = parseNumeric(a.cells[columnIndex].innerText);
          const bv = parseNumeric(b.cells[columnIndex].innerText);
          return dir === "asc" ? av - bv : bv - av;
        });
        rows.forEach(row => tbody.appendChild(row));

        table.querySelectorAll("th.sortable").forEach(header => {
          header.removeAttribute("data-sort-dir");
        });
        const activeHeader = table.tHead.rows[0].cells[columnIndex];
        if (activeHeader) activeHeader.setAttribute("data-sort-dir", dir);
      }
    </script>
    """

    result_count = len(visible_df)
    table_html = (
        f"<div class='results-table-shell'>"
        f"<div class='results-table-toolbar'>"
        f"<div class='results-table-toolbar__title'>Screening Results"
        f"<span class='results-count'>{result_count} match{'es' if result_count != 1 else ''}</span></div>"
        f"<div class='results-table-toolbar__meta'>Sort metrics · Select a symbol for the fast chart · "
        f"Use the candle icon for interactive view</div>"
        f"</div>"
        f"<div class='results-table-wrapper'>"
        f"<table class='hover-results-table'><thead><tr>{header_cells}</tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
        f"</div></div>"
        f"<div class='chart-panel' id='chart-panel'>"
        f"<div class='panel-placeholder'>📈 Select a stock for the fast chart or use its candle icon for the interactive chart</div></div>"
    )
    return f"{styles}{script}{table_html}"


def sortable_results_table(
    df,
    height=700,
    interactive_market=None,
    interactive_ma_periods=None,
):
    components.html(
        results_hover_table_html(
            df,
            interactive_market=interactive_market,
            interactive_ma_periods=interactive_ma_periods,
        ),
        height=height,
        scrolling=True,
    )
