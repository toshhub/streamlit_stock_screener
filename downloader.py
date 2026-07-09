import json
import hashlib
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import timedelta
from threading import Lock

import pandas as pd
import yfinance as yf

from config import DAILY_DIR, MONTHLY_DIR, WEEKLY_DIR

TIMEFRAME_CONFIG = {
    "DAY": {"interval": "1d", "period": "5y", "target_dir": DAILY_DIR},
    "WEEK": {"interval": "1wk", "period": "10y", "target_dir": WEEKLY_DIR},
    "MONTH": {"interval": "1mo", "period": "max", "target_dir": MONTHLY_DIR},
}

# Keep this conservative to avoid overloading Yahoo Finance or hitting rate limits.
# Increase carefully if your network and yfinance remain stable.
DEFAULT_MAX_DOWNLOAD_WORKERS = 3
DEFAULT_DOWNLOAD_BATCH_SIZE = 25
NIFTY_DATA_SYMBOL = "NIFTY"
INDEX_YFINANCE_SYMBOLS = {
    NIFTY_DATA_SYMBOL: "^NSEI",
}
YFINANCE_DOWNLOAD_LOCK = Lock()


def flatten_columns(df):
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [c[0] if c[0] else c[1] for c in df.columns]
    return df


def yfinance_symbol(symbol):
    clean = str(symbol).strip().upper()
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
    if not out_file.exists():
        return pd.DataFrame()

    try:
        records = json.loads(out_file.read_text())
    except Exception:
        return pd.DataFrame()

    if not isinstance(records, list):
        return pd.DataFrame()

    return _records_to_dataframe(records)


def _next_download_start(existing_df, interval):
    if existing_df.empty or "Date" not in existing_df.columns:
        return None

    latest = pd.to_datetime(existing_df["Date"], errors="coerce").max()
    if pd.isna(latest):
        return None

    if interval == "1mo":
        return latest + pd.DateOffset(months=1)
    if interval == "1wk":
        return latest + pd.DateOffset(weeks=1)
    return latest + pd.DateOffset(days=1)


def _prepare_downloaded_dataframe(data):
    data = flatten_columns(data)
    data.reset_index(inplace=True)
    if "Date" not in data.columns and "index" in data.columns:
        data = data.rename(columns={"index": "Date"})
    return _records_to_dataframe(data.to_dict(orient="records"))


def _downloaded_data_hash(df):
    if df.empty:
        return ""

    signature_columns = [
        column
        for column in ["Date", "Open", "High", "Low", "Close", "Volume"]
        if column in df.columns
    ]
    signature_df = df[signature_columns].copy()
    if "Date" in signature_df.columns:
        signature_df["Date"] = pd.to_datetime(signature_df["Date"], errors="coerce").dt.strftime("%Y-%m-%d")
    raw = signature_df.to_json(orient="records", date_format="iso", default_handler=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _write_records_atomic(out_file, df):
    output_df = df.copy()
    output_df["Date"] = pd.to_datetime(output_df["Date"], errors="coerce").dt.strftime("%Y-%m-%d")
    records = output_df.to_dict(orient="records")
    tmp_file = out_file.with_suffix(out_file.suffix + ".tmp")
    tmp_file.write_text(json.dumps(records, indent=2))
    tmp_file.replace(out_file)


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


def _chunked(items, size):
    size = max(1, int(size or 1))
    for index in range(0, len(items), size):
        yield items[index:index + size]


def _download_request_window(existing_df, interval, period, incremental):
    if not incremental:
        return None, None, period

    download_start = _next_download_start(existing_df, interval)
    today = pd.Timestamp.today().normalize()
    if download_start is not None and download_start.normalize() > today:
        return download_start, None, None

    if download_start is None:
        return None, None, period

    return download_start, today + timedelta(days=1), None


def _build_download_plan(symbols, config, incremental):
    today = pd.Timestamp.today().normalize()
    ready_rows = []
    download_tasks = []

    for index, symbol in enumerate(symbols):
        out_file = config["target_dir"] / f"{symbol}.json"
        existing_df = _load_existing_dataframe(out_file) if incremental else pd.DataFrame()
        start, end, period = _download_request_window(
            existing_df,
            config["interval"],
            config["period"],
            incremental,
        )

        if incremental and start is not None and start.normalize() > today:
            ready_rows.append({
                "Index": index,
                "Row": {"Symbol": symbol, "Downloaded": True, "Rows Added": 0, "Status": "Already current", "Error": ""},
            })
            continue

        download_tasks.append({
            "Index": index,
            "Symbol": symbol,
            "Ticker": yfinance_symbol(symbol),
            "OutFile": out_file,
            "ExistingData": existing_df,
            "RowsBefore": len(existing_df),
            "Start": start,
            "End": end,
            "Period": period,
        })

    return ready_rows, download_tasks


def _task_window_key(task):
    start_key = task["Start"].strftime("%Y-%m-%d") if task["Start"] is not None else ""
    end_key = task["End"].strftime("%Y-%m-%d") if task["End"] is not None else ""
    period_key = task["Period"] or ""
    return start_key, end_key, period_key


def _split_tasks_into_batches(tasks, batch_size):
    grouped = {}
    for task in tasks:
        grouped.setdefault(_task_window_key(task), []).append(task)

    batches = []
    for key in sorted(grouped):
        for chunk in _chunked(grouped[key], batch_size):
            batches.append(chunk)
    return batches


def _extract_ticker_frame(data, ticker):
    if data.empty:
        return pd.DataFrame()

    if isinstance(data.columns, pd.MultiIndex):
        if ticker in data.columns.get_level_values(0):
            return data.xs(ticker, axis=1, level=0, drop_level=True)
        if ticker in data.columns.get_level_values(-1):
            return data.xs(ticker, axis=1, level=-1, drop_level=True)
        return pd.DataFrame()

    return data


def _download_batch_data(batch, interval, max_retries=2):
    first = batch[0]
    tickers = [task["Ticker"] for task in batch]
    download_kwargs = {
        "tickers": tickers,
        "interval": interval,
        "auto_adjust": True,
        "progress": False,
        "threads": True,
        "group_by": "ticker",
    }
    if first["Start"] is not None:
        download_kwargs["start"] = first["Start"].strftime("%Y-%m-%d")
        download_kwargs["end"] = first["End"].strftime("%Y-%m-%d")
    else:
        download_kwargs["period"] = first["Period"]

    last_error = None
    for attempt in range(1, max_retries + 1):
        try:
            data = yf.download(**download_kwargs)
            return data
        except Exception as exc:
            last_error = str(exc)
            if attempt < max_retries:
                time.sleep(2)
                continue
            raise

    raise RuntimeError(last_error or "Batch download failed")


def _prepare_batch_frames(batch, data):
    prepared_by_symbol = {}
    hash_groups = {}

    for task in batch:
        symbol = task["Symbol"]
        ticker_data = _extract_ticker_frame(data, task["Ticker"])
        downloaded_df = _prepare_downloaded_dataframe(ticker_data) if not ticker_data.empty else pd.DataFrame()
        prepared_by_symbol[symbol] = downloaded_df
        data_hash = _downloaded_data_hash(downloaded_df)
        if data_hash:
            hash_groups.setdefault(data_hash, []).append(symbol)

    return prepared_by_symbol, hash_groups


def _process_downloaded_batch(batch, data, config=None):
    prepared_by_symbol, hash_groups = _prepare_batch_frames(batch, data)
    tasks_by_symbol = {task["Symbol"]: task for task in batch}
    rows = []

    duplicate_symbols = {
        symbol
        for symbols in hash_groups.values()
        if len(symbols) > 1
        for symbol in symbols
    }

    retry_symbols = set()
    if config is not None:
        for symbols in hash_groups.values():
            if len(symbols) <= 1:
                continue

            retry_symbols.update(symbols)
            duplicate_tasks = [tasks_by_symbol[symbol] for symbol in symbols]
            retry_batch_size = max(1, len(duplicate_tasks) // 2)
            for retry_batch in _chunked(duplicate_tasks, retry_batch_size):
                rows.extend(_download_batch_rows(retry_batch, config))

    for task in batch:
        symbol = task["Symbol"]
        if symbol in retry_symbols:
            continue

        downloaded_df = prepared_by_symbol[symbol]
        existing_df = task["ExistingData"]

        if downloaded_df.empty:
            if not existing_df.empty:
                rows.append({
                    "Index": task["Index"],
                    "Row": {"Symbol": symbol, "Downloaded": True, "Rows Added": 0, "Status": "Already current", "Error": ""},
                })
            else:
                rows.append({
                    "Index": task["Index"],
                    "Row": {"Symbol": symbol, "Downloaded": False, "Rows Added": 0, "Status": "Failed", "Error": "No data returned"},
                })
            continue

        if symbol in duplicate_symbols:
            duplicate_group = next(
                symbols for symbols in hash_groups.values()
                if symbol in symbols and len(symbols) > 1
            )
            rows.append({
                "Index": task["Index"],
                "Row": {
                    "Symbol": symbol,
                    "Downloaded": False,
                    "Rows Added": 0,
                    "Status": "Duplicate data blocked",
                    "Error": f"Identical candle data returned for: {', '.join(duplicate_group)}",
                },
            })
            continue

        merged_df = _merge_price_data(existing_df, downloaded_df)
        rows_before = task["RowsBefore"]
        rows_after = len(merged_df)
        rows_added = max(0, rows_after - rows_before)
        _write_records_atomic(task["OutFile"], merged_df)
        status = "Full download" if existing_df.empty else ("Updated" if rows_added else "Already current")
        rows.append({
            "Index": task["Index"],
            "Row": {"Symbol": symbol, "Downloaded": True, "Rows Added": rows_added, "Status": status, "Error": ""},
        })

    return rows


def _download_batch_rows(batch, config):
    try:
        data = _download_batch_data(batch, config["interval"])
        return _process_downloaded_batch(batch, data, config=config)
    except Exception as exc:
        return [
            {
                "Index": task["Index"],
                "Row": {"Symbol": task["Symbol"], "Downloaded": False, "Rows Added": 0, "Status": "Failed", "Error": str(exc)},
            }
            for task in batch
        ]


def download_symbol(symbol, interval, period, out_file, max_retries=2, incremental=True):
    existing_df = _load_existing_dataframe(out_file) if incremental else pd.DataFrame()
    download_start = _next_download_start(existing_df, interval) if incremental else None
    today = pd.Timestamp.today().normalize()
    if incremental and download_start is not None and download_start.normalize() > today:
        return {"Downloaded": True, "Rows Added": 0, "Status": "Already current"}

    last_error = None
    for attempt in range(1, max_retries + 1):
        try:
            download_kwargs = {
                "tickers": yfinance_symbol(symbol),
                "interval": interval,
                "auto_adjust": True,
                "progress": False,
                "threads": False,
            }
            if download_start is not None:
                download_kwargs["start"] = download_start.strftime("%Y-%m-%d")
                download_kwargs["end"] = (today + timedelta(days=1)).strftime("%Y-%m-%d")
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
            merged_df = _merge_price_data(existing_df, downloaded_df)
            rows_before = len(existing_df)
            rows_after = len(merged_df)
            rows_added = max(0, rows_after - rows_before)
            _write_records_atomic(out_file, merged_df)
            status = "Full download" if existing_df.empty else ("Updated" if rows_added else "Already current")
            return {"Downloaded": True, "Rows Added": rows_added, "Status": status}
        except Exception as exc:
            last_error = str(exc)
            if attempt < max_retries:
                time.sleep(2)
                continue
            raise

    return {"Downloaded": False, "Rows Added": 0, "Status": "Failed"}


def timeframe_config(timeframe):
    return TIMEFRAME_CONFIG.get(timeframe, TIMEFRAME_CONFIG["DAY"])


def clear_downloaded_json_files(timeframe):
    target_dir = timeframe_config(timeframe)["target_dir"]
    target_dir.mkdir(parents=True, exist_ok=True)

    deleted_count = 0
    for json_file in target_dir.glob("*.json"):
        json_file.unlink()
        deleted_count += 1

    return deleted_count


def clean_symbol(value):
    if pd.isna(value):
        return None

    symbol = str(value).strip().upper()
    symbol = re.sub(r"\.NS$", "", symbol)
    symbol = re.sub(r"^NSE[:\s-]*", "", symbol)
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


def load_top_symbols(excel_file, limit=1000):
    df = pd.read_excel(excel_file)
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
        symbol = clean_symbol(value)
        if symbol and symbol not in seen:
            symbols.append(symbol)
            seen.add(symbol)
        if len(symbols) >= limit:
            break

    return symbols


def _download_symbol_row(symbol, config, incremental=True):
    out_file = config["target_dir"] / f"{symbol}.json"
    try:
        result = download_symbol(
            symbol,
            config["interval"],
            config["period"],
            out_file,
            incremental=incremental,
        )
        return {"Symbol": symbol, **result, "Error": ""}
    except Exception as exc:
        return {"Symbol": symbol, "Downloaded": False, "Rows Added": 0, "Status": "Failed", "Error": str(exc)}


def download_nifty_index(timeframe, incremental=True):
    config = timeframe_config(timeframe)
    target_dir = config["target_dir"]
    target_dir.mkdir(parents=True, exist_ok=True)
    out_file = target_dir / f"{NIFTY_DATA_SYMBOL}.json"
    try:
        result = download_symbol(
            NIFTY_DATA_SYMBOL,
            config["interval"],
            config["period"],
            out_file,
            incremental=incremental,
        )
        return {"Symbol": NIFTY_DATA_SYMBOL, **result, "Error": ""}
    except Exception as exc:
        return {"Symbol": NIFTY_DATA_SYMBOL, "Downloaded": False, "Rows Added": 0, "Status": "Failed", "Error": str(exc)}


def download_top_stocks(
    excel_file,
    timeframe,
    limit=1000,
    progress_callback=None,
    max_workers=DEFAULT_MAX_DOWNLOAD_WORKERS,
    incremental=True,
    batch_size=DEFAULT_DOWNLOAD_BATCH_SIZE,
):
    config = timeframe_config(timeframe)
    target_dir = config["target_dir"]
    target_dir.mkdir(parents=True, exist_ok=True)

    symbols = load_top_symbols(excel_file, limit=limit)
    total = len(symbols)
    if total == 0:
        return []

    ready_rows, download_tasks = _build_download_plan(symbols, config, incremental)
    results_by_index = [None] * total

    completed_count = 0
    downloaded_count = 0
    for item in ready_rows:
        row = item["Row"]
        results_by_index[item["Index"]] = row
        completed_count += 1
        if row["Downloaded"]:
            downloaded_count += 1
        if progress_callback:
            progress_callback(completed_count, total, downloaded_count, row["Symbol"])

    batches = _split_tasks_into_batches(download_tasks, batch_size)
    worker_count = max(1, min(int(max_workers or 1), len(batches) or 1))

    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        future_to_batch = {
            executor.submit(_download_batch_rows, batch, config): batch
            for batch in batches
        }

        for future in as_completed(future_to_batch):
            try:
                batch_rows = future.result()
            except Exception as exc:
                batch_rows = [
                    {
                        "Index": task["Index"],
                        "Row": {
                            "Symbol": task["Symbol"],
                            "Downloaded": False,
                            "Rows Added": 0,
                            "Status": "Failed",
                            "Error": str(exc),
                        },
                    }
                    for task in future_to_batch[future]
                ]

            for item in batch_rows:
                row = item["Row"]
                results_by_index[item["Index"]] = row
                completed_count += 1
                if row["Downloaded"]:
                    downloaded_count += 1

                if progress_callback:
                    progress_callback(completed_count, total, downloaded_count, row["Symbol"])

    # Preserve the original symbol order for any downstream display/reporting.
    return [row for row in results_by_index if row is not None]
