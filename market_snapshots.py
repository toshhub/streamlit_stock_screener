"""Consolidated latest-price and monthly valuation snapshots."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd
import yfinance as yf

from config import META_DIR
from downloader import MARKET_INDIA, MARKET_US, normalize_market, yfinance_symbol
from stock_data import latest_stock_row, stock_exists


LATEST_VALUES_FILE = META_DIR / "latest_stock_values.parquet"
MONTHLY_VALUATIONS_FILE = META_DIR / "monthly_valuations.parquet"


def _write_atomic(df, path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    df.to_parquet(tmp, index=False, engine="pyarrow", compression="zstd")
    tmp.replace(path)


def refresh_latest_stock_values(market_directories):
    rows = []
    for market, directory in market_directories.items():
        for stock_dir in Path(directory).iterdir() if Path(directory).exists() else []:
            if not stock_dir.is_dir() or not stock_exists(stock_dir):
                continue
            latest = latest_stock_row(stock_dir)
            if latest is None:
                continue
            rows.append({
                "Market": normalize_market(market),
                "Symbol": stock_dir.name,
                "Date": pd.Timestamp(latest["Date"]).normalize(),
                "Close": float(latest["Close"]),
                "Volume": float(latest.get("Volume", 0) or 0),
                "UpdatedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
            })
    result = pd.DataFrame(rows)
    if not result.empty:
        _write_atomic(result, LATEST_VALUES_FILE)
    return result


def _existing_monthly():
    if not MONTHLY_VALUATIONS_FILE.exists():
        return pd.DataFrame()
    try:
        return pd.read_parquet(MONTHLY_VALUATIONS_FILE)
    except (OSError, ValueError):
        return pd.DataFrame()


def collect_monthly_valuations(symbols_by_market, month=None):
    """Append one valuation observation per stock for the requested month.

    Individual quote failures are returned and never abort the remaining
    symbols, matching the daily candle update's recovery behavior.
    """
    month_date = pd.Timestamp(month or datetime.now()).to_period("M").to_timestamp()
    existing = _existing_monthly()
    completed = set()
    if not existing.empty:
        completed = {
            (str(row.Market), str(row.Symbol))
            for row in existing[
                pd.to_datetime(existing["Month"], errors="coerce") == month_date
            ].itertuples()
        }
    new_rows = []
    failures = []
    pending = []
    for market, symbols in symbols_by_market.items():
        market = normalize_market(market)
        for symbol in symbols:
            symbol = str(symbol).strip().upper()
            if (market, symbol) in completed:
                continue
            pending.append((market, symbol))

    def fetch_one(market_symbol):
        market, symbol = market_symbol
        info = yf.Ticker(yfinance_symbol(symbol, market)).get_info()
        pe = pd.to_numeric(info.get("trailingPE"), errors="coerce")
        market_cap = pd.to_numeric(info.get("marketCap"), errors="coerce")
        revenue = pd.to_numeric(info.get("totalRevenue"), errors="coerce")
        market_cap_to_sales = (
            float(market_cap / revenue)
            if pd.notna(market_cap) and pd.notna(revenue) and revenue > 0
            else None
        )
        return {
            "Month": month_date,
            "Market": market,
            "Symbol": symbol,
            "PE": float(pe) if pd.notna(pe) else None,
            "MarketCapToSales": market_cap_to_sales,
            "CollectedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
        }

    # Fundamentals endpoints are much slower than candle downloads. A bounded
    # pool keeps the monthly all-stock pass practical without flooding Yahoo.
    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = {
            executor.submit(fetch_one, item): item
            for item in pending
        }
        for future in as_completed(futures):
            market, symbol = futures[future]
            try:
                new_rows.append(future.result())
            except Exception as exc:
                failures.append({"Market": market, "Symbol": symbol, "Error": str(exc)})
    if new_rows:
        merged = pd.concat([existing, pd.DataFrame(new_rows)], ignore_index=True)
        merged["Month"] = pd.to_datetime(merged["Month"], errors="coerce")
        merged = merged.drop_duplicates(["Month", "Market", "Symbol"], keep="last")
        merged = merged.sort_values(["Market", "Symbol", "Month"]).reset_index(drop=True)
        _write_atomic(merged, MONTHLY_VALUATIONS_FILE)
    return new_rows, failures


def valuation_chart_payload(symbol, market):
    existing = _existing_monthly()
    if existing.empty:
        return []
    rows = existing[
        (existing["Symbol"].astype(str).str.upper() == str(symbol).upper())
        & (existing["Market"].astype(str).str.upper() == normalize_market(market))
    ].copy()
    if rows.empty:
        return []
    rows["Month"] = pd.to_datetime(rows["Month"], errors="coerce")
    rows = rows.dropna(subset=["Month"]).sort_values("Month")
    payload = []
    for row in rows.itertuples():
        payload.append({
            "time": row.Month.strftime("%Y-%m-%d"),
            "pe": None if pd.isna(row.PE) else round(float(row.PE), 4),
            "marketCapToSales": (
                None if pd.isna(row.MarketCapToSales)
                else round(float(row.MarketCapToSales), 4)
            ),
        })
    return payload
