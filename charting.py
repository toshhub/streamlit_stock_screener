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

    plt.figure(figsize=(9, 4.8))
    plt.plot(chart_df["Date"], chart_df["Close"], label="Close", color="#111827", linewidth=1.8)

    for index, period in enumerate(ma_periods):
        plt.plot(
            chart_df["Date"],
            chart_df[f"SMA{period}"],
            label=f"SMA{period}",
            color=MA_COLORS[index % len(MA_COLORS)],
            linewidth=1.4,
        )

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
      .hover-results-table { border-collapse: collapse; width: 100%; font-size: 13px; }
      .hover-results-table th, .hover-results-table td {
        border-bottom: 1px solid #e5e7eb;
        padding: 7px 9px;
        text-align: left;
        vertical-align: top;
      }
      .hover-results-table th { background: #f8fafc; font-weight: 600; cursor: pointer; user-select: none; }
      .stock-hover { position: relative; color: #2563eb; font-weight: 600; cursor: default; }
      .stock-hover .chart-tooltip {
        display: none;
        position: fixed;
        z-index: 9999;
        left: 270px;
        top: 120px;
        width: 720px;
        max-width: calc(100vw - 320px);
        padding: 10px;
        background: white;
        border: 1px solid #cbd5e1;
        box-shadow: 0 14px 34px rgba(15, 23, 42, 0.22);
      }
      .stock-hover:hover .chart-tooltip { display: block; }
      .chart-tooltip img { width: 100%; height: auto; display: block; }
    </style>
    """

    header_cells = "".join(
        f"<th onclick=\"sortHoverTable({index})\">{html.escape(str(column))}</th>"
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
      const sortDirections = {};
      function sortHoverTable(columnIndex) {
        const table = document.querySelector(".hover-results-table");
        const tbody = table.tBodies[0];
        const rows = Array.from(tbody.rows);
        const direction = sortDirections[columnIndex] === "asc" ? "desc" : "asc";
        sortDirections[columnIndex] = direction;
        rows.sort((a, b) => {
          const av = a.cells[columnIndex].innerText.trim();
          const bv = b.cells[columnIndex].innerText.trim();
          const an = parseFloat(av.replace(/,/g, ""));
          const bn = parseFloat(bv.replace(/,/g, ""));
          let result;
          if (!Number.isNaN(an) && !Number.isNaN(bn)) {
            result = an - bn;
          } else {
            result = av.localeCompare(bv);
          }
          return direction === "asc" ? result : -result;
        });
        rows.forEach(row => tbody.appendChild(row));
      }
    </script>
    """

    return f"{styles}{script}<table class='hover-results-table'><thead><tr>{header_cells}</tr></thead><tbody>{''.join(rows)}</tbody></table>"


def sortable_results_table(df, height=700):
    components.html(results_hover_table_html(df), height=height, scrolling=True)
