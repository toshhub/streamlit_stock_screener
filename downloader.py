
import json
import re

import pandas as pd
import yfinance as yf

from config import DAILY_DIR, MONTHLY_DIR, WEEKLY_DIR

TIMEFRAME_CONFIG = {
    "DAY": {"interval": "1d", "period": "5y", "target_dir": DAILY_DIR},
    "WEEK": {"interval": "1wk", "period": "10y", "target_dir": WEEKLY_DIR},
    "MONTH": {"interval": "1mo", "period": "max", "target_dir": MONTHLY_DIR},
}

def flatten_columns(df):
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [c[0] if c[0] else c[1] for c in df.columns]
    return df

def download_symbol(symbol, interval, period, out_file):
    data = yf.download(
        symbol + ".NS",
        interval=interval,
        period=period,
        auto_adjust=True,
        progress=False,
    )
    if data.empty:
        return False

    data = flatten_columns(data)
    data.reset_index(inplace=True)
    if 'Date' in data.columns:
        data['Date'] = data['Date'].astype(str)
    out_file.write_text(json.dumps(data.to_dict(orient='records'), indent=2))
    return True

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

def download_top_stocks(excel_file, timeframe, limit=1000, progress_callback=None):
    config = timeframe_config(timeframe)
    target_dir = config["target_dir"]
    target_dir.mkdir(parents=True, exist_ok=True)

    symbols = load_top_symbols(excel_file, limit=limit)
    results = []

    total = len(symbols)

    for index, symbol in enumerate(symbols, start=1):
        out_file = target_dir / f"{symbol}.json"
        try:
            ok = download_symbol(
                symbol,
                config["interval"],
                config["period"],
                out_file,
            )
            results.append({"Symbol": symbol, "Downloaded": ok, "Error": ""})
        except Exception as exc:
            results.append({"Symbol": symbol, "Downloaded": False, "Error": str(exc)})

        if progress_callback:
            downloaded_count = sum(1 for row in results if row["Downloaded"])
            progress_callback(index, total, downloaded_count, symbol)

    return results
