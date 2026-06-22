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


def create_stock_chart(json_path, filter_set, output_dir=CHARTS_DIR, max_points=260, swing_annotations=None):
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
      .stock-hover .chart-tooltip {
        display: none;
        position: fixed;
        z-index: 9999;
        width: min(720px, calc(100vw - 24px));
        max-width: calc(100vw - 24px);
        padding: 10px;
        background: white;
        border: 1px solid #cbd5e1;
        box-shadow: 0 14px 34px rgba(15, 23, 42, 0.22);
        border-radius: 8px;
        pointer-events: none;
      }
      /* Show tooltip on hover (desktop) and on tap via .tooltip-visible class (mobile) */
      .stock-hover:hover .chart-tooltip,
      .stock-hover.tooltip-visible .chart-tooltip { display: block; }
      .chart-tooltip img { width: 100%; height: auto; display: block; }
      /* Mobile: keep table cells from wrapping excessively */
      @media screen and (max-width: 600px) {
        .hover-results-table { font-size: 12px; }
        .hover-results-table th, .hover-results-table td { padding: 5px 6px; }
      }
    </style>
    <script>
      (function() {
        var activeTooltip = null;

        function positionTooltip(el, tip) {
          var rect = el.getBoundingClientRect();
          var tipW = tip.offsetWidth || Math.min(720, window.innerWidth - 24);
          var tipH = tip.offsetHeight || 400;
          var cushion = 8;
          // Try right side first, fallback to left, then center
          var left = rect.right + cushion;
          if (left + tipW > window.innerWidth - cushion) {
            left = rect.left - tipW - cushion;
          }
          if (left < cushion) {
            left = Math.max(cushion, (window.innerWidth - tipW) / 2);
          }
          var top = rect.top;
          if (top + tipH > window.innerHeight - cushion) {
            top = Math.max(cushion, window.innerHeight - tipH - cushion);
          }
          if (top < cushion) top = cushion;
          tip.style.left = left + 'px';
          tip.style.top = top + 'px';
        }

        function showTooltip(el) {
          if (activeTooltip && activeTooltip !== el) {
            activeTooltip.classList.remove('tooltip-visible');
          }
          el.classList.add('tooltip-visible');
          var tip = el.querySelector('.chart-tooltip');
          if (tip) {
            tip.style.display = 'block';
            positionTooltip(el, tip);
          }
          activeTooltip = el;
        }

        function hideTooltip(el) {
          el.classList.remove('tooltip-visible');
          var tip = el.querySelector('.chart-tooltip');
          if (tip) tip.style.display = '';
        }

        function bindEvents() {
          document.querySelectorAll('.stock-hover').forEach(function(el) {
            // Desktop hover
            el.addEventListener('mouseenter', function(e) {
              var tip = el.querySelector('.chart-tooltip');
              if (!tip) return;
              tip.style.display = 'block';
              positionTooltip(el, tip);
            });
            el.addEventListener('mouseleave', function(e) {
              if (!el.classList.contains('tooltip-visible')) {
                var tip = el.querySelector('.chart-tooltip');
                if (tip) tip.style.display = '';
              }
            });
            // Mobile tap
            el.addEventListener('click', function(e) {
              e.stopPropagation();
              if (el.classList.contains('tooltip-visible')) {
                hideTooltip(el);
              } else {
                showTooltip(el);
              }
            });
          });
          // Tap anywhere else to close
          document.addEventListener('click', function() {
            if (activeTooltip) {
              hideTooltip(activeTooltip);
              activeTooltip = null;
            }
          });
          // Reposition on scroll/resize
          window.addEventListener('scroll', function() {
            if (activeTooltip) {
              var tip = activeTooltip.querySelector('.chart-tooltip');
              if (tip && tip.style.display === 'block') {
                positionTooltip(activeTooltip, tip);
              }
            }
          }, {passive: true});
          window.addEventListener('resize', function() {
            if (activeTooltip) {
              var tip = activeTooltip.querySelector('.chart-tooltip');
              if (tip && tip.style.display === 'block') {
                positionTooltip(activeTooltip, tip);
              }
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

    header_cells = "".join(
        (
            f"<th class=\"sortable\" onclick=\"sortPeRatioColumn({index})\">{html.escape(str(column))}</th>"
            if column == "PE Ratio"
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
                escaped_value = f'<span class="stock-hover">{escaped_value}{chart_html}</span>'
            cells.append(f"<td>{escaped_value}</td>")
        rows.append(f"<tr>{''.join(cells)}</tr>")

    script = """
    <script>
      let peSortDirection = "desc";
      function parsePeRatio(value) {
        const parsed = parseFloat(value.replace(/,/g, ""));
        return Number.isNaN(parsed) ? Number.POSITIVE_INFINITY : parsed;
      }
      function sortPeRatioColumn(columnIndex) {
        const table = document.querySelector(".hover-results-table");
        const tbody = table.tBodies[0];
        const rows = Array.from(tbody.rows);
        peSortDirection = peSortDirection === "asc" ? "desc" : "asc";
        rows.sort((a, b) => {
          const av = parsePeRatio(a.cells[columnIndex].innerText.trim());
          const bv = parsePeRatio(b.cells[columnIndex].innerText.trim());
          return peSortDirection === "asc" ? av - bv : bv - av;
        });
        rows.forEach(row => tbody.appendChild(row));
      }
    </script>
    """

    return f"{styles}{script}<div class='results-table-wrapper'><table class='hover-results-table'><thead><tr>{header_cells}</tr></thead><tbody>{''.join(rows)}</tbody></table></div>"


def sortable_results_table(df, height=700):
    components.html(results_hover_table_html(df), height=height, scrolling=True)
