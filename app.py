import json
import html
import hmac
import importlib
import inspect
import os
import queue
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

import charting as charting_module
from chart_context import (
    chart_alert_context,
    interactive_chart_query,
)
from backtest import (
    get_backtest_calendar_dates,
    run_backtest,
    split_favorite_filter,
    validate_sell_price_expression,
)
from config import *
from charting import (
    create_stock_chart,
    image_to_data_uri,
    render_interactive_stock_chart,
    sortable_results_table,
)

if (
    getattr(charting_module, "RESULTS_TABLE_RENDERER_VERSION", 0) < 9
    or "row_actions" not in inspect.signature(
        sortable_results_table
    ).parameters
):
    charting_module = importlib.reload(charting_module)
    sortable_results_table = charting_module.sortable_results_table
from cloud_storage import CloudStorageError, cloud_storage_from_config
from downloader import (
    MARKET_INDIA,
    MARKET_US,
    NIFTY_DATA_SYMBOL,
    background_download_snapshot,
    clear_downloaded_json_files,
    data_availability_summary,
    download_nifty_index,
    download_top_stocks,
    load_top_symbols,
    market_label,
    normalize_market,
    start_background_download,
    stock_files_for_symbols,
    timeframe_config,
)
from fundamentals import (
    get_company_fundamentals,
)
from market_snapshots import (
    hydrate_result_valuations,
    latest_monthly_pe_values,
)
from pattern import evaluate_pattern_filters_from_df, validate_expression
from price_alerts import (
    acknowledge_price_alerts,
    configure_cloud_alerts,
    create_price_alert,
    load_price_alerts,
    remove_price_alerts,
    set_current_alert_user,
    sort_price_alerts,
)
from screener import (
    DEFAULT_FILTER_SET,
    FILTER_TYPE_DEFAULTS,
    FILTER_TYPE_LABELS,
    custom_filter_expressions,
    filter_set_requires_pe,
    load_price_dataframe,
    merge_legacy_expression_filters,
    normalize_filter_set,
    price_near_ma_periods,
    required_ma_periods,
    screen_dataframe,
)
from storage import (
    configure_user_storage,
    load_favourite_filter_sets,
    load_pe_ratios,
    load_settings,
    save_favourite_filter_sets,
    update_settings,
)
from stock_data import (
    list_symbol_paths,
    stock_exists,
    symbol_from_path,
    symbol_path,
)
from user_auth import current_user, render_workspace_account_controls

st.set_page_config(layout="wide", page_title="NSE Stock Screener", page_icon="📈")

_APP_CHART_EVENT_COMPONENT = components.declare_component(
    "app_chart_events",
    path=str(Path(__file__).parent / "alert_table_component"),
)

shared_settings = load_settings()
shared_favorite_filter_sets = load_favourite_filter_sets()
if not shared_favorite_filter_sets and shared_settings.get("favorite_filter_sets"):
    shared_favorite_filter_sets = shared_settings["favorite_filter_sets"]
    save_favourite_filter_sets(shared_favorite_filter_sets)

cloud_store = cloud_storage_from_config(st)
app_user = current_user(st)
configure_user_storage(cloud_store, app_user.id if app_user else None)
configure_cloud_alerts(cloud_store, require_auth=True)
set_current_alert_user(app_user.id if app_user else None)

# Favorite callbacks set all required filter state before Streamlit reruns.
# Reuse the session copies for that rerun instead of blocking on Supabase.
fast_favorite_selection = st.session_state.pop(
    "_fast_favorite_selection",
    False,
)

cloud_startup_error = ""
try:
    if (
        fast_favorite_selection
        and "_cached_settings" in st.session_state
        and "_cached_personal_filter_sets" in st.session_state
    ):
        settings = deepcopy(st.session_state["_cached_settings"])
        personal_filter_sets = deepcopy(
            st.session_state["_cached_personal_filter_sets"]
        )
    else:
        settings = load_settings()
        personal_filter_sets = (
            cloud_store.load_filter_sets(app_user.id)
            if cloud_store is not None and app_user is not None
            else {}
        )
        st.session_state["_cached_settings"] = deepcopy(settings)
        st.session_state["_cached_personal_filter_sets"] = deepcopy(
            personal_filter_sets
        )
except CloudStorageError as exc:
    settings = dict(shared_settings)
    personal_filter_sets = {}
    cloud_startup_error = str(exc)


def update_changed_settings(values):
    """Avoid remote writes when a rerun has not changed persisted values."""
    changed = {
        key: value
        for key, value in values.items()
        if settings.get(key) != value
    }
    if not changed:
        return settings
    updated = update_settings(changed)
    if isinstance(updated, dict):
        settings.update(updated)
    else:
        settings.update(changed)
    st.session_state["_cached_settings"] = deepcopy(settings)
    return settings


def personal_favorite_display_name(name):
    """Keep shared names stable and disambiguate only a colliding personal name."""
    return f"{name} (My)" if name in shared_favorite_filter_sets else name


def favorite_option_label(display_name):
    if display_name in personal_favorite_keys:
        return f"My · {personal_favorite_keys[display_name]}"
    return f"Shared · {display_name}"


personal_favorite_keys = {
    personal_favorite_display_name(name): name
    for name in personal_filter_sets
}
favorite_filter_sets = dict(shared_favorite_filter_sets)
for display_name, stored_name in personal_favorite_keys.items():
    favorite_filter_sets[display_name] = personal_filter_sets[stored_name]

if cloud_startup_error:
    st.error(cloud_startup_error)


def render_login_prompt(message, key, error=False):
    """Explain an account-only action and provide an immediate Google login."""
    if error:
        st.error(message)
    else:
        st.info(message)
    st.button(
        "Continue with Google",
        key=key,
        type="primary",
        on_click=st.login,
    )


def query_param_value(name, default=None):
    value = st.query_params.get(name, default)
    if isinstance(value, list):
        return value[0] if value else default
    return value


def session_price_alerts(max_age_seconds=60):
    """Load this user's alerts once per short Streamlit session window."""
    cached_alerts = st.session_state.get("_cached_price_alerts")
    cached_at = float(
        st.session_state.get("_cached_price_alerts_at", 0.0) or 0.0
    )
    cache_is_fresh = (
        cached_alerts is not None
        and (time.monotonic() - cached_at) < max_age_seconds
    )
    if cache_is_fresh:
        return deepcopy(cached_alerts)
    fresh_alerts = load_price_alerts()
    st.session_state["_cached_price_alerts"] = deepcopy(fresh_alerts)
    st.session_state["_cached_price_alerts_at"] = time.monotonic()
    return fresh_alerts


def process_price_alert_request():
    alert_action = str(
        query_param_value("alert_action", "") or ""
    ).strip().lower()
    if alert_action in {"acknowledge", "remove"}:
        alert_id = str(query_param_value("alert_id", "") or "").strip()
        try:
            if not alert_id:
                raise ValueError("The selected alert is unavailable.")
            if alert_action == "acknowledge":
                changed = acknowledge_price_alerts([alert_id])
                message = f"Acknowledged {changed} price alert(s)."
            else:
                changed = remove_price_alerts([alert_id])
                message = f"Removed {changed} price alert(s)."
            st.session_state["price_alert_feedback"] = ("success", message)
            st.session_state.pop("_cached_price_alerts", None)
            st.session_state.pop("_cached_price_alerts_at", None)
        except PermissionError as exc:
            st.session_state["price_alert_feedback"] = ("error", str(exc))
            st.session_state["price_alert_login_required"] = True
        except (TypeError, ValueError, OSError, RuntimeError) as exc:
            action_label = (
                "acknowledge" if alert_action == "acknowledge" else "remove"
            )
            st.session_state["price_alert_feedback"] = (
                "error",
                f"Could not {action_label} alert: {exc}",
            )
        st.session_state["switch_to_alerts_tab"] = True
        st.query_params.clear()
        st.rerun()

    requested = str(query_param_value("create_price_alert", "") or "").lower()
    if requested not in {"1", "true", "yes"}:
        return
    symbol = str(query_param_value("alert_symbol", "") or "").strip()
    market = normalize_market(query_param_value("alert_market", MARKET_INDIA))
    target_price = query_param_value("alert_price", "")
    try:
        alert, created = create_price_alert(symbol, market, target_price)
        direction = "above" if alert["direction"] == "above" else "below"
        if created:
            message = f"Alert created for {symbol}: cross {direction} {alert['target_price']:g}."
        else:
            message = f"That {symbol} price alert already exists. No duplicate was added."
        st.session_state["price_alert_feedback"] = ("success", message)
        st.session_state.pop("_cached_price_alerts", None)
        st.session_state.pop("_cached_price_alerts_at", None)
    except PermissionError as exc:
        st.session_state["price_alert_feedback"] = ("error", str(exc))
        st.session_state["price_alert_login_required"] = True
    except (TypeError, ValueError, OSError, RuntimeError) as exc:
        st.session_state["price_alert_feedback"] = ("error", f"Could not create alert: {exc}")
    st.session_state["switch_to_alerts_tab"] = True
    st.query_params.clear()
    st.rerun()


def persist_backtest_widget_settings():
    """Persist only Backtest controls that currently exist in session state."""
    updates = {"backtest_tf": "DAY"}
    date_range = st.session_state.get("backtest_date_range_input")
    if isinstance(date_range, (tuple, list)) and len(date_range) == 2:
        updates["backtest_start_date"] = date_range[0].isoformat()
        updates["backtest_end_date"] = date_range[1].isoformat()

    key_map = {
        "backtest_selected_filters_input": "backtest_selected_filters",
        "backtest_target_expression_input": "backtest_target_expression",
        "backtest_stop_loss_expression_input": "backtest_stop_loss_expression",
        "backtest_closing_basis_input": "backtest_closing_basis",
        "backtest_green_candle_only_input": "backtest_green_candle_only",
    }
    for session_key, settings_key in key_map.items():
        if session_key in st.session_state:
            updates[settings_key] = st.session_state[session_key]
    update_settings(updates)


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


@st.cache_data(show_spinner=False)
def cached_symbols_for_market(market, symbols_file, file_mtime_ns):
    del file_mtime_ns
    return tuple(
        load_top_symbols(
            Path(symbols_file),
            limit=1_000_000,
            market=normalize_market(market),
        )
    )


def available_symbols_for_market(market):
    symbols_file = symbols_file_for_market(market)
    if not symbols_file.exists():
        return ()
    return cached_symbols_for_market(
        normalize_market(market),
        str(symbols_file),
        symbols_file.stat().st_mtime_ns,
    )


def download_limit_for_market(market, symbols_file):
    """Return the complete source-file universe for scheduled downloads."""
    market = normalize_market(market)
    if not symbols_file.exists():
        return 0
    # Cron must not inherit the smaller interactive slider value. Use every
    # unique valid symbol present in the selected market's source file.
    return len(load_top_symbols(symbols_file, limit=1_000_000, market=market))


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
            "Universe": limit,
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


process_price_alert_request()

if str(query_param_value("scheduled_download", "") or "").lower() in {"1", "true", "yes"} or str(query_param_value("ping", "") or "").lower() in {"1", "true", "yes"}:
    run_scheduled_download()


def run_interactive_chart_view():
    symbol = str(query_param_value("interactive_chart", "") or "").strip()
    market = normalize_market(query_param_value("market", settings.get("last_results_market", MARKET_INDIA)))
    embedded = str(query_param_value("embedded", "") or "").lower() in {"1", "true", "yes"}
    try:
        requested_embed_height = int(query_param_value("embed_height", 0) or 0)
    except (TypeError, ValueError):
        requested_embed_height = 0
    compact_landscape = str(
        query_param_value("compact_landscape", "") or ""
    ).lower() in {"1", "true", "yes"}
    embedded_chart_height = (
        max(240 if compact_landscape else 420, min(1400, requested_embed_height))
        if embedded and requested_embed_height
        else (1060 if embedded else 920)
    )
    if not symbol or Path(symbol).name != symbol:
        st.error("Invalid stock symbol.")
        st.stop()

    target_dir = timeframe_config("DAY", market)["target_dir"].resolve()
    stock_file = symbol_path(target_dir, symbol).resolve()
    if stock_file.parent != target_dir or not stock_exists(stock_file):
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
    trade_overlay = {
        "buyDate": query_param_value("buy_date", None),
        "exitDate": query_param_value("exit_date", None),
        "windowStart": query_param_value("window_start", None),
        "windowEnd": query_param_value("window_end", None),
        "buyPrice": query_param_value("buy_price", None),
        "targetPrice": query_param_value("target_price", None),
        "stopPrice": query_param_value("stop_price", None),
        "exitPrice": query_param_value("exit_price", None),
        "exitReason": query_param_value("exit_reason", None),
        "alertDate": query_param_value("alert_date", None),
        "alertPrice": query_param_value("alert_marker_price", None),
    }
    trade_overlay, alert_markers = chart_alert_context(
        session_price_alerts(),
        symbol,
        market,
        trade_overlay,
    )
    growth_metrics, valuation_medians = get_company_fundamentals(symbol, market)
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
    chart_watchlists = None
    if app_user is not None and cloud_store is not None:
        if "_cached_personal_watchlists" in st.session_state:
            chart_watchlists = deepcopy(
                st.session_state["_cached_personal_watchlists"]
            )
        else:
            try:
                chart_watchlists = cloud_store.load_watchlists(app_user.id)
            except CloudStorageError as exc:
                st.error(str(exc))
                chart_watchlists = []
            else:
                st.session_state["_cached_personal_watchlists"] = deepcopy(
                    chart_watchlists
                )

    def add_chart_stock_to_watchlist(event):
        watchlist_id = str(event.get("watchlistId", "") or "")
        selected_watchlist = next(
            (
                watchlist
                for watchlist in (chart_watchlists or [])
                if str(watchlist.get("id", "")) == watchlist_id
            ),
            None,
        )
        event_symbol = str(event.get("symbol", "") or "").strip().upper()
        event_market = normalize_market(event.get("market", market))
        if (
            selected_watchlist is None
            or event_symbol != symbol.upper()
            or event_market != market
        ):
            st.error("The selected watchlist or stock is no longer available.")
            return
        existing_items = selected_watchlist.get("items", [])
        already_saved = any(
            str(item.get("symbol", "")).strip().upper() == event_symbol
            and normalize_market(item.get("market", MARKET_INDIA))
            == event_market
            for item in existing_items
        )
        if already_saved:
            st.toast(
                f"{event_symbol} is already in {selected_watchlist['name']}."
            )
            return
        try:
            cloud_store.save_watchlist_item(
                app_user.id,
                watchlist_id,
                event_symbol,
                event_market,
                "",
                len(existing_items),
            )
        except CloudStorageError as exc:
            st.error(str(exc))
            return
        selected_watchlist.setdefault("items", []).append({
            "symbol": event_symbol,
            "market": event_market,
            "note": "",
            "position": len(existing_items),
        })
        st.session_state["_cached_personal_watchlists"] = deepcopy(
            chart_watchlists
        )
        st.toast(
            f"Added {event_symbol} to {selected_watchlist['name']}.",
            icon="⭐",
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
            trade_overlay=trade_overlay,
            alert_markers=alert_markers,
            alert_market=market,
            height=embedded_chart_height,
            watchlists=chart_watchlists,
            watchlist_add_callback=add_chart_stock_to_watchlist,
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        st.error(f"Unable to prepare the interactive chart: {exc}")
    st.stop()


# Interactive-chart URLs from older bookmarks are converted into the shared
# Chart workspace after the main tab navigation is initialized below.

# ---- Inject custom CSS ----
st.markdown(
    """
    <style>
    section[data-testid="stSidebar"],
    button[data-testid="stSidebarCollapsedControl"],
    [data-testid="collapsedControl"] {
        display: none !important;
    }
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
        --primary-nav-top: calc(3.65rem + env(safe-area-inset-top, 0px));
        --primary-nav-height: calc(3.15rem + 0.96rem + 2px);
        --primary-nav-clearance: 0.35rem;
    }
    .results-run-heading {
        margin: 0.4rem 0 1rem;
        padding: 1rem 1.1rem;
        border: 1px solid #d8e6ee;
        border-left: 5px solid #176b87;
        border-radius: 12px;
        background: linear-gradient(135deg, #ffffff, #f2f9fb);
        box-shadow: var(--shadow-sm);
    }
    .results-run-heading h3 { margin: 0 0 0.55rem; color: var(--ink-strong); }
    .results-run-heading__metrics { display: flex; flex-wrap: wrap; gap: 0.5rem 1.4rem; color: var(--ink); }
    .results-run-heading details { margin-top: 0.65rem; color: var(--ink); }
    .results-run-heading ul { margin-bottom: 0; }

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
        padding-top: calc(
            var(--primary-nav-top)
            + var(--primary-nav-height)
            + var(--primary-nav-clearance)
        );
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

    /* Tab-level context banners */
    [class*="st-key-workspace_banner_shell_"] {
        --banner-accent: #176b87;
        --banner-soft: #e9f6f8;
        --banner-border: #c6e3e9;
        position: relative;
        overflow: hidden;
        margin: -0.55rem 0 0.8rem;
        padding: 0.45rem 0.6rem;
        border: 1px solid var(--banner-border);
        border-left: 5px solid var(--banner-accent);
        border-radius: 16px;
        background: linear-gradient(112deg, #ffffff 0%, var(--banner-soft) 100%);
        box-shadow: 0 8px 24px rgba(16, 36, 62, 0.07);
    }
    [class*="st-key-workspace_banner_shell_data"] { --banner-accent: #2878b8; --banner-soft: #edf6fd; --banner-border: #c9e0f2; }
    [class*="st-key-workspace_banner_shell_screener"] { --banner-accent: #7652b6; --banner-soft: #f4effc; --banner-border: #ddd0f2; }
    [class*="st-key-workspace_banner_shell_backtest"] { --banner-accent: #c56d22; --banner-soft: #fff5e8; --banner-border: #f0d7b8; }
    [class*="st-key-workspace_banner_shell_results"] { --banner-accent: #27805a; --banner-soft: #ecf8f2; --banner-border: #c7e7d8; }
    [class*="st-key-workspace_banner_shell_chart"] { --banner-accent: #176b87; --banner-soft: #e9f6f8; --banner-border: #c6e3e9; }
    [class*="st-key-workspace_banner_shell_watchlists"] { --banner-accent: #a17818; --banner-soft: #fff9e9; --banner-border: #eadcae; }
    [class*="st-key-workspace_banner_shell_alerts"] { --banner-accent: #b66a16; --banner-soft: #fff7e8; --banner-border: #efd6aa; }
    [class*="st-key-workspace_banner_shell_"] [data-testid="stHorizontalBlock"] {
        align-items: center;
    }
    .workspace-banner {
        position: relative;
        display: grid;
        grid-template-columns: auto minmax(0, 1fr) auto;
        align-items: center;
        gap: 0.8rem;
        margin: 0;
        padding: 0.35rem 0.45rem;
    }
    .workspace-banner--data { --banner-accent: #2878b8; --banner-soft: #edf6fd; --banner-border: #c9e0f2; }
    .workspace-banner--screener { --banner-accent: #7652b6; --banner-soft: #f4effc; --banner-border: #ddd0f2; }
    .workspace-banner--backtest { --banner-accent: #c56d22; --banner-soft: #fff5e8; --banner-border: #f0d7b8; }
    .workspace-banner--results { --banner-accent: #27805a; --banner-soft: #ecf8f2; --banner-border: #c7e7d8; }
    .workspace-banner--chart { --banner-accent: #176b87; --banner-soft: #e9f6f8; --banner-border: #c6e3e9; }
    .workspace-banner--watchlists { --banner-accent: #a17818; --banner-soft: #fff9e9; --banner-border: #eadcae; }
    .workspace-banner--alerts { --banner-accent: #b66a16; --banner-soft: #fff7e8; --banner-border: #efd6aa; }
    .workspace-banner__icon {
        display: grid;
        place-items: center;
        width: 3.2rem;
        height: 3.2rem;
        border: 1px solid color-mix(in srgb, var(--banner-accent) 24%, white);
        border-radius: 13px;
        background: #ffffff;
        color: var(--banner-accent);
        font-size: 1.45rem;
        box-shadow: 0 5px 14px color-mix(in srgb, var(--banner-accent) 12%, transparent);
    }
    .workspace-banner__content { position: relative; z-index: 1; min-width: 0; }
    .workspace-banner__eyebrow {
        margin-bottom: 0.2rem;
        color: var(--banner-accent);
        font-size: 0.68rem;
        font-weight: 850;
        letter-spacing: 0.1em;
        text-transform: uppercase;
    }
    .workspace-banner__title {
        margin: 0 !important;
        color: var(--ink-strong) !important;
        font-size: 1.45rem;
        line-height: 1.18;
    }
    .workspace-banner__description {
        max-width: 760px;
        margin: 0.35rem 0 0;
        color: var(--ink-muted);
        font-size: 0.84rem;
        line-height: 1.4;
    }
    .workspace-banner__badge {
        position: relative;
        z-index: 1;
        padding: 0.32rem 0.68rem;
        border: 1px solid var(--banner-border);
        border-radius: 999px;
        background: rgba(255, 255, 255, 0.82);
        color: var(--banner-accent);
        font-size: 0.7rem;
        font-weight: 800;
        white-space: nowrap;
    }
    [class*="st-key-workspace_account_"] {
        display: grid;
        grid-template-columns: minmax(0, 1fr) auto;
        align-items: center;
        gap: 0.45rem;
        min-height: 0;
        padding: 0.3rem 0.2rem 0.3rem 0.8rem;
        border-left: 1px solid color-mix(in srgb, var(--banner-accent) 24%, white);
        background: transparent;
    }
    .workspace-account__label {
        color: var(--brand);
        font-size: 0.62rem;
        font-weight: 850;
        letter-spacing: 0.09em;
        line-height: 1;
        text-transform: uppercase;
    }
    .workspace-account__name {
        margin-top: 0.18rem;
        overflow: hidden;
        color: var(--ink-strong);
        font-size: 0.82rem;
        font-weight: 800;
        text-overflow: ellipsis;
        white-space: nowrap;
    }
    .workspace-account__email {
        overflow: hidden;
        color: var(--ink-muted);
        font-size: 0.64rem;
        text-overflow: ellipsis;
        white-space: nowrap;
    }
    [class*="st-key-workspace_account_"] .stButton > button {
        width: auto;
        min-height: 1.9rem;
        margin-top: 0;
        padding: 0.25rem 0.55rem;
        font-size: 0.72rem;
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
    div.stTabs {
        overflow: visible;
    }
    div.stTabs [role="tablist"] {
        position: fixed;
        z-index: 990;
        top: var(--primary-nav-top);
        left: 50%;
        width: fit-content;
        max-width: calc(100vw - 1.25rem);
        gap: 0.5rem;
        justify-content: center;
        padding: 0.48rem;
        border: 1px solid rgba(183, 204, 216, 0.82);
        border-radius: 18px;
        background: rgba(248, 251, 253, 0.92);
        box-shadow:
            0 12px 34px rgba(16, 53, 76, 0.17),
            inset 0 1px 0 rgba(255, 255, 255, 0.92);
        backdrop-filter: blur(18px) saturate(1.3);
        transform: translateX(-50%);
    }
    div.stTabs [role="tab"] {
        display: grid;
        place-items: center;
        flex: 0 0 3.15rem;
        width: 3.15rem;
        min-width: 3.15rem;
        height: 3.15rem;
        min-height: 3.15rem;
        padding: 0;
        border: 1px solid transparent;
        border-radius: 13px;
        background: transparent;
        color: var(--ink-muted);
        box-shadow: none;
        transition:
            transform 0.16s ease,
            background 0.16s ease,
            border-color 0.16s ease,
            box-shadow 0.16s ease;
    }
    div.stTabs [role="tab"] p {
        margin: 0;
        font-size: 0 !important;
        line-height: 1;
    }
    div.stTabs [role="tab"] p::before {
        display: block;
        font-size: 1.42rem;
        line-height: 1;
        filter: saturate(0.9);
    }
    div.stTabs [role="tab"]:nth-child(1) p::before { content: "📥"; }
    div.stTabs [role="tab"]:nth-child(2) p::before { content: "🔎"; }
    div.stTabs [role="tab"]:nth-child(3) p::before { content: "🧪"; }
    div.stTabs [role="tab"]:nth-child(4) p::before { content: "📊"; }
    div.stTabs [role="tab"]:nth-child(5) p::before { content: "📈"; }
    div.stTabs [role="tab"]:nth-child(6) p::before { content: "⭐"; }
    div.stTabs [role="tab"]:nth-child(7) p::before { content: "🔔"; }
    div.stTabs [role="tab"]:focus-visible {
        outline: 3px solid rgba(23, 107, 135, 0.20);
        outline-offset: 2px;
    }
    div.stTabs [role="tab"]:hover {
        background: var(--brand-soft);
        border-color: #c8e0e6;
        color: var(--brand-dark);
        transform: translateY(-2px);
    }
    div.stTabs [role="tab"][aria-selected="true"] {
        background: linear-gradient(135deg, #176b87, #168297);
        border-color: rgba(16, 83, 106, 0.45);
        color: #ffffff;
        box-shadow: 0 7px 18px rgba(23, 107, 135, 0.28);
        transform: translateY(-1px);
    }
    div.stTabs [role="tab"][aria-selected="true"] p {
        color: #ffffff !important;
    }
    div.stTabs .react-aria-SelectionIndicator {
        display: none;
    }
    div.stTabs [role="tabpanel"] {
        min-height: calc(100vh - 9rem);
        padding-top: 0;
        scroll-margin-top: calc(
            var(--primary-nav-top)
            + var(--primary-nav-height)
            + var(--primary-nav-clearance)
        );
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
    .data-status-card__coverage {
        display: block;
        margin-top: 0.3rem;
        font-size: 0.82rem;
        font-weight: 550;
    }
    .data-status-progress {
        display: flex;
        width: 100%;
        height: 0.72rem;
        margin-top: 0.65rem;
        overflow: hidden;
        border: 1px solid rgba(23, 97, 72, 0.18);
        border-radius: 999px;
        background: #e5e7eb;
        box-shadow: inset 0 1px 2px rgba(15, 23, 42, 0.12);
    }
    .data-status-progress__current {
        height: 100%;
        background: linear-gradient(90deg, #16a34a, #22c55e);
    }
    .data-status-progress__legend {
        display: flex;
        flex-wrap: wrap;
        justify-content: space-between;
        gap: 0.25rem 0.8rem;
        margin-top: 0.38rem;
        font-size: 0.72rem;
        font-weight: 650;
    }
    .data-status-progress__legend-current { color: #15803d; }
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
    .data-panel-heading.tone-blue span { background: #e8f2ff; color: #2563a8; }
    .data-panel-heading.tone-violet span { background: #f1eafe; color: #7048b5; }
    .data-panel-heading.tone-green span { background: #e5f7ee; color: #18794e; }
    .data-panel-heading.tone-amber span { background: #fff0dc; color: #b56318; }
    .data-panel-heading.tone-rose span { background: #fdebed; color: #b84354; }
    .data-panel-heading.tone-slate span { background: #edf2f6; color: #526b80; }
    [data-testid="stVerticalBlockBorderWrapper"]:has(.data-panel-heading.tone-blue) { border-top: 3px solid #5a9bd0 !important; }
    [data-testid="stVerticalBlockBorderWrapper"]:has(.data-panel-heading.tone-violet) { border-top: 3px solid #9677cc !important; }
    [data-testid="stVerticalBlockBorderWrapper"]:has(.data-panel-heading.tone-green) { border-top: 3px solid #50a57d !important; }
    [data-testid="stVerticalBlockBorderWrapper"]:has(.data-panel-heading.tone-amber) { border-top: 3px solid #dc934d !important; }
    [data-testid="stVerticalBlockBorderWrapper"]:has(.data-panel-heading.tone-rose) { border-top: 3px solid #d87582 !important; }
    .sell-strategy-help {
        position: relative;
        display: inline-block;
        margin-left: -0.35rem;
        z-index: 30;
    }
    .sell-strategy-help summary {
        display: grid;
        place-items: center;
        width: 1.35rem;
        height: 1.35rem;
        border: 1px solid #9ab8c4;
        border-radius: 999px;
        background: #ffffff;
        color: var(--brand-dark);
        font-size: 0.78rem;
        font-weight: 850;
        line-height: 1;
        cursor: pointer;
        list-style: none;
        box-shadow: 0 2px 6px rgba(15, 23, 42, 0.08);
    }
    .sell-strategy-help summary::-webkit-details-marker { display: none; }
    .sell-strategy-help summary:hover,
    .sell-strategy-help summary:focus-visible {
        border-color: var(--brand);
        background: var(--brand-soft);
        outline: none;
    }
    .sell-strategy-help__popup {
        position: absolute;
        top: 1.75rem;
        left: 50%;
        width: min(25rem, 78vw);
        padding: 0.9rem 1rem;
        border: 1px solid #cbd9e4;
        border-radius: 11px;
        background: #ffffff;
        color: #304860;
        font-size: 0.78rem;
        font-weight: 500;
        line-height: 1.45;
        box-shadow: 0 12px 28px rgba(15, 23, 42, 0.16);
    }
    .sell-strategy-help__popup strong {
        display: block;
        margin-bottom: 0.4rem;
        color: var(--ink-strong);
        font-size: 0.86rem;
    }
    .sell-strategy-help__popup ul {
        margin: 0;
        padding-left: 1.1rem;
    }
    .sell-strategy-help__popup li + li { margin-top: 0.3rem; }
    .sell-strategy-help__popup code {
        padding: 0.08rem 0.25rem;
        border-radius: 4px;
        background: #eef5f7;
        color: #155e75;
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
    .screener-section-heading__status {
        display: inline-flex;
        align-items: center;
        max-width: min(28rem, 55vw);
        padding: 0.28rem 0.62rem;
        border: 1px solid;
        border-radius: 999px;
        font-size: 0.75rem;
        font-weight: 800;
        letter-spacing: 0;
        line-height: 1.2;
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
    }
    .screener-section-heading__status.is-favorite {
        border-color: #b9e3ce;
        background: #eefbf4;
        color: #147a4b;
    }
    .screener-section-heading__status.is-custom {
        border-color: #f2d59e;
        background: #fff8e8;
        color: #986515;
    }
    @media (max-width: 768px) {
        .screener-section-heading {
            align-items: flex-start;
        }
        .screener-section-heading__title {
            flex-wrap: wrap;
        }
        .screener-section-heading__status {
            max-width: 72vw;
        }
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
    hr {
        border-color: var(--border) !important;
        margin: 1.4rem 0 !important;
    }

    /* Keep compact column layouts comfortable */
    [data-testid="stHorizontalBlock"] {
        gap: 1rem;
    }

    @media (max-width: 768px) {
        :root {
            --primary-nav-top: calc(3.45rem + env(safe-area-inset-top, 0px));
            --primary-nav-height: calc(2.8rem + 0.76rem + 2px);
            --primary-nav-clearance: 0.3rem;
        }
        [class*="st-key-workspace_banner_shell_"] [data-testid="stHorizontalBlock"] {
            flex-wrap: wrap;
        }
        [class*="st-key-workspace_banner_shell_"] [data-testid="column"] {
            min-width: min(100%, 270px);
            flex: 1 1 270px;
        }
        .workspace-banner {
            grid-template-columns: auto minmax(0, 1fr);
            padding: 0.45rem;
        }
        .workspace-banner__description { display: none; }
        .workspace-banner__badge { display: none; }
        .workspace-banner__icon {
            width: 2.7rem;
            height: 2.7rem;
        }
        .workspace-banner__title { font-size: 1.2rem; }
        [class*="st-key-workspace_account_"] {
            grid-template-columns: minmax(0, 1fr) auto;
            min-height: 0;
            padding: 0.35rem 0.45rem;
            border-top: 1px solid color-mix(in srgb, var(--banner-accent) 20%, white);
            border-left: 0;
        }
        div.stTabs [role="tablist"] {
            width: calc(100vw - 1rem);
            gap: clamp(0.2rem, 1.5vw, 0.45rem);
            justify-content: space-between;
            overflow: visible;
            padding: 0.38rem;
            border-radius: 16px;
        }
        div.stTabs [role="tab"] {
            flex: 0 0 clamp(2.6rem, 13vw, 3rem);
            width: clamp(2.6rem, 13vw, 3rem);
            min-width: clamp(2.6rem, 13vw, 3rem);
            height: 2.8rem;
            min-height: 2.8rem;
            border-radius: 11px;
        }
        div.stTabs [role="tab"] p::before {
            font-size: 1.26rem;
        }
    }
    @media (max-width: 460px) {
        .workspace-banner__icon { display: none; }
        .workspace-banner { grid-template-columns: 1fr; }
    }
    @media (orientation: landscape) and (max-height: 600px) {
        :root {
            --primary-nav-top: calc(3.05rem + env(safe-area-inset-top, 0px));
            --primary-nav-height: calc(2.35rem + 0.56rem + 2px);
            --primary-nav-clearance: 0.25rem;
        }
        div.stTabs [role="tablist"] {
            width: auto;
            padding: 0.28rem;
            border-radius: 14px;
        }
        div.stTabs [role="tab"] {
            flex-basis: 2.45rem;
            width: 2.45rem;
            min-width: 2.45rem;
            height: 2.35rem;
            min-height: 2.35rem;
            border-radius: 9px;
        }
        div.stTabs [role="tab"] p::before {
            font-size: 1.08rem;
        }
        [class*="st-key-workspace_account_"] {
            min-height: 76px;
            padding: 0.45rem 0.6rem;
        }
    }
    </style>
    """,
    unsafe_allow_html=True,
)

def render_workspace_banner(tone, eyebrow, title, description, icon, badge):
    """Render workspace identity and compact account actions as one banner."""
    with st.container(key=f"workspace_banner_shell_{tone}"):
        banner_col, account_col = st.columns(
            [4.8, 1.35],
            gap="medium",
            vertical_alignment="center",
        )
        with banner_col:
            st.markdown(
                f"""
                <section class="workspace-banner workspace-banner--{html.escape(tone)}">
                    <div class="workspace-banner__icon" aria-hidden="true">{html.escape(icon)}</div>
                    <div class="workspace-banner__content">
                        <div class="workspace-banner__eyebrow">{html.escape(eyebrow)}</div>
                        <h2 class="workspace-banner__title">{html.escape(title)}</h2>
                        <p class="workspace-banner__description">{html.escape(description)}</p>
                    </div>
                    <div class="workspace-banner__badge">{html.escape(badge)}</div>
                </section>
                """,
                unsafe_allow_html=True,
            )
        with account_col:
            render_workspace_account_controls(
                st,
                app_user,
                cloud_store is not None,
                tone,
            )


def sync_pattern_lookback_from_slider():
    st.session_state["pattern_lookback_days_number"] = st.session_state["pattern_lookback_days_slider"]


def sync_pattern_lookback_from_number():
    st.session_state["pattern_lookback_days_slider"] = st.session_state["pattern_lookback_days_number"]


def sync_pattern_reversal_from_slider():
    st.session_state["pattern_reversal_pct_number"] = st.session_state["pattern_reversal_pct_slider"]


def sync_pattern_reversal_from_number():
    st.session_state["pattern_reversal_pct_slider"] = st.session_state["pattern_reversal_pct_number"]


def clear_filter_widget_state():
    for key in list(st.session_state.keys()):
        if key.startswith("ma_filter_"):
            del st.session_state[key]


CUSTOM_FILTER_NAME = "Custom Filter"


def favorite_ma_filter_set(filter_name):
    saved_filter = favorite_filter_sets.get(filter_name)
    if saved_filter is None:
        return None
    if isinstance(saved_filter, list):
        return normalize_filter_set(saved_filter, use_default=False)
    pattern_settings = saved_filter.get("pattern", {})
    return merge_legacy_expression_filters(
        saved_filter.get("ma_filter_set", []),
        pattern_settings.get("expressions", []),
    )


def comparable_filter_set(filter_set):
    """Return the meaningful filter data without runtime-only row ids."""
    return [
        {
            "type": item["type"],
            "params": deepcopy(item.get("params", {})),
        }
        for item in normalize_filter_set(filter_set, use_default=False)
    ]


def filter_set_matches_favorite(filter_set, filter_name):
    saved_filter_set = favorite_ma_filter_set(filter_name)
    return (
        saved_filter_set is not None
        and comparable_filter_set(filter_set) == comparable_filter_set(saved_filter_set)
    )


def mark_current_filter_custom():
    active_name = st.session_state.get("_active_favorite_filter_name")
    if active_name:
        if app_user is None:
            st.session_state["_favorite_edit_login_required"] = True
    st.session_state["_active_favorite_filter_name"] = None


def apply_filter_selection_to_state(filter_name):
    saved_filter = favorite_filter_sets.get(filter_name)
    if saved_filter is None:
        return

    if isinstance(saved_filter, list):
        ma_filter_set = saved_filter
        pattern_settings = {}
    else:
        ma_filter_set = saved_filter.get("ma_filter_set", [])
        pattern_settings = saved_filter.get("pattern", {})

    loaded_ma_filter_set = merge_legacy_expression_filters(
        ma_filter_set,
        pattern_settings.get("expressions", []),
    )
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
    st.session_state["pattern_lookback_days_slider"] = lookback_days
    st.session_state["pattern_lookback_days_number"] = lookback_days
    st.session_state["pattern_reversal_pct_slider"] = reversal_pct
    st.session_state["pattern_reversal_pct_number"] = reversal_pct
    st.session_state["_active_favorite_filter_name"] = filter_name
    st.session_state["_fast_favorite_selection"] = True


def is_stock_data_file(path):
    return path.stem.upper() != NIFTY_DATA_SYMBOL


def stock_data_files(directory):
    if not directory or not directory.exists():
        return []
    return list_symbol_paths(directory, include_index=False)


CHART_CREATION_LOCK = threading.RLock()
SCREENER_JOBS_LOCK = threading.RLock()
SCREENER_JOBS = {}


def screener_job_owner_key():
    """Return the authenticated owner key used to recover background jobs."""
    if app_user is None:
        return None
    return f"user:{app_user.id}"


def attach_registered_screener_job():
    """Reconnect this browser session to its still-running server-side job."""
    job = st.session_state.get("screener_job")
    if job:
        return job

    owner_key = screener_job_owner_key()
    if owner_key is None:
        return None
    with SCREENER_JOBS_LOCK:
        job = SCREENER_JOBS.get(owner_key)
    if job and job.get("running"):
        st.session_state["screener_job"] = job
        return job
    return None


def screen_stock_file_worker(
    index,
    stock_file,
    market_cap_position,
    filter_set,
    market,
    pattern_lookback_days,
    pattern_reversal_pct,
    pattern_expressions,
    pe_cache,
    create_charts=False,
):
    del create_charts
    price_df = load_price_dataframe(stock_file, filter_set=filter_set)
    needs_pe = filter_set_requires_pe(filter_set, pattern_expressions)
    cached_pe = pe_cache.get(
        f"{normalize_market(market)}:{stock_file.stem}",
        pe_cache.get(stock_file.stem, ""),
    )
    result = screen_dataframe(
        price_df,
        stock_file.stem,
        filter_set=filter_set,
        include_pe=needs_pe,
        market=market,
        pe_ratio=cached_pe,
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
        pattern_passed, swings, pattern_error = evaluate_pattern_filters_from_df(
            price_df,
            pattern_lookback_days,
            pattern_reversal_pct,
            pattern_expressions,
            pe_ratio=result.get("PE Ratio"),
        )

    if pattern_passed:
        result["Market Cap Position"] = int(market_cap_position)
        if result.get("PE Ratio") in ("", None):
            result["PE Ratio"] = cached_pe

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
    market_cap_positions,
    filter_set,
    market,
    pattern_lookback_days,
    pattern_reversal_pct,
    pattern_expressions,
    create_charts,
):
    total = len(stock_files)
    max_workers = min(
        max(1, int(os.environ.get("SCREENER_MAX_WORKERS", "12"))),
        max(1, total),
    )
    matched_rows_by_index = {}
    failed_count = 0
    pe_cache = load_pe_ratios()
    for symbol, pe_ratio in latest_monthly_pe_values(market).items():
        pe_cache.setdefault(f"{normalize_market(market)}:{symbol}", pe_ratio)

    try:
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [
                executor.submit(
                    screen_stock_file_worker,
                    index,
                    stock_file,
                    market_cap_positions.get(stock_file.stem, index),
                    filter_set,
                    market,
                    pattern_lookback_days,
                    pattern_reversal_pct,
                    pattern_expressions,
                    pe_cache,
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
                        matched_rows_by_index[worker_result["index"]] = result
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
                    "matches": len(matched_rows_by_index),
                    "finished": stock_name,
                    "max_workers": max_workers,
                    "phase": "screening",
                })

        matched_rows = [
            matched_rows_by_index[index]
            for index in sorted(matched_rows_by_index)
        ]
        if create_charts and matched_rows:
            job_queue.put({
                "type": "phase",
                "phase": "charts",
                "charts_total": len(matched_rows),
            })
            for chart_done, result in enumerate(matched_rows, start=1):
                symbol = str(result.get("Symbol", "") or "")
                stock_file = symbol_path(
                    timeframe_config("DAY", market)["target_dir"],
                    symbol,
                )
                try:
                    with CHART_CREATION_LOCK:
                        chart_path = create_stock_chart(
                            stock_file,
                            filter_set,
                            pe_ratio=result.get("PE Ratio"),
                        )
                    if chart_path:
                        result["ChartPath"] = chart_path
                        result["ChartSource"] = symbol
                        job_queue.put({
                            "type": "row_update",
                            "row": dict(result),
                            "symbol": symbol,
                        })
                except Exception as exc:
                    job_queue.put({
                        "type": "worker_error",
                        "message": f"{symbol} chart: {exc}",
                    })
                job_queue.put({
                    "type": "phase",
                    "phase": "charts",
                    "charts_done": chart_done,
                    "charts_total": len(matched_rows),
                })
        job_queue.put({
            "type": "complete",
            "rows": matched_rows,
            "failed_count": failed_count,
            "total": total,
            "matches": len(matched_rows),
        })
    except Exception as exc:
        matched_rows = [
            matched_rows_by_index[index]
            for index in sorted(matched_rows_by_index)
        ]
        job_queue.put({"type": "fatal_error", "message": str(exc), "rows": matched_rows})


def start_live_screener_job(
    stock_files,
    market_cap_positions,
    filter_set,
    market,
    pattern_lookback_days,
    pattern_reversal_pct,
    pattern_expressions,
    create_charts,
    owner_key=None,
):
    job_queue = queue.Queue()
    total = len(stock_files)
    max_workers = min(
        max(1, int(os.environ.get("SCREENER_MAX_WORKERS", "12"))),
        max(1, total),
    )
    thread = threading.Thread(
        target=run_live_screener_job,
        args=(
            job_queue,
            stock_files,
            market_cap_positions,
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
        "phase": "screening",
        "started_at": datetime.now().strftime("%H:%M:%S"),
        "results_tab_opened": False,
    }
    if owner_key is not None:
        with SCREENER_JOBS_LOCK:
            SCREENER_JOBS[owner_key] = job
    thread.start()
    return job


def drain_live_screener_events():
    job = attach_registered_screener_job()
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
        elif event_type == "row_update":
            updated_row = event.get("row", {})
            updated_symbol = str(event.get("symbol", "") or "")
            for row_index, row in enumerate(rows):
                if str(row.get("Symbol", "") or "") == updated_symbol:
                    rows[row_index] = updated_row
                    break
        elif event_type == "progress":
            job["done"] = event.get("done", job.get("done", 0))
            job["total"] = event.get("total", job.get("total", 0))
            job["matches"] = event.get("matches", job.get("matches", len(rows)))
            job["last_finished"] = event.get("finished", "")
            job["max_workers"] = event.get("max_workers", job.get("max_workers", 1))
            job["phase"] = event.get("phase", job.get("phase", "screening"))
        elif event_type == "phase":
            job["phase"] = event.get("phase", job.get("phase", "screening"))
            job["charts_done"] = event.get(
                "charts_done",
                job.get("charts_done", 0),
            )
            job["charts_total"] = event.get(
                "charts_total",
                job.get("charts_total", 0),
            )
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
        job["error"] = job.get("error") or "The screener stopped unexpectedly."
    return job


@st.fragment(run_every=0.75)
def render_active_screener_progress():
    """Poll only the progress region while the server-side job is active."""
    job = drain_live_screener_events()
    if not job:
        return

    total = int(job.get("total", 0) or 0)
    done = int(job.get("done", 0) or 0)
    matches = int(job.get("matches", 0) or 0)
    if job.get("running"):
        if job.get("phase") == "charts":
            charts_done = int(job.get("charts_done", 0) or 0)
            charts_total = int(job.get("charts_total", matches) or 0)
            st.progress(
                min(1.0, charts_done / charts_total) if charts_total else 0.0,
                text=(
                    f"Screening complete · {matches:,} matches · "
                    f"Preparing charts {charts_done:,} of {charts_total:,}"
                ),
            )
        else:
            st.progress(
                min(1.0, done / total) if total else 0.0,
                text=(
                    f"Screening {done:,} of {total:,} stocks · "
                    f"{matches:,} matches found"
                ),
            )
        st.caption(
            "This screening job runs on the server. You can minimize the browser "
            "or switch to another mobile app and return later."
        )
        return

    if job.get("error"):
        st.error(f"Screener stopped: {job['error']}")
        return

    if not job.get("results_tab_opened"):
        st.progress(
            1.0,
            text=(
                f"Screening complete · {done:,} of {total:,} stocks · "
                f"{matches:,} matches found"
            ),
        )
        job["results_tab_opened"] = True
        st.session_state["switch_to_results_tab"] = True
        st.rerun()


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

        stock_file = symbol_path(target_dir, symbol)
        if not stock_exists(stock_file):
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
            "Candles",
            [
                ("Candle[0]", "The latest candle. Use Candle[-1] for the previous candle, Candle[-2] for two candles ago, and so on."),
                ("Candle[-1].High", "The high price of the previous candle. Open, High, Low, and Close are supported."),
                ("Candle[0..-4]", "A five-candle list ordered from the current candle back through four candles ago."),
                ("Candle[0..-4].Low", "A list containing the Low price from each candle in the selected range."),
                ("IsGreen(Candle[0])", "True when the selected candle's Close is greater than its Open."),
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
        '<div class="expression-example">IsGreen(Candle[0]) · latest candle closed above its open</div>'
        '<div class="expression-example">Candle[-1].High &gt; Candle[-2].High</div>'
        '<div class="expression-example">min(Candle[0..-4].Low) &gt; SMA200</div>'
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
            enriched_row.setdefault("Chart MA Periods", required_ma_periods(filter_set))
            stock_file = files_by_symbol.get(str(row.get("Symbol", "")))
            if stock_file and not enriched_row.get("ChartPath"):
                row_markers = date_markers
                if enriched_row.get("Buy Date") and enriched_row.get("Buy Price") is not None:
                    exit_label = {
                        "Target": "TARGET",
                        "Stop Loss": "STOP",
                        "End Date": "END",
                    }.get(enriched_row.get("Exit Reason"), "END")
                    row_markers = [
                        {
                            "label": "BUY",
                            "date": pd.to_datetime(enriched_row["Buy Date"], dayfirst=True),
                            "price": enriched_row["Buy Price"],
                        },
                        {
                            "label": exit_label,
                            "date": pd.to_datetime(enriched_row["Exit Date"], dayfirst=True),
                            "price": enriched_row["Exit Price"],
                        },
                    ]
                chart_path = create_stock_chart(
                    stock_file,
                    filter_set,
                    date_markers=row_markers,
                    window_start_date=enriched_row.get("Chart Start Date"),
                    window_end_date=enriched_row.get("Chart End Date"),
                )
                if chart_path:
                    enriched_row["ChartPath"] = chart_path
                    enriched_row["ChartSource"] = stock_file.stem
            enriched_rows.append(enriched_row)
        enriched_details[filter_name] = enriched_rows

    return enriched_details


def render_data_availability_status(market=MARKET_INDIA):
    """Render the latest available data date and its stock coverage."""
    market = normalize_market(market)
    directory = timeframe_config("DAY", market)["target_dir"]
    availability = data_availability_summary(directory, market=market)
    last_date = availability["Latest Date"]
    if last_date:
        date_formatted = last_date.strftime("%d-%m-%Y")
        stocks_on_date = availability["Stocks On Latest Date"]
        current_stock_files = availability["Current Stock Files"]
        # The active status universe intentionally excludes stale/failed files.
        # Those files remain retryable on disk but do not reduce this bar.
        progress_total = current_stock_files
        current_width = 100 if progress_total else 0
        st.markdown(
            f'<div class="data-status-card data-status-available">'
            f'📅 Last download: <b>{date_formatted}</b>'
            f'<span class="data-status-card__coverage">'
            f'Data available for this date: <b>{stocks_on_date:,}</b> of '
            f'<b>{current_stock_files:,}</b> stocks'
            f'</span>'
            f'<div class="data-status-progress" role="img" '
            f'aria-label="{current_stock_files} of {progress_total} downloaded stocks current">'
            f'<span class="data-status-progress__current" style="width:{current_width:.3f}%"></span>'
            f'</div>'
            f'<div class="data-status-progress__legend">'
            f'<span class="data-status-progress__legend-current">● Current: {current_stock_files:,}</span>'
            f'</div>'
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


def render_download_job_status(job):
    st.markdown(
        '<div class="data-panel-heading"><span>↻</span>Download Activity</div>',
        unsafe_allow_html=True,
    )
    total = int(job.get("total", 0) or 0)
    done = int(job.get("done", 0) or 0)
    downloaded_count = int(job.get("downloaded_count", 0) or 0)
    st.progress(min(1.0, done / total) if total else 0.0)

    if job.get("running"):
        symbol = job.get("symbol") or "preparing download"
        st.info(
            f"Processed {downloaded_count} of {total} stocks. "
            f"Processing {done}/{total}: {symbol}. You can close or minimize this page."
        )
        return

    if job.get("error"):
        st.error(f"Download stopped: {job['error']}")
        return

    completed_at = job.get("completed_at", "")
    st.success(
        f"✅ Processed {downloaded_count} of {total} stocks. "
        f"Last download: {completed_at}"
    )
    st.caption(f"Incremental rows added: {int(job.get('rows_added', 0) or 0):,}")

    nifty_row = job.get("nifty_row") or {}
    if job.get("market") == MARKET_INDIA and nifty_row.get("Downloaded"):
        st.success("Downloaded Nifty 50 benchmark data")
    elif job.get("market") == MARKET_INDIA and nifty_row:
        st.warning(
            "Could not download Nifty 50 benchmark data: "
            f"{nifty_row.get('Error') or 'No data returned'}"
        )

    failed = job.get("failed") or []
    if failed:
        st.markdown(pd.DataFrame(failed).to_html(index=False), unsafe_allow_html=True)


@st.fragment(run_every=1)
def render_live_download_activity(market):
    job = background_download_snapshot(market)
    if not job:
        return
    render_download_job_status(job)
    if not job.get("running"):
        st.rerun()


def render_backtest_results_table(
    summary_rows,
    series_by_filter,
    stock_details_by_filter,
    interactive_market=None,
    backtest_favorite_filter_sets=None,
    height=1200,
):
    payload = json.dumps(series_by_filter, default=str)
    chart_details_by_filter = {}
    for filter_name, rows in stock_details_by_filter.items():
        fallback_ma_periods = []
        if backtest_favorite_filter_sets and filter_name in backtest_favorite_filter_sets:
            filter_set, _ = split_favorite_filter(backtest_favorite_filter_sets[filter_name])
            fallback_ma_periods = required_ma_periods(filter_set)
        chart_rows = []
        for row in rows:
            chart_row = dict(row)
            chart_row.setdefault("Chart MA Periods", fallback_ma_periods)
            chart_path = chart_row.get("ChartPath")
            if chart_path:
                try:
                    chart_row["ChartSrc"] = image_to_data_uri(chart_path)
                except OSError:
                    chart_row["ChartSrc"] = ""
            chart_source = chart_row.get("ChartSource") or chart_row.get("Symbol")
            if chart_source and interactive_market:
                ma_periods = chart_row.get("Chart MA Periods") or []
                chart_row["InteractiveSrc"] = interactive_chart_query(
                    chart_source,
                    interactive_market,
                    ma_periods=ma_periods,
                    embedded=True,
                    initial_range="all",
                    trade_overlay={
                        "buyDate": pd.to_datetime(
                            chart_row.get("Buy Date"),
                            dayfirst=True,
                        ).strftime("%Y-%m-%d"),
                        "exitDate": pd.to_datetime(
                            chart_row.get("Exit Date"),
                            dayfirst=True,
                        ).strftime("%Y-%m-%d"),
                        "windowStart": chart_row.get("Chart Start Date"),
                        "windowEnd": chart_row.get("Chart End Date"),
                        "buyPrice": chart_row.get(
                            "Chart Buy Price",
                            chart_row.get("Buy Price"),
                        ),
                        "targetPrice": chart_row.get(
                            "Chart Target Price",
                            chart_row.get("Target Price"),
                        ),
                        "stopPrice": chart_row.get(
                            "Chart Stop Loss Price",
                            chart_row.get("Stop Loss Price"),
                        ),
                        "exitPrice": chart_row.get(
                            "Chart Exit Price",
                            chart_row.get("Exit Price"),
                        ),
                        "exitReason": chart_row.get("Exit Reason"),
                    },
                )
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
      .stock-symbol-actions {{ align-items: center; display: inline-flex; gap: 8px; }}
      .stock-interactive-link {{
        align-items: center;
        background: #fff7e8;
        border: 1px solid #f0b15f;
        border-radius: 6px;
        color: #b65d18;
        cursor: pointer;
        display: inline-grid;
        flex: 0 0 22px;
        height: 22px;
        padding: 0;
        place-items: center;
        width: 22px;
      }}
      .stock-interactive-link:hover, .stock-interactive-link.active {{ background: #ffedd2; border-color: #df7a2c; }}
      .stock-interactive-link svg {{ height: 13px; pointer-events: none; width: 13px; }}
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
      .stock-interactive-frame {{ border: 0; display: block; height: 50vh; min-height: 420px; width: 100%; }}
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
          const exitReason = escapeHtml(row["Exit Reason"] || "End Date");
          const exitDate = escapeHtml(row["Exit Date"] || "");
          const exitPrice = stockValue(row, "Exit Price");
          const staticSymbol = row["ChartSrc"]
            ? `<button class="stock-chart-link" data-symbol="${{symbol}}" data-chart-src="${{row["ChartSrc"]}}">${{symbol}}</button>`
            : `<span class="stock-symbol">${{symbol}}</span>`;
          const interactiveButton = row["InteractiveSrc"]
            ? `<button class="stock-interactive-link" data-symbol="${{symbol}}" data-interactive-src="${{escapeHtml(row["InteractiveSrc"])}}" title="Show ${{symbol}} interactive chart" aria-label="Show ${{symbol}} interactive chart"><svg viewBox="0 0 16 16" aria-hidden="true"><path d="M3 2v4M3 9v5M1.5 6h3v3h-3zM8 1v3M8 8v5M6.5 4h3v4h-3zM13 3v5M13 11v3M11.5 8h3v3h-3z" fill="none" stroke="currentColor" stroke-width="1.35" stroke-linecap="round"/></svg></button>`
            : "";
          const symbolCell = `<span class="stock-symbol-actions">${{staticSymbol}}${{interactiveButton}}</span>`;
          return `
            <tr>
              <td>${{symbolCell}}</td>
              <td class="${{gainClass(endGain)}}">${{signed(endGain)}}</td>
              <td class="${{gainClass(peakGain)}}">${{signed(peakGain)}}</td>
              <td>${{exitReason}}</td>
              <td>${{exitDate}}</td>
              <td>${{exitPrice.toFixed(2)}}</td>
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
          ownerSection.querySelectorAll(".stock-chart-link, .stock-interactive-link").forEach(item => item.classList.remove("active"));
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
        document.querySelectorAll(".stock-chart-link, .stock-interactive-link").forEach(item => item.classList.remove("active"));
        section.querySelectorAll(".stock-chart-link, .stock-interactive-link").forEach(item => item.classList.remove("active"));
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

      function renderInteractiveStockChart(section, button) {{
        const buttons = Array.from(section.querySelectorAll(".stock-interactive-link"));
        const index = buttons.indexOf(button);
        if (!button || index < 0) return;
        let market = "";
        try {{
          market = new URL(
            button.dataset.interactiveSrc,
            window.location.href
          ).searchParams.get("market") || "";
        }} catch (error) {{}}
        window.parent.postMessage({{
          source: "chart-workspace-open",
          symbol: button.dataset.symbol || "",
          market: market,
          interactiveSrc: button.dataset.interactiveSrc || "",
          symbols: buttons.map(item => item.dataset.symbol || ""),
          index: index
        }}, "*");
      }}

      function navigateActiveInteractiveChart(offset) {{
        const activeButton = document.querySelector(".stock-interactive-link.active");
        if (!activeButton) return;
        const section = activeButton.closest(".stock-detail-section");
        if (!section) return;
        const buttons = Array.from(section.querySelectorAll(".stock-interactive-link"));
        const index = buttons.indexOf(activeButton);
        const nextIndex = Math.max(0, Math.min(buttons.length - 1, index + offset));
        if (nextIndex !== index) renderInteractiveStockChart(section, buttons[nextIndex]);
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
        section.querySelectorAll(".stock-interactive-link").forEach(button => {{
          button.addEventListener("click", event => {{
            event.preventDefault();
            event.stopPropagation();
            renderInteractiveStockChart(section, button);
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

      window.addEventListener("message", event => {{
        const message = event && event.data;
        if (!message || message.source !== "nse-interactive-chart") return;
        if (message.action === "previous") navigateActiveInteractiveChart(-1);
        else if (message.action === "next") navigateActiveInteractiveChart(1);
        else if (message.action === "close") {{
          const activeButton = document.querySelector(".stock-interactive-link.active");
          if (activeButton) closeStockChart(activeButton.closest(".stock-detail-section"));
        }}
      }});

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
                    <th>Exit Reason</th>
                    <th>Exit Date</th>
                    <th>Exit Price</th>
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
    chart_event = _APP_CHART_EVENT_COMPONENT(
        table_html=component_html,
        default_height=height,
        key="backtest_results_chart_events",
        default=None,
    )
    chart_request = chart_request_from_component(chart_event)
    if chart_request:
        nonce = str(chart_event.get("nonce", ""))
        if (
            nonce
            and nonce
            != st.session_state.get("_handled_backtest_chart_nonce")
        ):
            st.session_state["_handled_backtest_chart_nonce"] = nonce
            if activate_chart_workspace(
                chart_request,
                fallback_market=interactive_market or MARKET_INDIA,
                origin_tab=2,
            ):
                st.rerun()


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
            window.parent.document.body.dataset.codexSuppressAlertsRefresh = 'true';
            tabs[tabIndex].click();
            delete window.parent.document.body.dataset.codexSuppressAlertsRefresh;
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


price_alerts_snapshot = session_price_alerts()
triggered_alert_badge_count = sum(
    alert.get("status") == "Triggered"
    and not bool(alert.get("acknowledged", False))
    for alert in price_alerts_snapshot
)
if triggered_alert_badge_count:
    st.markdown(
        f"""
        <style>
        .stTabs [role="tablist"] > [role="tab"]:nth-child(7) {{
            position: relative;
            padding-right: 1.75rem;
        }}
        .stTabs [role="tablist"] > [role="tab"]:nth-child(7)::after {{
            content: "{triggered_alert_badge_count}";
            position: absolute;
            top: 0.2rem;
            right: 0.32rem;
            display: grid;
            place-items: center;
            min-width: 1.05rem;
            height: 1.05rem;
            padding: 0 0.22rem;
            border: 2px solid white;
            border-radius: 999px;
            background: #dc2626;
            color: white;
            font-size: 0.66rem;
            font-weight: 800;
            line-height: 1;
            box-shadow: 0 1px 4px rgba(127, 29, 29, 0.28);
        }}
        </style>
        """,
        unsafe_allow_html=True,
    )


MAIN_TAB_LABELS = [
    "📥 Data",
    "🔍 Screener",
    "🧪 Backtest",
    "📊 Results",
    "📈 Chart",
    "⭐ Watchlists",
    "🔔 Alerts",
]

CHART_TAB_INDEX = 4
WATCHLISTS_TAB_INDEX = 5
ALERTS_TAB_INDEX = 6


def chart_workspace_context(request, fallback_market=MARKET_INDIA, origin_tab=3):
    """Normalize any chart launch into the shared Chart workspace state."""
    request = request if isinstance(request, dict) else {}
    source = str(request.get("interactiveSrc", "") or "")
    query = parse_qs(urlparse(source).query) if source else {}

    def query_value(name, default=""):
        values = query.get(name, [])
        return str(values[0]) if values else str(default or "")

    symbol = str(
        request.get("symbol")
        or query_value("interactive_chart")
        or ""
    ).strip().upper()
    market = normalize_market(
        request.get("market")
        or query_value("market")
        or fallback_market
    )
    symbols = []
    seen = set()
    for item in request.get("symbols", []) or []:
        clean = str(item or "").strip().upper()
        if clean and clean not in seen:
            symbols.append(clean)
            seen.add(clean)
    if symbol and symbol not in seen:
        symbols.append(symbol)
    try:
        requested_index = int(request.get("index", -1))
    except (TypeError, ValueError):
        requested_index = -1
    index = (
        requested_index
        if 0 <= requested_index < len(symbols)
        and symbols[requested_index] == symbol
        else symbols.index(symbol)
        if symbol in symbols
        else -1
    )
    ma_periods = []
    for token in query_value("ma").split(","):
        try:
            period = int(token)
        except (TypeError, ValueError):
            continue
        if period > 0 and period not in ma_periods:
            ma_periods.append(period)
    overlay = {
        "buyDate": query_value("buy_date"),
        "exitDate": query_value("exit_date"),
        "windowStart": query_value("window_start"),
        "windowEnd": query_value("window_end"),
        "buyPrice": query_value("buy_price"),
        "targetPrice": query_value("target_price"),
        "stopPrice": query_value("stop_price"),
        "exitPrice": query_value("exit_price"),
        "exitReason": query_value("exit_reason"),
        "alertDate": query_value("alert_date"),
        "alertPrice": query_value("alert_marker_price"),
    }
    return {
        "symbol": symbol,
        "market": market,
        "symbols": symbols,
        "index": index,
        "ma_periods": ma_periods,
        "initial_range": query_value("range", "252") or "252",
        "trade_overlay": {
            key: value for key, value in overlay.items() if value
        },
        "origin_tab": int(origin_tab),
    }


def activate_chart_workspace(request, fallback_market=MARKET_INDIA, origin_tab=3):
    context = chart_workspace_context(
        request,
        fallback_market=fallback_market,
        origin_tab=origin_tab,
    )
    if not context["symbol"]:
        return False
    st.session_state["_chart_workspace_context"] = context
    st.session_state["_main_workspace_tab"] = MAIN_TAB_LABELS[CHART_TAB_INDEX]
    st.session_state["_pending_main_tab_switch"] = CHART_TAB_INDEX
    return True


def chart_request_from_component(event):
    if not isinstance(event, dict):
        return None
    request = event.get("chartRequest")
    return request if isinstance(request, dict) else None


legacy_chart_symbol = str(
    query_param_value("interactive_chart", "") or ""
).strip()
if legacy_chart_symbol:
    activate_chart_workspace(
        {
            "symbol": legacy_chart_symbol,
            "market": query_param_value("market", MARKET_INDIA),
            "symbols": [legacy_chart_symbol],
            "index": 0,
        },
        origin_tab=3,
    )
    try:
        del st.query_params["interactive_chart"]
    except KeyError:
        pass

switch_to_results_requested = st.session_state.pop(
    "switch_to_results_tab",
    False,
)
if switch_to_results_requested:
    st.session_state["_main_workspace_tab"] = MAIN_TAB_LABELS[3]

tab1, tab2, tab3, tab4, tab5, tab6, tab7 = st.tabs(
    MAIN_TAB_LABELS,
    key="_main_workspace_tab",
    on_change="ignore",
)

pending_main_tab_switch = st.session_state.pop(
    "_pending_main_tab_switch",
    None,
)
if pending_main_tab_switch is not None:
    switch_to_tab(int(pending_main_tab_switch))

if switch_to_results_requested:
    switch_to_tab(3)

if st.session_state.pop("switch_to_alerts_tab", False) or query_param_value("open_alerts", ""):
    switch_to_tab(ALERTS_TAB_INDEX)

price_alert_feedback = st.session_state.pop("price_alert_feedback", None)
if price_alert_feedback:
    level, message = price_alert_feedback
    if level == "error":
        st.error(message)
    else:
        st.toast(message, icon="🔔")
if st.session_state.pop("price_alert_login_required", False):
    render_login_prompt(
        "Sign in with Google to create and manage personal price alerts.",
        key="price_alert_feedback_login",
        error=True,
    )


# =====================================================================
# TAB 1: DATA MANAGEMENT
# =====================================================================
with tab1:
    render_workspace_banner(
        "data",
        "Workspace 01 · Data foundation",
        "Data Management",
        "Maintain the market universe and keep price history ready for reliable screening and analysis.",
        "▣",
        "Prepare",
    )

    market_options = [MARKET_INDIA, MARKET_US]
    market_col, status_col = st.columns(2)
    with market_col:
        with st.container(border=True):
            st.markdown(
                '<div class="data-panel-heading tone-blue"><span>🌐</span>Market</div>'
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
                '<div class="data-panel-heading tone-green"><span>◷</span>Data Status</div>'
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
                '<div class="data-panel-heading tone-violet"><span>🗂️</span>Source File</div>'
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
                '<div class="data-panel-heading tone-amber"><span>⚙️</span>Download Settings</div>'
                '<p class="data-panel-subtitle">Set the universe size and choose incremental or full refresh.</p>',
                unsafe_allow_html=True,
            )
            download_limit_max = available_symbol_count or max(saved_download_limit, 1000)
            download_limit_value = min(max(1, saved_download_limit), int(download_limit_max))
            slider_key = f"download_limit_slider_{selected_market.lower()}"
            field_key = f"download_limit_field_{selected_market.lower()}"
            current_slider_value = int(st.session_state.get(slider_key, download_limit_value))
            current_slider_value = min(max(1, current_slider_value), int(download_limit_max))
            st.session_state[slider_key] = current_slider_value
            if field_key not in st.session_state:
                st.session_state[field_key] = str(current_slider_value)

            def sync_download_limit_field():
                st.session_state[field_key] = str(st.session_state[slider_key])

            def sync_download_limit_slider():
                try:
                    entered_value = int(str(st.session_state[field_key]).strip())
                except (TypeError, ValueError):
                    entered_value = int(st.session_state[slider_key])
                entered_value = min(max(1, entered_value), int(download_limit_max))
                st.session_state[slider_key] = entered_value
                st.session_state[field_key] = str(entered_value)

            slider_col, field_col = st.columns([4, 1])
            with slider_col:
                st.slider(
                    "Number of stocks",
                    min_value=1,
                    max_value=int(download_limit_max),
                    step=1,
                    key=slider_key,
                    on_change=sync_download_limit_field,
                    help=f"{available_symbol_count} symbols are available in the selected {source_label} file." if available_symbol_count else None,
                )
            with field_col:
                st.text_input(
                    "Exact count",
                    key=field_key,
                    on_change=sync_download_limit_slider,
                    help="Type an exact stock count and press Enter.",
                )
            download_limit = int(st.session_state[slider_key])

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

    update_changed_settings({
        "market": selected_market,
        "download_tf": download_tf,
        limit_setting_key: download_limit,
    })

    if download_clicked:
        if not symbols_file.exists():
            st.error(f"❌ Add {symbols_file.name} before downloading {market_label(selected_market)} stock data.")
        else:
            existing_job, started = start_background_download(
                symbols_file,
                download_tf,
                download_limit,
                incremental=not full_refresh,
                market=selected_market,
            )
            if not started:
                st.warning(
                    f"A {market_label(existing_job.get('market'))} stock download is already running."
                )

    download_job = background_download_snapshot(selected_market)
    if download_job:
        if download_job.get("running"):
            render_live_download_activity(selected_market)
        else:
            completed_at = download_job.get("completed_at", "")
            if completed_at and settings.get("last_download_job_id") != download_job.get("id"):
                update_settings({
                    "market": selected_market,
                    "last_download_at": completed_at,
                    "last_download_tf": download_tf,
                    "last_download_market": selected_market,
                    "last_download_job_id": download_job.get("id"),
                })
            render_download_job_status(download_job)


# =====================================================================
# TAB 2: SCREENER
# =====================================================================
@st.fragment
def render_screener_workspace():
    fragment_fast_favorite_selection = st.session_state.pop(
        "_fast_favorite_selection",
        fast_favorite_selection,
    )
    current_market = normalize_market(selected_market)
    render_workspace_banner(
        "screener",
        "Workspace 02 · Opportunity discovery",
        "Screener",
        "Build focused technical, valuation, and custom-expression rules to identify matching stocks.",
        "⌕",
        f"{market_label(current_market)} market",
    )
    st.markdown(
        f'<div class="screener-market-chip">● {html.escape(market_label(current_market))} market</div>',
        unsafe_allow_html=True,
    )

    # ---- Initialize session state for filter set ----
    if "screener_filter_set" in settings:
        loaded_filter_set = normalize_filter_set(settings.get("screener_filter_set"), use_default=False)
    else:
        loaded_filter_set = normalize_filter_set(DEFAULT_FILTER_SET)
    loaded_filter_set = merge_legacy_expression_filters(
        loaded_filter_set,
        settings.get("pattern_expressions", []),
    )

    if "current_filter_set" not in st.session_state:
        st.session_state["current_filter_set"] = deepcopy(loaded_filter_set)
        st.session_state["next_filter_id"] = (
            max((int(item.get("id", 0)) for item in loaded_filter_set), default=0) + 1
        )

    if "next_filter_id" not in st.session_state:
        st.session_state["next_filter_id"] = (
            max((int(item.get("id", 0)) for item in st.session_state["current_filter_set"]), default=0) + 1
        )

    if "_active_favorite_filter_name" not in st.session_state:
        saved_active_name = settings.get("selected_favorite_filter_set")
        if (
            saved_active_name in favorite_filter_sets
            and filter_set_matches_favorite(st.session_state["current_filter_set"], saved_active_name)
        ):
            st.session_state["_active_favorite_filter_name"] = saved_active_name
        else:
            st.session_state["_active_favorite_filter_name"] = None

    filter_widget_prefix = "ma_filter"

    # ===== TOP SECTION: Favorite Filter Selection + Run Screener =====
    command_col, builder_col = st.columns([1.35, 1])
    with command_col:
        quick_run_panel = st.container(border=True)
    with quick_run_panel:
        st.markdown(
            '<div class="data-panel-heading tone-violet"><span>⚡</span>Quick Run</div>'
            '<p class="data-panel-subtitle">Choose a filter set, adjust optional checks, and start screening.</p>',
            unsafe_allow_html=True,
        )

    tf = "DAY"
    with quick_run_panel:
        st.markdown(
            '<div class="quick-run-section-label">Options'
            '<span>Optional checks applied when screening</span></div>',
            unsafe_allow_html=True,
        )
        quick_run_options = st.container(key="quick_run_options")
    with quick_run_options:
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
    if not fragment_fast_favorite_selection:
        update_changed_settings({
            "tf": tf,
            "create_charts": create_charts,
            "green_candle_toggle": green_candle_toggle,
            "green_candle_min_gain_pct": green_candle_min_gain_pct,
        })

    # ---- Favorite Filter Set ----
    def request_saved_strategy_removal(display_name):
        st.session_state["_remove_saved_strategy_requested"] = display_name

    selected_fav = None
    with quick_run_panel:
        st.markdown(
            '<div class="quick-run-section-label">Saved strategies'
            '<span>Select the setup to load</span></div>',
            unsafe_allow_html=True,
        )
        favorite_names = sorted(favorite_filter_sets.keys())
        if favorite_names:
            favorite_options = favorite_names
            active_favorite_name = st.session_state.get("_active_favorite_filter_name")
            desired_widget_selection = (
                active_favorite_name if active_favorite_name in favorite_filter_sets else None
            )
            # Keep the card selection synchronized before the widgets are
            # instantiated. A custom working set intentionally selects no card.
            if st.session_state.get("_favorite_select_widget") != desired_widget_selection:
                st.session_state["_favorite_select_widget"] = desired_widget_selection

            def on_favorite_filter_selected():
                """Callback that fires immediately when the user picks a new favourite."""
                selected = st.session_state["_favorite_select_widget"]
                apply_filter_selection_to_state(selected)

            selected_fav = st.selectbox(
                "⭐ Filter Set To Run",
                favorite_options,
                key="_favorite_select_widget",
                on_change=on_favorite_filter_selected,
                format_func=favorite_option_label,
                removable_options=personal_favorite_keys.keys(),
                on_remove=request_saved_strategy_removal,
                help="Select a saved favorite filter set to load all of its filters.",
            )
        else:
            st.info("No saved favorite filters yet. Configure filters below and save them.")

    remove_saved_strategy = st.session_state.pop(
        "_remove_saved_strategy_requested",
        None,
    )
    if remove_saved_strategy:
        st.session_state["_remove_saved_strategy_pending"] = remove_saved_strategy

    @st.dialog("Remove saved strategy?")
    def confirm_saved_strategy_removal(display_name, stored_name):
        st.warning(
            f'You are about to permanently remove "{stored_name}". '
            "This saved filter setup cannot be recovered."
        )
        confirm_col, cancel_col = st.columns(2)
        with confirm_col:
            confirmed = st.button(
                "Remove strategy",
                type="primary",
                use_container_width=True,
                key="confirm_saved_strategy_remove",
            )
        with cancel_col:
            cancelled = st.button(
                "Cancel",
                use_container_width=True,
                key="cancel_saved_strategy_remove",
            )
        if cancelled:
            st.session_state.pop("_remove_saved_strategy_pending", None)
            st.rerun()
        if confirmed:
            try:
                cloud_store.delete_filter_set(app_user.id, stored_name)
            except CloudStorageError as exc:
                st.error(str(exc))
            else:
                if (
                    st.session_state.get("_active_favorite_filter_name")
                    == remove_saved_strategy
                ):
                    st.session_state["_active_favorite_filter_name"] = None
                    update_settings({"selected_favorite_filter_set": CUSTOM_FILTER_NAME})
                st.session_state.pop("_remove_saved_strategy_pending", None)
                st.session_state.pop("_favorite_select_widget", None)
                st.toast(f"Removed saved strategy: {stored_name}", icon="🗑️")
                st.rerun()

    pending_saved_strategy_removal = st.session_state.get(
        "_remove_saved_strategy_pending"
    )
    if pending_saved_strategy_removal:
        stored_name = personal_favorite_keys.get(pending_saved_strategy_removal)
        if app_user is None:
            st.session_state.pop("_remove_saved_strategy_pending", None)
            render_login_prompt(
                "Sign in with Google before removing a personal saved strategy.",
                key="saved_strategy_remove_login",
                error=True,
            )
        elif cloud_store is None:
            st.session_state.pop("_remove_saved_strategy_pending", None)
            st.error("Cloud storage is not configured, so this strategy cannot be removed.")
        elif not stored_name:
            st.session_state.pop("_remove_saved_strategy_pending", None)
            st.error("Only personal saved strategies can be removed.")
        else:
            confirm_saved_strategy_removal(
                pending_saved_strategy_removal,
                stored_name,
            )

    active_screener_job = attach_registered_screener_job()
    with quick_run_panel:
        # Keep live progress directly above the primary action so it appears
        # in the same place immediately after Run Screener is clicked.
        screener_progress_placeholder = st.empty()
        if (
            active_screener_job
            and (
                active_screener_job.get("running")
                or (
                    not active_screener_job.get("error")
                    and not active_screener_job.get("results_tab_opened")
                )
            )
        ):
            with screener_progress_placeholder.container():
                render_active_screener_progress()
        quick_run_action = st.container(key="quick_run_action")
    with quick_run_action:
        run_combined = st.button(
            "▶️ Run Screener",
            type="primary",
            use_container_width=True,
            disabled=bool(
                active_screener_job
                and active_screener_job.get("running")
            ),
        )

    # Read current_filter_set from session state now (after selectbox may have updated it)
    current_filter_set = st.session_state["current_filter_set"]

    # ---- Add Filter Row ----
    with builder_col:
        add_filter_panel = st.container(border=True)
    with add_filter_panel:
        st.markdown(
            '<div class="data-panel-heading tone-blue"><span>＋</span>Add a Filter</div>'
            '<p class="data-panel-subtitle">Choose a technical, valuation, or custom expression rule.</p>',
            unsafe_allow_html=True,
        )
        filter_type_to_add = st.selectbox(
            "Filter Category",
            [key for key in FILTER_TYPE_LABELS if key != "green_candle_today"],
            format_func=lambda value: FILTER_TYPE_LABELS[value],
        )
        add_filter = st.button(
            "➕ Add",
            use_container_width=True,
            help="Add the selected rule to the current filter set.",
        )

    if add_filter:
        mark_current_filter_custom()
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

    current_filter_heading = st.empty()

    if not current_filter_set:
        st.info("No filters selected. Screening will pass stocks through this tab.")

    rendered_filter_set = []
    valid_pattern_expressions = []
    invalid_pattern_errors = []
    pattern_lookback_days = int(settings.get("pattern_lookback_days", 120))
    pattern_reversal_pct = float(settings.get("pattern_reversal_pct", 5.0))
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
            current_filter_card = st.container(
                key=f"current_filter_card_{filter_id}_v{widget_key_version}"
            )

        with current_filter_card:
            filter_expander = st.expander(expander_label, expanded=False)
            remove_filter = st.button(
                "−",
                key=f"current_filter_remove_{filter_id}_v{widget_key_version}",
                help=f"Remove {filter_label} from the current filter set.",
            )

        if remove_filter:
            mark_current_filter_custom()
            st.session_state["current_filter_set"] = [
                item for item in current_filter_set if item["id"] != filter_id
            ]
            st.rerun()

        with filter_expander:
            if filter_type == "custom_expression":
                expression = st.text_input(
                    "Expression",
                    value=str(params.get("expression", "")),
                    key=f"{filter_widget_prefix}_{filter_id}_expression_v{widget_key_version}",
                    placeholder="e.g. P > SMA200 and ROI(50) > 0",
                    help="Every Custom Filter must evaluate to true for a stock to match.",
                    on_change=mark_current_filter_custom,
                )
                params["expression"] = expression
                if not expression.strip():
                    st.info("Enter an expression for this Custom Filter.")
                    invalid_pattern_errors.append(
                        f"Custom Filter {index}: Expression is required."
                    )
                else:
                    is_valid, error = validate_expression(expression)
                    if is_valid:
                        st.success("✅ Valid and supported expression")
                        valid_pattern_expressions.append(expression.strip())
                    else:
                        st.error(f"❌ {error}")
                        invalid_pattern_errors.append(f"Custom Filter {index}: {error}")
                st.markdown(
                    '<div class="data-panel-heading tone-slate"><span>⌨️</span>Allowed Keywords</div>'
                    '<p class="data-panel-subtitle">Tap or click any keyword to see what it means.</p>'
                    + expression_keyword_reference_html(),
                    unsafe_allow_html=True,
                )

            elif filter_type == "ma_rising":
                params["ma"] = int(st.number_input(
                    "MA",
                    min_value=2,
                    max_value=1000,
                    value=int(params.get("ma", 200)),
                    key=f"{filter_widget_prefix}_{filter_id}_ma_v{widget_key_version}",
                    on_change=mark_current_filter_custom,
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
                        on_change=mark_current_filter_custom,
                    ))
                with col2:
                    params["long_ma"] = int(st.number_input(
                        "Long MA",
                        min_value=2,
                        max_value=1000,
                        value=int(params.get("long_ma", 200)),
                        key=f"{filter_widget_prefix}_{filter_id}_long_ma_v{widget_key_version}",
                        on_change=mark_current_filter_custom,
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
                        on_change=mark_current_filter_custom,
                    ))
                with col2:
                    params["threshold_pct"] = float(st.number_input(
                        "Within Percent",
                        min_value=0.1,
                        max_value=100.0,
                        value=float(params.get("threshold_pct", 5.0)),
                        step=0.1,
                        key=f"{filter_widget_prefix}_{filter_id}_threshold_pct_v{widget_key_version}",
                        on_change=mark_current_filter_custom,
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
                        on_change=mark_current_filter_custom,
                    ))
                with col2:
                    params["long_ma"] = int(st.number_input(
                        "Long MA",
                        min_value=2,
                        max_value=1000,
                        value=int(params.get("long_ma", 200)),
                        key=f"{filter_widget_prefix}_{filter_id}_golden_long_ma_v{widget_key_version}",
                        on_change=mark_current_filter_custom,
                    ))
                with col3:
                    params["lookback_units"] = int(st.number_input(
                        "Last N Time Frame Units",
                        min_value=1,
                        max_value=1000,
                        value=int(params.get("lookback_units", 20)),
                        key=f"{filter_widget_prefix}_{filter_id}_golden_lookback_v{widget_key_version}",
                        on_change=mark_current_filter_custom,
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
                        on_change=mark_current_filter_custom,
                    ))
                with col2:
                    params["down_pct"] = float(st.number_input(
                        "Down Percent",
                        min_value=0.1,
                        max_value=100.0,
                        value=float(params.get("down_pct", 5.0)),
                        step=0.1,
                        key=f"{filter_widget_prefix}_{filter_id}_down_pct_v{widget_key_version}",
                        on_change=mark_current_filter_custom,
                    ))
                with col3:
                    params["lookback_units"] = int(st.number_input(
                        "Last M Time Frame Units",
                        min_value=2,
                        max_value=2000,
                        value=int(params.get("lookback_units", 50)),
                        key=f"{filter_widget_prefix}_{filter_id}_down_lookback_v{widget_key_version}",
                        on_change=mark_current_filter_custom,
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
                        on_change=mark_current_filter_custom,
                    ))
                with col2:
                    params["up_pct"] = float(st.number_input(
                        "Up Percent",
                        min_value=0.1,
                        max_value=100.0,
                        value=float(params.get("up_pct", 5.0)),
                        step=0.1,
                        key=f"{filter_widget_prefix}_{filter_id}_up_pct_v{widget_key_version}",
                        on_change=mark_current_filter_custom,
                    ))
                with col3:
                    params["lookback_units"] = int(st.number_input(
                        "Last M Time Frame Units",
                        min_value=2,
                        max_value=2000,
                        value=int(params.get("lookback_units", 50)),
                        key=f"{filter_widget_prefix}_{filter_id}_up_lookback_v{widget_key_version}",
                        on_change=mark_current_filter_custom,
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
                        on_change=mark_current_filter_custom,
                        help="Number of previous data frames to search for the All-Time High.",
                    ))
                with col2:
                    params["recent_n"] = int(st.number_input(
                        "ATH Hit In Last N Frames",
                        min_value=1,
                        max_value=500,
                        value=int(params.get("recent_n", 10)),
                        key=f"{filter_widget_prefix}_{filter_id}_ath_recent_n_v{widget_key_version}",
                        on_change=mark_current_filter_custom,
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
                        on_change=mark_current_filter_custom,
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
                        on_change=mark_current_filter_custom,
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
                        on_change=mark_current_filter_custom,
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
                    on_change=mark_current_filter_custom,
                ))

            elif filter_type == "green_candle_today":
                params["min_gain_pct"] = float(st.number_input(
                    "Minimum Gain Percent",
                    min_value=0.0,
                    max_value=100.0,
                    value=float(params.get("min_gain_pct", 1.0)),
                    step=0.1,
                    key=f"{filter_widget_prefix}_{filter_id}_green_min_gain_pct_v{widget_key_version}",
                    on_change=mark_current_filter_custom,
                ))

        rendered_filter_set.append({
            "id": filter_id,
            "type": filter_type,
            "params": params,
        })

    st.session_state["current_filter_set"] = rendered_filter_set
    filter_set = normalize_filter_set(rendered_filter_set, use_default=False)
    active_filter_count = len(filter_set)

    active_favorite_name = st.session_state.get("_active_favorite_filter_name")
    if (
        active_favorite_name
        and not filter_set_matches_favorite(filter_set, active_favorite_name)
    ):
        mark_current_filter_custom()
        active_favorite_name = None

    current_filter_label = active_favorite_name or CUSTOM_FILTER_NAME
    current_filter_status_class = (
        "is-favorite" if active_favorite_name else "is-custom"
    )
    current_filter_status_icon = "⭐" if active_favorite_name else "✦"
    save_custom_favorite = False
    inline_favorite_name = ""
    with current_filter_heading.container():
        heading_col, save_strategy_col = st.columns(
            [12, 1],
            vertical_alignment="center",
        )
        with heading_col:
            st.markdown(
                f'<div class="screener-section-heading">'
                f'<div class="screener-section-heading__title">Current Filter Set '
                f'<span class="screener-section-heading__status {current_filter_status_class}">'
                f'{current_filter_status_icon} {html.escape(current_filter_label)}</span></div>'
                f'<div class="screener-section-heading__count">{active_filter_count} active</div>'
                f'</div>',
                unsafe_allow_html=True,
            )
        with save_strategy_col:
            if not active_favorite_name:
                with st.popover(
                    "Save Filters",
                    key="save_custom_strategy_popover",
                    use_container_width=True,
                    disabled=not active_filter_count,
                    help="Save this custom filter set as a personal favorite.",
                ):
                    st.markdown("**Save custom strategy**")
                    inline_favorite_name = st.text_input(
                        "Strategy name",
                        key="_inline_favorite_name",
                        placeholder="e.g. Golden Cross + PE",
                    )
                    save_custom_favorite = st.button(
                        "Save",
                        key="save_custom_strategy",
                        type="primary",
                        use_container_width=True,
                    )

    if not fragment_fast_favorite_selection:
        update_changed_settings({
            "selected_favorite_filter_set": (
                st.session_state.get("_active_favorite_filter_name")
                or CUSTOM_FILTER_NAME
            ),
            "screener_filter_set": filter_set,
            "pattern_lookback_days": pattern_lookback_days,
            "pattern_reversal_pct": pattern_reversal_pct,
            "pattern_expressions": custom_filter_expressions(filter_set),
        })

    if st.session_state.pop("_favorite_edit_login_required", False):
        render_login_prompt(
            "Your filter changes are temporary in guest mode. Sign in with Google to save them as a personal favorite.",
            key="favorite_edit_login",
            error=True,
        )

    if save_custom_favorite:
        if app_user is None:
            render_login_prompt(
                "Sign in with Google before saving this custom strategy.",
                key="inline_favorite_save_login",
                error=True,
            )
        elif cloud_store is None:
            st.error("Cloud storage is not configured, so this strategy cannot be saved.")
        elif not inline_favorite_name.strip():
            st.error("Enter a strategy name before saving.")
        elif inline_favorite_name.strip() in personal_filter_sets:
            st.error("A personal strategy with that name already exists. Choose a new name.")
        else:
            clean_name = inline_favorite_name.strip()
            favorite_data = {
                "ma_filter_set": filter_set,
                "pattern": {
                    "lookback_days": pattern_lookback_days,
                    "reversal_pct": pattern_reversal_pct,
                    "expressions": custom_filter_expressions(filter_set),
                },
            }
            try:
                cloud_store.save_filter_set(app_user.id, clean_name, favorite_data)
            except (CloudStorageError, ValueError) as exc:
                st.error(str(exc))
            else:
                display_name = personal_favorite_display_name(clean_name)
                st.session_state["_active_favorite_filter_name"] = display_name
                update_settings({"selected_favorite_filter_set": display_name})
                st.session_state.pop("_favorite_select_widget", None)
                st.session_state.pop("_inline_favorite_name", None)
                st.toast(f"Saved personal strategy: {clean_name}", icon="⭐")
                st.rerun()

    # ===== RUN SCREENER LOGIC =====
    if run_combined:
        screener_progress_placeholder.progress(
            0.0,
            text="Preparing the stock universe…",
        )
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
        selected_symbols = load_top_symbols(
            symbols_file,
            limit=int(download_limit),
            market=current_market,
        )
        market_cap_positions = {
            symbol: position
            for position, symbol in enumerate(selected_symbols, start=1)
        }
        stock_files = stock_files_for_symbols(target_dir, selected_symbols)
        missing_stock_count = len(selected_symbols) - len(stock_files)
        if missing_stock_count:
            st.warning(
                f"{missing_stock_count} of the selected {len(selected_symbols)} stocks do not have "
                "downloaded JSON data and will be skipped."
            )
        if not stock_files:
            st.error("No downloaded stock data is available for the selected Data Management universe.")
            st.stop()

        active_job = drain_live_screener_events()
        if active_job and active_job.get("running"):
            st.warning("A screener run is already in progress. Its progress is shown above.")
            st.stop()

        st.session_state["results"] = []
        active_name = st.session_state.get("_active_favorite_filter_name")
        result_filter_name = (
            active_name
            if active_name and active_name in favorite_filter_sets
            else CUSTOM_FILTER_NAME
        )
        latest_summary = data_availability_summary(
            target_dir,
            market=current_market,
        )
        filter_condition_lines = [
            f"{FILTER_TYPE_LABELS.get(item['type'], item['type'])}: "
            + ", ".join(f"{key}={value}" for key, value in item.get("params", {}).items())
            for item in run_filter_set
        ]
        result_metadata = {
            "filter_name": result_filter_name,
            "market": current_market,
            "latest_data_date": (
                latest_summary["Latest Date"].strftime("%Y-%m-%d")
                if latest_summary.get("Latest Date") is not None
                else ""
            ),
            "filter_conditions": filter_condition_lines + list(run_pattern_expressions),
            "create_charts": bool(create_charts),
            "run_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        }
        st.session_state["last_results_metadata"] = result_metadata
        update_changed_settings({
            "selected_favorite_filter_set": result_filter_name,
            "screener_filter_set": run_filter_set,
            "pattern_lookback_days": run_lookback_days,
            "pattern_reversal_pct": run_reversal_pct,
            "pattern_expressions": list(run_pattern_expressions),
            "last_results_market": current_market,
        })
        st.session_state["screener_job"] = start_live_screener_job(
            stock_files,
            market_cap_positions,
            run_filter_set,
            current_market,
            run_lookback_days,
            run_reversal_pct,
            run_pattern_expressions,
            create_charts,
            owner_key=screener_job_owner_key(),
        )
        st.rerun(scope="fragment")


with tab2:
    render_screener_workspace()


# =====================================================================
# TAB 3: BACKTEST
# =====================================================================
with tab3:
    current_market = normalize_market(selected_market)
    render_workspace_banner(
        "backtest",
        "Workspace 03 · Strategy validation",
        "Backtest",
        "Test saved strategies across historical market data with equal-weight performance analysis.",
        "↻",
        f"{market_label(current_market)} market",
    )

    favorite_names = sorted(favorite_filter_sets.keys())
    if not favorite_names:
        st.info("No saved favorite filters yet. Save filters from the Screener tab before running a backtest.")
    else:
        backtest_tf = "DAY"

        target_dir = timeframe_config(backtest_tf, current_market)["target_dir"]
        stock_files = stock_data_files(target_dir)
        benchmark_file = symbol_path(target_dir, NIFTY_DATA_SYMBOL) if current_market == MARKET_INDIA else None
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

            if "backtest_date_range_input" not in st.session_state:
                st.session_state["backtest_date_range_input"] = (default_start, default_end)
            else:
                saved_range = st.session_state["backtest_date_range_input"]
                if (
                    not isinstance(saved_range, (tuple, list))
                    or len(saved_range) != 2
                    or saved_range[0] < min_date
                    or saved_range[1] > max_date
                    or saved_range[0] >= saved_range[1]
                ):
                    st.session_state["backtest_date_range_input"] = (default_start, default_end)

            selected_start_date, selected_end_date = st.slider(
                "Backtest date range",
                min_value=min_date,
                max_value=max_date,
                format="DD-MM-YYYY",
                help="Find stocks on the start date, then calculate the equal-weight portfolio gain through the end date.",
                key="backtest_date_range_input",
                on_change=persist_backtest_widget_settings,
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
        if "backtest_selected_filters_input" not in st.session_state:
            st.session_state["backtest_selected_filters_input"] = (
                saved_backtest_filters or favorite_names[:1]
            )
        else:
            available_session_filters = [
                name
                for name in st.session_state["backtest_selected_filters_input"]
                if name in favorite_names
            ]
            if available_session_filters != st.session_state["backtest_selected_filters_input"]:
                st.session_state["backtest_selected_filters_input"] = available_session_filters

        selected_backtest_filters = st.multiselect(
            "Favorite filters",
            favorite_names,
            key="backtest_selected_filters_input",
            format_func=favorite_option_label,
            help="Select one or more saved favorite filter sets to compare.",
            on_change=persist_backtest_widget_settings,
        )

        if "backtest_green_candle_only_input" not in st.session_state:
            st.session_state["backtest_green_candle_only_input"] = bool(
                settings.get("backtest_green_candle_only", False)
            )
        backtest_green_candle_only = st.toggle(
            "🟢 Green Candle on Buy Date",
            key="backtest_green_candle_only_input",
            help=(
                "Apply to every selected favorite filter. Only stocks whose Buy Date candle "
                "has Close greater than Open will be included in the backtest."
            ),
            on_change=persist_backtest_widget_settings,
        )

        if "backtest_target_expression_input" not in st.session_state:
            st.session_state["backtest_target_expression_input"] = str(
                settings.get("backtest_target_expression", "")
            )
        if "backtest_stop_loss_expression_input" not in st.session_state:
            st.session_state["backtest_stop_loss_expression_input"] = str(
                settings.get("backtest_stop_loss_expression", "")
            )
        if "backtest_closing_basis_input" not in st.session_state:
            st.session_state["backtest_closing_basis_input"] = bool(
                settings.get("backtest_closing_basis", False)
            )

        with st.container(border=True):
            st.markdown(
                '<div class="data-panel-heading tone-amber"><span>🎯</span>Sell Strategy'
                '<details class="sell-strategy-help">'
                '<summary aria-label="Sell Strategy help" title="Sell Strategy help">?</summary>'
                '<div class="sell-strategy-help__popup">'
                '<strong>Sell Strategy Help</strong>'
                '<ul>'
                '<li><code>10%</code> sets a price 10% above the buy price.</li>'
                '<li><code>-10%</code> sets a price 10% below the buy price.</li>'
                '<li><code>min(Candle[0..-1].Low) - 1%</code> sets a price 1% below the lower Low of the buy candle and its previous candle.</li>'
                '<li><code>SMA50 - 1%</code> is dynamic: every future candle uses that day\'s SMA50 value, reduced by 1%.</li>'
                '<li>In these expressions, <code>Candle[0]</code> is always the buy-date candle.</li>'
                '</ul>'
                '</div>'
                '</details>'
                '</div>'
                '<p class="data-panel-subtitle">Book each equal-weight position when its target or stop loss is hit.</p>',
                unsafe_allow_html=True,
            )
            target_col, stop_col = st.columns(2)
            with target_col:
                backtest_target_expression = st.text_input(
                    "Target",
                    key="backtest_target_expression_input",
                    placeholder="e.g. 10% or Candle[0].High + 2%",
                    help="10% means 10% above the buy price. A candle expression is evaluated once on the buy date.",
                    on_change=persist_backtest_widget_settings,
                )
                target_is_valid, target_error = validate_sell_price_expression(
                    backtest_target_expression
                )
                if not backtest_target_expression.strip():
                    st.caption("Optional — leave blank for no target exit.")
                elif target_is_valid:
                    st.success("✅ Valid and supported expression")
                else:
                    st.error(f"❌ {target_error}")
            with stop_col:
                backtest_stop_loss_expression = st.text_input(
                    "Stop Loss",
                    key="backtest_stop_loss_expression_input",
                    placeholder="e.g. -10% or min(Candle[0..-1].Low) - 1%",
                    help=(
                        "-10% means 10% below the buy price. Candle[0] remains the buy-date candle. "
                        "SMA-based stops are recalculated for every future candle."
                    ),
                    on_change=persist_backtest_widget_settings,
                )
                stop_is_valid, stop_error = validate_sell_price_expression(
                    backtest_stop_loss_expression
                )
                if not backtest_stop_loss_expression.strip():
                    st.caption("Optional — leave blank for no stop-loss exit.")
                elif stop_is_valid:
                    st.success("✅ Valid and supported expression")
                else:
                    st.error(f"❌ {stop_error}")
            backtest_closing_basis = st.checkbox(
                "Closing Basis",
                key="backtest_closing_basis_input",
                help=(
                    "Enabled: use Close >= Target or Close <= Stop Loss and book at that close. "
                    "Disabled: use High/Low touches and book at the evaluated target/stop price."
                ),
                on_change=persist_backtest_widget_settings,
            )
            st.caption(
                "Exit checks start on the candle after the buy date. If Target and Stop Loss are both touched "
                "inside the same candle, Stop Loss is applied first."
            )
        sell_strategy = {
            "target": backtest_target_expression.strip(),
            "stop_loss": backtest_stop_loss_expression.strip(),
            "closing_basis": backtest_closing_basis,
        }

        run_backtest_clicked = st.button("Backtest", type="primary", use_container_width=True)

        if run_backtest_clicked:
            persist_backtest_widget_settings()
            sell_expression_errors = []
            for label, expression, is_valid, error in (
                ("Target", sell_strategy["target"], target_is_valid, target_error),
                ("Stop Loss", sell_strategy["stop_loss"], stop_is_valid, stop_error),
            ):
                if not is_valid:
                    sell_expression_errors.append(f"{label}: {error}")

            if sell_expression_errors:
                st.error("Fix the Sell Strategy: " + " ".join(sell_expression_errors))
            elif not selected_backtest_filters:
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
                        sell_strategy=sell_strategy,
                        green_candle_only=backtest_green_candle_only,
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
                st.session_state["backtest_result_sell_strategy"] = dict(sell_strategy)
                st.session_state["backtest_result_green_candle_only"] = backtest_green_candle_only

        summary_rows = st.session_state.get("backtest_summary_rows", [])
        series_by_filter = st.session_state.get("backtest_series_by_filter", {})
        stock_details_by_filter = st.session_state.get("backtest_stock_details_by_filter", {})
        if summary_rows:
            result_start, result_end = st.session_state.get("backtest_result_range", ("start date", "end date"))
            st.info(
                f"Showing equal-weight portfolio variation for stocks found on {result_start} through {result_end}."
            )
            result_sell_strategy = st.session_state.get("backtest_result_sell_strategy", {})
            if result_sell_strategy.get("target") or result_sell_strategy.get("stop_loss"):
                basis_label = "closing basis" if result_sell_strategy.get("closing_basis") else "intraday High/Low basis"
                st.caption(
                    f"Sell Strategy — Target: {result_sell_strategy.get('target') or 'not set'}; "
                    f"Stop Loss: {result_sell_strategy.get('stop_loss') or 'not set'}; {basis_label}."
                )
            if st.session_state.get("backtest_result_green_candle_only", False):
                st.caption("Green Candle filter applied: Buy Date Close must be greater than Open.")
            render_backtest_results_table(
                summary_rows,
                series_by_filter,
                stock_details_by_filter,
                interactive_market=current_market,
                backtest_favorite_filter_sets=favorite_filter_sets,
            )


# =====================================================================
# TAB 4: RESULTS
# =====================================================================
with tab4:
    render_workspace_banner(
        "results",
        "Workspace 04 · Decision review",
        "Results",
        "Review matched stocks, compare key metrics, and move from summary tables into detailed charts.",
        "▥",
        "Analyze",
    )

    live_screener_job = drain_live_screener_events()

    # Screening results intentionally live only in this browser session.
    if "results" not in st.session_state:
        st.session_state["results"] = []
        st.session_state["last_results_metadata"] = {}

    rows = st.session_state.get("results", [])
    live_job_running = bool(
        live_screener_job and live_screener_job.get("running")
    )
    if live_screener_job:
        total = live_screener_job.get("total", 0)
        done = live_screener_job.get("done", 0)
        matches = live_screener_job.get("matches", len(rows))
        max_workers = live_screener_job.get("max_workers", 1)
        if live_screener_job.get("running"):
            if live_screener_job.get("phase") == "charts":
                charts_done = live_screener_job.get("charts_done", 0)
                charts_total = live_screener_job.get("charts_total", matches)
                st.info(
                    f"Screening complete with {matches} match(es). "
                    f"Charts are attaching in the background: "
                    f"{charts_done}/{charts_total}."
                )
            else:
                st.info(
                    f"Screening is running with {max_workers} workers: "
                    f"{done}/{total} processed and {matches} match(es) found. "
                    "Progress remains available in the Screener tab."
                )
        elif live_screener_job.get("error"):
            st.error(f"Screener stopped: {live_screener_job['error']}")
        else:
            failed_count = live_screener_job.get("failed_count", 0)
            st.success(f"Screening complete: {done}/{total} processed, {matches} match(es) found.")
            if failed_count:
                st.warning(f"{failed_count} stock file(s) were skipped due to errors.")

    if (
        rows
        and not live_job_running
        and (not fast_favorite_selection or tab4.open)
    ):
        result_market_for_repair = normalize_market(settings.get("last_results_market", selected_market))
        result_timeframe_for_repair = "DAY"
        repair_filter_set = normalize_filter_set(
            settings.get("screener_filter_set", st.session_state.get("current_filter_set", [])),
            use_default=False,
        )
        result_metadata_for_repair = st.session_state.get(
            "last_results_metadata",
            {},
        )
        if (
            not live_job_running
            and result_metadata_for_repair.get("create_charts")
            and repair_blank_result_charts(
                rows,
                repair_filter_set,
                result_market_for_repair,
                result_timeframe_for_repair,
            )
        ):
            st.session_state["results"] = rows

    if (
        rows
        and not live_job_running
        and (not fast_favorite_selection or tab4.open)
    ):
        # Determine heading: favorite filter name or the edited working set.
        result_metadata = st.session_state.get("last_results_metadata", {})
        heading_label = result_metadata.get("filter_name") or CUSTOM_FILTER_NAME
        result_market = normalize_market(
            result_metadata.get("market", settings.get("last_results_market", selected_market))
        )
        latest_result_summary = data_availability_summary(
            timeframe_config("DAY", result_market)["target_dir"],
            market=result_market,
        )
        latest_result_date = latest_result_summary.get("Latest Date")
        latest_data_date = (
            latest_result_date.strftime("%Y-%m-%d")
            if latest_result_date is not None
            else result_metadata.get("latest_data_date") or "Unavailable"
        )
        conditions = result_metadata.get("filter_conditions") or []
        conditions_html = "".join(
            f"<li>{html.escape(str(condition))}</li>" for condition in conditions
        ) or "<li>No conditions recorded</li>"
        st.markdown(
            '<section class="results-run-heading">'
            f"<h3>{html.escape(str(heading_label))}</h3>"
            '<div class="results-run-heading__metrics">'
            f"<span><b>Market</b> {html.escape(market_label(result_market))}</span>"
            f"<span><b>Results</b> {len(rows)}</span>"
            f"<span><b>Latest data</b> {html.escape(str(latest_data_date))}</span>"
            "</div>"
            f"<details><summary>Filter conditions used</summary><ul>{conditions_html}</ul></details>"
            "</section>",
            unsafe_allow_html=True,
        )
        if result_market != normalize_market(selected_market):
            st.warning(
                f"These results are from {market_label(result_market)}. "
                f"Run the screener again to refresh results for {market_label(selected_market)}."
            )

        result_symbols_file = symbols_file_for_market(result_market)
        ranked_symbols = (
            load_top_symbols(
                result_symbols_file,
                limit=1_000_000,
                market=result_market,
            )
            if result_symbols_file.exists()
            else []
        )
        market_cap_positions = {
            symbol: position
            for position, symbol in enumerate(ranked_symbols, start=1)
        }
        display_rows = []
        for display_row in hydrate_result_valuations(rows, result_market):
            symbol = str(display_row.get("Symbol", "")).strip().upper()
            display_row["Market Cap Position"] = market_cap_positions.get(
                symbol,
                display_row.get("Market Cap Position", ""),
            )
            display_rows.append(display_row)
        display_rows.sort(
            key=lambda row: (
                int(row["Market Cap Position"])
                if str(row.get("Market Cap Position", "")).isdigit()
                else float("inf")
            )
        )

        df = pd.DataFrame(display_rows)
        df.index = range(1, len(df) + 1)
        display_df = df

        near_ma_periods = set(price_near_ma_periods(repair_filter_set))
        near_ma_periods.update(
            int(match.group(1))
            for column in df.columns
            if (match := re.fullmatch(r"ROI(\d+)", str(column)))
        )
        near_ma_periods = sorted(near_ma_periods)
        for period in near_ma_periods:
            roi_column = f"ROI{period}"
            if roi_column not in df.columns:
                df[roi_column] = None

        result_columns = [
            "Symbol",
            "PE Ratio",
            "Market Cap Position",
            *(f"ROI{period}" for period in near_ma_periods),
        ]

        display_df = display_df[[column for column in result_columns if column in display_df.columns]]

        table_df = display_df.copy()
        if "ValuationMedians" in df.columns:
            table_df["ValuationMedians"] = df["ValuationMedians"]

        if "ChartPath" in df.columns:
            chart_df = table_df.copy()
            chart_df["ChartPath"] = df["ChartPath"]
            if "ChartSource" in df.columns:
                chart_df["ChartSource"] = df["ChartSource"]
            result_chart_event = sortable_results_table(
                chart_df,
                interactive_market=result_market,
                interactive_ma_periods=required_ma_periods(repair_filter_set),
                component_key=f"results_table_{result_market}",
            )
        else:
            result_chart_event = sortable_results_table(
                table_df,
                interactive_market=result_market,
                interactive_ma_periods=required_ma_periods(repair_filter_set),
                component_key=f"results_table_{result_market}",
            )
        result_chart_request = chart_request_from_component(result_chart_event)
        if result_chart_request:
            result_chart_nonce = str(result_chart_event.get("nonce", ""))
            if (
                result_chart_nonce
                and st.session_state.get("_handled_results_chart_nonce")
                != result_chart_nonce
            ):
                st.session_state["_handled_results_chart_nonce"] = (
                    result_chart_nonce
                )
                if activate_chart_workspace(
                    result_chart_request,
                    fallback_market=result_market,
                    origin_tab=3,
                ):
                    st.rerun()

    else:
        if live_screener_job and live_screener_job.get("running"):
            st.info(
                "Screening is still running. Progress is shown in the Screener "
                "tab, and this tab will open automatically when the run finishes."
            )
        elif live_screener_job and not live_screener_job.get("error"):
            st.info("Screening completed, but no stocks matched the selected filters.")
        else:
            st.info("No results yet. Run the screener from the 'Screener' tab to see results here.")

# =====================================================================
# TAB 5: CHART
# =====================================================================
with tab5:
    render_workspace_banner(
        "chart",
        "Workspace 05 · Market chart",
        "Chart",
        "Search any downloaded stock, inspect candles and indicators, and review active alert levels.",
        "▥",
        "Analyze",
    )

    chart_workspace = st.session_state.get("_chart_workspace_context")
    if not isinstance(chart_workspace, dict) or not chart_workspace.get("symbol"):
        st.info(
            "Search a stock below, or open its interactive chart from Results, "
            "Backtest, Watchlists, or Alerts."
        )
        search_market_col, search_symbol_col = st.columns([1, 2.2])
        with search_market_col:
            chart_search_market = st.selectbox(
                "Market",
                [MARKET_INDIA, MARKET_US],
                format_func=market_label,
                key="chart_workspace_search_market",
            )
        chart_search_dir = timeframe_config(
            "DAY",
            chart_search_market,
        )["target_dir"].resolve()
        chart_search_symbols = [
            symbol_from_path(path)
            for path in list_symbol_paths(
                chart_search_dir,
                include_index=False,
            )
        ]
        with search_symbol_col:
            chart_search_symbol = st.selectbox(
                "Stock name",
                chart_search_symbols,
                index=None,
                placeholder="Type a stock symbol",
                key=f"chart_workspace_search_{chart_search_market}",
            )
        if chart_search_symbol and activate_chart_workspace(
            {
                "symbol": chart_search_symbol,
                "market": chart_search_market,
                "symbols": [],
                "index": -1,
            },
            fallback_market=chart_search_market,
            origin_tab=CHART_TAB_INDEX,
        ):
            st.rerun()
    else:
        chart_symbol = str(chart_workspace.get("symbol", "")).strip().upper()
        chart_market = normalize_market(
            chart_workspace.get("market", MARKET_INDIA)
        )
        chart_symbols = [
            str(item or "").strip().upper()
            for item in chart_workspace.get("symbols", [])
            if str(item or "").strip()
        ]
        try:
            chart_index = int(chart_workspace.get("index", -1))
        except (TypeError, ValueError):
            chart_index = -1
        if not (
            0 <= chart_index < len(chart_symbols)
            and chart_symbols[chart_index] == chart_symbol
        ):
            chart_index = (
                chart_symbols.index(chart_symbol)
                if chart_symbol in chart_symbols
                else -1
            )

        chart_target_dir = timeframe_config(
            "DAY",
            chart_market,
        )["target_dir"].resolve()
        chart_stock_file = symbol_path(
            chart_target_dir,
            chart_symbol,
        ).resolve()
        if (
            chart_stock_file.parent != chart_target_dir
            or not stock_exists(chart_stock_file)
        ):
            st.error(
                f"Daily chart data is unavailable for {chart_symbol}."
            )
        else:
            chart_overlay, chart_alert_markers = chart_alert_context(
                session_price_alerts(),
                chart_symbol,
                chart_market,
                chart_workspace.get("trade_overlay"),
            )
            chart_growth, chart_valuations = get_company_fundamentals(
                chart_symbol,
                chart_market,
            )
            workspace_watchlists = None
            if app_user is not None and cloud_store is not None:
                if "_cached_personal_watchlists" in st.session_state:
                    workspace_watchlists = deepcopy(
                        st.session_state["_cached_personal_watchlists"]
                    )
                else:
                    try:
                        workspace_watchlists = cloud_store.load_watchlists(
                            app_user.id
                        )
                    except CloudStorageError as exc:
                        st.error(str(exc))
                        workspace_watchlists = []
                    else:
                        st.session_state[
                            "_cached_personal_watchlists"
                        ] = deepcopy(workspace_watchlists)

            def handle_chart_navigation(event):
                action = str(event.get("action", ""))
                updated = deepcopy(chart_workspace)
                if action in {"previous", "next"}:
                    offset = -1 if action == "previous" else 1
                    next_index = chart_index + offset
                    if 0 <= next_index < len(chart_symbols):
                        updated["index"] = next_index
                        updated["symbol"] = chart_symbols[next_index]
                        updated["trade_overlay"] = {}
                elif action in {"symbol-select", "symbol-search"}:
                    requested = str(
                        event.get("symbol", "")
                    ).strip().upper()
                    requested_file = symbol_path(
                        chart_target_dir,
                        requested,
                    ).resolve()
                    if (
                        requested_file.parent != chart_target_dir
                        or not stock_exists(requested_file)
                    ):
                        st.toast(
                            f"No downloaded {chart_market} data for "
                            f"{requested}.",
                            icon="⚠️",
                        )
                        return
                    updated["symbol"] = requested
                    updated["index"] = (
                        chart_symbols.index(requested)
                        if requested in chart_symbols
                        else -1
                    )
                    updated["trade_overlay"] = {}
                elif action == "close":
                    st.session_state.pop("_chart_workspace_context", None)
                    origin = int(chart_workspace.get("origin_tab", 3))
                    origin = max(0, min(len(MAIN_TAB_LABELS) - 1, origin))
                    st.session_state["_main_workspace_tab"] = (
                        MAIN_TAB_LABELS[origin]
                    )
                    st.session_state["_pending_main_tab_switch"] = origin
                    st.rerun()
                    return
                else:
                    return
                st.session_state["_chart_workspace_context"] = updated
                st.rerun()

            def add_workspace_chart_to_watchlist(event):
                watchlist_id = str(event.get("watchlistId", "") or "")
                selected = next(
                    (
                        item
                        for item in (workspace_watchlists or [])
                        if str(item.get("id", "")) == watchlist_id
                    ),
                    None,
                )
                if selected is None or app_user is None or cloud_store is None:
                    st.toast("Select an available watchlist.", icon="⚠️")
                    return
                existing_items = selected.get("items", [])
                if any(
                    str(item.get("symbol", "")).strip().upper()
                    == chart_symbol
                    and normalize_market(
                        item.get("market", MARKET_INDIA)
                    )
                    == chart_market
                    for item in existing_items
                ):
                    st.toast(
                        f"{chart_symbol} is already in {selected['name']}."
                    )
                    return
                try:
                    cloud_store.save_watchlist_item(
                        app_user.id,
                        watchlist_id,
                        chart_symbol,
                        chart_market,
                        "",
                        len(existing_items),
                    )
                except CloudStorageError as exc:
                    st.error(str(exc))
                    return
                selected.setdefault("items", []).append({
                    "symbol": chart_symbol,
                    "market": chart_market,
                    "note": "",
                    "position": len(existing_items),
                })
                st.session_state["_cached_personal_watchlists"] = deepcopy(
                    workspace_watchlists
                )
                st.toast(
                    f"Added {chart_symbol} to {selected['name']}.",
                    icon="⭐",
                )

            render_interactive_stock_chart(
                chart_symbol,
                chart_stock_file,
                ma_periods=chart_workspace.get("ma_periods") or None,
                match_position=chart_index + 1 if chart_index >= 0 else None,
                match_total=len(chart_symbols) if chart_index >= 0 else None,
                has_previous=chart_index > 0,
                has_next=(
                    chart_index >= 0
                    and chart_index < len(chart_symbols) - 1
                ),
                initial_range=chart_workspace.get(
                    "initial_range",
                    "252",
                ),
                growth_metrics=chart_growth,
                valuation_medians=chart_valuations,
                trade_overlay=chart_overlay,
                alert_markers=chart_alert_markers,
                alert_market=chart_market,
                height=820,
                watchlists=workspace_watchlists,
                watchlist_add_callback=add_workspace_chart_to_watchlist,
                navigation_callback=handle_chart_navigation,
            )


# =====================================================================
# TAB 6: WATCHLISTS
# =====================================================================
with tab6:
    render_workspace_banner(
        "watchlists",
        "Workspace 06 · Personal tracking",
        "Watchlists",
        "Create private lists, add or remove stocks, and open a stock chart directly.",
        "⭐",
        "Organize",
    )
    if app_user is None:
        render_login_prompt(
            "Sign in with Google to create private watchlists.",
            key="watchlists_login",
        )
    elif cloud_store is None:
        st.warning("Cloud storage is not configured, so personal watchlists are unavailable.")
    else:
        try:
            if (
                fast_favorite_selection
                and "_cached_personal_watchlists" in st.session_state
            ):
                personal_watchlists = deepcopy(
                    st.session_state["_cached_personal_watchlists"]
                )
            else:
                personal_watchlists = cloud_store.load_watchlists(app_user.id)
                st.session_state["_cached_personal_watchlists"] = deepcopy(
                    personal_watchlists
                )
        except CloudStorageError as exc:
            st.error(str(exc))
            personal_watchlists = []

        with st.form("create_watchlist_form", clear_on_submit=True):
            new_watchlist_name = st.text_input(
                "New watchlist name",
                max_chars=120,
                placeholder="e.g. Quality compounders",
            )
            create_watchlist_clicked = st.form_submit_button("Create watchlist", type="primary")
        if create_watchlist_clicked:
            try:
                cloud_store.save_watchlist(
                    app_user.id,
                    uuid.uuid4().hex,
                    new_watchlist_name,
                    len(personal_watchlists),
                )
            except (CloudStorageError, ValueError) as exc:
                st.error(str(exc))
            else:
                st.rerun()

        if not personal_watchlists:
            st.info("No watchlists yet. Create your first list above.")
        for watchlist in personal_watchlists:
            watchlist_id = str(watchlist["id"])
            with st.expander(
                f"⭐ {watchlist['name']} · {len(watchlist.get('items', []))} stock(s)",
                expanded=False,
            ):
                add_market = st.selectbox(
                    "Market",
                    [MARKET_INDIA, MARKET_US],
                    format_func=market_label,
                    key=f"watchlist_market_{watchlist_id}",
                )
                available_symbols = available_symbols_for_market(add_market)
                with st.form(f"watchlist_add_{watchlist_id}", clear_on_submit=True):
                    add_symbol = st.selectbox(
                        "Stock symbol",
                        options=available_symbols,
                        index=None,
                        key=f"watchlist_symbol_{watchlist_id}",
                        placeholder=(
                            f"Type to search {market_label(add_market)} symbols"
                            if available_symbols
                            else "No symbols are available for this market"
                        ),
                    )
                    add_clicked = st.form_submit_button(
                        "Add stock",
                        disabled=not available_symbols,
                    )
                if add_clicked:
                    clean_symbol = str(add_symbol).strip().upper()
                    if clean_symbol not in set(available_symbols):
                        st.error(
                            "Select a stock symbol from the available "
                            f"{market_label(add_market)} suggestions."
                        )
                    else:
                        try:
                            cloud_store.save_watchlist_item(
                                app_user.id,
                                watchlist_id,
                                clean_symbol,
                                add_market,
                                "",
                                len(watchlist.get("items", [])),
                            )
                        except CloudStorageError as exc:
                            st.error(str(exc))
                        else:
                            st.session_state.pop(
                                "_cached_personal_watchlists",
                                None,
                            )
                            st.session_state["_main_workspace_tab"] = (
                                MAIN_TAB_LABELS[WATCHLISTS_TAB_INDEX]
                            )
                            st.rerun()

                items = watchlist.get("items", [])
                if items:
                    stock_header, remove_header = st.columns([5, 1])
                    stock_header.markdown("**Stock**")
                    remove_header.markdown("**Remove**")
                    for item_index, item in enumerate(items):
                        item_symbol = str(item.get("symbol", ""))
                        item_market = normalize_market(
                            item.get("market", MARKET_INDIA)
                        )
                        item_key = re.sub(
                            r"[^A-Za-z0-9_-]",
                            "_",
                            (
                                f"{watchlist_id}_{item_market}_"
                                f"{item_symbol}_{item_index}"
                            ),
                        )
                        symbol_col, remove_col = st.columns([5, 1])
                        market_watchlist_symbols = [
                            str(candidate.get("symbol", "")).strip().upper()
                            for candidate in items
                            if normalize_market(
                                candidate.get("market", MARKET_INDIA)
                            )
                            == item_market
                        ]
                        if symbol_col.button(
                            f"📈 {item_symbol} · "
                            f"{market_label(item_market)}",
                            key=f"watchlist_chart_{item_key}",
                            use_container_width=True,
                        ):
                            activate_chart_workspace(
                                {
                                    "symbol": item_symbol,
                                    "market": item_market,
                                    "symbols": market_watchlist_symbols,
                                    "index": market_watchlist_symbols.index(
                                        item_symbol.upper()
                                    ),
                                },
                                fallback_market=item_market,
                                origin_tab=WATCHLISTS_TAB_INDEX,
                            )
                            st.rerun()
                        if remove_col.button(
                            "−",
                            key=f"watchlist_remove_{item_key}",
                            help=f"Remove {item_symbol}",
                            use_container_width=True,
                        ):
                            try:
                                cloud_store.delete_watchlist_items(
                                    app_user.id,
                                    watchlist_id,
                                    [item_symbol],
                                )
                            except CloudStorageError as exc:
                                st.error(str(exc))
                            else:
                                st.session_state.pop(
                                    "_cached_personal_watchlists",
                                    None,
                                )
                                st.session_state["_main_workspace_tab"] = (
                                    MAIN_TAB_LABELS[WATCHLISTS_TAB_INDEX]
                                )
                                st.rerun()
                else:
                    st.caption("This watchlist is empty.")
                if st.button(
                    "Delete watchlist",
                    key=f"delete_watchlist_{watchlist_id}",
                    type="secondary",
                ):
                    try:
                        cloud_store.delete_watchlist(
                            app_user.id,
                            watchlist_id,
                        )
                    except CloudStorageError as exc:
                        st.error(str(exc))
                    else:
                        st.session_state.pop(
                            "_cached_personal_watchlists",
                            None,
                        )
                        st.session_state["_main_workspace_tab"] = (
                            MAIN_TAB_LABELS[WATCHLISTS_TAB_INDEX]
                        )
                        st.rerun()


with tab7:
    render_workspace_banner(
        "alerts",
        "Workspace 07 · Price monitoring",
        "Price Alerts",
        "Monitor price levels created from interactive charts. Alerts are evaluated whenever daily stock data is downloaded.",
        "🔔",
        "Monitor",
    )

    if app_user is None:
        render_login_prompt(
            "Guest mode: sign in with Google to create and manage personal price alerts.",
            key="alerts_guest_login",
        )
    elif cloud_store is None:
        st.warning("Cloud storage is not configured, so personal alerts are unavailable.")

    alerts = session_price_alerts()
    sorted_alerts = sort_price_alerts(alerts)
    active_alerts = [
        alert for alert in sorted_alerts
        if alert.get("status") == "Active"
    ]
    new_alerts = [
        alert for alert in sorted_alerts
        if alert.get("status") == "Triggered"
        and not bool(alert.get("acknowledged", False))
    ]
    old_alerts = [
        alert for alert in sorted_alerts
        if alert.get("status") == "Triggered"
        and bool(alert.get("acknowledged", False))
    ]

    metric_active, metric_new, metric_old = st.columns(3)
    metric_active.metric("Active Alerts", len(active_alerts))
    metric_new.metric("New Alerts", len(new_alerts))
    metric_old.metric("Old Alerts", len(old_alerts))

    if not alerts:
        if app_user is not None and cloud_store is not None:
            st.info("No personal price alerts yet. Move or tap the interactive chart crosshair, then click the + at that price.")
    else:
        def alert_number_for_table(value):
            try:
                number = float(value)
                if pd.isna(number):
                    return "—"
                return f"{number:,.4f}".rstrip("0").rstrip(".")
            except (TypeError, ValueError):
                return "—"

        def alert_date_for_table(value):
            parsed = pd.to_datetime(value, errors="coerce")
            if pd.isna(parsed):
                return "—"
            return parsed.strftime("%d %b %Y")

        def alert_action_button_key(action, alert_id):
            clean_id = re.sub(
                r"[^A-Za-z0-9_-]",
                "_",
                str(alert_id),
            )
            return f"alert_{action}_{clean_id}"

        def run_alert_row_action(action, alert_id):
            try:
                if action == "acknowledge":
                    changed = acknowledge_price_alerts([str(alert_id)])
                    message = f"Acknowledged {changed} price alert(s)."
                else:
                    changed = remove_price_alerts([str(alert_id)])
                    message = f"Removed {changed} price alert(s)."
            except PermissionError as exc:
                st.session_state["price_alert_feedback"] = (
                    "error",
                    str(exc),
                )
                st.session_state["price_alert_login_required"] = True
            except (OSError, RuntimeError) as exc:
                st.session_state["price_alert_feedback"] = (
                    "error",
                    f"Could not {action} alert: {exc}",
                )
            else:
                st.session_state["price_alert_feedback"] = (
                    "success",
                    message,
                )
                st.session_state.pop("_cached_price_alerts", None)
                st.session_state.pop("_cached_price_alerts_at", None)
            st.session_state["_main_workspace_tab"] = MAIN_TAB_LABELS[ALERTS_TAB_INDEX]

        def alert_table_dataframe(table_alerts, *, acknowledge=False):
            table_rows = []
            for alert in table_alerts:
                alert_id = str(alert.get("id", ""))
                symbol = str(alert.get("symbol", "") or "").strip().upper()
                market = normalize_market(alert.get("market", MARKET_INDIA))
                direction = (
                    "Cross above"
                    if alert.get("direction") == "above"
                    else "Cross below"
                )
                remove_button_key = alert_action_button_key(
                    "remove",
                    alert_id,
                )
                acknowledge_button_key = ""
                if acknowledge:
                    acknowledge_button_key = alert_action_button_key(
                        "acknowledge",
                        alert_id,
                    )
                table_rows.append({
                    "Symbol": symbol,
                    "Alert": (
                        f"{market_label(market)} · {direction} · Target "
                        f"{alert_number_for_table(alert.get('target_price'))} / "
                        f"Ref "
                        f"{alert_number_for_table(alert.get('reference_price'))}"
                    ),
                    "Dates": (
                        f"Created "
                        f"{alert_date_for_table(alert.get('created_at'))} / "
                        f"Triggered "
                        f"{alert_date_for_table(alert.get('triggered_candle_date'))}"
                    ),
                    "Actions": "",
                    "ChartSource": symbol,
                    "Interactive Market": market,
                    "Alert Date": alert.get("created_at"),
                    "Alert Price": alert.get("target_price"),
                    "Acknowledge Button Key": acknowledge_button_key,
                    "Remove Button Key": remove_button_key,
                })
            return pd.DataFrame(table_rows)

        def render_results_style_alert_table(
            table_alerts,
            title,
            empty_message,
            *,
            acknowledge=False,
            section_key,
        ):
            st.subheader(title)
            if not table_alerts:
                st.info(empty_message)
                return
            action_map = {}
            for alert in table_alerts:
                alert_id = str(alert.get("id", ""))
                action_map[
                    alert_action_button_key("remove", alert_id)
                ] = ("remove", alert_id)
                if acknowledge:
                    action_map[
                        alert_action_button_key(
                            "acknowledge",
                            alert_id,
                        )
                    ] = ("acknowledge", alert_id)

            action_event = sortable_results_table(
                alert_table_dataframe(
                    table_alerts,
                    acknowledge=acknowledge,
                ),
                height=700,
                interactive_ma_periods=[],
                table_title=title,
                row_actions=True,
                count_label=(
                    "alert" if len(table_alerts) == 1 else "alerts"
                ),
                component_key=f"alerts_table_{section_key}",
            )
            if not isinstance(action_event, dict):
                return
            nonce = str(action_event.get("nonce", ""))
            chart_request = chart_request_from_component(action_event)
            if chart_request:
                handled_chart_nonce_key = (
                    f"_handled_alert_chart_request_{section_key}"
                )
                if (
                    nonce
                    and nonce
                    != st.session_state.get(handled_chart_nonce_key)
                ):
                    st.session_state[handled_chart_nonce_key] = nonce
                    if activate_chart_workspace(
                        chart_request,
                        fallback_market=MARKET_INDIA,
                        origin_tab=ALERTS_TAB_INDEX,
                    ):
                        st.rerun()
                return
            action_key = str(action_event.get("actionKey", ""))
            handled_nonce_key = (
                f"_handled_alert_table_action_{section_key}"
            )
            if (
                not nonce
                or nonce == st.session_state.get(handled_nonce_key)
                or action_key not in action_map
            ):
                return
            st.session_state[handled_nonce_key] = nonce
            run_alert_row_action(*action_map[action_key])
            st.rerun()

        st.session_state.pop("_selected_alert_chart", None)
        st.session_state.pop("_pending_alert_removal", None)
        render_results_style_alert_table(
            active_alerts,
            "Active Alerts",
            "No active alerts.",
            section_key="active",
        )
        render_results_style_alert_table(
            new_alerts,
            "New Alerts",
            "No new alerts.",
            acknowledge=True,
            section_key="new",
        )
        render_results_style_alert_table(
            old_alerts,
            "Old Alerts",
            "No old alerts.",
            section_key="old",
        )
        st.stop()

        alert_by_id = {
            str(alert.get("id", "")): alert
            for alert in sorted_alerts
        }

        def alert_number(value):
            try:
                number = float(value)
                if pd.isna(number):
                    return "—"
                return f"{number:,.4f}".rstrip("0").rstrip(".")
            except (TypeError, ValueError):
                return "—"

        def short_alert_date(value):
            parsed = pd.to_datetime(value, errors="coerce")
            if pd.isna(parsed):
                return "—"
            return parsed.strftime("%d %b %Y")

        def clear_alert_cache():
            st.session_state.pop("_cached_price_alerts", None)

        @st.dialog("Remove price alert?")
        def confirm_alert_removal(alert_id):
            alert = alert_by_id.get(str(alert_id), {})
            symbol = str(alert.get("symbol", "") or "this stock")
            st.warning(
                f"Remove the saved price alert for {symbol}? "
                "This action cannot be undone."
            )
            confirm_col, cancel_col = st.columns(2)
            with confirm_col:
                confirmed = st.button(
                    "Remove alert",
                    type="primary",
                    use_container_width=True,
                    key="confirm_alert_row_removal",
                )
            with cancel_col:
                cancelled = st.button(
                    "Cancel",
                    use_container_width=True,
                    key="cancel_alert_row_removal",
                )
            if cancelled:
                st.session_state.pop("_pending_alert_removal", None)
                st.rerun()
            if confirmed:
                try:
                    removed = remove_price_alerts([str(alert_id)])
                except PermissionError as exc:
                    render_login_prompt(
                        str(exc),
                        key="alert_row_remove_login",
                        error=True,
                    )
                except (OSError, RuntimeError) as exc:
                    st.error(f"Could not remove alert: {exc}")
                else:
                    clear_alert_cache()
                    if (
                        st.session_state.get("_selected_alert_chart")
                        == str(alert_id)
                    ):
                        st.session_state.pop("_selected_alert_chart", None)
                    st.session_state.pop("_pending_alert_removal", None)
                    st.toast(f"Removed {removed} price alert(s).")
                    st.rerun()

        def render_selected_alert_chart():
            selected_id = str(
                st.session_state.get("_selected_alert_chart", "") or ""
            )
            selected_alert = alert_by_id.get(selected_id)
            if selected_alert is None:
                return
            symbol = str(selected_alert.get("symbol", "") or "").strip().upper()
            market = normalize_market(
                selected_alert.get("market", MARKET_INDIA)
            )
            chart_header, close_col = st.columns([5, 1])
            chart_header.markdown(f"#### {symbol} alert chart")
            if close_col.button(
                "← Back to alerts",
                key="close_alert_chart",
                use_container_width=True,
            ):
                st.session_state.pop("_selected_alert_chart", None)
                st.rerun()
            target_dir = timeframe_config("DAY", market)["target_dir"].resolve()
            stock_file = symbol_path(target_dir, symbol).resolve()
            if stock_file.parent != target_dir or not stock_exists(stock_file):
                st.error(f"Daily chart data is unavailable for {symbol}.")
                return
            selected_overlay, selected_alert_markers = chart_alert_context(
                alerts,
                symbol,
                market,
                {
                    "alertDate": selected_alert.get("created_at"),
                    "alertPrice": selected_alert.get("target_price"),
                },
            )
            render_interactive_stock_chart(
                symbol,
                stock_file,
                trade_overlay=selected_overlay,
                alert_markers=selected_alert_markers,
                alert_market=market,
                height=780,
            )

        def render_alert_rows(table_alerts, section_key, *, acknowledge=False):
            header = st.columns([1, 1.7, 1.25, 1.45, 1.55, 1.45])
            for column, label in zip(
                header,
                ("Market", "Symbol", "Condition", "Prices", "Dates", "Actions"),
            ):
                column.markdown(f"**{label}**")
            for row_index, alert in enumerate(table_alerts):
                alert_id = str(alert.get("id", ""))
                symbol = str(alert.get("symbol", "") or "")
                market = normalize_market(alert.get("market", MARKET_INDIA))
                row_key = re.sub(
                    r"[^A-Za-z0-9_-]",
                    "_",
                    f"{section_key}_{alert_id}_{row_index}",
                )
                with st.container(border=True):
                    (
                        market_col,
                        symbol_col,
                        condition_col,
                        prices_col,
                        dates_col,
                        actions_col,
                    ) = st.columns([1, 1.7, 1.25, 1.45, 1.55, 1.45])
                    market_col.write(market_label(market))
                    with symbol_col:
                        name_col, chart_col = st.columns([4, 1])
                        name_col.markdown(f"**{html.escape(symbol)}**")
                        if chart_col.button(
                            "📈",
                            key=f"open_alert_chart_{row_key}",
                            help=f"Open {symbol} interactive chart",
                        ):
                            current = str(
                                st.session_state.get(
                                    "_selected_alert_chart",
                                    "",
                                )
                            )
                            if current == alert_id:
                                st.session_state.pop(
                                    "_selected_alert_chart",
                                    None,
                                )
                            else:
                                st.session_state["_selected_alert_chart"] = (
                                    alert_id
                                )
                            st.rerun()
                    direction = (
                        "Cross above"
                        if alert.get("direction") == "above"
                        else "Cross below"
                    )
                    condition_col.write(direction)
                    prices_col.markdown(
                        f"Target **{alert_number(alert.get('target_price'))}**  \n"
                        f"Reference {alert_number(alert.get('reference_price'))}"
                    )
                    dates_col.markdown(
                        f"Created {short_alert_date(alert.get('created_at'))}  \n"
                        f"Triggered {short_alert_date(alert.get('triggered_candle_date'))}"
                    )
                    with actions_col:
                        action_columns = st.columns(2 if acknowledge else 1)
                        if acknowledge:
                            if action_columns[0].button(
                                "✓",
                                key=f"acknowledge_alert_{row_key}",
                                help="Acknowledge this alert",
                                use_container_width=True,
                            ):
                                try:
                                    acknowledged_count = acknowledge_price_alerts(
                                        [alert_id]
                                    )
                                except PermissionError as exc:
                                    render_login_prompt(
                                        str(exc),
                                        key=f"alert_ack_login_{row_key}",
                                        error=True,
                                    )
                                except (OSError, RuntimeError) as exc:
                                    st.error(
                                        f"Could not acknowledge alert: {exc}"
                                    )
                                else:
                                    clear_alert_cache()
                                    st.toast(
                                        f"Acknowledged {acknowledged_count} alert(s)."
                                    )
                                    st.rerun()
                        remove_action_col = (
                            action_columns[1]
                            if acknowledge
                            else action_columns[0]
                        )
                        if remove_action_col.button(
                            "🗑",
                            key=f"remove_alert_{row_key}",
                            help="Remove this alert",
                            use_container_width=True,
                        ):
                            st.session_state["_pending_alert_removal"] = alert_id
        selected_alert_id = str(
            st.session_state.get("_selected_alert_chart", "") or ""
        )
        if selected_alert_id in alert_by_id:
            render_selected_alert_chart()
        else:
            st.session_state.pop("_selected_alert_chart", None)

            st.subheader("Active Alerts")
            st.caption("Price conditions that are still being monitored.")
            if active_alerts:
                render_alert_rows(active_alerts, "active")
            else:
                st.info("No active alerts.")

            st.subheader("New Alerts")
            st.caption("Triggered alerts awaiting your acknowledgement.")
            if new_alerts:
                render_alert_rows(new_alerts, "new", acknowledge=True)
            else:
                st.info("No new alerts.")

            st.subheader("Old Alerts")
            st.caption("Triggered alerts you have already acknowledged.")
            if old_alerts:
                render_alert_rows(old_alerts, "old")
            else:
                st.info("No old alerts.")

            pending_alert_removal = st.session_state.get(
                "_pending_alert_removal",
            )
            if pending_alert_removal:
                confirm_alert_removal(pending_alert_removal)
