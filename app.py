import json
from copy import deepcopy
from datetime import datetime

import pandas as pd
import streamlit as st

from config import *
from charting import create_stock_chart, sortable_results_table
from downloader import clear_downloaded_json_files, download_top_stocks, timeframe_config
from emailer import send_results_email
from pattern import evaluate_pattern_filters, validate_expression
from screener import (
    DEFAULT_FILTER_SET,
    FILTER_TYPE_DEFAULTS,
    FILTER_TYPE_LABELS,
    normalize_filter_set,
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

# ---- Inject custom CSS ----
st.markdown(
    """
    <style>
    /* Primary button - green */
    div.stButton > button[kind="primary"] {
        background-color: #4CAF50;
        border-color: #4CAF50;
        color: #ffffff;
        font-weight: bold;
        border-radius: 8px;
        padding: 0.5rem 1.5rem;
        transition: all 0.2s ease;
    }
    div.stButton > button[kind="primary"]:hover,
    div.stButton > button[kind="primary"]:focus {
        background-color: #43A047;
        border-color: #43A047;
        color: #ffffff;
        box-shadow: 0 2px 8px rgba(76,175,80,0.4);
    }

    /* Favorite save button - yellow */
    button[kind="secondary"][data-testid="baseButton-secondary"] {
        background-color: #FFC107 !important;
        border-color: #FFC107 !important;
        color: #000000 !important;
        font-weight: bold;
    }

    /* Run Screener button - prominent */
    div.stButton > button[kind="primary"]#run-screener-btn {
        background-color: #1565C0 !important;
        border-color: #1565C0 !important;
        font-size: 1.1rem;
        padding: 0.6rem 2rem;
    }

    /* Tab styling */
    div.stTabs [data-baseweb="tab-list"] {
        gap: 4px;
        background: linear-gradient(135deg, #1a237e 0%, #283593 50%, #1a237e 100%);
        border-radius: 12px 12px 0 0;
        padding: 6px 8px 0 8px;
    }
    div.stTabs [data-baseweb="tab"] {
        border-radius: 10px 10px 0 0;
        padding: 10px 24px;
        font-weight: 600;
        font-size: 0.95rem;
        color: #ffffffcc;
        background: rgba(255,255,255,0.08);
        border: none;
        transition: all 0.2s ease;
    }
    div.stTabs [data-baseweb="tab"]:hover {
        background: rgba(255,255,255,0.18);
        color: #ffffff;
    }
    div.stTabs [data-baseweb="tab"][aria-selected="true"] {
        background: #ffffff;
        color: #1a237e;
        font-weight: 700;
    }

    /* Filter row badges */
    .filter-badge {
        display: inline-block;
        padding: 4px 12px;
        border-radius: 16px;
        font-weight: 600;
        font-size: 0.85rem;
        color: #fff;
        margin: 2px 4px;
    }

    /* Data availability cards */
    .data-status-card {
        border-radius: 10px;
        padding: 12px 16px;
        margin: 6px 0;
        color: #fff;
        font-weight: 600;
    }
    .data-status-available {
        background: linear-gradient(135deg, #2E7D32, #43A047);
    }
    .data-status-empty {
        background: linear-gradient(135deg, #757575, #9E9E9E);
    }

    /* Section headers */
    .section-header {
        font-size: 1.15rem;
        font-weight: 700;
        margin-top: 18px;
        margin-bottom: 8px;
        padding-bottom: 4px;
        border-bottom: 2px solid #e0e0e0;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

st.title("📈 NSE Stock Screener")


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
    files = sorted(json_dir.glob("*.json"))[:top_n]
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


def render_data_availability_status():
    """Render data availability cards for all timeframes."""
    st.markdown(
        '<p class="section-header">📊 Data Availability Status</p>',
        unsafe_allow_html=True,
    )

    timeframes = [
        ("Daily", DAILY_DIR),
        ("Weekly", WEEKLY_DIR),
        ("Monthly", MONTHLY_DIR),
    ]

    any_available = False
    for label, directory in timeframes:
        file_count = len(list(directory.glob("*.json"))) if directory.exists() else 0
        last_date = _get_last_date_from_json_dir(directory)
        if file_count > 0 and last_date:
            any_available = True
            date_formatted = last_date.strftime("%d-%m-%Y")
            st.markdown(
                f'<div class="data-status-card data-status-available">'
                f'✅ <b>{label}</b> — {file_count} stocks | '
                f'Latest data: <b>{date_formatted}</b>'
                f'</div>',
                unsafe_allow_html=True,
            )
        else:
            st.markdown(
                f'<div class="data-status-card data-status-empty">'
                f'❌ <b>{label}</b> — No stocks data available'
                f'</div>',
                unsafe_allow_html=True,
            )
    if not any_available:
        st.warning("No stock data found for any timeframe. Click '⬇️ Download Stocks Data' to begin.")


tab1, tab2, tab3 = st.tabs(["📥 Data", "🔍 Screener", "📊 Results"])


# =====================================================================
# TAB 1: DATA MANAGEMENT
# =====================================================================
with tab1:
    st.header("📥 Data Management")

    render_data_availability_status()

    st.markdown(
        '<p class="section-header">⬇️ Download Fresh Stock Data</p>',
        unsafe_allow_html=True,
    )

    excel_file = EXCEL_DIR / "MCAP_JUGAAD.xlsx"

    download_tf = st.selectbox(
        "📅 Download Timeframe",
        ["DAY", "WEEK", "MONTH"],
        index=["DAY", "WEEK", "MONTH"].index(settings.get("download_tf", "DAY")),
    )
    download_limit = st.number_input(
        "🔢 Number of stocks to download",
        min_value=1,
        value=int(settings.get("download_limit", 1000)),
        step=50,
    )

    if excel_file.exists():
        st.success(f"✅ Default Excel Found: {excel_file.name}")
    else:
        st.warning("⚠️ Upload initial MCAP_JUGAAD.xlsx")

    uploaded = st.file_uploader("📂 Replace Excel", type=["xlsx"])
    if uploaded:
        excel_file.write_bytes(uploaded.getbuffer())
        st.success("✅ Excel replaced")

    update_settings({
        "download_tf": download_tf,
        "download_limit": download_limit,
    })

    if st.button("⬇️ Download Stocks Data", type="primary"):
        if not excel_file.exists():
            st.error("❌ Upload MCAP_JUGAAD.xlsx before downloading stock data.")
        else:
            progress_bar = st.progress(0)
            progress_text = st.empty()

            def show_download_progress(done, total, downloaded_count, symbol):
                progress = done / total if total else 0
                progress_bar.progress(progress)
                progress_text.info(
                    f"Downloaded {downloaded_count} of {total} stocks. "
                    f"Processing {done}/{total}: {symbol}"
                )

            deleted_count = clear_downloaded_json_files(download_tf)
            if deleted_count:
                progress_text.info(f"Cleared {deleted_count} old {download_tf.lower()} JSON files.")

            with st.spinner(f"⬇️ Downloading top {download_limit} stocks from yfinance..."):
                download_rows = download_top_stocks(
                    excel_file,
                    download_tf,
                    limit=download_limit,
                    progress_callback=show_download_progress,
                )

            downloaded_count = sum(1 for row in download_rows if row["Downloaded"])
            progress_bar.progress(1.0)
            last_download_at = datetime.now().strftime("%d-%m-%Y %H:%M:%S")
            update_settings({
                "last_download_at": last_download_at,
                "last_download_tf": download_tf,
            })
            progress_text.success(
                f"✅ Downloaded {downloaded_count} of {len(download_rows)} stocks. "
                f"Last download: {last_download_at}"
            )
            st.success(f"✅ Downloaded {downloaded_count} of {len(download_rows)} stocks")

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
    st.markdown(
        '<p class="section-header">⚡ Quick Run</p>',
        unsafe_allow_html=True,
    )

    # ---- Screening Timeframe ----
    col_tf, col_charts = st.columns([1, 1])
    with col_tf:
        tf = st.selectbox(
            "📅 Screening Timeframe",
            ["DAY", "WEEK", "MONTH"],
            index=["DAY", "WEEK", "MONTH"].index(settings.get("tf", "DAY")),
        )
    with col_charts:
        create_charts = st.toggle(
            "📈 Create charts",
            value=bool(settings.get("create_charts", False)),
            key="create_charts_toggle",
        )
    update_settings({
        "tf": tf,
        "create_charts": create_charts,
    })

    # ---- Favorite Filter Set + Run Button side by side ----
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
                help="Select a saved favorite filter set to load its MA & pattern filters.",
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

    st.divider()

    # ===== MA Based Filtering =====
    st.subheader("📐 MA Based Filtering")

    # ---- Add Filter Row ----
    col1, col2 = st.columns([3, 1])
    with col1:
        filter_type_to_add = st.selectbox(
            "Filter Category",
            list(FILTER_TYPE_LABELS.keys()),
            format_func=lambda value: FILTER_TYPE_LABELS[value],
        )
    with col2:
        add_filter = st.button("➕ Add Filter")

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

    st.markdown('<p class="section-header">📋 Current Filter Set</p>', unsafe_allow_html=True)

    if not current_filter_set:
        st.info("No MA filters selected. Screening will pass stocks through this tab.")

    rendered_filter_set = []

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

        with st.expander(expander_label, expanded=True):
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
    st.info(f"📌 Filters in current set: {active_filter_count}")

    update_settings({
        "screener_filter_set": filter_set,
    })

    st.divider()

    # ===== Pattern Based Filtering =====
    st.subheader("🔄 Pattern Based Filtering")

    initialize_pattern_expression_state()

    if "pattern_lookback_days_slider" not in st.session_state:
        st.session_state["pattern_lookback_days_slider"] = int(settings.get("pattern_lookback_days", 120))
    if "pattern_lookback_days_number" not in st.session_state:
        st.session_state["pattern_lookback_days_number"] = int(settings.get("pattern_lookback_days", 120))
    if "pattern_reversal_pct_slider" not in st.session_state:
        st.session_state["pattern_reversal_pct_slider"] = float(settings.get("pattern_reversal_pct", 5.0))
    if "pattern_reversal_pct_number" not in st.session_state:
        st.session_state["pattern_reversal_pct_number"] = float(settings.get("pattern_reversal_pct", 5.0))

    col1, col2 = st.columns(2)
    with col1:
        st.slider(
            "📅 Lookback Days",
            min_value=10,
            max_value=1000,
            step=5,
            key="pattern_lookback_days_slider",
            on_change=sync_pattern_lookback_from_slider,
        )
    with col2:
        st.number_input(
            "📅 Lookback Days",
            min_value=10,
            max_value=1000,
            step=1,
            key="pattern_lookback_days_number",
            on_change=sync_pattern_lookback_from_number,
        )

    col1, col2 = st.columns(2)
    with col1:
        st.slider(
            "📉 Swing Reversal %",
            min_value=0.5,
            max_value=50.0,
            step=0.5,
            key="pattern_reversal_pct_slider",
            on_change=sync_pattern_reversal_from_slider,
        )
    with col2:
        st.number_input(
            "📉 Swing Reversal %",
            min_value=0.5,
            max_value=50.0,
            step=0.1,
            key="pattern_reversal_pct_number",
            on_change=sync_pattern_reversal_from_number,
        )

    pattern_lookback_days = int(st.session_state["pattern_lookback_days_number"])
    pattern_reversal_pct = float(st.session_state["pattern_reversal_pct_number"])

    st.markdown('<p class="section-header">📝 Swing Expression Filters</p>', unsafe_allow_html=True)
    if st.button("➕ Add Expression", key="add_pattern_filter"):
        st.session_state["pattern_expression_filters"].append({
            "id": st.session_state["next_pattern_expression_id"],
            "expression": "",
        })
        st.session_state["next_pattern_expression_id"] += 1
        st.rerun()

    pattern_expression_filters = st.session_state["pattern_expression_filters"]
    valid_pattern_expressions = []
    invalid_pattern_errors = []

    if not pattern_expression_filters:
        st.info("No swing filters selected. Pattern screening will pass stocks through this tab.")

    for index, expression_filter in enumerate(pattern_expression_filters, start=1):
        filter_id = expression_filter["id"]
        col1, col2 = st.columns([5, 1])
        with col1:
            expression = st.text_input(
                f"Expression {index}",
                value=expression_filter.get("expression", ""),
                key=f"pattern_expression_{filter_id}",
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
            st.info("Blank expression ignored.")
            continue

        is_valid, error = validate_expression(expression)
        if is_valid:
            st.success("✅ Valid expression")
            valid_pattern_expressions.append(expression.strip())
        else:
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

    st.divider()

    # ===== Save Current Filters =====
    st.markdown('<p class="section-header">💾 Save Current Filters</p>', unsafe_allow_html=True)
    col_save_name, col_save_btn = st.columns([3, 1])
    with col_save_name:
        favorite_name = st.text_input(
            "Favorite Filter Name",
            key="_favorite_name_to_save",
            placeholder="e.g. Golden Cross + PE < 30",
        )
    with col_save_btn:
        st.write("")  # spacer
        save_fav = st.button("⭐ Add To Favorites", use_container_width=True)

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

    # ===== Remove Favorite Filters =====
    if favorite_filter_sets:
        st.divider()
        st.markdown('<p class="section-header">🗑️ Remove Favorite</p>', unsafe_allow_html=True)
        col_del_name, col_del_btn = st.columns([3, 1])
        with col_del_name:
            del_favorite_name = st.selectbox(
                "Select favorite to remove",
                sorted(favorite_filter_sets.keys()),
                key="delete_favorite_select",
            )
        with col_del_btn:
            st.write("")  # spacer
            delete_fav = st.button("🗑️ Remove Favorite", use_container_width=True)

        if delete_fav:
            if del_favorite_name in favorite_filter_sets:
                del favorite_filter_sets[del_favorite_name]
                save_favourite_filter_sets(favorite_filter_sets)
                if settings.get("selected_favorite_filter_set") == del_favorite_name:
                    update_settings({"selected_favorite_filter_set": "Current Filters"})
                st.session_state.pop("_favorite_select_widget", None)
                st.success(f"🗑️ Removed favorite: {del_favorite_name}")
                st.rerun()

    # ===== RUN SCREENER LOGIC =====
    if run_combined:
        run_filter_set = filter_set
        run_lookback_days = pattern_lookback_days
        run_reversal_pct = pattern_reversal_pct
        run_pattern_expressions = valid_pattern_expressions
        run_invalid_pattern_errors = invalid_pattern_errors

        if run_invalid_pattern_errors:
            st.error("Fix invalid swing expressions before running the screener.")
            st.stop()

        for filter_item in run_filter_set:
            params = filter_item["params"]
            label = FILTER_TYPE_LABELS[filter_item["type"]]
            if filter_item["type"] in {"short_above_long", "golden_cross"} and params["short_ma"] >= params["long_ma"]:
                st.error(f"Short MA must be less than Long MA in: {label}.")
                st.stop()

        target_dir = timeframe_config(tf)["target_dir"]
        rows = []
        stock_files = list(target_dir.glob("*.json"))

        # Render progress bar inside the placeholder below the Run button
        with screener_progress_placeholder.container():
            progress_bar = st.progress(0)
            progress_text = st.empty()

            for index, f in enumerate(stock_files, start=1):
                r = screen_json_file(
                    f,
                    filter_set=run_filter_set,
                )
                if r:
                    pattern_passed = True
                    swings = []
                    pattern_error = ""
                    if run_pattern_expressions:
                        pattern_passed, swings, pattern_error = evaluate_pattern_filters(
                            f,
                            run_lookback_days,
                            run_reversal_pct,
                            run_pattern_expressions,
                        )
                    if pattern_passed:
                        if create_charts:
                            has_pattern_filters = bool(run_pattern_expressions)
                            chart_path = create_stock_chart(
                                f,
                                run_filter_set,
                                swing_annotations=swings if has_pattern_filters else None,
                            )
                            if chart_path:
                                r["ChartPath"] = chart_path
                        rows.append(r)

                total = len(stock_files)
                progress = index / total if total else 0
                progress_bar.progress(progress)
                progress_text.info(
                    f"🔍 Screened {index} of {total} stocks. "
                    f"Matches found: {len(rows)}. Processing: {f.stem}"
                )

            # Persist results both in-memory and on-disk
            st.session_state["results"] = rows
            save_results(rows)

            progress_bar.progress(1.0)
            progress_text.success(f"✅ Screened {len(stock_files)} stocks. Matches found: {len(rows)}")
            st.success(f"🎯 {len(rows)} stocks found")


# =====================================================================
# TAB 3: RESULTS
# =====================================================================
with tab3:
    st.header("📊 Results")

    # Load persisted results if session state is empty
    if "results" not in st.session_state:
        st.session_state["results"] = load_results()

    rows = st.session_state.get("results", [])

    if rows:
        # Determine heading: favorite filter name or "Custom Filter"
        selected_filter_name = settings.get("selected_favorite_filter_set", "Current Filters")
        if selected_filter_name and selected_filter_name != "Current Filters":
            heading_label = selected_filter_name
        else:
            heading_label = "Custom Filter"

        st.info(f"📌 Showing last screener run results — {len(rows)} stock(s) matched | **{heading_label}**")

        df = pd.DataFrame(rows)
        df.index = range(1, len(df) + 1)
        display_df = df

        # Base columns that always appear (if present)
        result_columns = ["Symbol", "PE Ratio"]

        # Insert DiffSMA* columns (absolute % diff from price to each MA) right after PE Ratio
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

        if "ChartPath" in df.columns:
            chart_df = display_df.copy()
            chart_df["ChartPath"] = df["ChartPath"]
            sortable_results_table(chart_df)
        else:
            sortable_results_table(display_df)

        st.download_button(
            "📥 Download Results CSV",
            display_df.to_csv(index=False),
            "results.csv",
            "text/csv",
        )

        st.divider()
        st.subheader("📧 Email Results")
        st.caption("Use a Gmail App Password. Your password is used only for this send and is not saved.")

        gmail_id = st.text_input(
            "📧 Gmail ID",
            value=settings.get("gmail_id", ""),
            placeholder="yourname@gmail.com",
        )
        gmail_app_password = st.text_input(
            "🔑 Gmail App Password",
            type="password",
            placeholder="16-digit app password",
        )
        recipient_email = st.text_input(
            "📩 Recipient Email",
            value=settings.get("recipient_email", ""),
            placeholder="recipient@example.com",
        )
        email_subject = st.text_input(
            "📋 Subject",
            value=settings.get("email_subject", "NSE Stock Screener Results"),
        )
        email_body = st.text_area(
            "📝 Message",
            value=settings.get("email_body", "Attached are the latest filtered stock screener results."),
        )

        update_settings({
            "gmail_id": gmail_id,
            "recipient_email": recipient_email,
            "email_subject": email_subject,
            "email_body": email_body,
        })

        if st.button("✉️ Send Results Email"):
            if not gmail_id or not gmail_app_password or not recipient_email:
                st.error("❌ Enter Gmail ID, Gmail App Password, and recipient email.")
            else:
                try:
                    send_results_email(
                        gmail_id,
                        gmail_app_password,
                        recipient_email,
                        email_subject,
                        email_body,
                        display_df.to_csv(index=False),
                    )
                    st.success("✅ Email sent successfully.")
                except Exception as exc:
                    st.error(f"❌ Email failed: {exc}")
    else:
        st.info("No results yet. Run the screener from the '🔍 Screener' tab to see results here.")