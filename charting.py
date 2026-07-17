import base64
import hashlib
import html
import json
import re
from pathlib import Path

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


def results_hover_table_html(df):
    visible_df = df.drop(columns=["ChartPath", "ChartSource"], errors="ignore")
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
      .stock-hover {
        display: inline-flex;
        align-items: center;
        gap: 6px;
        padding: 4px 9px;
        border: 1px solid #c7e2e7;
        border-radius: 999px;
        background: var(--brand-soft);
        color: var(--brand-dark);
        font-weight: 800;
        cursor: pointer;
        transition: transform 0.14s ease, box-shadow 0.14s ease;
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
      .stock-hover .chart-tooltip { display: none; }
      .chart-tooltip img { width: 100%; height: auto; display: block; object-fit: contain; }
      .stock-hover-active {
        border-color: var(--brand) !important;
        background: #d9f1f3 !important;
        box-shadow: 0 0 0 3px rgba(23, 107, 135, 0.10);
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
        var activeIndex = -1;
        var touchStartX = 0;
        var touchStartY = 0;

        function getChartItems() {
          return Array.from(document.querySelectorAll('.stock-hover'));
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

        // ---- Panel-based chart display (all screen sizes) ----
        function showChart(el, forceOpen) {
          if (activeRow === el && !forceOpen) {
            el.classList.remove('stock-hover-active');
            clearPanel();
            setActiveRow(null);
            return;
          }
          setActiveRow(el);
          renderPanel(el);
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
            panel.innerHTML = '<div class="panel-placeholder">📈 Tap a stock symbol to view its chart</div>';
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

          var panel = document.getElementById('chart-panel');
          if (panel) {
            panel.addEventListener('click', function(e) {
              e.stopPropagation();
            });
          }

          document.addEventListener('keydown', function(e) {
            if (!activeRow) return;
            if (e.key === 'ArrowLeft') {
              e.preventDefault();
              showChartByOffset(-1);
            } else if (e.key === 'ArrowRight') {
              e.preventDefault();
              showChartByOffset(1);
            } else if (e.key === 'Escape') {
              activeRow.classList.remove('stock-hover-active');
              clearPanel();
              setActiveRow(null);
            }
          });

          // Click anywhere else deselects
          document.addEventListener('click', function() {
            if (activeRow) {
              activeRow.classList.remove('stock-hover-active');
              clearPanel();
              setActiveRow(null);
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
    for row_index, row in visible_df.iterrows():
        cells = []
        chart_path = chart_paths.loc[row_index] if chart_paths is not None else None
        chart_source = chart_sources.loc[row_index] if chart_sources is not None else None
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
            if column == "Symbol" and chart_html and data_uri:
                escaped_value = (
                    f'<span class="stock-hover" '
                    f'data-symbol="{html.escape(value, quote=True)}" '
                    f'data-chart-src="{html.escape(data_uri, quote=True)}">'
                    f'{escaped_value}{chart_html}'
                    f'</span>'
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
        f"<div class='results-table-toolbar__meta'>Click a metric to sort · Select a symbol to view its chart</div>"
        f"</div>"
        f"<div class='results-table-wrapper'>"
        f"<table class='hover-results-table'><thead><tr>{header_cells}</tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
        f"</div></div>"
        f"<div class='chart-panel' id='chart-panel'>"
        f"<div class='panel-placeholder'>📈 Select a stock symbol to view its chart</div></div>"
    )
    return f"{styles}{script}{table_html}"


def sortable_results_table(df, height=700):
    components.html(results_hover_table_html(df), height=height, scrolling=True)
