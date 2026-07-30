import re
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from threading import Lock
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf
from pandas import Timestamp as PandasTimestamp

from config import DAILY_DIR, META_DIR, US_DAILY_DIR
from price_alerts import check_price_alerts_for_symbol
from stock_data import (
    latest_stock_date,
    list_symbol_paths,
    load_stock_dataframe,
    stock_exists,
    symbol_path,
    write_stock_data,
)

MARKET_INDIA = "INDIA"
MARKET_US = "US"
MARKET_LABELS = {
    MARKET_INDIA: "India",
    MARKET_US: "US",
}

TIMEFRAME_CONFIG = {
    MARKET_INDIA: {
        "DAY": {"interval": "1d", "period": "10y", "target_dir": DAILY_DIR},
    },
    MARKET_US: {
        "DAY": {"interval": "1d", "period": "10y", "target_dir": US_DAILY_DIR},
    },
}

# Keep this conservative to avoid overloading Yahoo Finance or hitting rate limits.
# Increase carefully if your network and yfinance remain stable.
DEFAULT_MAX_DOWNLOAD_WORKERS = 3
NIFTY_DATA_SYMBOL = "NIFTY"
INDEX_YFINANCE_SYMBOLS = {
    NIFTY_DATA_SYMBOL: "^NSEI",
}
YFINANCE_DOWNLOAD_LOCK = Lock()
DOWNLOAD_JOBS_LOCK = Lock()
DOWNLOAD_JOBS = {}
RECENT_RECONCILIATION_SESSIONS = 5
MAX_HISTORY_YEARS = 10
LATEST_VALUES_FILE = META_DIR / "latest_stock_values.parquet"

# GitHub runners and sandboxed deployments may not have a writable user cache.
# Keep Yahoo's timezone/cookie databases inside the application data area.
YFINANCE_CACHE_DIR = META_DIR / ".yfinance_cache"
YFINANCE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
yf.set_tz_cache_location(str(YFINANCE_CACHE_DIR))


def normalize_market(market):
    clean = str(market or MARKET_INDIA).strip().upper()
    return clean if clean in MARKET_LABELS else MARKET_INDIA


def market_label(market):
    return MARKET_LABELS[normalize_market(market)]


def flatten_columns(df):
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [c[0] if c[0] else c[1] for c in df.columns]
    return df


def yfinance_symbol(symbol, market=MARKET_INDIA):
    clean = str(symbol).strip().upper()
    if normalize_market(market) == MARKET_US:
        return INDEX_YFINANCE_SYMBOLS.get(clean, clean)
    return INDEX_YFINANCE_SYMBOLS.get(clean, clean + ".NS")


def _records_to_dataframe(records):
    df = pd.DataFrame(records)
    if df.empty or "Date" not in df.columns:
        return pd.DataFrame()

    df = df.copy()
    df["Date"] = pd.to_datetime(df["Date"], errors="coerce")
    df = df.dropna(subset=["Date"])
    if df.empty:
        return pd.DataFrame()

    df["Date"] = df["Date"].dt.strftime("%Y-%m-%d")
    return df


def _load_existing_dataframe(out_file):
    return load_stock_dataframe(out_file)


def _last_saved_date(out_file):
    return latest_stock_date(out_file)


def _data_availability_from_snapshot(snapshot_file, market):
    """Return market coverage from the consolidated daily snapshot."""
    snapshot_file = Path(snapshot_file)
    if not snapshot_file.exists():
        return None

    try:
        rows = pd.read_parquet(
            snapshot_file,
            columns=["Market", "Symbol", "Date"],
        )
    except (OSError, ValueError, KeyError):
        return None

    if rows.empty:
        return None

    market = normalize_market(market)
    rows = rows[
        rows["Market"].astype(str).str.strip().str.upper().eq(market)
    ].copy()
    if rows.empty:
        return None

    rows["Symbol"] = rows["Symbol"].astype(str).str.strip().str.upper()
    rows = rows[rows["Symbol"].ne(NIFTY_DATA_SYMBOL)]
    rows["Date"] = pd.to_datetime(rows["Date"], errors="coerce").dt.normalize()
    rows = rows.dropna(subset=["Date", "Symbol"])
    if rows.empty:
        return None

    # One row is expected per stock. Keeping the newest duplicate makes the
    # snapshot resilient to a partially migrated or manually combined file.
    rows = (
        rows.sort_values("Date")
        .drop_duplicates("Symbol", keep="last")
        .reset_index(drop=True)
    )
    latest_date = rows["Date"].max()
    stocks_on_latest_date = int(rows["Date"].eq(latest_date).sum())
    return {
        "Latest Date": PandasTimestamp(latest_date).normalize(),
        "Stocks On Latest Date": stocks_on_latest_date,
        "Current Stock Files": stocks_on_latest_date,
        "Stock Files": int(rows["Symbol"].nunique()),
    }


def _stock_data_mtime_ns(path):
    """Return the newest storage-file timestamp for one stock."""
    path = Path(path)
    candidates = [path] if path.is_file() else list(path.glob("*.parquet"))
    mtimes = []
    for candidate in candidates:
        try:
            mtimes.append(candidate.stat().st_mtime_ns)
        except OSError:
            continue
    return max(mtimes, default=0)


def data_availability_summary(
    directory,
    *,
    market=None,
    snapshot_file=LATEST_VALUES_FILE,
):
    """Return the latest date and stock-file coverage for that date."""
    try:
        from r2_stock_data import get_r2_store, r2_configured

        if market is not None and r2_configured():
            return get_r2_store().market_status(normalize_market(market).lower())
    except Exception:
        # The local snapshot remains a useful offline fallback.
        pass
    snapshot_summary = None
    if market is not None:
        snapshot_summary = _data_availability_from_snapshot(
            snapshot_file,
            market,
        )

    if not directory or not directory.exists():
        return snapshot_summary or {
            "Latest Date": None,
            "Stocks On Latest Date": 0,
            "Current Stock Files": 0,
            "Stock Files": 0,
        }

    stock_files = list_symbol_paths(directory, include_index=False)
    if snapshot_summary is not None:
        try:
            snapshot_mtime_ns = Path(snapshot_file).stat().st_mtime_ns
        except OSError:
            snapshot_mtime_ns = 0
        changed_stock_files = [
            path
            for path in stock_files
            if _stock_data_mtime_ns(path) > snapshot_mtime_ns
        ]
        if not changed_stock_files:
            return snapshot_summary
        changed_dates = [
            latest_date
            for path in changed_stock_files
            if (latest_date := _last_saved_date(path)) is not None
        ]
        snapshot_date = snapshot_summary.get("Latest Date")
        changed_latest = max(changed_dates) if changed_dates else None
        if (
            changed_latest is None
            or snapshot_date is not None
            and changed_latest <= snapshot_date
        ):
            return snapshot_summary
        stocks_on_latest_date = sum(
            date == changed_latest
            for date in changed_dates
        )
        return {
            "Latest Date": changed_latest,
            "Stocks On Latest Date": stocks_on_latest_date,
            "Current Stock Files": stocks_on_latest_date,
            "Stock Files": len(stock_files),
        }

    latest_dates = [
        latest_date
        for path in stock_files
        if (latest_date := _last_saved_date(path)) is not None
    ]
    if not latest_dates:
        return {
            "Latest Date": None,
            "Stocks On Latest Date": 0,
            "Current Stock Files": 0,
            "Stock Files": len(stock_files),
        }

    latest_date = max(latest_dates)
    stocks_on_latest_date = sum(date == latest_date for date in latest_dates)
    return {
        "Latest Date": latest_date,
        "Stocks On Latest Date": stocks_on_latest_date,
        # The displayed active universe contains only stocks successfully
        # downloaded through the latest market date. Stale files remain on
        # disk so future incremental runs can retry and recover them.
        "Current Stock Files": stocks_on_latest_date,
        "Stock Files": len(stock_files),
    }


def refresh_data_availability_snapshot():
    """Refresh the fast market-status snapshot after daily downloads."""
    # Import locally to avoid the downloader/market_snapshots module cycle
    # during application startup.
    from market_snapshots import refresh_latest_stock_values

    return refresh_latest_stock_values({
        market: timeframe_config("DAY", market)["target_dir"]
        for market in MARKET_LABELS
    })


def _next_download_start(existing_df, interval):
    if existing_df.empty or "Date" not in existing_df.columns:
        return None

    latest = pd.to_datetime(existing_df["Date"], errors="coerce").max()
    if pd.isna(latest):
        return None

    if interval == "1d":
        # Always reconcile five prior market weekdays. A candle first saved
        # during trading hours is therefore replaced by Yahoo's settled daily
        # candle on a later run, as are other recent corrections.
        return latest.normalize() - pd.offsets.BDay(
            RECENT_RECONCILIATION_SESSIONS
        )
    return _date_after_latest(latest, interval)


def _date_after_latest(latest, interval):
    if interval == "1mo":
        return latest + pd.DateOffset(months=1)
    if interval == "1wk":
        return latest + pd.DateOffset(weeks=1)
    return latest + pd.offsets.BDay(1)


def _prepare_downloaded_dataframe(data):
    data = flatten_columns(data)
    data.reset_index(inplace=True)
    if "Date" not in data.columns and "index" in data.columns:
        data = data.rename(columns={"index": "Date"})
    return _records_to_dataframe(data.to_dict(orient="records"))


def _write_records_atomic(out_file, df):
    return write_stock_data(out_file, df, keep_years=MAX_HISTORY_YEARS)


def _merge_price_data(existing_df, downloaded_df):
    if existing_df.empty:
        merged = downloaded_df.copy()
    elif downloaded_df.empty:
        merged = existing_df.copy()
    else:
        merged = pd.concat([existing_df, downloaded_df], ignore_index=True)

    if merged.empty:
        return merged

    merged["Date"] = pd.to_datetime(merged["Date"], errors="coerce")
    merged = merged.dropna(subset=["Date"])
    merged = merged.sort_values("Date")
    merged["Date"] = merged["Date"].dt.strftime("%Y-%m-%d")
    merged = merged.drop_duplicates(subset=["Date"], keep="last")
    return merged


def last_reliable_completed_candle(now=None, market=MARKET_INDIA):
    """Latest date that may safely be persisted as a final daily candle."""
    market = normalize_market(market)
    timezone = ZoneInfo("America/New_York" if market == MARKET_US else "Asia/Kolkata")
    local_now = PandasTimestamp(now or datetime.now(timezone))
    if local_now.tzinfo is None:
        local_now = local_now.tz_localize(timezone)
    else:
        local_now = local_now.tz_convert(timezone)
    # Allow time for Yahoo's official daily candle to settle after the close.
    close_hour, close_minute = (16, 30) if market == MARKET_US else (16, 0)
    candidate = local_now.normalize()
    if (local_now.hour, local_now.minute) < (close_hour, close_minute):
        candidate -= pd.Timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate -= pd.Timedelta(days=1)
    return candidate.tz_localize(None).normalize()


def _confirmed_daily_candles(downloaded_df, reliable_date):
    """Keep only usable candles no later than the completed-session cutoff."""
    if downloaded_df.empty or "Date" not in downloaded_df.columns:
        return pd.DataFrame(columns=downloaded_df.columns)
    confirmed = downloaded_df.copy()
    confirmed["Date"] = pd.to_datetime(confirmed["Date"], errors="coerce")
    confirmed = confirmed.dropna(subset=["Date"])
    confirmed["Date"] = confirmed["Date"].dt.tz_localize(None).dt.normalize()
    confirmed = confirmed[confirmed["Date"] <= PandasTimestamp(reliable_date)]
    if "Close" in confirmed.columns:
        confirmed["Close"] = pd.to_numeric(confirmed["Close"], errors="coerce")
        confirmed = confirmed.dropna(subset=["Close"])
    return confirmed.sort_values("Date").drop_duplicates("Date", keep="last")


def download_symbol(
    symbol,
    interval,
    period,
    out_file,
    max_retries=2,
    incremental=True,
    market=MARKET_INDIA,
):
    out_file = Path(out_file)
    today = pd.Timestamp.today().normalize()
    reliable_date = last_reliable_completed_candle(market=market)
    existing_df = _load_existing_dataframe(out_file) if incremental else pd.DataFrame()
    # Each symbol can have a different last saved candle. Start immediately
    # after this file's own latest date so no already-stored history is fetched.
    download_start = _next_download_start(existing_df, interval) if incremental else None
    if incremental and download_start is not None and download_start.normalize() > reliable_date:
        return {"Downloaded": True, "Rows Added": 0, "Status": "Already current"}

    last_error = None
    for attempt in range(1, max_retries + 1):
        try:
            download_kwargs = {
                "tickers": yfinance_symbol(symbol, market),
                "interval": interval,
                "auto_adjust": True,
                "progress": False,
                "threads": False,
            }
            if download_start is not None:
                download_kwargs["start"] = download_start.strftime("%Y-%m-%d")
                # Yahoo's end date is exclusive. Do not even request an
                # in-progress session; this prevents an intraday daily bar
                # from entering the reconciliation data.
                request_through = min(today, reliable_date)
                download_kwargs["end"] = (
                    request_through + timedelta(days=1)
                ).strftime("%Y-%m-%d")
            else:
                download_kwargs["period"] = period

            # yfinance keeps shared module-level state during downloads. Calling it
            # concurrently can return one ticker's candles to another worker.
            with YFINANCE_DOWNLOAD_LOCK:
                data = yf.download(**download_kwargs)
            if data.empty:
                if not existing_df.empty:
                    return {"Downloaded": True, "Rows Added": 0, "Status": "Already current"}
                last_error = "No data returned (empty DataFrame)"
                if attempt < max_retries:
                    time.sleep(2)
                    continue
                return {"Downloaded": False, "Rows Added": 0, "Status": "Failed"}

            downloaded_df = _prepare_downloaded_dataframe(data)
            downloaded_df = _confirmed_daily_candles(
                downloaded_df,
                reliable_date,
            )
            # Downloaded duplicates win, while valid stored candles omitted
            # from a partial Yahoo response remain intact.
            merged_df = _merge_price_data(existing_df, downloaded_df)
            merged_df["Date"] = pd.to_datetime(merged_df["Date"], errors="coerce")
            merged_df = merged_df[merged_df["Date"] <= reliable_date]
            rows_before = len(existing_df)
            rows_after = len(merged_df)
            rows_added = max(0, rows_after - rows_before)
            changed_files = _write_records_atomic(out_file, merged_df)
            status = "Full download" if existing_df.empty else ("Updated" if changed_files else "Already current")
            latest_confirmed = (
                pd.to_datetime(downloaded_df["Date"], errors="coerce").max()
                if not downloaded_df.empty else None
            )
            return {
                "Downloaded": True,
                "Rows Added": rows_added,
                "Files Updated": len(changed_files),
                "Reconciled From": (
                    download_start.strftime("%Y-%m-%d")
                    if download_start is not None else ""
                ),
                "Latest Confirmed Candle": (
                    PandasTimestamp(latest_confirmed).strftime("%Y-%m-%d")
                    if pd.notna(latest_confirmed) else ""
                ),
                "Status": status,
            }
        except Exception as exc:
            last_error = str(exc)
            if attempt < max_retries:
                time.sleep(2)
                continue
            raise

    return {"Downloaded": False, "Rows Added": 0, "Status": "Failed"}


def timeframe_config(timeframe, market=MARKET_INDIA):
    market_config = TIMEFRAME_CONFIG.get(normalize_market(market), TIMEFRAME_CONFIG[MARKET_INDIA])
    return market_config.get(timeframe, market_config["DAY"])


def clear_downloaded_json_files(timeframe, market=MARKET_INDIA):
    target_dir = timeframe_config(timeframe, market)["target_dir"]
    target_dir.mkdir(parents=True, exist_ok=True)

    deleted_count = 0
    for json_file in target_dir.glob("*.json"):
        json_file.unlink()
        deleted_count += 1

    return deleted_count


def clean_symbol(value, market=MARKET_INDIA):
    if pd.isna(value):
        return None

    symbol = str(value).strip().upper()
    if normalize_market(market) == MARKET_INDIA:
        symbol = re.sub(r"\.NS$", "", symbol)
        symbol = re.sub(r"^NSE[:\s-]*", "", symbol)
    else:
        symbol = symbol.replace("/", "-")
    symbol = symbol.replace(" ", "")
    return symbol or None


def find_column(columns, required_terms, optional_terms=None):
    optional_terms = optional_terms or []
    for column in columns:
        label = str(column).strip().lower()
        if all(term in label for term in required_terms):
            return column

    for column in columns:
        label = str(column).strip().lower()
        if any(term in label for term in optional_terms):
            return column

    return None


def _read_symbols_file(symbols_file):
    if str(symbols_file).lower().endswith(".csv"):
        return pd.read_csv(symbols_file)
    return pd.read_excel(symbols_file)


def load_top_symbols(symbols_file, limit=1000, market=MARKET_INDIA):
    df = _read_symbols_file(symbols_file)
    if df.empty:
        return []

    symbol_col = find_column(
        df.columns,
        required_terms=["symbol"],
        optional_terms=["nse code", "nse symbol", "ticker"],
    )
    if symbol_col is None:
        symbol_col = df.columns[0]

    market_cap_col = find_column(
        df.columns,
        required_terms=["market", "cap"],
        optional_terms=["mcap", "marketcap", "mkt cap"],
    )

    if market_cap_col is not None:
        df = df.copy()
        df["_market_cap"] = pd.to_numeric(
            df[market_cap_col].astype(str).str.replace(",", "", regex=False),
            errors="coerce",
        )
        df = df.sort_values("_market_cap", ascending=False, na_position="last")

    symbols = []
    seen = set()
    for value in df[symbol_col]:
        symbol = clean_symbol(value, market)
        if symbol and symbol not in seen:
            symbols.append(symbol)
            seen.add(symbol)
        if len(symbols) >= limit:
            break

    return symbols


def stock_files_for_symbols(directory, symbols):
    """Map symbols to canonical stock paths while preserving source order."""
    if not directory or not directory.exists():
        return []

    files = []
    seen = set()
    for symbol in symbols:
        clean = str(symbol).strip()
        if not clean or clean in seen or clean.upper() == NIFTY_DATA_SYMBOL:
            continue
        stock_file = symbol_path(directory, clean)
        if stock_exists(stock_file):
            files.append(stock_file)
            seen.add(clean)
    return files


def _download_symbol_row(
    symbol,
    config,
    incremental=True,
    market=MARKET_INDIA,
):
    out_file = symbol_path(config["target_dir"], symbol)
    try:
        result = download_symbol(
            symbol,
            config["interval"],
            config["period"],
            out_file,
            incremental=incremental,
            market=market,
        )
        if result.get("Downloaded"):
            try:
                result["Alerts Triggered"] = len(
                    check_price_alerts_for_symbol(
                        symbol,
                        market,
                        stock_file=out_file,
                    )
                )
            except Exception as alert_exc:
                # Alert processing must never turn a successful market-data
                # download into a failed download.
                result["Alert Error"] = str(alert_exc)
        return {"Symbol": symbol, **result, "Error": ""}
    except Exception as exc:
        return {"Symbol": symbol, "Downloaded": False, "Rows Added": 0, "Status": "Failed", "Error": str(exc)}


def download_nifty_index(timeframe, incremental=True, market=MARKET_INDIA):
    if normalize_market(market) != MARKET_INDIA:
        return {"Symbol": NIFTY_DATA_SYMBOL, "Downloaded": False, "Rows Added": 0, "Status": "Skipped", "Error": ""}

    config = timeframe_config(timeframe, market)
    target_dir = config["target_dir"]
    target_dir.mkdir(parents=True, exist_ok=True)
    out_file = symbol_path(target_dir, NIFTY_DATA_SYMBOL)
    try:
        result = download_symbol(
            NIFTY_DATA_SYMBOL,
            config["interval"],
            config["period"],
            out_file,
            incremental=incremental,
            market=market,
        )
        return {"Symbol": NIFTY_DATA_SYMBOL, **result, "Error": ""}
    except Exception as exc:
        return {"Symbol": NIFTY_DATA_SYMBOL, "Downloaded": False, "Rows Added": 0, "Status": "Failed", "Error": str(exc)}


def download_top_stocks(
    symbols_file,
    timeframe,
    limit=1000,
    progress_callback=None,
    max_workers=DEFAULT_MAX_DOWNLOAD_WORKERS,
    incremental=True,
    market=MARKET_INDIA,
):
    market = normalize_market(market)
    config = timeframe_config(timeframe, market)
    target_dir = config["target_dir"]
    target_dir.mkdir(parents=True, exist_ok=True)

    symbols = load_top_symbols(symbols_file, limit=limit, market=market)
    total = len(symbols)
    if total == 0:
        return []

    rows = []
    downloaded_count = 0

    for completed_count, symbol in enumerate(symbols, start=1):
        row = _download_symbol_row(
            symbol,
            config,
            incremental=incremental,
            market=market,
        )
        rows.append(row)
        if row["Downloaded"]:
            downloaded_count += 1

        if progress_callback:
            progress_callback(completed_count, total, downloaded_count, symbol)

    return rows


def _run_background_download(job, symbols_file, timeframe, limit, incremental, market):
    def update_progress(done, total, downloaded_count, symbol):
        with DOWNLOAD_JOBS_LOCK:
            job.update({
                "done": done,
                "total": total,
                "downloaded_count": downloaded_count,
                "symbol": symbol,
                "status": "Downloading",
            })

    try:
        if not incremental:
            deleted_count = clear_downloaded_json_files(timeframe, market=market)
            with DOWNLOAD_JOBS_LOCK:
                job["deleted_count"] = deleted_count
                job["status"] = "Cleared old data"

        download_rows = download_top_stocks(
            symbols_file,
            timeframe,
            limit=limit,
            progress_callback=update_progress,
            incremental=incremental,
            market=market,
        )
        nifty_row = download_nifty_index(
            timeframe,
            incremental=incremental,
            market=market,
        )
        downloaded_count = sum(1 for row in download_rows if row["Downloaded"])
        rows_added = sum(int(row.get("Rows Added", 0) or 0) for row in download_rows)
        failed = [row for row in download_rows if not row["Downloaded"]]
        snapshot_error = ""
        try:
            with DOWNLOAD_JOBS_LOCK:
                job["status"] = "Refreshing data status"
            refresh_data_availability_snapshot()
        except Exception as exc:
            # A summary refresh must not turn a successful price download into
            # a failed job; the on-demand reconciliation above remains valid.
            snapshot_error = str(exc)
        with DOWNLOAD_JOBS_LOCK:
            job.update({
                "running": False,
                "status": "Completed",
                "done": len(download_rows),
                "total": len(download_rows),
                "downloaded_count": downloaded_count,
                "rows_added": rows_added,
                "failed": failed,
                "nifty_row": nifty_row,
                "snapshot_error": snapshot_error,
                "completed_at": time.strftime("%d-%m-%Y %H:%M:%S"),
            })
    except Exception as exc:
        with DOWNLOAD_JOBS_LOCK:
            job.update({
                "running": False,
                "status": "Failed",
                "error": str(exc),
                "completed_at": time.strftime("%d-%m-%Y %H:%M:%S"),
            })


def start_background_download(symbols_file, timeframe, limit, incremental=True, market=MARKET_INDIA):
    """Start one server-side download that survives a disconnected browser session."""
    market = normalize_market(market)
    with DOWNLOAD_JOBS_LOCK:
        running_job = next((job for job in DOWNLOAD_JOBS.values() if job.get("running")), None)
        if running_job:
            return dict(running_job), False

        job = {
            "id": f"{market}-{time.time_ns()}",
            "market": market,
            "running": True,
            "status": "Starting",
            "done": 0,
            "total": int(limit),
            "downloaded_count": 0,
            "symbol": "",
            "rows_added": 0,
            "failed": [],
            "error": "",
            "started_at": time.strftime("%d-%m-%Y %H:%M:%S"),
        }
        DOWNLOAD_JOBS[market] = job

    thread = threading.Thread(
        target=_run_background_download,
        args=(job, symbols_file, timeframe, int(limit), incremental, market),
        daemon=True,
        name=f"stock-download-{market.lower()}",
    )
    thread.start()
    with DOWNLOAD_JOBS_LOCK:
        return dict(job), True


def background_download_snapshot(market=MARKET_INDIA):
    market = normalize_market(market)
    with DOWNLOAD_JOBS_LOCK:
        job = DOWNLOAD_JOBS.get(market)
        return dict(job) if job else None
