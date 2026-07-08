import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

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
DEFAULT_MAX_DOWNLOAD_WORKERS = 8
NIFTY_DATA_SYMBOL = "NIFTY"
INDEX_YFINANCE_SYMBOLS = {
    NIFTY_DATA_SYMBOL: "^NSEI",
}


def flatten_columns(df):
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [c[0] if c[0] else c[1] for c in df.columns]
    return df


def yfinance_symbol(symbol):
    clean = str(symbol).strip().upper()
    return INDEX_YFINANCE_SYMBOLS.get(clean, clean + ".NS")


def download_symbol(symbol, interval, period, out_file, max_retries=2):
    last_error = None
    for attempt in range(1, max_retries + 1):
        try:
            data = yf.download(
                yfinance_symbol(symbol),
                interval=interval,
                period=period,
                auto_adjust=True,
                progress=False,
                threads=False,
            )
            if data.empty:
                last_error = "No data returned (empty DataFrame)"
                if attempt < max_retries:
                    time.sleep(2)
                    continue
                return False

            data = flatten_columns(data)
            data.reset_index(inplace=True)
            if 'Date' in data.columns:
                data['Date'] = data['Date'].astype(str)
            out_file.write_text(json.dumps(data.to_dict(orient='records'), indent=2))
            return True
        except Exception as exc:
            last_error = str(exc)
            if attempt < max_retries:
                time.sleep(2)
                continue
            raise

    return False


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


def _download_symbol_row(symbol, config):
    out_file = config["target_dir"] / f"{symbol}.json"
    try:
        ok = download_symbol(
            symbol,
            config["interval"],
            config["period"],
            out_file,
        )
        return {"Symbol": symbol, "Downloaded": ok, "Error": ""}
    except Exception as exc:
        return {"Symbol": symbol, "Downloaded": False, "Error": str(exc)}


def download_nifty_index(timeframe):
    config = timeframe_config(timeframe)
    target_dir = config["target_dir"]
    target_dir.mkdir(parents=True, exist_ok=True)
    out_file = target_dir / f"{NIFTY_DATA_SYMBOL}.json"
    try:
        ok = download_symbol(
            NIFTY_DATA_SYMBOL,
            config["interval"],
            config["period"],
            out_file,
        )
        return {"Symbol": NIFTY_DATA_SYMBOL, "Downloaded": ok, "Error": ""}
    except Exception as exc:
        return {"Symbol": NIFTY_DATA_SYMBOL, "Downloaded": False, "Error": str(exc)}


def download_top_stocks(
    excel_file,
    timeframe,
    limit=1000,
    progress_callback=None,
    max_workers=DEFAULT_MAX_DOWNLOAD_WORKERS,
):
    config = timeframe_config(timeframe)
    target_dir = config["target_dir"]
    target_dir.mkdir(parents=True, exist_ok=True)

    symbols = load_top_symbols(excel_file, limit=limit)
    total = len(symbols)
    if total == 0:
        return []

    worker_count = max(1, min(int(max_workers or 1), total))
    results_by_index = [None] * total
    downloaded_count = 0
    completed_count = 0

    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        future_to_index = {
            executor.submit(_download_symbol_row, symbol, config): index
            for index, symbol in enumerate(symbols)
        }

        for future in as_completed(future_to_index):
            index = future_to_index[future]
            symbol = symbols[index]
            try:
                row = future.result()
            except Exception as exc:
                # Defensive fallback: _download_symbol_row already catches exceptions,
                # but keep this guard so one unexpected failure never stops the full batch.
                row = {"Symbol": symbol, "Downloaded": False, "Error": str(exc)}

            results_by_index[index] = row
            completed_count += 1
            if row["Downloaded"]:
                downloaded_count += 1

            if progress_callback:
                progress_callback(completed_count, total, downloaded_count, symbol)

    # Preserve the original symbol order for any downstream display/reporting.
    return [row for row in results_by_index if row is not None]
