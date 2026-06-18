from copy import deepcopy
from datetime import datetime

import pandas as pd
import streamlit as st

from config import *
from downloader import download_top_stocks, timeframe_config
from emailer import send_results_email
from screener import (
    DEFAULT_FILTER_SET,
    FILTER_TYPE_DEFAULTS,
    FILTER_TYPE_LABELS,
    normalize_filter_set,
    screen_json_file,
)
from storage import load_settings, update_settings

st.set_page_config(layout="wide")

settings = load_settings()

st.title("NSE Stock Screener")

tab1, tab2, tab3, tab4 = st.tabs(
    ["Data", "MA Screener", "Pattern Screener", "Results"]
)


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
        ["DAY", "WEEK", "MONTH"],
        index=["DAY", "WEEK", "MONTH"].index(settings.get("download_tf", "DAY")),
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
        loaded_filter_set = normalize_filter_set(settings.get("screener_filter_set", DEFAULT_FILTER_SET))
    else:
        loaded_filter_set = normalize_filter_set(favorite_filter_sets[selected_favorite])

    if st.session_state.get("loaded_favorite_filter_set") != selected_favorite:
        st.session_state["current_filter_set"] = deepcopy(loaded_filter_set)
        st.session_state["loaded_favorite_filter_set"] = selected_favorite
        st.session_state["next_filter_id"] = (
            max((int(item.get("id", 0)) for item in loaded_filter_set), default=0) + 1
        )

    if "current_filter_set" not in st.session_state:
        st.session_state["current_filter_set"] = deepcopy(loaded_filter_set)

    if "next_filter_id" not in st.session_state:
        st.session_state["next_filter_id"] = (
            max((int(item.get("id", 0)) for item in st.session_state["current_filter_set"]), default=0) + 1
        )

    current_filter_set = st.session_state["current_filter_set"]

    tf = st.selectbox(
        "Timeframe",
        ["DAY", "WEEK", "MONTH"],
        index=["DAY", "WEEK", "MONTH"].index(settings.get("tf", "DAY")),
    )

    st.subheader("Add Filter")
    col1, col2 = st.columns([3, 1])
    with col1:
        filter_type_to_add = st.selectbox(
            "Filter Category",
            list(FILTER_TYPE_LABELS.keys()),
            format_func=lambda value: FILTER_TYPE_LABELS[value],
        )
    with col2:
        add_filter = st.button("Add Filter")

    if add_filter:
        current_filter_set.append({
            "id": st.session_state["next_filter_id"],
            "type": filter_type_to_add,
            "params": deepcopy(FILTER_TYPE_DEFAULTS[filter_type_to_add]),
        })
        st.session_state["next_filter_id"] += 1
        st.rerun()

    st.subheader("Current Filter Set")

    if not current_filter_set:
        st.info("No filters added yet. Add at least one filter before running the screener.")

    rendered_filter_set = []

    for index, filter_item in enumerate(current_filter_set, start=1):
        filter_id = filter_item["id"]
        filter_type = filter_item["type"]
        params = deepcopy(FILTER_TYPE_DEFAULTS[filter_type])
        params.update(filter_item.get("params", {}))

        with st.expander(f"{index}. {FILTER_TYPE_LABELS[filter_type]}", expanded=True):
            remove_filter = st.button("Remove Filter", key=f"remove_filter_{filter_id}")
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
                    key=f"filter_{filter_id}_ma",
                ))

            elif filter_type == "short_above_long":
                col1, col2 = st.columns(2)
                with col1:
                    params["short_ma"] = int(st.number_input(
                        "Short MA",
                        min_value=2,
                        max_value=500,
                        value=int(params.get("short_ma", 50)),
                        key=f"filter_{filter_id}_short_ma",
                    ))
                with col2:
                    params["long_ma"] = int(st.number_input(
                        "Long MA",
                        min_value=2,
                        max_value=1000,
                        value=int(params.get("long_ma", 200)),
                        key=f"filter_{filter_id}_long_ma",
                    ))

            elif filter_type == "price_near_long":
                col1, col2 = st.columns(2)
                with col1:
                    params["long_ma"] = int(st.number_input(
                        "Long MA",
                        min_value=2,
                        max_value=1000,
                        value=int(params.get("long_ma", 200)),
                        key=f"filter_{filter_id}_price_long_ma",
                    ))
                with col2:
                    params["threshold_pct"] = float(st.number_input(
                        "Within Percent",
                        min_value=0.1,
                        max_value=100.0,
                        value=float(params.get("threshold_pct", 5.0)),
                        step=0.1,
                        key=f"filter_{filter_id}_threshold_pct",
                    ))

            elif filter_type == "golden_cross":
                col1, col2, col3 = st.columns(3)
                with col1:
                    params["short_ma"] = int(st.number_input(
                        "Short MA",
                        min_value=2,
                        max_value=500,
                        value=int(params.get("short_ma", 50)),
                        key=f"filter_{filter_id}_golden_short_ma",
                    ))
                with col2:
                    params["long_ma"] = int(st.number_input(
                        "Long MA",
                        min_value=2,
                        max_value=1000,
                        value=int(params.get("long_ma", 200)),
                        key=f"filter_{filter_id}_golden_long_ma",
                    ))
                with col3:
                    params["lookback_units"] = int(st.number_input(
                        "Last N Time Frame Units",
                        min_value=1,
                        max_value=1000,
                        value=int(params.get("lookback_units", 20)),
                        key=f"filter_{filter_id}_golden_lookback",
                    ))

            elif filter_type == "long_ma_down_from_max":
                col1, col2, col3 = st.columns(3)
                with col1:
                    params["long_ma"] = int(st.number_input(
                        "Long MA",
                        min_value=2,
                        max_value=1000,
                        value=int(params.get("long_ma", 200)),
                        key=f"filter_{filter_id}_down_long_ma",
                    ))
                with col2:
                    params["down_pct"] = float(st.number_input(
                        "Down Percent",
                        min_value=0.1,
                        max_value=100.0,
                        value=float(params.get("down_pct", 5.0)),
                        step=0.1,
                        key=f"filter_{filter_id}_down_pct",
                    ))
                with col3:
                    params["lookback_units"] = int(st.number_input(
                        "Last M Time Frame Units",
                        min_value=2,
                        max_value=2000,
                        value=int(params.get("lookback_units", 50)),
                        key=f"filter_{filter_id}_down_lookback",
                    ))

        rendered_filter_set.append({
            "id": filter_id,
            "type": filter_type,
            "params": params,
        })

    st.session_state["current_filter_set"] = rendered_filter_set
    filter_set = normalize_filter_set(rendered_filter_set)
    active_filter_count = len(filter_set)
    st.info(f"Filters in current set: {active_filter_count}")

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
        elif not filter_set:
            st.error("Add at least one filter before saving a favorite.")
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
            st.error("Add at least one filter before running the screener.")
            st.stop()

        for filter_item in filter_set:
            params = filter_item["params"]
            label = FILTER_TYPE_LABELS[filter_item["type"]]
            if filter_item["type"] in {"short_above_long", "golden_cross"} and params["short_ma"] >= params["long_ma"]:
                st.error(f"Short MA must be less than Long MA in: {label}.")
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
        df.index = range(1, len(df) + 1)
        st.dataframe(df, use_container_width=True)

        st.download_button(
            "Download Results CSV",
            df.to_csv(index=False),
            "results.csv",
            "text/csv",
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
