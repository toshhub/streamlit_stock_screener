import base64
import html
import json

import matplotlib.pyplot as plt
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


def create_stock_chart(json_path, filter_set, output_dir=CHARTS_DIR, max_points=780, swing_annotations=None):
    df = load_price_data(json_path)
    ma_periods = required_ma_periods(filter_set)
    if df.empty:
        return None

    for period in ma_periods:
        df[f"SMA{period}"] = df["Close"].rolling(period).mean()

    chart_df = df.tail(max_points)
    output_dir.mkdir(parents=True, exist_ok=True)
    out_file = output_dir / f"{json_path.stem}.png"

    plt.figure(figsize=(10, 5.5))

    last_date = chart_df["Date"].iloc[-1]
    x_lim_right = chart_df["Date"].iloc[-1] + pd.Timedelta(days=(chart_df["Date"].iloc[-1] - chart_df["Date"].iloc[0]).days * 0.12)

    plt.plot(chart_df["Date"], chart_df["Close"], label="Close", color="#111827", linewidth=1.8)

    for index, period in enumerate(ma_periods):
        plt.plot(
            chart_df["Date"],
            chart_df[f"SMA{period}"],
            label=f"SMA{period}",
            color=MA_COLORS[index % len(MA_COLORS)],
            linewidth=1.4,
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
        plt.annotate(
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

    plt.xlim(chart_df["Date"].iloc[0], x_lim_right)

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
                plt.scatter([swing["date"]], [swing["price"]], color=color, marker=marker, s=58, zorder=5)
                plt.annotate(
                    label,
                    (swing["date"], swing["price"]),
                    textcoords="offset points",
                    xytext=(0, 9 if swing_type == "H" else -15),
                    ha="center",
                    color=color,
                    fontsize=9,
                    fontweight="bold",
                )

    plt.title(json_path.stem)
    plt.xlabel("Date")
    plt.ylabel("Price")
    plt.grid(True, alpha=0.25)
    plt.legend(loc="best")
    plt.tight_layout()
    plt.savefig(out_file, dpi=120)
    plt.close()

    return str(out_file)


def image_to_data_uri(path):
    with open(path, "rb") as image_file:
        encoded = base64.b64encode(image_file.read()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def results_hover_table_html(df):
    visible_df = df.drop(columns=["ChartPath"], errors="ignore")
    chart_paths = df.get("ChartPath")

    styles = """
    <style>
      .results-table-wrapper { overflow-x: auto; -webkit-overflow-scrolling: touch; }
      .hover-results-table { border-collapse: collapse; width: 100%; font-size: 13px; min-width: 400px; }
      .hover-results-table th, .hover-results-table td {
        border-bottom: 1px solid #e5e7eb;
        padding: 7px 9px;
        text-align: left;
        vertical-align: top;
      }
      .hover-results-table th { background: #f8fafc; font-weight: 600; user-select: none; }
      .hover-results-table th.sortable { cursor: pointer; color: #2563eb; }
      .stock-hover { color: #2563eb; font-weight: 600; cursor: pointer; }
      .stock-hover .chart-tooltip { display: none; }
      .chart-tooltip img { width: 100%; height: auto; display: block; object-fit: contain; }
      .stock-hover-active { background-color: #e0e7ff !important; border-radius: 4px; }

      /* ---- Fixed chart panel below table (all screen sizes) ---- */
      .chart-panel {
        display: block;
        position: sticky;
        bottom: 0;
        z-index: 1000;
        background: #fff;
        border-top: 2px solid #cbd5e1;
        padding: 8px;
        max-height: 55vh;
        overflow-y: auto;
        -webkit-overflow-scrolling: touch;
        box-shadow: 0 -4px 16px rgba(15, 23, 42, 0.15);
      }
      .chart-panel img { width: 100%; height: auto; display: block; max-height: 50vh; object-fit: contain; }
      .chart-panel .panel-placeholder {
        color: #9ca3af;
        font-size: 13px;
        text-align: center;
        padding: 16px 0;
      }
      .chart-frame { position: relative; width: 100%; min-height: 160px; }
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
              '<div class="chart-help-text">Use arrows to move through charts. Tap another symbol anytime to jump.</div>' +
            '</div>';

          panel.querySelectorAll('[data-chart-nav]').forEach(function(btn) {
            btn.addEventListener('click', function(e) {
              e.preventDefault();
              e.stopPropagation();
              showChartByOffset(btn.getAttribute('data-chart-nav') === 'next' ? 1 : -1);
            });
          });

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

    header_cells = "".join(
        (
            f"<th class=\"sortable\" onclick=\"sortNumericColumn({index})\">{html.escape(str(column))}</th>"
            if column not in _sort_exempt
            else f"<th>{html.escape(str(column))}</th>"
        )
        for index, column in enumerate(visible_df.columns)
    )
    rows = []
    for row_index, row in visible_df.iterrows():
        cells = []
        chart_path = chart_paths.loc[row_index] if chart_paths is not None else None
        chart_html = ""
        if chart_path:
            chart_html = (
                f'<span class="chart-tooltip">'
                f'<img src="{image_to_data_uri(chart_path)}" alt="{html.escape(str(row.get("Symbol", "Chart")))} chart">'
                f'</span>'
            )

        for column in visible_df.columns:
            value = "" if pd.isna(row[column]) else str(row[column])
            escaped_value = html.escape(value)
            if column == "Symbol" and chart_html:
                data_uri = image_to_data_uri(chart_path)
                escaped_value = (
                    f'<span class="stock-hover" '
                    f'data-symbol="{html.escape(value, quote=True)}" '
                    f'data-chart-src="{html.escape(data_uri, quote=True)}">'
                    f'{escaped_value}{chart_html}'
                    f'</span>'
                )
            cells.append(f"<td>{escaped_value}</td>")
        rows.append(f"<tr>{''.join(cells)}</tr>")

    script = """
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
      }
    </script>
    """

    return f"{styles}{script}<div class='results-table-wrapper'><table class='hover-results-table'><thead><tr>{header_cells}</tr></thead><tbody>{''.join(rows)}</tbody></table></div><div class='chart-panel' id='chart-panel'><div class='panel-placeholder'>📈 Tap a stock symbol to view its chart</div></div>"


def sortable_results_table(df, height=700):
    components.html(results_hover_table_html(df), height=height, scrolling=True)
