import json
import html
import hmac
import os
import queue
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
from datetime import datetime
from pathlib import Path

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

from backtest import get_backtest_calendar_dates, run_backtest, split_favorite_filter
from config import *
from charting import (
    create_stock_chart,
    image_to_data_uri,
    render_interactive_stock_chart,
    sortable_results_table,
)
from downloader import (
    MARKET_INDIA,
    MARKET_US,
    NIFTY_DATA_SYMBOL,
    clear_downloaded_json_files,
    download_nifty_index,
    download_top_stocks,
    load_top_symbols,
    market_label,
    normalize_market,
    timeframe_config,
)
from fundamentals import (
    enrich_result_with_growth_metrics,
    get_cached_company_growth_metrics,
    get_cached_company_valuation_medians,
    refresh_result_with_growth_metrics,
)
from pattern import evaluate_pattern_filters, validate_expression
from screener import (
    DEFAULT_FILTER_SET,
    FILTER_TYPE_DEFAULTS,
    FILTER_TYPE_LABELS,
    normalize_filter_set,
    required_ma_periods,
    screen_json_file,
)
from storage import (
    load_favourite_filter_sets,
    load_results,
    load_settings,
    save_favourite_filter_sets,
    save_results,
    update_settings,
)

st.set_page_config(layout="wide", page_title="NSE Stock Screener", page_icon="📈")

settings = load_settings()
favorite_filter_sets = load_favourite_filter_sets()
if not favorite_filter_sets and settings.get("favorite_filter_sets"):
    favorite_filter_sets = settings["favorite_filter_sets"]
    save_favourite_filter_sets(favorite_filter_sets)


def query_param_value(name, default=None):
    value = st.query_params.get(name, default)
    if isinstance(value, list):
        return value[0] if value else default
    return value


def scheduled_task_token():
    try:
        token = st.secrets.get("SCHEDULED_DOWNLOAD_TOKEN", "")
    except Exception:
        token = ""
    return str(token or os.environ.get("SCHEDULED_DOWNLOAD_TOKEN", "")).strip()


def valid_scheduled_task_token():
    expected = scheduled_task_token()
    provided = str(query_param_value("token", "") or "")
    return bool(expected) and hmac.compare_digest(provided, expected)


def symbols_file_for_market(market):
    market = normalize_market(market)
    if market == MARKET_US:
        return EXCEL_DIR / "nasdaq_screener_1784114565446.csv"
    return EXCEL_DIR / "MCAP_JUGAAD.xlsx"


def download_limit_for_market(market, symbols_file):
    market = normalize_market(market)
    if market == MARKET_US:
        if not symbols_file.exists():
            return int(settings.get("download_limit_us", 1000))
        default_limit = len(load_top_symbols(symbols_file, limit=1_000_000, market=market))
        return int(settings.get("download_limit_us", default_limit))
    return int(settings.get("download_limit", 1000))


def run_scheduled_download():
    if not valid_scheduled_task_token():
        st.error("Unauthorized scheduled task request.")
        st.stop()

    scheduled_mode = str(query_param_value("scheduled_download", "") or "").lower()
    ping_mode = str(query_param_value("ping", "") or "").lower()
    if scheduled_mode not in {"1", "true", "yes"} and ping_mode in {"1", "true", "yes"}:
        st.success("pong")
        st.stop()

    requested_market = str(query_param_value("market", settings.get("market", MARKET_INDIA)) or "").upper()
    markets = [MARKET_INDIA, MARKET_US] if requested_market == "ALL" else [normalize_market(requested_market)]
    timeframe = "DAY"
    incremental = str(query_param_value("full_refresh", "0") or "0").lower() not in {"1", "true", "yes"}

    st.header("Scheduled Stock Data Download")
    summary_rows = []
    total_rows_added = 0
    for market in markets:
        symbols_file = symbols_file_for_market(market)
        if not symbols_file.exists():
            summary_rows.append({
                "Market": market_label(market),
                "Status": "Missing symbols file",
                "Processed": 0,
                "Rows Added": 0,
                "File": str(symbols_file),
            })
            continue

        limit = download_limit_for_market(market, symbols_file)
        if not incremental:
            clear_downloaded_json_files(timeframe, market=market)

        download_rows = download_top_stocks(
            symbols_file,
            timeframe,
            limit=limit,
            incremental=incremental,
            market=market,
        )
        if market == MARKET_INDIA:
            download_nifty_index(timeframe, incremental=incremental, market=market)

        downloaded_count = sum(1 for row in download_rows if row["Downloaded"])
        rows_added = sum(int(row.get("Rows Added", 0) or 0) for row in download_rows)
        total_rows_added += rows_added
        summary_rows.append({
            "Market": market_label(market),
            "Status": "Completed",
            "Processed": f"{downloaded_count}/{len(download_rows)}",
            "Rows Added": rows_added,
            "File": symbols_file.name,
        })

    last_download_at = datetime.now().strftime("%d-%m-%Y %H:%M:%S")
    update_settings({
        "last_download_at": last_download_at,
        "last_download_tf": timeframe,
        "last_download_market": ",".join(markets),
        "last_scheduled_download_at": last_download_at,
    })
    st.success(f"Scheduled download complete at {last_download_at}. Rows added: {total_rows_added}")
    st.dataframe(pd.DataFrame(summary_rows), use_container_width=True)
    st.stop()


if str(query_param_value("scheduled_download", "") or "").lower() in {"1", "true", "yes"} or str(query_param_value("ping", "") or "").lower() in {"1", "true", "yes"}:
    run_scheduled_download()


def run_interactive_chart_view():
    symbol = str(query_param_value("interactive_chart", "") or "").strip()
    market = normalize_market(query_param_value("market", settings.get("last_results_market", MARKET_INDIA)))
    embedded = str(query_param_value("embedded", "") or "").lower() in {"1", "true", "yes"}
    if not symbol or Path(symbol).name != symbol:
        st.error("Invalid stock symbol.")
        st.stop()

    target_dir = timeframe_config("DAY", market)["target_dir"].resolve()
    stock_file = (target_dir / f"{symbol}.json").resolve()
    if stock_file.parent != target_dir or not stock_file.exists():
        st.error(f"Daily chart data is unavailable for {symbol}.")
        st.stop()

    requested_periods = [
        token.strip()
        for token in str(query_param_value("ma", "") or "").split(",")
        if token.strip()
    ]
    pe_ratio = query_param_value("pe", None)
    try:
        match_position = int(query_param_value("position", 0) or 0)
        match_total = int(query_param_value("total", 0) or 0)
    except (TypeError, ValueError):
        match_position = 0
        match_total = 0
    has_previous = str(query_param_value("has_previous", "") or "").lower() in {"1", "true", "yes"}
    has_next = str(query_param_value("has_next", "") or "").lower() in {"1", "true", "yes"}
    chart_range = str(query_param_value("range", "252") or "252").lower()
    growth_metrics = get_cached_company_growth_metrics(symbol, market)
    valuation_medians = get_cached_company_valuation_medians(symbol, market)
    embedded_layout_css = (
        """
        .stMainBlockContainer {
            max-width: none;
            padding: 0 !important;
        }
        """
        if embedded
        else
        """
        .stMainBlockContainer {
            max-width: 1600px;
            padding: 0.35rem 0.5rem 0.5rem;
        }
        """
    )
    st.markdown(
        f"""
        <style>
        {embedded_layout_css}
        header[data-testid="stHeader"] {{
            display: none;
        }}
        </style>
        """,
        unsafe_allow_html=True,
    )
    try:
        render_interactive_stock_chart(
            symbol,
            stock_file,
            ma_periods=requested_periods,
            pe_ratio=pe_ratio,
            match_position=match_position,
            match_total=match_total,
            has_previous=has_previous,
            has_next=has_next,
            initial_range=chart_range,
            growth_metrics=growth_metrics,
            valuation_medians=valuation_medians,
            height=1060 if embedded else 920,
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        st.error(f"Unable to prepare the interactive chart: {exc}")
    st.stop()


if query_param_value("interactive_chart", ""):
    run_interactive_chart_view()


def run_fundamentals_retry():
    requested_symbol = str(query_param_value("retry_fundamentals", "") or "").strip()
    if not requested_symbol:
        return

    result_market = normalize_market(
        settings.get("last_results_market", MARKET_INDIA)
    )
    rows = load_results()
    matching_row = next(
        (
            row
            for row in rows
            if str(row.get("Symbol", "")).strip().upper() == requested_symbol.upper()
        ),
        None,
    )

    if result_market != MARKET_INDIA:
        notice = ("warning", "Screener.in fundamentals are available only for Indian stocks.")
    elif matching_row is None:
        notice = ("warning", f"{requested_symbol} is not present in the saved screening results.")
    else:
        with st.spinner(f"Retrying Screener.in fundamentals for {requested_symbol}…"):
            refresh_succeeded = refresh_result_with_growth_metrics(
                matching_row,
                requested_symbol,
                result_market,
            )
        matching_row["FundamentalsRefreshToken"] = uuid.uuid4().hex
        save_results(rows)
        st.session_state["results"] = rows
        has_fundamentals = bool(
            matching_row.get("GrowthMetrics") or matching_row.get("ValuationMedians")
        )
        notice = (
            (
                "success",
                f"Refreshed Screener.in fundamentals for {requested_symbol}.",
            )
            if refresh_succeeded and has_fundamentals
            else (
                "warning",
                (
                    f"Screener.in data is still unavailable for {requested_symbol}. "
                    "You can retry later."
                    if refresh_succeeded
                    else (
                        f"The Screener.in request for {requested_symbol} did not complete. "
                        "Existing saved data was left unchanged."
                    )
                ),
            )
        )

    st.session_state["_fundamentals_retry_notice"] = notice
    st.session_state["switch_to_results_tab"] = True
    st.query_params.clear()
    st.rerun()


run_fundamentals_retry()

# ---- Inject custom CSS ----
st.markdown(
    """
    <style>
    :root {
        --ink-strong: #10243e;
        --ink: #334a63;
        --ink-muted: #6b7f93;
        --brand: #176b87;
        --brand-dark: #10536a;
        --brand-soft: #e9f6f8;
        --accent: #e89b35;
        --surface: #ffffff;
        --surface-soft: #f5f8fb;
        --border: #dce6ee;
        --shadow-sm: 0 1px 2px rgba(16, 36, 62, 0.05);
        --shadow-md: 0 10px 30px rgba(16, 36, 62, 0.09);
    }

    /* App shell */
    .stApp {
        background:
            radial-gradient(circle at 8% -10%, rgba(23, 107, 135, 0.10), transparent 28rem),
            radial-gradient(circle at 92% 0%, rgba(232, 155, 53, 0.08), transparent 24rem),
            #f5f8fb;
        color: var(--ink);
    }
    .stMainBlockContainer {
        max-width: 1480px;
        padding-top: 1.35rem;
        padding-bottom: 4rem;
    }
    header[data-testid="stHeader"] {
        background: rgba(245, 248, 251, 0.82);
        backdrop-filter: blur(14px);
    }
    h1, h2, h3 {
        color: var(--ink-strong);
        letter-spacing: -0.025em;
    }
    h2 {
        margin-top: 0.8rem;
    }
    p, label, .stCaption {
        color: var(--ink);
    }

    /* Product header */
    .app-hero {
        position: relative;
        overflow: hidden;
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 2rem;
        min-height: 126px;
        margin: 0 0 1.15rem;
        padding: 1.55rem 1.8rem;
        border: 1px solid rgba(255, 255, 255, 0.18);
        border-radius: 22px;
        background: linear-gradient(118deg, #10243e 0%, #145a73 58%, #1c7c8f 100%);
        box-shadow: 0 16px 38px rgba(16, 53, 76, 0.20);
    }
    .app-hero::after {
        content: "";
        position: absolute;
        width: 230px;
        height: 230px;
        right: -55px;
        top: -105px;
        border-radius: 50%;
        border: 42px solid rgba(255, 255, 255, 0.07);
    }
    .app-hero__content {
        position: relative;
        z-index: 1;
    }
    .app-hero__eyebrow {
        margin-bottom: 0.35rem;
        color: #8de0e4;
        font-size: 0.72rem;
        font-weight: 800;
        letter-spacing: 0.14em;
        text-transform: uppercase;
    }
    .app-hero__title {
        margin: 0;
        color: #ffffff !important;
        font-size: clamp(1.75rem, 3vw, 2.55rem);
        font-weight: 800;
        line-height: 1.1;
        letter-spacing: -0.035em;
    }
    .app-hero__subtitle {
        max-width: 650px;
        margin: 0.55rem 0 0;
        color: rgba(255, 255, 255, 0.78);
        font-size: 0.96rem;
    }
    .app-hero__mark {
        position: relative;
        z-index: 1;
        display: grid;
        place-items: center;
        width: 70px;
        height: 70px;
        flex: 0 0 70px;
        border: 1px solid rgba(255, 255, 255, 0.22);
        border-radius: 20px;
        background: rgba(255, 255, 255, 0.11);
        box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.20);
        color: #ffffff;
        font-size: 2rem;
    }

    /* Buttons */
    div.stButton > button,
    div.stDownloadButton > button {
        min-height: 2.65rem;
        border-radius: 10px;
        border: 1px solid #cbd9e4;
        background: #ffffff;
        color: var(--ink-strong);
        font-weight: 700;
        box-shadow: var(--shadow-sm);
        transition: transform 0.16s ease, box-shadow 0.16s ease, border-color 0.16s ease;
    }
    div.stButton > button:hover,
    div.stDownloadButton > button:hover {
        transform: translateY(-1px);
        border-color: #78a9b9;
        color: var(--brand-dark);
        box-shadow: 0 7px 18px rgba(16, 53, 76, 0.10);
    }
    div.stButton > button:focus-visible,
    div.stDownloadButton > button:focus-visible {
        outline: 3px solid rgba(23, 107, 135, 0.22);
        outline-offset: 2px;
    }
    div.stButton > button[kind="primary"] {
        background: linear-gradient(135deg, #176b87, #168297);
        border-color: #176b87;
        color: #ffffff;
        box-shadow: 0 7px 18px rgba(23, 107, 135, 0.22);
    }
    div.stButton > button[kind="primary"]:hover,
    div.stButton > button[kind="primary"]:focus {
        background: linear-gradient(135deg, #10536a, #176b87);
        border-color: #10536a;
        color: #ffffff;
        box-shadow: 0 10px 24px rgba(23, 107, 135, 0.28);
    }
    div.stButton > button[kind="primary"] p {
        color: #ffffff !important;
    }

    /* Secondary actions */
    button[kind="secondary"][data-testid="baseButton-secondary"] {
        background: #ffffff;
        border-color: #cbd9e4;
        color: var(--ink-strong);
    }

    /* Inputs */
    div[data-baseweb="select"] > div,
    div[data-baseweb="input"] > div,
    div[data-baseweb="base-input"],
    textarea,
    [data-testid="stFileUploaderDropzone"] {
        border-radius: 10px !important;
        border-color: #cbd9e4 !important;
        background-color: rgba(255, 255, 255, 0.92) !important;
        transition: border-color 0.16s ease, box-shadow 0.16s ease;
    }
    div[data-baseweb="select"] > div:focus-within,
    div[data-baseweb="input"] > div:focus-within,
    div[data-baseweb="base-input"]:focus-within,
    textarea:focus {
        border-color: var(--brand) !important;
        box-shadow: 0 0 0 3px rgba(23, 107, 135, 0.12) !important;
    }
    [data-testid="stWidgetLabel"] p {
        color: #304860;
        font-weight: 650;
    }

    /* Toggle and checkbox accent */
    label[data-baseweb="checkbox"]:has(input[aria-checked="true"]) > div:first-child {
        background-color: var(--brand) !important;
        border-color: var(--brand) !important;
    }

    /* Primary navigation */
    div.stTabs [data-baseweb="tab-list"] {
        gap: 0.4rem;
        padding: 0.4rem;
        border: 1px solid var(--border);
        border-radius: 14px;
        background: rgba(255, 255, 255, 0.78);
        box-shadow: var(--shadow-sm);
    }
    div.stTabs [data-baseweb="tab"] {
        min-height: 2.85rem;
        padding: 0.6rem 1.35rem;
        border-radius: 10px;
        border: none;
        background: transparent;
        color: var(--ink-muted);
        font-size: 0.93rem;
        font-weight: 700;
        transition: all 0.16s ease;
    }
    div.stTabs [data-baseweb="tab"]:hover {
        background: var(--brand-soft);
        color: var(--brand-dark);
    }
    div.stTabs [data-baseweb="tab"][aria-selected="true"] {
        background: linear-gradient(135deg, #176b87, #168297);
        color: #ffffff;
        box-shadow: 0 5px 14px rgba(23, 107, 135, 0.19);
    }
    div.stTabs [data-baseweb="tab"][aria-selected="true"] p {
        color: #ffffff !important;
    }
    div.stTabs [data-baseweb="tab-highlight"],
    div.stTabs [data-baseweb="tab-border"] {
        display: none;
    }
    div.stTabs [data-baseweb="tab-panel"] {
        padding-top: 1.1rem;
    }

    /* Filter row badges */
    .filter-badge {
        display: inline-block;
        padding: 0.3rem 0.75rem;
        border-radius: 999px;
        font-weight: 700;
        font-size: 0.8rem;
        color: #fff;
        margin: 0.15rem 0.25rem;
        box-shadow: var(--shadow-sm);
    }

    /* Data availability cards */
    .data-status-card {
        border: 1px solid;
        border-radius: 12px;
        padding: 0.85rem 1rem;
        margin: 0.5rem 0;
        font-weight: 650;
        box-shadow: var(--shadow-sm);
    }
    .data-status-available {
        border-color: #b9dfd1;
        background: linear-gradient(135deg, #effaf5, #e4f6ef);
        color: #176148;
    }
    .data-status-empty {
        border-color: #dce4ea;
        background: linear-gradient(135deg, #f7f9fb, #eef3f6);
        color: #65788a;
    }
    .data-panel-heading {
        display: flex;
        align-items: center;
        gap: 0.65rem;
        margin: 0 0 0.25rem;
        color: var(--ink-strong);
        font-size: 1rem;
        font-weight: 800;
        letter-spacing: -0.015em;
    }
    .data-panel-heading span {
        display: inline-grid;
        place-items: center;
        width: 2rem;
        height: 2rem;
        border-radius: 9px;
        background: var(--brand-soft);
        font-size: 1rem;
    }
    .data-panel-subtitle {
        min-height: 2.4rem;
        margin: 0 0 0.85rem;
        color: var(--ink-muted);
        font-size: 0.82rem;
        line-height: 1.45;
    }
    .source-file-summary {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 1rem;
        margin-bottom: 0.8rem;
        padding: 0.75rem 0.85rem;
        border: 1px solid #cfe4dc;
        border-radius: 10px;
        background: #f1faf6;
        color: #285c4a;
    }
    .source-file-summary__name {
        overflow: hidden;
        font-weight: 750;
        text-overflow: ellipsis;
        white-space: nowrap;
    }
    .source-file-summary__badge {
        flex: 0 0 auto;
        padding: 0.2rem 0.55rem;
        border-radius: 999px;
        background: #d9f2e7;
        color: #176148;
        font-size: 0.72rem;
        font-weight: 800;
        letter-spacing: 0.04em;
        text-transform: uppercase;
    }
    [data-testid="stVerticalBlockBorderWrapper"] {
        border-color: var(--border) !important;
        border-radius: 15px !important;
        background: rgba(255, 255, 255, 0.88);
        box-shadow: 0 7px 22px rgba(16, 36, 62, 0.06);
    }
    .screener-market-chip {
        display: inline-flex;
        align-items: center;
        gap: 0.4rem;
        margin: 0.1rem 0 1rem;
        padding: 0.28rem 0.7rem;
        border: 1px solid #cfe2e8;
        border-radius: 999px;
        background: #edf7f9;
        color: var(--brand-dark);
        font-size: 0.78rem;
        font-weight: 750;
    }
    .screener-section-heading {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 1rem;
        margin: 1.45rem 0 0.7rem;
    }
    .screener-section-heading__title {
        display: flex;
        align-items: center;
        gap: 0.55rem;
        color: var(--ink-strong);
        font-size: 1.18rem;
        font-weight: 800;
        letter-spacing: -0.02em;
    }
    .screener-section-heading__count {
        flex: 0 0 auto;
        padding: 0.25rem 0.65rem;
        border: 1px solid #cfe2e8;
        border-radius: 999px;
        background: var(--brand-soft);
        color: var(--brand-dark);
        font-size: 0.75rem;
        font-weight: 800;
    }
    .screener-section-copy {
        margin: -0.45rem 0 0.8rem;
        color: var(--ink-muted);
        font-size: 0.84rem;
    }
    .expression-reference {
        display: grid;
        gap: 0.85rem;
    }
    .expression-reference__group {
        display: grid;
        gap: 0.38rem;
    }
    .expression-reference__label {
        color: var(--ink-muted);
        font-size: 0.7rem;
        font-weight: 800;
        letter-spacing: 0.07em;
        text-transform: uppercase;
    }
    .expression-reference__chips {
        display: flex;
        flex-wrap: wrap;
        gap: 0.35rem;
    }
    details.expression-keyword {
        overflow: hidden;
        flex: 0 0 auto;
        border: 1px solid #cfe2e8;
        border-radius: 7px;
        background: #f0f8fa;
        color: var(--brand-dark);
        font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
        font-size: 0.72rem;
        font-weight: 750;
        transition: border-color 0.16s ease, background 0.16s ease, box-shadow 0.16s ease;
    }
    details.expression-keyword:hover {
        border-color: #86b5c2;
        background: #e8f5f7;
    }
    details.expression-keyword[open] {
        flex: 1 0 100%;
        border-color: #78a9b9;
        background: #ffffff;
        box-shadow: 0 5px 14px rgba(16, 53, 76, 0.08);
    }
    .expression-keyword summary {
        display: flex;
        align-items: center;
        gap: 0.35rem;
        padding: 0.3rem 0.5rem;
        cursor: pointer;
        list-style: none;
        user-select: none;
    }
    .expression-keyword summary::-webkit-details-marker {
        display: none;
    }
    .expression-keyword summary::after {
        content: "?";
        display: inline-grid;
        place-items: center;
        width: 0.9rem;
        height: 0.9rem;
        border-radius: 50%;
        background: #d7edf1;
        color: #176b87;
        font-family: system-ui, sans-serif;
        font-size: 0.58rem;
        font-weight: 850;
    }
    .expression-keyword[open] summary::after {
        content: "×";
    }
    .expression-keyword__meaning {
        padding: 0.55rem 0.65rem 0.65rem;
        border-top: 1px solid #e2edf2;
        background: #f8fbfc;
        color: #40586d;
        font-family: system-ui, sans-serif;
        font-size: 0.75rem;
        font-weight: 500;
        line-height: 1.45;
    }
    .expression-example {
        padding: 0.55rem 0.65rem;
        border-left: 3px solid var(--brand);
        border-radius: 6px;
        background: #f5f8fb;
        color: #314a61;
        font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
        font-size: 0.72rem;
        line-height: 1.45;
        overflow-wrap: anywhere;
    }

    /* Section headers */
    .section-header {
        display: flex;
        align-items: center;
        gap: 0.4rem;
        margin: 1.35rem 0 0.75rem;
        padding-bottom: 0.55rem;
        border-bottom: 1px solid var(--border);
        color: var(--ink-strong);
        font-size: 1.08rem;
        font-weight: 800;
        letter-spacing: -0.01em;
    }

    /* Expanders act as filter cards */
    [data-testid="stExpander"] {
        overflow: hidden;
        margin-bottom: 0.65rem;
        border: 1px solid var(--border);
        border-radius: 13px;
        background: rgba(255, 255, 255, 0.88);
        box-shadow: var(--shadow-sm);
    }
    [data-testid="stExpander"] summary {
        min-height: 3.2rem;
        color: var(--ink-strong);
        font-weight: 750;
    }
    [data-testid="stExpander"]:hover {
        border-color: #bdd2de;
        box-shadow: 0 6px 20px rgba(16, 36, 62, 0.07);
    }
    [data-testid="stExpander"]:focus-within {
        border-color: #78a9b9 !important;
        box-shadow: 0 0 0 3px rgba(23, 107, 135, 0.10);
    }

    /* Status messages, tables and progress */
    [data-testid="stAlert"] {
        border-radius: 12px;
        border-width: 1px;
        box-shadow: var(--shadow-sm);
    }
    [data-testid="stDataFrame"],
    [data-testid="stTable"] {
        overflow: hidden;
        border: 1px solid var(--border);
        border-radius: 12px;
        background: #ffffff;
        box-shadow: var(--shadow-sm);
    }
    [data-testid="stProgress"] > div {
        overflow: hidden;
        border: 1px solid #c8d4de;
        border-radius: 999px;
        background: #e8eef3;
        box-shadow: inset 0 1px 2px rgba(16, 36, 62, 0.10);
    }
    [data-testid="stProgress"] > div > div {
        background: linear-gradient(90deg, #f6b73c, #ed762f) !important;
        box-shadow: 0 0 8px rgba(237, 118, 47, 0.34);
    }
    hr {
        border-color: var(--border) !important;
        margin: 1.4rem 0 !important;
    }

    /* Keep compact column layouts comfortable */
    [data-testid="stHorizontalBlock"] {
        gap: 1rem;
    }

    @media (max-width: 768px) {
        .stMainBlockContainer {
            padding-top: 0.8rem;
        }
        .app-hero {
            min-height: 112px;
            padding: 1.25rem;
            border-radius: 17px;
        }
        .app-hero__mark {
            display: none;
        }
        .app-hero__subtitle {
            font-size: 0.86rem;
        }
        div.stTabs [data-baseweb="tab-list"] {
            overflow-x: auto;
            justify-content: flex-start;
        }
        div.stTabs [data-baseweb="tab"] {
            flex: 0 0 auto;
            padding-inline: 0.9rem;
        }
    }
    </style>
    """,
    unsafe_allow_html=True,
)

st.markdown(
    """
    <section class="app-hero">
        <div class="app-hero__content">
            <div class="app-hero__eyebrow">Market intelligence workspace</div>
            <h1 class="app-hero__title">NSE Stock Screener</h1>
            <p class="app-hero__subtitle">
                Download market data, build precise screeners, validate strategies,
                and review opportunities in one focused workspace.
            </p>
        </div>
        <div class="app-hero__mark" aria-hidden="true">↗</div>
    </section>
    """,
    unsafe_allow_html=True,
)


def sync_pattern_lookback_from_slider():
    st.session_state["pattern_lookback_days_number"] = st.session_state["pattern_lookback_days_slider"]


def sync_pattern_lookback_from_number():
    st.session_state["pattern_lookback_days_slider"] = st.session_state["pattern_lookback_days_number"]


def sync_pattern_reversal_from_slider():
    st.session_state["pattern_reversal_pct_number"] = st.session_state["pattern_reversal_pct_slider"]


def sync_pattern_reversal_from_number():
    st.session_state["pattern_reversal_pct_slider"] = st.session_state["pattern_reversal_pct_number"]


def initialize_pattern_expression_state():
    if "pattern_expression_filters" not in st.session_state:
        saved_expressions = settings.get("pattern_expressions", [])
        st.session_state["pattern_expression_filters"] = [
            {"id": index, "expression": expression}
            for index, expression in enumerate(saved_expressions, start=1)
        ]
        st.session_state["next_pattern_expression_id"] = len(saved_expressions) + 1


def clear_filter_widget_state():
    for key in list(st.session_state.keys()):
        if key.startswith("ma_filter_") or key.startswith("pattern_expression_"):
            del st.session_state[key]


def apply_filter_selection_to_state(filter_name):
    if filter_name == "Current Filters":
        update_settings({"selected_favorite_filter_set": filter_name})
        return

    saved_filter = favorite_filter_sets.get(filter_name)
    if saved_filter is None:
        return

    if isinstance(saved_filter, list):
        ma_filter_set = saved_filter
        pattern_settings = {}
    else:
        ma_filter_set = saved_filter.get("ma_filter_set", [])
        pattern_settings = saved_filter.get("pattern", {})

    loaded_ma_filter_set = normalize_filter_set(ma_filter_set, use_default=False)
    loaded_expressions = [
        str(expression).strip()
        for expression in pattern_settings.get("expressions", [])
        if str(expression).strip()
    ]
    lookback_days = int(pattern_settings.get("lookback_days", settings.get("pattern_lookback_days", 120)))
    reversal_pct = float(pattern_settings.get("reversal_pct", settings.get("pattern_reversal_pct", 5.0)))

    clear_filter_widget_state()
    # Bump widget key version so Streamlit frontend creates fresh widgets
    # with the loaded values instead of reusing cached values from old keys.
    st.session_state["_widget_key_version"] = st.session_state.get("_widget_key_version", 1) + 1
    st.session_state["current_filter_set"] = deepcopy(loaded_ma_filter_set)
    st.session_state["next_filter_id"] = (
        max((int(item.get("id", 0)) for item in loaded_ma_filter_set), default=0) + 1
    )
    st.session_state["pattern_expression_filters"] = [
        {"id": index, "expression": expression}
        for index, expression in enumerate(loaded_expressions, start=1)
    ]
    st.session_state["next_pattern_expression_id"] = len(loaded_expressions) + 1
    st.session_state["pattern_lookback_days_slider"] = lookback_days
    st.session_state["pattern_lookback_days_number"] = lookback_days
    st.session_state["pattern_reversal_pct_slider"] = reversal_pct
    st.session_state["pattern_reversal_pct_number"] = reversal_pct

    update_settings({
        "selected_favorite_filter_set": filter_name,
        "screener_filter_set": loaded_ma_filter_set,
        "pattern_lookback_days": lookback_days,
        "pattern_reversal_pct": reversal_pct,
        "pattern_expressions": loaded_expressions,
    })


def _get_last_date_from_json_dir(json_dir, top_n=10):
    """Scan up to `top_n` JSON files in `json_dir` and return the latest 'Date' found, or None."""
    if not json_dir or not json_dir.exists():
        return None
    files = stock_data_files(json_dir)[:top_n]
    latest = None
    for f in files:
        try:
            records = json.loads(f.read_text())
            if records:
                last_rec = records[-1]
                date_str = last_rec.get("Date")
                if date_str:
                    dt = pd.Timestamp(date_str).to_pydatetime()
                    if latest is None or dt > latest:
                        latest = dt
        except Exception:
            continue
    return latest


def is_stock_data_file(path):
    return path.stem.upper() != NIFTY_DATA_SYMBOL


def stock_data_files(directory):
    if not directory or not directory.exists():
        return []
    return sorted(path for path in directory.glob("*.json") if is_stock_data_file(path))


CHART_CREATION_LOCK = threading.RLock()


def screen_stock_file_worker(
    index,
    stock_file,
    filter_set,
    market,
    pattern_lookback_days,
    pattern_reversal_pct,
    pattern_expressions,
    create_charts=False,
):
    result = screen_json_file(
        stock_file,
        filter_set=filter_set,
        market=market,
    )
    if not result:
        return {
            "index": index,
            "path": stock_file,
            "result": None,
            "swings": [],
            "pattern_error": "",
            "error": "",
        }

    pattern_passed = True
    swings = []
    pattern_error = ""
    if pattern_expressions:
        pattern_passed, swings, pattern_error = evaluate_pattern_filters(
            stock_file,
            pattern_lookback_days,
            pattern_reversal_pct,
            pattern_expressions,
            pe_ratio=result.get("PE Ratio"),
        )

    if pattern_passed:
        enrich_result_with_growth_metrics(result, stock_file.stem, market)

    if pattern_passed and create_charts:
        with CHART_CREATION_LOCK:
            chart_path = create_stock_chart(
                stock_file,
                filter_set,
                pe_ratio=result.get("PE Ratio"),
            )
        if chart_path:
            result["ChartPath"] = chart_path
            result["ChartSource"] = stock_file.stem

    return {
        "index": index,
        "path": stock_file,
        "result": result if pattern_passed else None,
        "swings": swings,
        "pattern_error": pattern_error,
        "error": "",
    }


def run_live_screener_job(
    job_queue,
    stock_files,
    filter_set,
    market,
    pattern_lookback_days,
    pattern_reversal_pct,
    pattern_expressions,
    create_charts,
):
    total = len(stock_files)
    max_workers = min(8, max(1, total))
    matched_rows = []
    failed_count = 0

    try:
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [
                executor.submit(
                    screen_stock_file_worker,
                    index,
                    stock_file,
                    filter_set,
                    market,
                    pattern_lookback_days,
                    pattern_reversal_pct,
                    pattern_expressions,
                    create_charts,
                )
                for index, stock_file in enumerate(stock_files, start=1)
            ]

            for done, future in enumerate(as_completed(futures), start=1):
                stock_name = "unknown"
                try:
                    worker_result = future.result()
                    stock_file = worker_result.get("path")
                    stock_name = stock_file.stem if stock_file else stock_name
                    result = worker_result.get("result")
                    if result:
                        matched_rows.append(result)
                        job_queue.put({
                            "type": "match",
                            "row": result,
                            "symbol": result.get("Symbol", stock_name),
                            "done": done,
                            "total": total,
                        })
                except Exception as exc:
                    failed_count += 1
                    job_queue.put({
                        "type": "worker_error",
                        "message": str(exc),
                        "done": done,
                        "total": total,
                    })

                job_queue.put({
                    "type": "progress",
                    "done": done,
                    "total": total,
                    "matches": len(matched_rows),
                    "finished": stock_name,
                    "max_workers": max_workers,
                })

        save_results(matched_rows)
        job_queue.put({
            "type": "complete",
            "rows": matched_rows,
            "failed_count": failed_count,
            "total": total,
            "matches": len(matched_rows),
        })
    except Exception as exc:
        job_queue.put({"type": "fatal_error", "message": str(exc), "rows": matched_rows})


def start_live_screener_job(
    stock_files,
    filter_set,
    market,
    pattern_lookback_days,
    pattern_reversal_pct,
    pattern_expressions,
    create_charts,
):
    job_queue = queue.Queue()
    total = len(stock_files)
    max_workers = min(8, max(1, total))
    thread = threading.Thread(
        target=run_live_screener_job,
        args=(
            job_queue,
            stock_files,
            filter_set,
            market,
            pattern_lookback_days,
            pattern_reversal_pct,
            pattern_expressions,
            create_charts,
        ),
        daemon=True,
    )
    job = {
        "id": uuid.uuid4().hex,
        "queue": job_queue,
        "thread": thread,
        "total": total,
        "done": 0,
        "matches": 0,
        "failed_count": 0,
        "max_workers": max_workers,
        "running": True,
        "error": "",
        "started_at": datetime.now().strftime("%H:%M:%S"),
    }
    thread.start()
    return job


def drain_live_screener_events():
    job = st.session_state.get("screener_job")
    if not job:
        return None

    rows = st.session_state.setdefault("results", [])
    while True:
        try:
            event = job["queue"].get_nowait()
        except queue.Empty:
            break

        event_type = event.get("type")
        if event_type == "match":
            rows.append(event["row"])
            job["matches"] = len(rows)
            job["last_symbol"] = event.get("symbol", "")
        elif event_type == "progress":
            job["done"] = event.get("done", job.get("done", 0))
            job["total"] = event.get("total", job.get("total", 0))
            job["matches"] = event.get("matches", job.get("matches", len(rows)))
            job["last_finished"] = event.get("finished", "")
            job["max_workers"] = event.get("max_workers", job.get("max_workers", 1))
        elif event_type == "worker_error":
            job["failed_count"] = job.get("failed_count", 0) + 1
            job["last_error"] = event.get("message", "")
        elif event_type == "complete":
            st.session_state["results"] = event.get("rows", rows)
            job["done"] = event.get("total", job.get("total", 0))
            job["total"] = event.get("total", job.get("total", 0))
            job["matches"] = event.get("matches", len(st.session_state["results"]))
            job["failed_count"] = event.get("failed_count", job.get("failed_count", 0))
            job["running"] = False
        elif event_type == "fatal_error":
            job["error"] = event.get("message", "Unknown screener error")
            job["running"] = False

    thread = job.get("thread")
    if job.get("running") and thread is not None and not thread.is_alive():
        job["running"] = False
    return job


def chart_file_needs_regeneration(chart_path):
    if not chart_path:
        return True
    try:
        path = Path(chart_path)
        return not path.exists() or path.stat().st_size < 10_000
    except OSError:
        return True


def repair_blank_result_charts(rows, filter_set, market, timeframe):
    if not rows:
        return False

    target_dir = timeframe_config(timeframe, market)["target_dir"]
    changed = False
    for row in rows:
        if not chart_file_needs_regeneration(row.get("ChartPath")):
            continue

        symbol = row.get("ChartSource") or row.get("Symbol")
        if not symbol:
            continue

        stock_file = target_dir / f"{symbol}.json"
        if not stock_file.exists():
            continue

        with CHART_CREATION_LOCK:
            chart_path = create_stock_chart(stock_file, filter_set, pe_ratio=row.get("PE Ratio"))
        if chart_path:
            row["ChartPath"] = chart_path
            row["ChartSource"] = symbol
            changed = True

    return changed


def expression_keyword_reference_html():
    keyword_groups = [
        (
            "Market values",
            [
                ("P", "The stock's current price, using the latest available closing price."),
                ("PE", "The stock's current price-to-earnings ratio."),
                ("SMA100", "The latest 100-day simple moving average of closing prices."),
                ("SMA200", "The latest 200-day simple moving average of closing prices."),
                (
                    "SMA&lt;days&gt;",
                    "Any simple moving average. Replace days with a positive whole number, such as SMA20 or SMA75.",
                ),
            ],
        ),
        (
            "MA functions",
            [
                (
                    "CD(short, long)",
                    "Days since the short-period SMA most recently crossed above the long-period SMA. "
                    "Example: CD(50, 200) &lt; 40.",
                ),
                (
                    "ROI(period)",
                    "The one-day percentage increase or decrease in the selected SMA. Example: ROI(50) &gt; 0.",
                ),
                (
                    "MA_MIN(period, days)",
                    "The lowest value of the selected SMA during the specified recent number of trading days.",
                ),
                (
                    "MA_MAX(period, days)",
                    "The highest value of the selected SMA during the specified recent number of trading days.",
                ),
                (
                    "MA_VAR(period, days)",
                    "The percentage range from the maximum to minimum value of the selected SMA during the lookback.",
                ),
            ],
        ),
        (
            "Logic & comparisons",
            [
                ("and", "Both conditions must be true."),
                ("or", "At least one of the conditions must be true."),
                ("&gt;", "The value on the left must be greater than the value on the right."),
                ("&gt;=", "The value on the left must be greater than or equal to the value on the right."),
                ("&lt;", "The value on the left must be less than the value on the right."),
                ("&lt;=", "The value on the left must be less than or equal to the value on the right."),
                ("==", "The values on both sides must be equal."),
                ("!=", "The values on both sides must be different."),
            ],
        ),
        (
            "Math & functions",
            [
                ("+", "Adds two values."),
                ("−", "Subtracts the right value from the left value. Type it using the standard minus sign: -."),
                ("*", "Multiplies two values."),
                ("/", "Divides the value on the left by the value on the right."),
                ("%", "Returns the remainder after division."),
                ("**", "Raises the value on the left to the power on the right."),
                ("abs()", "Returns the absolute value of a number."),
                ("min()", "Returns the smallest supplied value."),
                ("max()", "Returns the largest supplied value."),
                ("round()", "Rounds a value to the requested number of decimal places."),
            ],
        ),
    ]

    groups_html = []
    for group_label, keywords in keyword_groups:
        chips_html = "".join(
            '<details class="expression-keyword">'
            f"<summary>{label}</summary>"
            f'<div class="expression-keyword__meaning">{meaning}</div>'
            "</details>"
            for label, meaning in keywords
        )
        groups_html.append(
            '<div class="expression-reference__group">'
            f'<div class="expression-reference__label">{group_label}</div>'
            f'<div class="expression-reference__chips">{chips_html}</div>'
            "</div>"
        )

    examples_html = (
        '<div class="expression-reference__group">'
        '<div class="expression-reference__label">Examples &amp; meaning</div>'
        '<div class="expression-example">SMA100 &gt; SMA200</div>'
        '<div class="expression-example">CD(50, 200) &lt; 40 · bullish cross within 40 days</div>'
        '<div class="expression-example">ROI(50) &gt; 0 · one-day SMA growth rate %</div>'
        '<div class="expression-example">MA_MIN(50, 120) · lowest SMA50 in 120 days</div>'
        '<div class="expression-example">MA_MAX(50, 100) · highest SMA50 in 100 days</div>'
        '<div class="expression-example">MA_VAR(200, 150) &gt; 15 · max-to-min variation %</div>'
        '<div class="expression-example">P &gt; SMA200 and PE &lt; 30</div>'
        '<div class="expression-example">Positive decimal parameters are rounded to the nearest trading day.</div>'
        "</div>"
    )
    return '<div class="expression-reference">' + "".join(groups_html) + examples_html + "</div>"


def attach_backtest_chart_paths(stock_details_by_filter, stock_files, favorite_filter_sets, start_date=None, end_date=None):
    files_by_symbol = {path.stem: path for path in stock_files}
    enriched_details = {}
    date_markers = []
    if start_date:
        date_markers.append({"label": "Start", "date": start_date})
    if end_date:
        date_markers.append({"label": "End", "date": end_date})

    for filter_name, rows in stock_details_by_filter.items():
        filter_set, _ = split_favorite_filter(favorite_filter_sets.get(filter_name, []))
        enriched_rows = []
        for row in rows:
            enriched_row = dict(row)
            stock_file = files_by_symbol.get(str(row.get("Symbol", "")))
            if stock_file:
                chart_path = create_stock_chart(stock_file, filter_set, date_markers=date_markers)
                if chart_path:
                    enriched_row["ChartPath"] = chart_path
                    enriched_row["ChartSource"] = stock_file.stem
            enriched_rows.append(enriched_row)
        enriched_details[filter_name] = enriched_rows

    return enriched_details


def render_data_availability_status(market=MARKET_INDIA):
    """Render the latest available data date."""
    market = normalize_market(market)
    directory = timeframe_config("DAY", market)["target_dir"]
    last_date = _get_last_date_from_json_dir(directory)
    if last_date:
        date_formatted = last_date.strftime("%d-%m-%Y")
        st.markdown(
            f'<div class="data-status-card data-status-available">'
            f'📅 Last download: <b>{date_formatted}</b>'
            f'</div>',
            unsafe_allow_html=True,
        )
    else:
        st.markdown(
            '<div class="data-status-card data-status-empty">'
            'No stock data available'
            '</div>',
            unsafe_allow_html=True,
        )
        st.warning("No stock data found. Click '⬇️ Download Stocks Data' to begin.")


def render_backtest_results_table(summary_rows, series_by_filter, stock_details_by_filter, height=1200):
    payload = json.dumps(series_by_filter, default=str)
    chart_details_by_filter = {}
    for filter_name, rows in stock_details_by_filter.items():
        chart_rows = []
        for row in rows:
            chart_row = dict(row)
            chart_path = chart_row.get("ChartPath")
            if chart_path:
                try:
                    chart_row["ChartSrc"] = image_to_data_uri(chart_path)
                except OSError:
                    chart_row["ChartSrc"] = ""
            chart_rows.append(chart_row)
        chart_details_by_filter[filter_name] = chart_rows
    stock_payload = json.dumps(chart_details_by_filter, default=str)
    rows_html = []
    for row in summary_rows:
        filter_name = row["Filter Name"]
        gain = row.get("Portfolio Gain at End Date", row.get("Gain at End Date", row.get("Gain for next M days")))
        gain_label = "No matches" if gain is None else f"{gain:.2f}%"
        peak_gain = row.get("Peak Portfolio Gain %", row.get("Peak Average Gain %"))
        peak_gain_label = "No matches" if peak_gain is None else f"{peak_gain:.2f}%"
        rows_html.append(
            "<tr>"
            f"<td>{html.escape(filter_name)}</td>"
            f"<td>{html.escape(gain_label)}</td>"
            f"<td>{html.escape(peak_gain_label)}</td>"
            f"<td>{int(row.get('Stocks Found', 0))}</td>"
            "</tr>"
        )

    component_html = f"""
    <style>
      .backtest-wrap {{ overflow-x: auto; font-family: sans-serif; }}
      .backtest-table {{ border-collapse: collapse; width: 100%; font-size: 14px; }}
      .backtest-table th, .backtest-table td {{
        border-bottom: 1px solid #e5e7eb;
        padding: 9px 10px;
        text-align: left;
      }}
      .backtest-table th {{ background: #f8fafc; font-weight: 700; }}
      .gain-link {{
        background: transparent;
        border: 0;
        color: #2563eb;
        cursor: pointer;
        font: inherit;
        font-weight: 700;
        padding: 0;
        text-decoration: underline;
      }}
      .gain-link.active {{ color: #15803d; }}
      #backtest-chart-panel {{
        border-top: 1px solid #cbd5e1;
        margin-top: 14px;
        padding-top: 12px;
      }}
      .chart-title {{ color: #334155; font-weight: 700; margin-bottom: 8px; }}
      .chart-empty {{ color: #64748b; padding: 18px 0; text-align: center; }}
      .axis-label {{ fill: #64748b; font-size: 12px; }}
      .point-label {{ fill: #0f172a; font-size: 11px; }}
      .zero-label {{ fill: #475569; font-size: 11px; }}
      .gain-point {{ cursor: pointer; }}
      .gain-point:hover, .gain-point.active {{ fill: #15803d; stroke: #14532d; stroke-width: 2; }}
      .chart-legend {{
        display: flex;
        flex-wrap: wrap;
        gap: 10px 16px;
        margin: 8px 0 4px 0;
      }}
      .legend-item {{
        align-items: center;
        color: #334155;
        display: inline-flex;
        font-size: 13px;
        gap: 6px;
      }}
      .legend-swatch {{
        border-radius: 999px;
        display: inline-block;
        height: 10px;
        width: 10px;
      }}
      .series-toggle-row {{
        display: flex;
        flex-wrap: wrap;
        gap: 8px 14px;
        margin: 8px 0 6px 0;
      }}
      .series-toggle {{
        align-items: center;
        border: 1px solid #cbd5e1;
        border-radius: 6px;
        color: #334155;
        cursor: pointer;
        display: inline-flex;
        font-size: 13px;
        gap: 6px;
        padding: 5px 8px;
        user-select: none;
      }}
      .series-toggle input {{ cursor: pointer; margin: 0; }}
      .chart-detail {{
        background: #f8fafc;
        border: 1px solid #cbd5e1;
        border-radius: 6px;
        color: #334155;
        font-size: 13px;
        margin-top: 8px;
        padding: 8px 10px;
      }}
      .crosshair-line {{ pointer-events: none; }}
      .touch-layer {{ cursor: crosshair; touch-action: none; }}
      #stock-detail-panel {{
        border-top: 1px solid #cbd5e1;
        margin-top: 14px;
        padding-top: 12px;
      }}
      .stock-detail-section {{
        border-top: 1px solid #e5e7eb;
        margin-top: 18px;
        padding-top: 14px;
      }}
      .stock-detail-section:first-child {{ border-top: 0; margin-top: 0; padding-top: 0; }}
      .stock-symbol {{ font-weight: 700; }}
      .stock-chart-link {{
        background: transparent;
        border: 0;
        color: #2563eb;
        cursor: pointer;
        font: inherit;
        font-weight: 700;
        padding: 0;
        text-decoration: underline;
      }}
      .stock-chart-link.active {{ background: #e0e7ff; border-radius: 4px; color: #1d4ed8; }}
      .backtest-table th.sortable {{
        color: #2563eb;
        cursor: pointer;
        user-select: none;
      }}
      .stock-gain-positive {{ color: #15803d; }}
      .stock-gain-negative {{ color: #dc2626; }}
      .stock-chart-panel {{
        background: #ffffff;
        border-top: 2px solid #cbd5e1;
        box-shadow: 0 -4px 16px rgba(15, 23, 42, 0.12);
        margin-top: 10px;
        overflow: hidden;
        padding: 8px;
      }}
      .stock-chart-panel.active {{
        bottom: 0;
        left: 0;
        margin-top: 0;
        max-height: 58vh;
        position: fixed;
        right: 0;
        z-index: 50;
      }}
      .stock-chart-panel img {{ display: block; height: auto; max-height: 46vh; object-fit: contain; width: 100%; }}
      .stock-chart-frame {{ position: relative; touch-action: pan-y; user-select: none; }}
      .stock-chart-title {{ color: #334155; font-size: 13px; font-weight: 700; margin-bottom: 6px; text-align: center; }}
      .stock-chart-title-row {{
        align-items: center;
        color: #334155;
        display: flex;
        font-size: 13px;
        font-weight: 700;
        gap: 8px;
        justify-content: space-between;
        margin-bottom: 6px;
        min-height: 38px;
        padding: 0 48px 0 8px;
        text-align: center;
      }}
      .stock-chart-symbol {{
        flex: 1;
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
      }}
      .stock-chart-counter {{ color: #64748b; font-size: 12px; font-weight: 600; white-space: nowrap; }}
      .stock-chart-close {{
        align-items: center;
        background: #f1f5f9;
        border: 1px solid #cbd5e1;
        border-radius: 999px;
        color: #0f172a;
        cursor: pointer;
        display: flex;
        font-size: 20px;
        font-weight: 700;
        height: 34px;
        justify-content: center;
        line-height: 1;
        position: absolute;
        right: 8px;
        top: 2px;
        width: 34px;
        z-index: 4;
      }}
      .stock-chart-close:hover, .stock-chart-close:focus {{ background: #e2e8f0; outline: none; }}
      .stock-chart-nav {{
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
      }}
      .stock-chart-nav:hover, .stock-chart-nav:focus {{ background: rgba(15, 23, 42, 0.95); outline: none; }}
      .stock-chart-nav:disabled {{ cursor: not-allowed; opacity: 0.28; }}
      .stock-chart-prev {{ left: 6px; }}
      .stock-chart-next {{ right: 6px; }}
      .stock-chart-image-wrap {{ padding: 0 46px; }}
      .stock-chart-help {{ color: #64748b; font-size: 12px; margin-top: 5px; text-align: center; }}
      .stock-chart-empty {{ color: #64748b; font-size: 13px; padding: 14px 0; text-align: center; }}
    </style>
    <div class="backtest-wrap">
      <table class="backtest-table">
        <thead>
          <tr>
            <th>Filter Name</th>
            <th>Portfolio Gain at End Date</th>
            <th>Peak Portfolio Gain</th>
            <th>Stocks Found</th>
          </tr>
        </thead>
        <tbody>{''.join(rows_html)}</tbody>
      </table>
      <div id="backtest-chart-panel" class="chart-empty">Preparing comparison chart...</div>
      <div id="stock-detail-panel" class="chart-empty">Preparing stock tables...</div>
    </div>
    <script>
      const backtestSeries = {payload};
      const backtestStockDetails = {stock_payload};
      const backtestStockFilterNames = Object.keys(backtestStockDetails);
      backtestStockFilterNames.forEach(filterName => {{
        backtestStockDetails[filterName] = [...(backtestStockDetails[filterName] || [])].sort(
          (a, b) => stockValue(b, "Gain at End Date") - stockValue(a, "Gain at End Date")
        );
      }});
      const comparisonColors = ["#2563eb", "#dc2626", "#16a34a", "#9333ea", "#ea580c", "#0891b2", "#be123c", "#4f46e5"];
      const comparisonVisible = {{}};

      function signed(value) {{
        return (value > 0 ? "+" : "") + value.toFixed(2) + "%";
      }}

      function escapeHtml(value) {{
        return String(value ?? "")
          .replace(/&/g, "&amp;")
          .replace(/</g, "&lt;")
          .replace(/>/g, "&gt;")
          .replace(/"/g, "&quot;")
          .replace(/'/g, "&#39;");
      }}

      function gainClass(value) {{
        if (value > 0) return "stock-gain-positive";
        if (value < 0) return "stock-gain-negative";
        return "";
      }}

      function renderChart(filterName) {{
        const panel = document.getElementById("backtest-chart-panel");
        const rows = backtestSeries[filterName] || [];
        if (!rows.length) {{
          panel.className = "chart-empty";
          panel.textContent = "No matching historical signals for " + filterName + ".";
          return;
        }}

        const width = 900;
        const height = 320;
        const pad = {{ left: 58, right: 22, top: 30, bottom: 54 }};
        const gains = rows.map(row => Number(row["Portfolio Gain %"] ?? row["Average Gain %"]));
        const minY = Math.min(...gains, 0);
        const maxY = Math.max(...gains, 0);
        const spanY = Math.max(1, maxY - minY);
        const xSpan = Math.max(1, rows.length - 1);
        const plotW = width - pad.left - pad.right;
        const plotH = height - pad.top - pad.bottom;

        function x(i) {{ return pad.left + (i / xSpan) * plotW; }}
        function y(v) {{ return pad.top + ((maxY - v) / spanY) * plotH; }}
        function signed(value) {{ return (value > 0 ? "+" : "") + value.toFixed(2) + "%"; }}
        function pointDateLabel(row) {{
          return row["Date"] || row["Start Date"] || "N/A";
        }}

        const points = gains.map((gain, index) => `${{x(index).toFixed(2)}},${{y(gain).toFixed(2)}}`).join(" ");
        const zeroY = y(0);
        const firstDate = pointDateLabel(rows[0]);
        const lastDate = pointDateLabel(rows[rows.length - 1]);
        const lastGain = signed(gains[gains.length - 1]);
        const yTickValues = Array.from(new Set([minY, minY + spanY * 0.25, minY + spanY * 0.5, minY + spanY * 0.75, maxY, 0].map(value => Number(value.toFixed(2))))).sort((a, b) => b - a);
        const xTickIndexes = Array.from(new Set(rows.map((_, index) => index).filter((_, index) => index % Math.max(1, Math.ceil(rows.length / 7)) === 0).concat([0, rows.length - 1]))).sort((a, b) => a - b);
        const yTicks = yTickValues.map(value => `
          <line x1="${{pad.left - 5}}" y1="${{y(value).toFixed(2)}}" x2="${{width - pad.right}}" y2="${{y(value).toFixed(2)}}" stroke="#e2e8f0" />
          <text x="${{pad.left - 9}}" y="${{(y(value) + 4).toFixed(2)}}" text-anchor="end" class="axis-label">${{signed(value)}}</text>
        `).join("");
        const xTicks = xTickIndexes.map(index => `
          <line x1="${{x(index).toFixed(2)}}" y1="${{height - pad.bottom}}" x2="${{x(index).toFixed(2)}}" y2="${{height - pad.bottom + 5}}" stroke="#94a3b8" />
          <text x="${{x(index).toFixed(2)}}" y="${{height - 18}}" text-anchor="middle" class="axis-label">${{pointDateLabel(rows[index])}}</text>
        `).join("");
        const circles = gains.map((gain, index) => {{
          const label = `Date: ${{pointDateLabel(rows[index])}} | Gain: ${{signed(gain)}} | Stocks: ${{rows[index]["Stocks Found"]}}`;
          return `<circle class="gain-point" data-index="${{index}}" cx="${{x(index).toFixed(2)}}" cy="${{y(gain).toFixed(2)}}" r="4.5" fill="#2563eb"><title>${{label}}</title></circle>`;
        }}).join("");

        panel.className = "";
        panel.innerHTML = `
          <div class="chart-title">${{filterName}} - equal-weight portfolio gain path</div>
          <svg viewBox="0 0 ${{width}} ${{height}}" width="100%" height="320" role="img">
            ${{yTicks}}
            <line x1="${{pad.left}}" y1="${{pad.top}}" x2="${{pad.left}}" y2="${{height - pad.bottom}}" stroke="#cbd5e1" />
            <line x1="${{pad.left}}" y1="${{height - pad.bottom}}" x2="${{width - pad.right}}" y2="${{height - pad.bottom}}" stroke="#cbd5e1" />
            <line x1="${{pad.left}}" y1="${{zeroY}}" x2="${{width - pad.right}}" y2="${{zeroY}}" stroke="#94a3b8" stroke-dasharray="4 4" />
            <polyline points="${{points}}" fill="none" stroke="#2563eb" stroke-width="3" />
            ${{circles}}
            ${{xTicks}}
            <text x="${{pad.left}}" y="20" class="axis-label">Portfolio gain %</text>
            <text x="${{pad.left}}" y="${{Math.max(14, zeroY - 6)}}" class="zero-label">0%</text>
            <text x="${{pad.left}}" y="${{height - 4}}" class="axis-label">Start ${{firstDate}}</text>
            <text x="${{width - pad.right}}" y="${{height - 4}}" text-anchor="end" class="axis-label">End ${{lastDate}}</text>
            <text x="${{width - pad.right}}" y="${{Math.max(16, y(gains[gains.length - 1]) - 8)}}" text-anchor="end" class="point-label">${{lastGain}}</text>
          </svg>
          <div id="backtest-point-detail" class="chart-detail">Click or tap a point to see its date and portfolio gain.</div>
        `;

        const detail = panel.querySelector("#backtest-point-detail");
        panel.querySelectorAll(".gain-point").forEach(point => {{
          point.addEventListener("click", event => {{
            event.preventDefault();
            event.stopPropagation();
            panel.querySelectorAll(".gain-point").forEach(item => item.classList.remove("active"));
            point.classList.add("active");
            const row = rows[Number(point.dataset.index)];
            const gain = Number(row["Portfolio Gain %"] ?? row["Average Gain %"]);
            detail.textContent = `Date: ${{pointDateLabel(row)}} | Portfolio gain: ${{signed(gain)}} | Stocks: ${{row["Stocks Found"]}}`;
          }});
        }});
      }}

      document.querySelectorAll(".gain-link").forEach(button => {{
        button.addEventListener("click", () => {{
          document.querySelectorAll(".gain-link").forEach(item => item.classList.remove("active"));
          button.classList.add("active");
          renderChart(button.dataset.filter);
        }});
      }});

      function stockValue(row, key) {{
        const value = Number(row[key]);
        return Number.isFinite(value) ? value : 0;
      }}

      function stockRowsHtml(rows) {{
        return rows.map(row => {{
          const endGain = stockValue(row, "Gain at End Date");
          const peakGain = stockValue(row, "Peak Gain %");
          const symbol = escapeHtml(row["Symbol"]);
          const symbolCell = row["ChartSrc"]
            ? `<button class="stock-chart-link" data-symbol="${{symbol}}" data-chart-src="${{row["ChartSrc"]}}">${{symbol}}</button>`
            : `<span class="stock-symbol">${{symbol}}</span>`;
          return `
            <tr>
              <td>${{symbolCell}}</td>
              <td class="${{gainClass(endGain)}}">${{signed(endGain)}}</td>
              <td class="${{gainClass(peakGain)}}">${{signed(peakGain)}}</td>
            </tr>
          `;
        }}).join("");
      }}

      function resetStockChartPanel(panel) {{
        if (!panel) return;
        panel.classList.remove("active");
        panel.innerHTML = `<div class="stock-chart-empty">Tap a stock symbol to view its chart</div>`;
      }}

      function closeStockChart(section) {{
        const chartPanel = section ? section.querySelector(".stock-chart-panel") : document.querySelector(".stock-chart-panel.active");
        if (!chartPanel) return;
        const ownerSection = section || chartPanel.closest(".stock-detail-section");
        if (ownerSection) {{
          ownerSection.querySelectorAll(".stock-chart-link").forEach(item => item.classList.remove("active"));
        }}
        resetStockChartPanel(chartPanel);
      }}

      function renderStockChart(section, button) {{
        const chartPanel = section.querySelector(".stock-chart-panel");
        const buttons = Array.from(section.querySelectorAll(".stock-chart-link"));
        const index = buttons.indexOf(button);
        if (!chartPanel || !button || index < 0) return;

        document.querySelectorAll(".stock-chart-panel").forEach(panel => {{
          if (panel !== chartPanel) {{
            resetStockChartPanel(panel);
          }}
        }});
        document.querySelectorAll(".stock-chart-link").forEach(item => item.classList.remove("active"));
        section.querySelectorAll(".stock-chart-link").forEach(item => item.classList.remove("active"));
        button.classList.add("active");
        chartPanel.classList.add("active");
        const symbol = escapeHtml(button.dataset.symbol);
        const prevDisabled = index <= 0 ? "disabled" : "";
        const nextDisabled = index >= buttons.length - 1 ? "disabled" : "";
        chartPanel.innerHTML = `
          <div class="stock-chart-frame">
            <div class="stock-chart-title-row">
              <span class="stock-chart-symbol">${{symbol}}</span>
              <span class="stock-chart-counter">${{index + 1}} / ${{buttons.length}}</span>
              <button type="button" class="stock-chart-close" data-chart-close aria-label="Close chart">&times;</button>
            </div>
            <button type="button" class="stock-chart-nav stock-chart-prev" data-chart-nav="prev" aria-label="Previous chart" ${{prevDisabled}}>&lsaquo;</button>
            <button type="button" class="stock-chart-nav stock-chart-next" data-chart-nav="next" aria-label="Next chart" ${{nextDisabled}}>&rsaquo;</button>
            <div class="stock-chart-image-wrap"><img src="${{button.dataset.chartSrc}}" alt="${{symbol}} chart"></div>
            <div class="stock-chart-help">Swipe chart or use arrows to move through this filter's stocks.</div>
          </div>
        `;

        const closeButton = chartPanel.querySelector("[data-chart-close]");
        if (closeButton) {{
          closeButton.addEventListener("click", event => {{
            event.preventDefault();
            event.stopPropagation();
            closeStockChart(section);
          }});
        }}

        chartPanel.querySelectorAll("[data-chart-nav]").forEach(navButton => {{
          navButton.addEventListener("click", event => {{
            event.preventDefault();
            event.stopPropagation();
            const offset = navButton.dataset.chartNav === "next" ? 1 : -1;
            const nextIndex = Math.max(0, Math.min(buttons.length - 1, index + offset));
            if (nextIndex !== index) renderStockChart(section, buttons[nextIndex]);
          }});
        }});

        bindStockChartSwipe(section, chartPanel.querySelector(".stock-chart-frame"));
      }}

      function bindStockChartSwipe(section, frame) {{
        if (!frame) return;
        let touchStartX = 0;
        let touchStartY = 0;
        frame.addEventListener("touchstart", event => {{
          if (!event.changedTouches || !event.changedTouches.length) return;
          touchStartX = event.changedTouches[0].clientX;
          touchStartY = event.changedTouches[0].clientY;
        }}, {{ passive: true }});
        frame.addEventListener("touchend", event => {{
          if (!event.changedTouches || !event.changedTouches.length) return;
          const deltaX = event.changedTouches[0].clientX - touchStartX;
          const deltaY = event.changedTouches[0].clientY - touchStartY;
          if (Math.abs(deltaX) < 45 || Math.abs(deltaX) < Math.abs(deltaY) * 1.2) return;
          event.preventDefault();
          const buttons = Array.from(section.querySelectorAll(".stock-chart-link"));
          const activeIndex = buttons.findIndex(button => button.classList.contains("active"));
          const nextIndex = Math.max(0, Math.min(buttons.length - 1, activeIndex + (deltaX < 0 ? 1 : -1)));
          if (nextIndex >= 0 && nextIndex !== activeIndex) renderStockChart(section, buttons[nextIndex]);
        }}, {{ passive: false }});
      }}

      function bindStockChartLinks(section) {{
        section.querySelectorAll(".stock-chart-link").forEach(button => {{
          button.addEventListener("click", event => {{
            event.preventDefault();
            event.stopPropagation();
            renderStockChart(section, button);
          }});
        }});
      }}

      function bindStockSection(section) {{
        bindStockChartLinks(section);

        section.querySelectorAll("th.sortable").forEach(header => {{
          header.addEventListener("click", () => {{
            const sortKey = header.dataset.sortKey;
            const currentDir = header.dataset.sortDir === "asc" ? "asc" : "desc";
            const nextDir = currentDir === "asc" ? "desc" : "asc";
            const filterName = backtestStockFilterNames[Number(section.dataset.filterIndex)];
            const rows = [...(backtestStockDetails[filterName] || [])].sort((a, b) => {{
              const aValue = sortKey === "Symbol" ? String(a[sortKey] || "") : stockValue(a, sortKey);
              const bValue = sortKey === "Symbol" ? String(b[sortKey] || "") : stockValue(b, sortKey);
              if (typeof aValue === "string") {{
                return nextDir === "asc" ? aValue.localeCompare(bValue) : bValue.localeCompare(aValue);
              }}
              return nextDir === "asc" ? aValue - bValue : bValue - aValue;
            }});
            backtestStockDetails[filterName] = rows;
            section.querySelector("tbody").innerHTML = stockRowsHtml(rows);
            section.querySelectorAll("th.sortable").forEach(item => {{
              item.dataset.sortDir = "";
              item.textContent = item.dataset.label;
            }});
            header.dataset.sortDir = nextDir;
            header.textContent = `${{header.dataset.label}} ${{nextDir === "asc" ? "^" : "v"}}`;
            const chartPanel = section.querySelector(".stock-chart-panel");
            resetStockChartPanel(chartPanel);
            bindStockChartLinks(section);
          }});
        }});
      }}

      document.addEventListener("keydown", event => {{
        if (event.key === "Escape") {{
          closeStockChart();
        }}
      }});

      function renderAllStockDetails() {{
        const panel = document.getElementById("stock-detail-panel");
        const entries = Object.entries(backtestStockDetails);
        if (!entries.length) {{
          panel.className = "chart-empty";
          panel.textContent = "No stocks were found on the selected start date.";
          return;
        }}

        panel.className = "";
        panel.innerHTML = entries.map(([filterName, rows], index) => {{
          const safeFilter = escapeHtml(filterName);
          if (!rows.length) {{
            return `
              <section class="stock-detail-section" data-filter-index="${{index}}">
                <div class="chart-title">${{safeFilter}} - stocks found on start date</div>
                <div class="stock-chart-empty">No stocks were found for this favorite filter.</div>
              </section>
            `;
          }}
          return `
            <section class="stock-detail-section" data-filter-index="${{index}}">
              <div class="chart-title">${{safeFilter}} - stocks found on start date</div>
              <table class="backtest-table">
                <thead>
                  <tr>
                    <th class="sortable" data-sort-key="Symbol" data-label="Stock">Stock</th>
                    <th class="sortable" data-sort-key="Gain at End Date" data-label="Gain at End Date" data-sort-dir="desc">Gain at End Date v</th>
                    <th class="sortable" data-sort-key="Peak Gain %" data-label="Peak Gain">Peak Gain</th>
                  </tr>
                </thead>
                <tbody>${{stockRowsHtml(rows)}}</tbody>
              </table>
              <div class="stock-chart-panel">
                <div class="stock-chart-empty">Tap a stock symbol to view its chart</div>
              </div>
            </section>
          `;
        }}).join("");

        panel.querySelectorAll(".stock-detail-section").forEach(section => bindStockSection(section));
      }}

      function renderComparisonChart() {{
        const panel = document.getElementById("backtest-chart-panel");
        const allEntries = Object.entries(backtestSeries)
          .filter(([_, rows]) => rows && rows.length)
          .map(([filterName, rows], seriesIndex) => ({{
            filterName,
            rows,
            color: comparisonColors[seriesIndex % comparisonColors.length],
          }}));
        if (!allEntries.length) {{
          panel.className = "chart-empty";
          panel.textContent = "No matching stocks found for the selected filters and dates.";
          return;
        }}

        allEntries.forEach(entry => {{
          if (!(entry.filterName in comparisonVisible)) comparisonVisible[entry.filterName] = true;
        }});
        const entries = allEntries.filter(entry => comparisonVisible[entry.filterName]);
        const controls = allEntries.map((entry, seriesIndex) => `
          <label class="series-toggle">
            <input type="checkbox" data-series-index="${{seriesIndex}}" ${{comparisonVisible[entry.filterName] ? "checked" : ""}}>
            <span class="legend-swatch" style="background:${{entry.color}}"></span>
            <span>${{escapeHtml(entry.filterName)}}</span>
          </label>
        `).join("");

        if (!entries.length) {{
          panel.className = "";
          panel.innerHTML = `
            <div class="chart-title">Equal-weight portfolio gain comparison</div>
            <div class="series-toggle-row">${{controls}}</div>
            <div class="chart-empty">Select at least one filter or benchmark to show the chart.</div>
          `;
          panel.querySelectorAll("[data-series-index]").forEach(input => {{
            input.addEventListener("change", event => {{
              const entry = allEntries[Number(event.target.dataset.seriesIndex)];
              if (entry) comparisonVisible[entry.filterName] = event.target.checked;
              renderComparisonChart();
            }});
          }});
          return;
        }}

        const width = 960;
        const height = 380;
        const pad = {{ left: 62, right: 26, top: 34, bottom: 64 }};
        const plotW = width - pad.left - pad.right;
        const plotH = height - pad.top - pad.bottom;
        const maxLen = Math.max(...entries.map(entry => entry.rows.length));
        const allGains = entries.flatMap(entry => entry.rows.map(row => Number(row["Portfolio Gain %"] ?? row["Average Gain %"])));
        const minY = Math.min(...allGains, 0);
        const maxY = Math.max(...allGains, 0);
        const spanY = Math.max(1, maxY - minY);
        const xSpan = Math.max(1, maxLen - 1);

        function x(index) {{ return pad.left + (index / xSpan) * plotW; }}
        function y(value) {{ return pad.top + ((maxY - value) / spanY) * plotH; }}
        function signed(value) {{ return (value > 0 ? "+" : "") + value.toFixed(2) + "%"; }}
        function pointDateLabel(row) {{ return row ? (row["Date"] || row["Start Date"] || "N/A") : "N/A"; }}
        function escapeHtml(value) {{
          return String(value ?? "")
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;")
            .replace(/'/g, "&#39;");
        }}

        const referenceRows = entries.reduce((best, entry) => entry.rows.length > best.length ? entry.rows : best, []);
        const firstDate = pointDateLabel(referenceRows[0]);
        const lastDate = pointDateLabel(referenceRows[referenceRows.length - 1]);
        const yTickValues = Array.from(new Set([minY, minY + spanY * 0.25, minY + spanY * 0.5, minY + spanY * 0.75, maxY, 0].map(value => Number(value.toFixed(2))))).sort((a, b) => b - a);
        const xTickIndexes = Array.from(new Set(referenceRows.map((_, index) => index).filter((_, index) => index % Math.max(1, Math.ceil(referenceRows.length / 7)) === 0).concat([0, referenceRows.length - 1]))).sort((a, b) => a - b);
        const zeroY = y(0);

        const yTicks = yTickValues.map(value => `
          <line x1="${{pad.left - 5}}" y1="${{y(value).toFixed(2)}}" x2="${{width - pad.right}}" y2="${{y(value).toFixed(2)}}" stroke="#e2e8f0" />
          <text x="${{pad.left - 9}}" y="${{(y(value) + 4).toFixed(2)}}" text-anchor="end" class="axis-label">${{signed(value)}}</text>
        `).join("");
        const xTicks = xTickIndexes.map(index => `
          <line x1="${{x(index).toFixed(2)}}" y1="${{height - pad.bottom}}" x2="${{x(index).toFixed(2)}}" y2="${{height - pad.bottom + 5}}" stroke="#94a3b8" />
          <text x="${{x(index).toFixed(2)}}" y="${{height - 22}}" text-anchor="middle" class="axis-label">${{pointDateLabel(referenceRows[index])}}</text>
        `).join("");

        const seriesLines = entries.map(entry => {{
          const points = entry.rows.map((row, index) => {{
            const gain = Number(row["Portfolio Gain %"] ?? row["Average Gain %"]);
            return `${{x(index).toFixed(2)}},${{y(gain).toFixed(2)}}`;
          }}).join(" ");
          return `<polyline points="${{points}}" fill="none" stroke="${{entry.color}}" stroke-width="2.8" stroke-linejoin="round" stroke-linecap="round" />`;
        }}).join("");

        const legend = entries.map(entry => {{
          return `<span class="legend-item"><span class="legend-swatch" style="background:${{entry.color}}"></span>${{escapeHtml(entry.filterName)}}</span>`;
        }}).join("");

        panel.className = "";
        panel.innerHTML = `
          <div class="chart-title">Equal-weight portfolio gain comparison</div>
          <div class="series-toggle-row">${{controls}}</div>
          <div class="chart-legend">${{legend}}</div>
          <svg id="comparison-chart" viewBox="0 0 ${{width}} ${{height}}" width="100%" height="380" role="img">
            ${{yTicks}}
            <line x1="${{pad.left}}" y1="${{pad.top}}" x2="${{pad.left}}" y2="${{height - pad.bottom}}" stroke="#cbd5e1" />
            <line x1="${{pad.left}}" y1="${{height - pad.bottom}}" x2="${{width - pad.right}}" y2="${{height - pad.bottom}}" stroke="#cbd5e1" />
            <line x1="${{pad.left}}" y1="${{zeroY}}" x2="${{width - pad.right}}" y2="${{zeroY}}" stroke="#94a3b8" stroke-dasharray="4 4" />
            ${{seriesLines}}
            ${{xTicks}}
            <line id="comparison-guide" class="crosshair-line" x1="${{pad.left}}" y1="${{pad.top}}" x2="${{pad.left}}" y2="${{height - pad.bottom}}" stroke="#334155" stroke-width="1.2" stroke-dasharray="3 3" opacity="0" />
            <g id="comparison-points"></g>
            <rect class="touch-layer" x="${{pad.left}}" y="${{pad.top}}" width="${{plotW}}" height="${{plotH}}" fill="transparent" />
            <text x="${{pad.left}}" y="20" class="axis-label">Portfolio gain %</text>
            <text x="${{pad.left}}" y="${{Math.max(14, zeroY - 6)}}" class="zero-label">0%</text>
            <text x="${{pad.left}}" y="${{height - 6}}" class="axis-label">Start ${{firstDate}}</text>
            <text x="${{width - pad.right}}" y="${{height - 6}}" text-anchor="end" class="axis-label">End ${{lastDate}}</text>
          </svg>
          <div id="comparison-detail" class="chart-detail">Touch, drag, or move across the chart to compare portfolio gains by date.</div>
        `;

        const svg = panel.querySelector("#comparison-chart");
        const guide = panel.querySelector("#comparison-guide");
        const pointLayer = panel.querySelector("#comparison-points");
        const detail = panel.querySelector("#comparison-detail");
        panel.querySelectorAll("[data-series-index]").forEach(input => {{
          input.addEventListener("change", event => {{
            const entry = allEntries[Number(event.target.dataset.seriesIndex)];
            if (entry) comparisonVisible[entry.filterName] = event.target.checked;
            renderComparisonChart();
          }});
        }});

        function showIndex(index) {{
          const boundedIndex = Math.max(0, Math.min(maxLen - 1, index));
          const guideX = x(boundedIndex);
          guide.setAttribute("x1", guideX.toFixed(2));
          guide.setAttribute("x2", guideX.toFixed(2));
          guide.setAttribute("opacity", "1");

          const dateLabel = pointDateLabel(referenceRows[boundedIndex]);
          const detailRows = [];
          const markers = [];
          entries.forEach(entry => {{
            const row = entry.rows[Math.min(boundedIndex, entry.rows.length - 1)];
            if (!row) return;
            const gain = Number(row["Portfolio Gain %"] ?? row["Average Gain %"]);
            markers.push(`<circle cx="${{guideX.toFixed(2)}}" cy="${{y(gain).toFixed(2)}}" r="4.5" fill="${{entry.color}}" stroke="#ffffff" stroke-width="1.5" />`);
            const suffix = row["Benchmark"] ? "benchmark" : `${{row["Stocks Found"]}} stocks`;
            detailRows.push(`<span class="legend-item"><span class="legend-swatch" style="background:${{entry.color}}"></span>${{escapeHtml(entry.filterName)}}: <b>${{signed(gain)}}</b> (${{suffix}})</span>`);
          }});

          pointLayer.innerHTML = markers.join("");
          detail.innerHTML = `<b>${{dateLabel}}</b><br>${{detailRows.join("<br>")}}`;
        }}

        function indexFromClientX(clientX) {{
          const rect = svg.getBoundingClientRect();
          const localX = ((clientX - rect.left) / rect.width) * width;
          return Math.round(((localX - pad.left) / plotW) * xSpan);
        }}

        svg.addEventListener("mousemove", event => showIndex(indexFromClientX(event.clientX)));
        svg.addEventListener("pointerdown", event => {{
          event.preventDefault();
          showIndex(indexFromClientX(event.clientX));
        }});
        svg.addEventListener("touchmove", event => {{
          if (!event.touches || !event.touches.length) return;
          event.preventDefault();
          showIndex(indexFromClientX(event.touches[0].clientX));
        }}, {{ passive: false }});

        showIndex(maxLen - 1);
      }}

      renderComparisonChart();
      renderAllStockDetails();
    </script>
    """
    components.html(component_html, height=height, scrolling=True)


@st.cache_data(show_spinner=False)
def cached_backtest_calendar_dates(file_signatures):
    stock_files = [Path(path) for path, _, _ in file_signatures]
    return [date.date() for date in get_backtest_calendar_dates(stock_files)]


def stock_file_signatures(stock_files):
    signatures = []
    for path in stock_files:
        try:
            stat = path.stat()
        except OSError:
            continue
        signatures.append((str(path), stat.st_mtime_ns, stat.st_size))
    return tuple(signatures)


def switch_to_tab(tab_index):
    switch_token = st.session_state.get("_tab_switch_token", 0) + 1
    st.session_state["_tab_switch_token"] = switch_token
    components.html(
        f"""
        <script>
        const tabIndex = {tab_index};
        const switchToken = {switch_token};
        const clickTargetTab = () => {{
          const tabs = Array.from(window.parent.document.querySelectorAll('[role="tab"]'));
          if (tabs[tabIndex]) {{
            window.parent.document.body.dataset.codexLastTabSwitch = String(switchToken);
            tabs[tabIndex].click();
            return true;
          }}
          return false;
        }};

        if (!clickTargetTab()) {{
          let attempts = 0;
          const timer = window.setInterval(() => {{
            attempts += 1;
            if (clickTargetTab() || attempts >= 50) {{
              window.clearInterval(timer);
            }}
          }}, 100);
        }}
        </script>
        """,
        height=0,
    )


tab1, tab2, tab3, tab4 = st.tabs(["📥 Data", "🔍 Screener", "Backtest", "📊 Results"])

if st.session_state.pop("switch_to_results_tab", False):
    switch_to_tab(3)


# =====================================================================
# TAB 1: DATA MANAGEMENT
# =====================================================================
with tab1:
    st.header("📥 Data Management")
    st.caption("Manage your market universe and refresh stock prices from one place.")

    market_options = [MARKET_INDIA, MARKET_US]
    market_col, status_col = st.columns(2)
    with market_col:
        with st.container(border=True):
            st.markdown(
                '<div class="data-panel-heading"><span>🌐</span>Market</div>'
                '<p class="data-panel-subtitle">Choose the stock market universe used throughout the app.</p>',
                unsafe_allow_html=True,
            )
            selected_market = st.selectbox(
                "Market",
                market_options,
                index=market_options.index(normalize_market(settings.get("market", MARKET_INDIA))),
                format_func=market_label,
                help="Select India to use the XLS universe with .NS Yahoo symbols, or US to use the Nasdaq CSV with plain Yahoo symbols.",
                label_visibility="collapsed",
            )

    with status_col:
        with st.container(border=True):
            st.markdown(
                '<div class="data-panel-heading"><span>◷</span>Data Status</div>'
                '<p class="data-panel-subtitle">The latest date currently available for the selected market.</p>',
                unsafe_allow_html=True,
            )
            render_data_availability_status(selected_market)

    india_excel_file = EXCEL_DIR / "MCAP_JUGAAD.xlsx"
    us_csv_file = EXCEL_DIR / "nasdaq_screener_1784114565446.csv"
    symbols_file = us_csv_file if selected_market == MARKET_US else india_excel_file
    source_label = "CSV" if selected_market == MARKET_US else "Excel"

    available_symbol_count = 0
    if symbols_file.exists():
        available_symbol_count = len(load_top_symbols(symbols_file, limit=1_000_000, market=selected_market))

    limit_setting_key = "download_limit_us" if selected_market == MARKET_US else "download_limit"
    default_download_limit = available_symbol_count if selected_market == MARKET_US and available_symbol_count else 1000
    saved_download_limit = int(settings.get(limit_setting_key, default_download_limit))
    if available_symbol_count:
        saved_download_limit = min(saved_download_limit, available_symbol_count)

    download_tf = "DAY"
    source_col, settings_col = st.columns(2)
    with source_col:
        with st.container(border=True):
            st.markdown(
                '<div class="data-panel-heading"><span>🗂️</span>Source File</div>'
                '<p class="data-panel-subtitle">Review or replace the symbol universe used for downloads.</p>',
                unsafe_allow_html=True,
            )
            if symbols_file.exists():
                st.markdown(
                    f'<div class="source-file-summary">'
                    f'<span class="source-file-summary__name">{html.escape(symbols_file.name)}</span>'
                    f'<span class="source-file-summary__badge">Ready</span>'
                    f'</div>',
                    unsafe_allow_html=True,
                )
                st.caption(f"{available_symbol_count:,} symbols available")
            else:
                st.warning(f"Add {symbols_file.name} before downloading stock data.")

            if selected_market == MARKET_INDIA:
                uploaded = st.file_uploader(
                    "Replace source file",
                    type=["xlsx"],
                    help="Upload a replacement Excel file containing the India stock universe.",
                )
                if uploaded:
                    india_excel_file.write_bytes(uploaded.getbuffer())
                    st.success("Source file replaced successfully.")
            else:
                st.caption("The US market uses the configured Nasdaq CSV source.")

    with settings_col:
        with st.container(border=True):
            st.markdown(
                '<div class="data-panel-heading"><span>⚙️</span>Download Settings</div>'
                '<p class="data-panel-subtitle">Set the universe size and choose incremental or full refresh.</p>',
                unsafe_allow_html=True,
            )
            download_limit = st.number_input(
                "Number of stocks",
                min_value=1,
                max_value=available_symbol_count or None,
                value=saved_download_limit,
                step=50,
                help=f"{available_symbol_count} symbols are available in the selected {source_label} file." if available_symbol_count else None,
            )

            full_refresh = st.checkbox(
                "Clear existing data before downloading",
                value=False,
                help="Leave unchecked for a faster incremental refresh that appends only candles after each stock file's latest saved date.",
            )

            download_clicked = st.button(
                "⬇️ Download Stocks Data",
                type="primary",
                use_container_width=True,
            )

    update_settings({
        "market": selected_market,
        "download_tf": download_tf,
        limit_setting_key: download_limit,
    })

    if download_clicked:
        if not symbols_file.exists():
            st.error(f"❌ Add {symbols_file.name} before downloading {market_label(selected_market)} stock data.")
        else:
            st.markdown(
                '<div class="data-panel-heading"><span>↻</span>Download Activity</div>',
                unsafe_allow_html=True,
            )
            progress_bar = st.progress(0)
            progress_text = st.empty()

            def show_download_progress(done, total, downloaded_count, symbol):
                progress = done / total if total else 0
                progress_bar.progress(progress)
                progress_text.info(
                    f"Processed {downloaded_count} of {total} stocks. "
                    f"Processing {done}/{total}: {symbol}"
                )

            if full_refresh:
                deleted_count = clear_downloaded_json_files(download_tf, market=selected_market)
                if deleted_count:
                    progress_text.info(f"Cleared {deleted_count} old {download_tf.lower()} JSON files.")

            with st.spinner(f"⬇️ Downloading {download_limit} {market_label(selected_market)} stocks from yfinance..."):
                download_rows = download_top_stocks(
                    symbols_file,
                    download_tf,
                    limit=download_limit,
                    progress_callback=show_download_progress,
                    incremental=not full_refresh,
                    market=selected_market,
                )
                nifty_row = download_nifty_index(download_tf, incremental=not full_refresh, market=selected_market)

            downloaded_count = sum(1 for row in download_rows if row["Downloaded"])
            rows_added = sum(int(row.get("Rows Added", 0) or 0) for row in download_rows)
            progress_bar.progress(1.0)
            last_download_at = datetime.now().strftime("%d-%m-%Y %H:%M:%S")
            update_settings({
                "market": selected_market,
                "last_download_at": last_download_at,
                "last_download_tf": download_tf,
                "last_download_market": selected_market,
            })
            progress_text.success(
                f"✅ Processed {downloaded_count} of {len(download_rows)} stocks. "
                f"Last download: {last_download_at}"
            )
            st.success(f"✅ Processed {downloaded_count} of {len(download_rows)} stocks")

            st.caption(f"Incremental rows added: {rows_added}")

            if selected_market == MARKET_INDIA and nifty_row["Downloaded"]:
                st.success("Downloaded Nifty 50 benchmark data")
            elif selected_market == MARKET_INDIA:
                st.warning(f"Could not download Nifty 50 benchmark data: {nifty_row['Error'] or 'No data returned'}")

            failed = [row for row in download_rows if not row["Downloaded"]]
            if failed:
                st.markdown(
                    pd.DataFrame(failed).to_html(index=False),
                    unsafe_allow_html=True,
                )

            # Refresh data availability display
            st.rerun()


# =====================================================================
# TAB 2: SCREENER
# =====================================================================
with tab2:
    st.header("🔍 Screener")
    current_market = normalize_market(selected_market)
    st.markdown(
        f'<div class="screener-market-chip">● {html.escape(market_label(current_market))} market</div>',
        unsafe_allow_html=True,
    )

    # ---- Initialize session state for filter set ----
    if "screener_filter_set" in settings:
        loaded_filter_set = normalize_filter_set(settings.get("screener_filter_set"), use_default=False)
    else:
        loaded_filter_set = normalize_filter_set(DEFAULT_FILTER_SET)

    if "current_filter_set" not in st.session_state:
        st.session_state["current_filter_set"] = deepcopy(loaded_filter_set)
        st.session_state["next_filter_id"] = (
            max((int(item.get("id", 0)) for item in loaded_filter_set), default=0) + 1
        )

    if "next_filter_id" not in st.session_state:
        st.session_state["next_filter_id"] = (
            max((int(item.get("id", 0)) for item in st.session_state["current_filter_set"]), default=0) + 1
        )

    filter_widget_prefix = "ma_filter"

    # ===== TOP SECTION: Favorite Filter Selection + Run Screener =====
    command_col, builder_col = st.columns([1.35, 1])
    with command_col:
        quick_run_panel = st.container(border=True)
    with quick_run_panel:
        st.markdown(
            '<div class="data-panel-heading"><span>⚡</span>Quick Run</div>'
            '<p class="data-panel-subtitle">Choose a filter set, adjust optional checks, and start screening.</p>',
            unsafe_allow_html=True,
        )

    tf = "DAY"
    with quick_run_panel:
        col_green, col_charts = st.columns(2)
    with col_green:
        green_candle_toggle = st.toggle(
            "🟢 Green Candle Today",
            value=bool(settings.get("green_candle_toggle", False)),
            key="green_candle_toggle",
            help="Only show stocks that closed higher than they opened, with a minimum gain from previous close.",
        )
    with col_charts:
        create_charts = st.toggle(
            "📈 Create charts",
            value=bool(settings.get("create_charts", False)),
            key="create_charts_toggle",
        )
    green_candle_min_gain_pct = float(settings.get("green_candle_min_gain_pct", 1.0))
    if green_candle_toggle:
        with quick_run_panel:
            green_candle_min_gain_pct = float(st.number_input(
                "Minimum Gain %",
                min_value=0.0,
                max_value=100.0,
                value=green_candle_min_gain_pct,
                step=0.1,
                key="green_candle_min_gain_pct",
                help="Minimum percentage gain from previous close required for the green candle filter.",
            ))
    update_settings({
        "tf": tf,
        "create_charts": create_charts,
        "green_candle_toggle": green_candle_toggle,
        "green_candle_min_gain_pct": green_candle_min_gain_pct,
    })

    # ---- Favorite Filter Set + Run Button side by side ----
    with quick_run_panel:
        col_fav, col_run = st.columns([3, 1])
    with col_fav:
        favorite_names = sorted(favorite_filter_sets.keys())
        if favorite_names:
            favorite_options = ["Current Filters"] + favorite_names
            # Initialize widget state on first load
            if "_favorite_select_widget" not in st.session_state:
                st.session_state["_favorite_select_widget"] = settings.get(
                    "selected_favorite_filter_set", "Current Filters"
                )

            def on_favorite_filter_selected():
                """Callback that fires immediately when the user picks a new favourite."""
                selected = st.session_state["_favorite_select_widget"]
                apply_filter_selection_to_state(selected)
                st.session_state["_favorite_name_to_save"] = (
                    selected if selected != "Current Filters" else ""
                )

            selected_fav = st.selectbox(
                "⭐ Filter Set To Run",
                favorite_options,
                key="_favorite_select_widget",
                on_change=on_favorite_filter_selected,
                help="Select a saved favorite filter set to load its MA and expression filters.",
            )
            # Ensure the save-name field is pre-filled with the currently-selected favourite
            if "_favorite_name_to_save" not in st.session_state:
                st.session_state["_favorite_name_to_save"] = (
                    selected_fav if selected_fav != "Current Filters" else ""
                )
            elif st.session_state.get("_favorite_name_to_save") == "" and selected_fav != "Current Filters":
                st.session_state["_favorite_name_to_save"] = selected_fav
        else:
            st.info("No saved favorite filters yet. Configure filters below and save them.")
    with col_run:
        st.write("")  # spacer
        run_combined = st.button("▶️ Run Screener", type="primary", use_container_width=True)

    # Placeholder for progress bar — will be filled when screener runs
    screener_progress_placeholder = st.empty()

    # Read current_filter_set from session state now (after selectbox may have updated it)
    current_filter_set = st.session_state["current_filter_set"]

    # ---- Add Filter Row ----
    with builder_col:
        add_filter_panel = st.container(border=True)
    with add_filter_panel:
        st.markdown(
            '<div class="data-panel-heading"><span>＋</span>Add a Filter</div>'
            '<p class="data-panel-subtitle">Choose a technical or valuation rule for the current set.</p>',
            unsafe_allow_html=True,
        )
        col1, col2 = st.columns([3, 1])
    with col1:
        filter_type_to_add = st.selectbox(
            "Filter Category",
            [key for key in FILTER_TYPE_LABELS if key != "green_candle_today"],
            format_func=lambda value: FILTER_TYPE_LABELS[value],
        )
    with col2:
        add_filter = st.button(
            "➕ Add",
            use_container_width=True,
            help="Add the selected rule to the current filter set.",
        )

    if add_filter:
        current_filter_set.append({
            "id": st.session_state["next_filter_id"],
            "type": filter_type_to_add,
            "params": deepcopy(FILTER_TYPE_DEFAULTS[filter_type_to_add]),
        })
        st.session_state["next_filter_id"] += 1
        st.rerun()

    # Use a widget-key version so that when a favourite is loaded new widget
    # instances are created and their value= parameters take effect instead of
    # Streamlit reusing frontend-cached values from the previous filter set.
    widget_key_version = st.session_state.get("_widget_key_version", 1)

    st.markdown(
        f'<div class="screener-section-heading">'
        f'<div class="screener-section-heading__title">Current Filter Set</div>'
        f'<div class="screener-section-heading__count">{len(current_filter_set)} active</div>'
        f'</div>',
        unsafe_allow_html=True,
    )

    if not current_filter_set:
        st.info("No MA filters selected. Screening will pass stocks through this tab.")

    rendered_filter_set = []
    filter_grid_columns = st.columns(2)

    for index, filter_item in enumerate(current_filter_set, start=1):
        filter_id = filter_item["id"]
        filter_type = filter_item["type"]
        # Start from the item's own saved params so that custom field values
        # stored in favourite_filter_sets are preserved on load.
        params = deepcopy(filter_item.get("params", {}))
        # Back-fill any missing keys from the type defaults.
        for k, v in FILTER_TYPE_DEFAULTS[filter_type].items():
            if k not in params:
                params[k] = deepcopy(v)

        filter_label = FILTER_TYPE_LABELS[filter_type]

        expander_label = f"{index}. {filter_label}"

        with filter_grid_columns[(index - 1) % 2]:
            filter_expander = st.expander(expander_label, expanded=False)

        with filter_expander:
            remove_filter = st.button(
                "❌ Remove Filter",
                key=f"{filter_widget_prefix}_remove_filter_{filter_id}_v{widget_key_version}",
            )
            if remove_filter:
                st.session_state["current_filter_set"] = [
                    item for item in current_filter_set if item["id"] != filter_id
                ]
                st.rerun()

            if filter_type == "ma_rising":
                params["ma"] = int(st.number_input(
                    "MA",
                    min_value=2,
                    max_value=1000,
                    value=int(params.get("ma", 200)),
                    key=f"{filter_widget_prefix}_{filter_id}_ma_v{widget_key_version}",
                ))

            elif filter_type == "short_above_long":
                col1, col2 = st.columns(2)
                with col1:
                    params["short_ma"] = int(st.number_input(
                        "Short MA",
                        min_value=2,
                        max_value=500,
                        value=int(params.get("short_ma", 50)),
                        key=f"{filter_widget_prefix}_{filter_id}_short_ma_v{widget_key_version}",
                    ))
                with col2:
                    params["long_ma"] = int(st.number_input(
                        "Long MA",
                        min_value=2,
                        max_value=1000,
                        value=int(params.get("long_ma", 200)),
                        key=f"{filter_widget_prefix}_{filter_id}_long_ma_v{widget_key_version}",
                    ))

            elif filter_type == "price_near_long":
                col1, col2 = st.columns(2)
                with col1:
                    params["long_ma"] = int(st.number_input(
                        "Long MA",
                        min_value=2,
                        max_value=1000,
                        value=int(params.get("long_ma", 200)),
                        key=f"{filter_widget_prefix}_{filter_id}_price_long_ma_v{widget_key_version}",
                    ))
                with col2:
                    params["threshold_pct"] = float(st.number_input(
                        "Within Percent",
                        min_value=0.1,
                        max_value=100.0,
                        value=float(params.get("threshold_pct", 5.0)),
                        step=0.1,
                        key=f"{filter_widget_prefix}_{filter_id}_threshold_pct_v{widget_key_version}",
                    ))

            elif filter_type == "golden_cross":
                col1, col2, col3 = st.columns(3)
                with col1:
                    params["short_ma"] = int(st.number_input(
                        "Short MA",
                        min_value=2,
                        max_value=500,
                        value=int(params.get("short_ma", 50)),
                        key=f"{filter_widget_prefix}_{filter_id}_golden_short_ma_v{widget_key_version}",
                    ))
                with col2:
                    params["long_ma"] = int(st.number_input(
                        "Long MA",
                        min_value=2,
                        max_value=1000,
                        value=int(params.get("long_ma", 200)),
                        key=f"{filter_widget_prefix}_{filter_id}_golden_long_ma_v{widget_key_version}",
                    ))
                with col3:
                    params["lookback_units"] = int(st.number_input(
                        "Last N Time Frame Units",
                        min_value=1,
                        max_value=1000,
                        value=int(params.get("lookback_units", 20)),
                        key=f"{filter_widget_prefix}_{filter_id}_golden_lookback_v{widget_key_version}",
                    ))

            elif filter_type == "long_ma_down_from_max":
                col1, col2, col3 = st.columns(3)
                with col1:
                    params["long_ma"] = int(st.number_input(
                        "Long MA",
                        min_value=2,
                        max_value=1000,
                        value=int(params.get("long_ma", 200)),
                        key=f"{filter_widget_prefix}_{filter_id}_down_long_ma_v{widget_key_version}",
                    ))
                with col2:
                    params["down_pct"] = float(st.number_input(
                        "Down Percent",
                        min_value=0.1,
                        max_value=100.0,
                        value=float(params.get("down_pct", 5.0)),
                        step=0.1,
                        key=f"{filter_widget_prefix}_{filter_id}_down_pct_v{widget_key_version}",
                    ))
                with col3:
                    params["lookback_units"] = int(st.number_input(
                        "Last M Time Frame Units",
                        min_value=2,
                        max_value=2000,
                        value=int(params.get("lookback_units", 50)),
                        key=f"{filter_widget_prefix}_{filter_id}_down_lookback_v{widget_key_version}",
                    ))

            elif filter_type == "long_ma_up_from_min":
                col1, col2, col3 = st.columns(3)
                with col1:
                    params["long_ma"] = int(st.number_input(
                        "Long MA",
                        min_value=2,
                        max_value=1000,
                        value=int(params.get("long_ma", 200)),
                        key=f"{filter_widget_prefix}_{filter_id}_up_long_ma_v{widget_key_version}",
                    ))
                with col2:
                    params["up_pct"] = float(st.number_input(
                        "Up Percent",
                        min_value=0.1,
                        max_value=100.0,
                        value=float(params.get("up_pct", 5.0)),
                        step=0.1,
                        key=f"{filter_widget_prefix}_{filter_id}_up_pct_v{widget_key_version}",
                    ))
                with col3:
                    params["lookback_units"] = int(st.number_input(
                        "Last M Time Frame Units",
                        min_value=2,
                        max_value=2000,
                        value=int(params.get("lookback_units", 50)),
                        key=f"{filter_widget_prefix}_{filter_id}_up_lookback_v{widget_key_version}",
                    ))

            elif filter_type == "hitting_all_time_high":
                col1, col2 = st.columns(2)
                with col1:
                    params["ts_lookback"] = int(st.number_input(
                        "TimeSpan Lookback",
                        min_value=2,
                        max_value=5000,
                        value=int(params.get("ts_lookback", 200)),
                        key=f"{filter_widget_prefix}_{filter_id}_ath_ts_lookback_v{widget_key_version}",
                        help="Number of previous data frames to search for the All-Time High.",
                    ))
                with col2:
                    params["recent_n"] = int(st.number_input(
                        "ATH Hit In Last N Frames",
                        min_value=1,
                        max_value=500,
                        value=int(params.get("recent_n", 10)),
                        key=f"{filter_widget_prefix}_{filter_id}_ath_recent_n_v{widget_key_version}",
                        help="Return True only if ATH was hit in any of the last N data frames.",
                    ))

            elif filter_type == "price_near_old_ath":
                col1, col2, col3 = st.columns(3)
                with col1:
                    params["n_bars"] = int(st.number_input(
                        "ATH Before N Time Frames",
                        min_value=1,
                        max_value=5000,
                        value=int(params.get("n_bars", 200)),
                        key=f"{filter_widget_prefix}_{filter_id}_old_ath_n_bars_v{widget_key_version}",
                        help="Search for ATH value excluding the most recent N time frames.",
                    ))
                with col2:
                    params["range_low"] = float(st.number_input(
                        "Range Low % (r₁)",
                        min_value=-100.0,
                        max_value=100.0,
                        value=float(params.get("range_low", -5.0)),
                        step=0.1,
                        key=f"{filter_widget_prefix}_{filter_id}_old_ath_range_low_v{widget_key_version}",
                        help="Lower bound %. e.g. -4 means price can be 4% below old ATH.",
                    ))
                with col3:
                    params["range_high"] = float(st.number_input(
                        "Range High % (r₂)",
                        min_value=-100.0,
                        max_value=500.0,
                        value=float(params.get("range_high", 10.0)),
                        step=0.1,
                        key=f"{filter_widget_prefix}_{filter_id}_old_ath_range_high_v{widget_key_version}",
                        help="Upper bound %. e.g. +10 means price can be 10% above old ATH.",
                    ))

            elif filter_type == "pe_less_than":
                params["max_pe"] = float(st.number_input(
                    "PE Less Than",
                    min_value=0.1,
                    max_value=500.0,
                    value=float(params.get("max_pe", 30.0)),
                    step=0.1,
                    key=f"{filter_widget_prefix}_{filter_id}_max_pe_v{widget_key_version}",
                ))

            elif filter_type == "green_candle_today":
                params["min_gain_pct"] = float(st.number_input(
                    "Minimum Gain Percent",
                    min_value=0.0,
                    max_value=100.0,
                    value=float(params.get("min_gain_pct", 1.0)),
                    step=0.1,
                    key=f"{filter_widget_prefix}_{filter_id}_green_min_gain_pct_v{widget_key_version}",
                ))

        rendered_filter_set.append({
            "id": filter_id,
            "type": filter_type,
            "params": params,
        })

    st.session_state["current_filter_set"] = rendered_filter_set
    filter_set = normalize_filter_set(rendered_filter_set, use_default=False)
    active_filter_count = len(filter_set)

    update_settings({
        "screener_filter_set": filter_set,
    })

    # ===== Expression Based Filtering =====
    initialize_pattern_expression_state()

    pattern_lookback_days = int(
        st.session_state.get("pattern_lookback_days_number", settings.get("pattern_lookback_days", 120))
    )
    pattern_reversal_pct = float(
        st.session_state.get("pattern_reversal_pct_number", settings.get("pattern_reversal_pct", 5.0))
    )
    pattern_expression_filters = st.session_state["pattern_expression_filters"]

    st.markdown(
        '<div class="screener-section-heading">'
        '<div class="screener-section-heading__title">📝 Expression Filters</div>'
        '</div>'
        '<p class="screener-section-copy">Build formulas with price, PE, moving averages, crosses, and MA statistics.</p>',
        unsafe_allow_html=True,
    )

    if pattern_expression_filters:
        expression_col, reference_col = st.columns([1.35, 1])
        with expression_col:
            expression_panel = st.container(border=True)
    else:
        expression_panel = st.container(border=True)

    with expression_panel:
        st.markdown(
            '<div class="data-panel-heading"><span>📝</span>Expressions</div>'
            '<p class="data-panel-subtitle">Every expression must evaluate to true for a stock to match.</p>',
            unsafe_allow_html=True,
        )
        add_expression = st.button(
            "➕ Add Expression",
            key="add_pattern_filter",
            use_container_width=True,
        )
    if add_expression:
        st.session_state["pattern_expression_filters"].append({
            "id": st.session_state["next_pattern_expression_id"],
            "expression": "",
        })
        st.session_state["next_pattern_expression_id"] += 1
        st.rerun()

    valid_pattern_expressions = []
    invalid_pattern_errors = []

    if not pattern_expression_filters:
        with expression_panel:
            st.info("No expressions selected. Click Add Expression to build a custom rule.")
    else:
        with reference_col:
            with st.container(border=True):
                st.markdown(
                    '<div class="data-panel-heading"><span>⌨️</span>Allowed Keywords</div>'
                    '<p class="data-panel-subtitle">Tap or click any keyword to see what it means.</p>'
                    + expression_keyword_reference_html(),
                    unsafe_allow_html=True,
                )

    for index, expression_filter in enumerate(pattern_expression_filters, start=1):
        filter_id = expression_filter["id"]
        with expression_panel:
            col1, col2 = st.columns([5, 1])
        with col1:
            expression = st.text_input(
                f"Expression {index}",
                value=expression_filter.get("expression", ""),
                key=f"pattern_expression_{filter_id}",
                placeholder="e.g. P > SMA200 and ROI(50) > 0",
                help="Use the allowed-keyword reference shown beside this form.",
            )
        with col2:
            remove_expression = st.button(
                "❌ Remove",
                key=f"remove_pattern_expression_{filter_id}",
            )

        if remove_expression:
            st.session_state["pattern_expression_filters"] = [
                item for item in pattern_expression_filters if item["id"] != filter_id
            ]
            st.rerun()

        expression_filter["expression"] = expression
        if not expression.strip():
            with expression_panel:
                st.info("Blank expression ignored.")
            continue

        is_valid, error = validate_expression(expression)
        if is_valid:
            with expression_panel:
                st.success("✅ Valid expression")
            valid_pattern_expressions.append(expression.strip())
        else:
            with expression_panel:
                st.error(f"❌ {error}")
            invalid_pattern_errors.append(f"Expression {index}: {error}")

    update_settings({
        "pattern_lookback_days": pattern_lookback_days,
        "pattern_reversal_pct": pattern_reversal_pct,
        "pattern_expressions": [
            item.get("expression", "")
            for item in st.session_state["pattern_expression_filters"]
        ],
    })

    # ===== Favorite Filter Management =====
    st.markdown(
        '<div class="screener-section-heading">'
        '<div class="screener-section-heading__title">⭐ Favorite Sets</div>'
        '</div>'
        '<p class="screener-section-copy">Save the current setup for reuse or remove a set you no longer need.</p>',
        unsafe_allow_html=True,
    )
    save_col, remove_col = st.columns(2)
    with save_col:
        with st.container(border=True):
            st.markdown(
                '<div class="data-panel-heading"><span>💾</span>Save Current Set</div>'
                '<p class="data-panel-subtitle">Store all current MA and expression filters under a memorable name.</p>',
                unsafe_allow_html=True,
            )
            favorite_name = st.text_input(
                "Favorite Filter Name",
                key="_favorite_name_to_save",
                placeholder="e.g. Golden Cross + PE < 30",
            )
            save_fav = st.button(
                "⭐ Add To Favorites",
                type="primary",
                use_container_width=True,
            )

    delete_fav = False
    del_favorite_name = None
    with remove_col:
        with st.container(border=True):
            st.markdown(
                '<div class="data-panel-heading"><span>🗑️</span>Remove Saved Set</div>'
                '<p class="data-panel-subtitle">Delete a saved set without changing the filters currently on screen.</p>',
                unsafe_allow_html=True,
            )
            if favorite_filter_sets:
                del_favorite_name = st.selectbox(
                    "Favorite Filter Set",
                    sorted(favorite_filter_sets.keys()),
                    key="delete_favorite_select",
                )
                delete_fav = st.button(
                    "Remove Favorite",
                    type="primary",
                    use_container_width=True,
                )
            else:
                st.info("No saved favorite sets yet.")

    if save_fav:
        clean_name = favorite_name.strip()
        if not clean_name:
            st.error("Enter a favorite filter name before saving.")
        else:
            favorite_filter_sets[clean_name] = {
                "ma_filter_set": filter_set,
                "pattern": {
                    "lookback_days": pattern_lookback_days,
                    "reversal_pct": pattern_reversal_pct,
                    "expressions": [
                        item.get("expression", "")
                        for item in st.session_state["pattern_expression_filters"]
                    ],
                },
            }
            save_favourite_filter_sets(favorite_filter_sets)
            update_settings({"selected_favorite_filter_set": clean_name})
            st.session_state.pop("_favorite_select_widget", None)
            st.success(f"⭐ Saved favorite filters: {clean_name}")
            st.rerun()

    if delete_fav and del_favorite_name in favorite_filter_sets:
        del favorite_filter_sets[del_favorite_name]
        save_favourite_filter_sets(favorite_filter_sets)
        if settings.get("selected_favorite_filter_set") == del_favorite_name:
            update_settings({"selected_favorite_filter_set": "Current Filters"})
        st.session_state.pop("_favorite_select_widget", None)
        st.success(f"🗑️ Removed favorite: {del_favorite_name}")
        st.rerun()

    # ===== RUN SCREENER LOGIC =====
    if run_combined:
        run_filter_set = list(filter_set)  # shallow copy so we can inject green_candle_today
        run_lookback_days = pattern_lookback_days
        run_reversal_pct = pattern_reversal_pct
        run_pattern_expressions = valid_pattern_expressions
        run_invalid_pattern_errors = invalid_pattern_errors

        # Inject green_candle_today filter from toggle
        if green_candle_toggle:
            next_id = max((int(item.get("id", 0)) for item in run_filter_set), default=0) + 1
            run_filter_set.append({
                "id": next_id,
                "type": "green_candle_today",
                "params": {"min_gain_pct": green_candle_min_gain_pct},
            })

        if run_invalid_pattern_errors:
            st.error("Fix invalid expressions before running the screener.")
            st.stop()

        for filter_item in run_filter_set:
            params = filter_item["params"]
            label = FILTER_TYPE_LABELS.get(filter_item["type"], filter_item["type"])
            if filter_item["type"] in {"short_above_long", "golden_cross"} and params["short_ma"] >= params["long_ma"]:
                st.error(f"Short MA must be less than Long MA in: {label}.")
                st.stop()

        target_dir = timeframe_config(tf, current_market)["target_dir"]
        stock_files = stock_data_files(target_dir)

        active_job = drain_live_screener_events()
        if active_job and active_job.get("running"):
            st.warning("A screener run is already in progress. Open the Results tab to watch live matches.")
            st.session_state["switch_to_results_tab"] = True
            st.rerun()

        st.session_state["results"] = []
        update_settings({"last_results_market": current_market})
        st.session_state["screener_job"] = start_live_screener_job(
            stock_files,
            run_filter_set,
            current_market,
            run_lookback_days,
            run_reversal_pct,
            run_pattern_expressions,
            create_charts,
        )
        st.session_state["switch_to_results_tab"] = True
        st.rerun()


# =====================================================================
# TAB 3: BACKTEST
# =====================================================================
with tab3:
    st.header("Backtest")
    current_market = normalize_market(selected_market)
    st.caption(f"Market: {market_label(current_market)}")

    favorite_names = sorted(favorite_filter_sets.keys())
    if not favorite_names:
        st.info("No saved favorite filters yet. Save filters from the Screener tab before running a backtest.")
    else:
        backtest_tf = "DAY"

        target_dir = timeframe_config(backtest_tf, current_market)["target_dir"]
        stock_files = stock_data_files(target_dir)
        benchmark_file = target_dir / f"{NIFTY_DATA_SYMBOL}.json" if current_market == MARKET_INDIA else None
        available_dates = cached_backtest_calendar_dates(stock_file_signatures(stock_files))

        selected_start_date = None
        selected_end_date = None
        effective_start_date = None
        effective_end_date = None
        if not stock_files:
            st.warning(f"No downloaded {backtest_tf.lower()} data found. Download stock data first from the Data tab.")
        elif len(available_dates) < 2:
            st.warning(f"Not enough {backtest_tf.lower()} candles found for backtesting.")
        else:
            min_date = available_dates[0]
            max_date = available_dates[-1]

            saved_start = pd.to_datetime(settings.get("backtest_start_date"), errors="coerce")
            saved_end = pd.to_datetime(settings.get("backtest_end_date"), errors="coerce")
            default_start = (
                saved_start.date()
                if pd.notna(saved_start) and min_date <= saved_start.date() < max_date
                else available_dates[max(0, len(available_dates) - 31)]
            )
            default_end = (
                saved_end.date()
                if pd.notna(saved_end) and default_start < saved_end.date() <= max_date
                else max_date
            )

            selected_start_date, selected_end_date = st.slider(
                "Backtest date range",
                min_value=min_date,
                max_value=max_date,
                value=(default_start, default_end),
                format="DD-MM-YYYY",
                help="Find stocks on the start date, then calculate the equal-weight portfolio gain through the end date.",
            )

            start_candidates = [date for date in available_dates if date >= selected_start_date]
            end_candidates = [date for date in available_dates if date <= selected_end_date]
            effective_start_date = start_candidates[0] if start_candidates else None
            effective_end_date = end_candidates[-1] if end_candidates else None

            if effective_start_date != selected_start_date or effective_end_date != selected_end_date:
                st.caption(
                    "Using nearest available market dates: "
                    f"{effective_start_date.strftime('%d-%m-%Y')} to {effective_end_date.strftime('%d-%m-%Y')}"
                )

        saved_backtest_filters = [
            name for name in settings.get("backtest_selected_filters", favorite_names[:1])
            if name in favorite_names
        ]
        selected_backtest_filters = st.multiselect(
            "Favorite filters",
            favorite_names,
            default=saved_backtest_filters or favorite_names[:1],
            help="Select one or more saved favorite filter sets to compare.",
        )

        update_settings({
            "backtest_tf": backtest_tf,
            "backtest_start_date": selected_start_date.isoformat() if selected_start_date else None,
            "backtest_end_date": selected_end_date.isoformat() if selected_end_date else None,
            "backtest_selected_filters": selected_backtest_filters,
        })

        run_backtest_clicked = st.button("Backtest", type="primary", use_container_width=True)

        if run_backtest_clicked:
            if not selected_backtest_filters:
                st.error("Select at least one favorite filter.")
            elif not stock_files:
                st.error("No stock data available for the selected timeframe.")
            elif not effective_start_date or not effective_end_date or effective_start_date >= effective_end_date:
                st.error("Select a valid start date before the end date.")
            else:
                progress_bar = st.progress(0)
                progress_text = st.empty()
                nifty_download_row = None

                def show_backtest_progress(done, total):
                    progress = done / total if total else 0
                    progress_bar.progress(progress)
                    progress_text.info(
                        f"Processed {done} of {total} stocks across "
                        f"{len(selected_backtest_filters)} favorite filter(s)."
                    )

                with st.spinner("Running backtest across saved filters and selected dates..."):
                    if benchmark_file is not None and not benchmark_file.exists():
                        progress_text.info("Downloading Nifty 50 benchmark data for this timeframe...")
                        nifty_download_row = download_nifty_index(backtest_tf, market=current_market)
                    summary_rows, series_by_filter, stock_details_by_filter = run_backtest(
                        stock_files,
                        favorite_filter_sets,
                        selected_backtest_filters,
                        effective_start_date,
                        effective_end_date,
                        progress_callback=show_backtest_progress,
                        benchmark_file=benchmark_file,
                        market=current_market,
                    )
                    stock_details_by_filter = attach_backtest_chart_paths(
                        stock_details_by_filter,
                        stock_files,
                        favorite_filter_sets,
                        start_date=effective_start_date,
                        end_date=effective_end_date,
                    )
                progress_bar.progress(1.0)
                match_summary = ", ".join(
                    f"{row['Filter Name']}: {int(row.get('Stocks Found', 0))}"
                    for row in summary_rows
                )
                progress_text.success(
                    f"Backtest complete. Processed {len(stock_files)} stocks. "
                    f"Stocks found on start date: {match_summary or 'none'}."
                )
                if current_market == MARKET_INDIA and "Nifty 50" not in series_by_filter:
                    if nifty_download_row and not nifty_download_row["Downloaded"]:
                        st.warning(
                            "Nifty 50 benchmark could not be downloaded, so it was not added to the chart. "
                            f"Reason: {nifty_download_row['Error'] or 'No data returned'}"
                        )
                    else:
                        st.warning(
                            "Nifty 50 benchmark data is not available for the selected date range, "
                            "so it was not added to the chart."
                        )
                st.session_state["backtest_summary_rows"] = summary_rows
                st.session_state["backtest_series_by_filter"] = series_by_filter
                st.session_state["backtest_stock_details_by_filter"] = stock_details_by_filter
                st.session_state["backtest_result_range"] = (
                    effective_start_date.strftime("%d-%m-%Y"),
                    effective_end_date.strftime("%d-%m-%Y"),
                )

        summary_rows = st.session_state.get("backtest_summary_rows", [])
        series_by_filter = st.session_state.get("backtest_series_by_filter", {})
        stock_details_by_filter = st.session_state.get("backtest_stock_details_by_filter", {})
        if summary_rows:
            result_start, result_end = st.session_state.get("backtest_result_range", ("start date", "end date"))
            st.info(
                f"Showing equal-weight portfolio variation for stocks found on {result_start} through {result_end}."
            )
            render_backtest_results_table(summary_rows, series_by_filter, stock_details_by_filter)


# =====================================================================
# TAB 4: RESULTS
# =====================================================================
with tab4:
    st.header("📊 Results")

    fundamentals_retry_notice = st.session_state.pop(
        "_fundamentals_retry_notice",
        None,
    )
    if fundamentals_retry_notice:
        notice_type, notice_message = fundamentals_retry_notice
        if notice_type == "success":
            st.success(notice_message)
        else:
            st.warning(notice_message)

    live_screener_job = drain_live_screener_events()

    # Load persisted results if session state is empty
    if "results" not in st.session_state:
        st.session_state["results"] = load_results()

    rows = st.session_state.get("results", [])
    if live_screener_job:
        total = live_screener_job.get("total", 0)
        done = live_screener_job.get("done", 0)
        matches = live_screener_job.get("matches", len(rows))
        max_workers = live_screener_job.get("max_workers", 1)
        progress = done / total if total else 0
        st.progress(progress)
        if live_screener_job.get("running"):
            st.info(
                f"Screening live with {max_workers} workers: {done}/{total} processed, "
                f"{matches} match(es) streamed so far."
            )
        elif live_screener_job.get("error"):
            st.error(f"Screener stopped: {live_screener_job['error']}")
        else:
            failed_count = live_screener_job.get("failed_count", 0)
            st.success(f"Screening complete: {done}/{total} processed, {matches} match(es) found.")
            if failed_count:
                st.warning(f"{failed_count} stock file(s) were skipped due to errors.")

    if rows:
        result_market_for_repair = normalize_market(settings.get("last_results_market", selected_market))
        result_timeframe_for_repair = "DAY"
        repair_filter_set = normalize_filter_set(
            settings.get("screener_filter_set", st.session_state.get("current_filter_set", [])),
            use_default=False,
        )
        if repair_blank_result_charts(rows, repair_filter_set, result_market_for_repair, result_timeframe_for_repair):
            st.session_state["results"] = rows
            save_results(rows)

    if rows:
        # Determine heading: favorite filter name or "Custom Filter"
        selected_filter_name = settings.get("selected_favorite_filter_set", "Current Filters")
        if selected_filter_name and selected_filter_name != "Current Filters":
            heading_label = selected_filter_name
        else:
            heading_label = "Custom Filter"

        result_market = normalize_market(settings.get("last_results_market", selected_market))
        st.info(
            f"📌 Showing last screener run results — {len(rows)} stock(s) matched | "
            f"**{heading_label}** | **{market_label(result_market)}**"
        )
        if result_market != normalize_market(selected_market):
            st.warning(
                f"These results are from {market_label(result_market)}. "
                f"Run the screener again to refresh results for {market_label(selected_market)}."
            )

        df = pd.DataFrame(rows)
        df.index = range(1, len(df) + 1)
        display_df = df

        # Base columns that always appear (if present)
        result_columns = ["Symbol", "PE Ratio"]

        # Insert DiffSMA* columns (signed % diff from price to each MA) right after PE Ratio
        diff_ma_cols = sorted(
            [col for col in display_df.columns if col.startswith("DiffSMA")],
            key=lambda c: int(c.replace("DiffSMA", "")),
        )
        result_columns.extend(diff_ma_cols)

        # Insert RocSMA* columns (Rate of Change of MA from 2 bars back)
        roc_ma_cols = sorted(
            [col for col in display_df.columns if col.startswith("RocSMA")],
            key=lambda c: int(c.replace("RocSMA", "")),
        )
        result_columns.extend(roc_ma_cols)

        display_df = display_df[[column for column in result_columns if column in display_df.columns]]

        table_df = display_df.copy()
        if "GrowthMetrics" in df.columns:
            table_df["GrowthMetrics"] = df["GrowthMetrics"]
        if "ValuationMedians" in df.columns:
            table_df["ValuationMedians"] = df["ValuationMedians"]
        if "FundamentalsRefreshToken" in df.columns:
            table_df["FundamentalsRefreshToken"] = df["FundamentalsRefreshToken"]

        if "ChartPath" in df.columns:
            chart_df = table_df.copy()
            chart_df["ChartPath"] = df["ChartPath"]
            if "ChartSource" in df.columns:
                chart_df["ChartSource"] = df["ChartSource"]
            sortable_results_table(
                chart_df,
                interactive_market=result_market,
                interactive_ma_periods=required_ma_periods(repair_filter_set),
            )
        else:
            sortable_results_table(
                table_df,
                interactive_market=result_market,
                interactive_ma_periods=required_ma_periods(repair_filter_set),
            )

    else:
        if live_screener_job and live_screener_job.get("running"):
            st.info("Waiting for the first matching stock. Results will appear here automatically.")
        else:
            st.info("No results yet. Run the screener from the 'Screener' tab to see results here.")

    if live_screener_job and live_screener_job.get("running"):
        st.session_state["switch_to_results_tab"] = True
        time.sleep(0.75)
        st.rerun()
