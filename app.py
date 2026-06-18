
import streamlit as st
import pandas as pd
from datetime import datetime

from config import *
from storage import load_settings, update_settings
from screener import DEFAULT_FILTER_SET, screen_json_file
from downloader import download_top_stocks, timeframe_config
from emailer import send_results_email

st.set_page_config(layout="wide")

settings = load_settings()

st.title("NSE Stock Screener")

tab1, tab2, tab3, tab4 = st.tabs(
    ["Data","MA Screener","Pattern Screener","Results"]
)

results = []

with tab1:
    st.header("Data Management")

    excel_file = EXCEL_DIR / "MCAP_JUGAAD.xlsx"
    last_download_at = settings.get("last_download_at")
    last_download_tf = settings.get("last_download_tf")
    last_download_status = st.empty()

    def show_last_download_status(downloaded_at, downloaded_tf):
        if downloaded_at:
            label = f"Last stock data download: {downloaded_at}"
            if downloaded_tf:
                label += f" ({downloaded_tf})"
            last_download_status.info(label)
        else:
            last_download_status.info("Last stock data download: Not available")

    show_last_download_status(last_download_at, last_download_tf)

    download_tf = st.selectbox(
        "Download Timeframe",
        ["DAY","WEEK","MONTH"],
        index=["DAY","WEEK","MONTH"].index(settings.get("download_tf", "DAY")),
    )

    if excel_file.exists():
        st.success(f"Default Excel Found: {excel_file.name}")
    else:
        st.warning("Upload initial MCAP_JUGAAD.xlsx")

    uploaded = st.file_uploader("Replace Excel", type=["xlsx"])

    if uploaded:
        excel_file.write_bytes(uploaded.getbuffer())
        st.success("Excel replaced")

    update_settings({"download_tf": download_tf})

    if st.button("Download Top 1000 Stocks"):
        if not excel_file.exists():
            st.error("Upload MCAP_JUGAAD.xlsx before downloading stock data.")
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

            with st.spinner("Downloading top 1000 stocks from yfinance..."):
                download_rows = download_top_stocks(
                    excel_file,
                    download_tf,
                    limit=1000,
                    progress_callback=show_download_progress,
                )
            downloaded_count = sum(1 for row in download_rows if row["Downloaded"])
            progress_bar.progress(1.0)
            last_download_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            update_settings({
                "last_download_at": last_download_at,
                "last_download_tf": download_tf,
            })
            show_last_download_status(last_download_at, download_tf)
            progress_text.success(
                f"Downloaded {downloaded_count} of {len(download_rows)} stocks. "
                f"Last download: {last_download_at}"
            )
            st.success(f"Downloaded {downloaded_count} of {len(download_rows)} stocks")
            failed = [row for row in download_rows if not row["Downloaded"]]
            if failed:
                st.dataframe(pd.DataFrame(failed), use_container_width=True)

with tab2:
    st.header("MA Screener")

    favorite_filter_sets = settings.get("favorite_filter_sets", {})
    favorite_names = ["Custom"] + sorted(favorite_filter_sets.keys())
    saved_selected_favorite = settings.get("selected_favorite_filter_set", "Custom")
    favorite_index = favorite_names.index(saved_selected_favorite) if saved_selected_favorite in favorite_names else 0

    selected_favorite = st.selectbox(
        "Favorite Filter Set",
        favorite_names,
        index=favorite_index,
    )

    if selected_favorite == "Custom":
        base_filter_set = settings.get("screener_filter_set", DEFAULT_FILTER_SET)
    else:
        base_filter_set = favorite_filter_sets[selected_favorite]

    filter_widget_prefix = f"filter_set_{selected_favorite}"

    def filter_defaults(name):
        defaults = DEFAULT_FILTER_SET[name].copy()
        defaults.update(base_filter_set.get(name, {}))
        return defaults

    tf = st.selectbox(
        "Timeframe",
        ["DAY","WEEK","MONTH"],
        index=["DAY","WEEK","MONTH"].index(settings.get("tf", "DAY")),
    )

    ma_rising_defaults = filter_defaults("ma_rising")
    st.subheader("MA Rising")
    col1, col2 = st.columns([1, 2])
    with col1:
        ma_rising_enabled = st.checkbox(
            "Use MA Rising",
            value=bool(ma_rising_defaults.get("enabled", False)),
            key=f"{filter_widget_prefix}_ma_rising_enabled",
        )
    with col2:
        ma_rising_period = st.number_input(
            "MA",
            min_value=2,
            max_value=1000,
            value=int(ma_rising_defaults.get("ma", 200)),
            key=f"{filter_widget_prefix}_ma_rising_period",
        )

    short_above_defaults = filter_defaults("short_above_long")
    st.subheader("Short MA Above Long MA")
    col1, col2, col3 = st.columns([1, 2, 2])
    with col1:
        short_above_enabled = st.checkbox(
            "Use Short Above Long",
            value=bool(short_above_defaults.get("enabled", False)),
            key=f"{filter_widget_prefix}_short_above_enabled",
        )
    with col2:
        short_above_short_ma = st.number_input(
            "Short MA",
            min_value=2,
            max_value=500,
            value=int(short_above_defaults.get("short_ma", 50)),
            key=f"{filter_widget_prefix}_short_above_short_ma",
        )
    with col3:
        short_above_long_ma = st.number_input(
            "Long MA",
            min_value=2,
            max_value=1000,
            value=int(short_above_defaults.get("long_ma", 200)),
            key=f"{filter_widget_prefix}_short_above_long_ma",
        )

    price_near_defaults = filter_defaults("price_near_long")
    st.subheader("Current Price Near And Above Long MA")
    col1, col2, col3 = st.columns([1, 2, 2])
    with col1:
        price_near_enabled = st.checkbox(
            "Use Price Near Long",
            value=bool(price_near_defaults.get("enabled", False)),
            key=f"{filter_widget_prefix}_price_near_enabled",
        )
    with col2:
        price_near_long_ma = st.number_input(
            "Long MA",
            min_value=2,
            max_value=1000,
            value=int(price_near_defaults.get("long_ma", 200)),
            key=f"{filter_widget_prefix}_price_near_long_ma",
        )
    with col3:
        price_near_threshold = st.number_input(
            "Within Percent",
            min_value=0.1,
            max_value=100.0,
            value=float(price_near_defaults.get("threshold_pct", 5.0)),
            step=0.1,
            key=f"{filter_widget_prefix}_price_near_threshold",
        )

    golden_cross_defaults = filter_defaults("golden_cross")
    st.subheader("Short MA Crossed Long MA - Golden Cross")
    col1, col2, col3, col4 = st.columns([1, 2, 2, 2])
    with col1:
        golden_cross_enabled = st.checkbox(
            "Use Golden Cross",
            value=bool(golden_cross_defaults.get("enabled", False)),
            key=f"{filter_widget_prefix}_golden_cross_enabled",
        )
    with col2:
        golden_cross_short_ma = st.number_input(
            "Short MA",
            min_value=2,
            max_value=500,
            value=int(golden_cross_defaults.get("short_ma", 50)),
            key=f"{filter_widget_prefix}_golden_cross_short_ma",
        )
    with col3:
        golden_cross_long_ma = st.number_input(
            "Long MA",
            min_value=2,
            max_value=1000,
            value=int(golden_cross_defaults.get("long_ma", 200)),
            key=f"{filter_widget_prefix}_golden_cross_long_ma",
        )
    with col4:
        golden_cross_lookback = st.number_input(
            "Last N Time Frame Units",
            min_value=1,
            max_value=1000,
            value=int(golden_cross_defaults.get("lookback_units", 20)),
            key=f"{filter_widget_prefix}_golden_cross_lookback",
        )

    down_from_max_defaults = filter_defaults("long_ma_down_from_max")
    st.subheader("Long MA Down From Recent Max")
    col1, col2, col3, col4 = st.columns([1, 2, 2, 2])
    with col1:
        down_from_max_enabled = st.checkbox(
            "Use Long MA Down",
            value=bool(down_from_max_defaults.get("enabled", False)),
            key=f"{filter_widget_prefix}_down_from_max_enabled",
        )
    with col2:
        down_from_max_long_ma = st.number_input(
            "Long MA",
            min_value=2,
            max_value=1000,
            value=int(down_from_max_defaults.get("long_ma", 200)),
            key=f"{filter_widget_prefix}_down_from_max_long_ma",
        )
    with col3:
        down_from_max_pct = st.number_input(
            "Down Percent",
            min_value=0.1,
            max_value=100.0,
            value=float(down_from_max_defaults.get("down_pct", 5.0)),
            step=0.1,
            key=f"{filter_widget_prefix}_down_from_max_pct",
        )
    with col4:
        down_from_max_lookback = st.number_input(
            "Last M Time Frame Units",
            min_value=2,
            max_value=2000,
            value=int(down_from_max_defaults.get("lookback_units", 50)),
            key=f"{filter_widget_prefix}_down_from_max_lookback",
        )

    filter_set = {
        "ma_rising": {
            "enabled": ma_rising_enabled,
            "ma": int(ma_rising_period),
        },
        "short_above_long": {
            "enabled": short_above_enabled,
            "short_ma": int(short_above_short_ma),
            "long_ma": int(short_above_long_ma),
        },
        "price_near_long": {
            "enabled": price_near_enabled,
            "long_ma": int(price_near_long_ma),
            "threshold_pct": float(price_near_threshold),
        },
        "golden_cross": {
            "enabled": golden_cross_enabled,
            "short_ma": int(golden_cross_short_ma),
            "long_ma": int(golden_cross_long_ma),
            "lookback_units": int(golden_cross_lookback),
        },
        "long_ma_down_from_max": {
            "enabled": down_from_max_enabled,
            "long_ma": int(down_from_max_long_ma),
            "down_pct": float(down_from_max_pct),
            "lookback_units": int(down_from_max_lookback),
        },
    }

    active_filter_count = sum(1 for config in filter_set.values() if config["enabled"])
    st.info(f"Active filters in current set: {active_filter_count}")

    st.subheader("Favorite Filter Sets")
    col1, col2, col3 = st.columns([3, 1, 1])
    with col1:
        favorite_name = st.text_input("Favorite Name", value=selected_favorite if selected_favorite != "Custom" else "")
    with col2:
        save_favorite = st.button("Save Favorite")
    with col3:
        remove_favorite = st.button("Remove Favorite", disabled=selected_favorite == "Custom")

    selected_favorite_for_settings = selected_favorite

    if save_favorite:
        clean_name = favorite_name.strip()
        if not clean_name:
            st.error("Enter a favorite name before saving.")
        else:
            favorite_filter_sets[clean_name] = filter_set
            selected_favorite_for_settings = clean_name
            update_settings({
                "favorite_filter_sets": favorite_filter_sets,
                "selected_favorite_filter_set": clean_name,
                "screener_filter_set": filter_set,
            })
            st.success(f"Saved favorite filter set: {clean_name}")

    if remove_favorite and selected_favorite != "Custom":
        favorite_filter_sets.pop(selected_favorite, None)
        selected_favorite_for_settings = "Custom"
        update_settings({
            "favorite_filter_sets": favorite_filter_sets,
            "selected_favorite_filter_set": "Custom",
        })
        st.success(f"Removed favorite filter set: {selected_favorite}")

    update_settings({
        "tf": tf,
        "selected_favorite_filter_set": selected_favorite_for_settings,
        "screener_filter_set": filter_set,
    })

    run = st.button("Run Screener")

    if run:

        if active_filter_count == 0:
            st.error("Select at least one filter before running the screener.")
            st.stop()

        if short_above_enabled and short_above_short_ma >= short_above_long_ma:
            st.error("Short MA must be less than Long MA in the Short MA Above Long MA filter.")
            st.stop()

        if golden_cross_enabled and golden_cross_short_ma >= golden_cross_long_ma:
            st.error("Short MA must be less than Long MA in the Golden Cross filter.")
            st.stop()

        target_dir = timeframe_config(tf)["target_dir"]
        rows = []
        stock_files = list(target_dir.glob("*.json"))
        progress_bar = st.progress(0)
        progress_text = st.empty()

        for index, f in enumerate(stock_files, start=1):
            r = screen_json_file(
                f,
                filter_set=filter_set,
            )
            if r:
                rows.append(r)

            total = len(stock_files)
            progress = index / total if total else 0
            progress_bar.progress(progress)
            progress_text.info(
                f"Screened {index} of {total} stocks. "
                f"Matches found: {len(rows)}. Processing: {f.stem}"
            )

        st.session_state["results"] = rows
        progress_bar.progress(1.0)
        progress_text.success(f"Screened {len(stock_files)} stocks. Matches found: {len(rows)}")
        st.success(f"{len(rows)} stocks found")

with tab3:
    st.header("Pattern Screener")
    st.info("Add Cup&Handle, Double Bottom, Bull Flag scanners here.")

with tab4:
    st.header("Results")

    rows = st.session_state.get("results", [])

    if rows:
        df = pd.DataFrame(rows)
        st.dataframe(df, use_container_width=True)

        st.download_button(
            "Download Results CSV",
            df.to_csv(index=False),
            "results.csv",
            "text/csv"
        )

        st.subheader("Email Results")
        st.caption("Use a Gmail App Password. Your password is used only for this send and is not saved.")

        gmail_id = st.text_input(
            "Gmail ID",
            value=settings.get("gmail_id", ""),
            placeholder="yourname@gmail.com",
        )
        gmail_app_password = st.text_input(
            "Gmail App Password",
            type="password",
            placeholder="16-digit app password",
        )
        recipient_email = st.text_input(
            "Recipient Email",
            value=settings.get("recipient_email", ""),
            placeholder="recipient@example.com",
        )
        email_subject = st.text_input(
            "Subject",
            value=settings.get("email_subject", "NSE Stock Screener Results"),
        )
        email_body = st.text_area(
            "Message",
            value=settings.get("email_body", "Attached are the latest filtered stock screener results."),
        )

        update_settings({
            "gmail_id": gmail_id,
            "recipient_email": recipient_email,
            "email_subject": email_subject,
            "email_body": email_body,
        })

        if st.button("Send Results Email"):
            if not gmail_id or not gmail_app_password or not recipient_email:
                st.error("Enter Gmail ID, Gmail App Password, and recipient email.")
            else:
                try:
                    send_results_email(
                        gmail_id,
                        gmail_app_password,
                        recipient_email,
                        email_subject,
                        email_body,
                        df.to_csv(index=False),
                    )
                    st.success("Email sent successfully.")
                except Exception as exc:
                    st.error(f"Email failed: {exc}")
    else:
        st.info("Run screener first.")
