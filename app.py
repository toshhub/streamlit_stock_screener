
import streamlit as st
import pandas as pd
from datetime import datetime

from config import *
from storage import load_settings, update_settings
from screener import screen_json_file
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

    tf = st.selectbox(
        "Timeframe",
        ["DAY","WEEK","MONTH"],
        index=["DAY","WEEK","MONTH"].index(settings.get("tf", "DAY")),
    )

    col1, col2 = st.columns(2)
    with col1:
        short_ma = st.number_input("Short MA", min_value=2, max_value=500, value=int(settings.get("short_ma", 50)))
    with col2:
        long_ma = st.number_input("Long MA", min_value=2, max_value=1000, value=int(settings.get("long_ma", 200)))

    col1, col2, col3 = st.columns(3)
    with col1:
        support_threshold = st.number_input(
            "MA Support Distance %",
            min_value=0.1,
            max_value=50.0,
            value=float(settings.get("support_threshold", 5.0)),
            step=0.1,
        )
    with col2:
        cross_lookback_days = st.number_input(
            "Golden Cross Lookback Days",
            min_value=1,
            max_value=365,
            value=int(settings.get("cross_lookback_days", 20)),
        )
    with col3:
        cross_threshold = st.number_input(
            "Golden Cross Distance %",
            min_value=0.1,
            max_value=50.0,
            value=float(settings.get("cross_threshold", 5.0)),
            step=0.1,
        )

    update_settings({
        "tf": tf,
        "short_ma": short_ma,
        "long_ma": long_ma,
        "support_threshold": support_threshold,
        "cross_lookback_days": cross_lookback_days,
        "cross_threshold": cross_threshold,
    })

    run = st.button("Run Screener")

    if run:

        if short_ma >= long_ma:
            st.error("Short MA must be less than Long MA.")
            st.stop()

        target_dir = timeframe_config(tf)["target_dir"]
        rows = []
        stock_files = list(target_dir.glob("*.json"))
        progress_bar = st.progress(0)
        progress_text = st.empty()

        for index, f in enumerate(stock_files, start=1):
            r = screen_json_file(
                f,
                short_ma=int(short_ma),
                long_ma=int(long_ma),
                support_threshold_pct=float(support_threshold),
                cross_lookback_days=int(cross_lookback_days),
                cross_threshold_pct=float(cross_threshold),
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
