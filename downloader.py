import json
import re
import time
from datetime import timedelta
from threading import Lock

import pandas as pd
import yfinance as yf

from config import DAILY_DIR, US_DAILY_DIR

MARKET_INDIA = "INDIA"
MARKET_US = "US"
MARKET_LABELS = {
    MARKET_INDIA: "India",
    MARKET_US: "US",
}

TIMEFRAME_CONFIG = {
    MARKET_INDIA: {
        "DAY": {"interval": "1d", "period": "5y", "target_dir": DAILY_DIR},
    },
    MARKET_US: {
        "DAY": {"interval": "1d", "period": "5y", "target_dir": US_DAILY_DIR},
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


def download_symbol(symbol, interval, period, out_file, max_retries=2, incremental=True, market=MARKET_INDIA):
    existing_df = _load_existing_dataframe(out_file) if incremental else pd.DataFrame()
    download_start = _next_download_start(existing_df, interval) if incremental else None
    today = pd.Timestamp.today().normalize()
    if incremental and download_start is not None and download_start.normalize() > today:
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


def _download_symbol_row(symbol, config, incremental=True, market=MARKET_INDIA):
    out_file = config["target_dir"] / f"{symbol}.json"
    try:
        result = download_symbol(
            symbol,
            config["interval"],
            config["period"],
            out_file,
            incremental=incremental,
            market=market,
        )
        return {"Symbol": symbol, **result, "Error": ""}
    except Exception as exc:
        return {"Symbol": symbol, "Downloaded": False, "Rows Added": 0, "Status": "Failed", "Error": str(exc)}


def download_nifty_index(timeframe, incremental=True, market=MARKET_INDIA):
    if normalize_market(market) != MARKET_INDIA:
        return {"Symbol": NIFTY_DATA_SYMBOL, "Downloaded": False, "Rows Added": 0, "Status": "Skipped", "Error": ""}

    config = timeframe_config(timeframe, market)
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
        row = _download_symbol_row(symbol, config, incremental=incremental, market=market)
        rows.append(row)
        if row["Downloaded"]:
            downloaded_count += 1

        if progress_callback:
            progress_callback(completed_count, total, downloaded_count, symbol)

    return rows
