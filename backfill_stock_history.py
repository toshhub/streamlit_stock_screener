"""Resumable all-stock 10-year candle backfill.

Yahoo is queried in batches for efficiency. Each returned ticker is merged
independently and written to one JSON file per stock, so one failed symbol
never prevents successful symbols in the same market from being saved.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import pandas as pd
import yfinance as yf

from app_paths import symbols_file_for_market
from config import DAILY_DIR, META_DIR, US_DAILY_DIR
from downloader import (
    MARKET_INDIA,
    MARKET_US,
    MAX_HISTORY_YEARS,
    _merge_price_data,
    _prepare_downloaded_dataframe,
    last_reliable_completed_candle,
    load_top_symbols,
    normalize_market,
    yfinance_symbol,
)
from price_alerts import check_price_alerts_for_symbol
from stock_data import load_stock_dataframe, symbol_path, write_stock_data


CHECKPOINT_FILE = META_DIR / ".ten_year_backfill_checkpoint.json"
MARKET_DIRECTORIES = {
    MARKET_INDIA: DAILY_DIR,
    MARKET_US: US_DAILY_DIR,
}


def _load_checkpoint():
    try:
        payload = json.loads(CHECKPOINT_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    return {str(item) for item in payload if item}


def _save_checkpoint(completed):
    CHECKPOINT_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = CHECKPOINT_FILE.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(sorted(completed), indent=2),
        encoding="utf-8",
    )
    temporary.replace(CHECKPOINT_FILE)


def _ticker_frame(batch_data, ticker, ticker_count):
    if batch_data is None or batch_data.empty:
        return pd.DataFrame()
    if not isinstance(batch_data.columns, pd.MultiIndex):
        return batch_data.copy() if ticker_count == 1 else pd.DataFrame()
    first_level = set(batch_data.columns.get_level_values(0))
    second_level = set(batch_data.columns.get_level_values(1))
    if ticker in first_level:
        return batch_data[ticker].copy()
    if ticker in second_level:
        return batch_data.xs(ticker, axis=1, level=1).copy()
    return pd.DataFrame()


def _store_symbol(symbol, market, downloaded):
    target = symbol_path(MARKET_DIRECTORIES[market], symbol)
    existing = load_stock_dataframe(target)
    downloaded = _prepare_downloaded_dataframe(downloaded)
    if (
        downloaded.empty
        or "Close" not in downloaded.columns
        or pd.to_numeric(downloaded["Close"], errors="coerce").notna().sum() == 0
    ):
        return None
    reliable_date = last_reliable_completed_candle(market=market)
    downloaded["Date"] = pd.to_datetime(downloaded["Date"], errors="coerce")
    downloaded["Close"] = pd.to_numeric(downloaded["Close"], errors="coerce")
    downloaded = downloaded.dropna(subset=["Date", "Close"])
    downloaded = downloaded[downloaded["Date"] <= reliable_date]
    if downloaded.empty:
        return None
    merged = _merge_price_data(existing, downloaded)
    merged["Date"] = pd.to_datetime(merged["Date"], errors="coerce")
    merged = merged[merged["Date"] <= reliable_date]
    changed_files = write_stock_data(
        target,
        merged,
        keep_years=MAX_HISTORY_YEARS,
    )
    try:
        check_price_alerts_for_symbol(symbol, market, stock_file=target)
    except Exception:
        # Alert infrastructure must not invalidate a successful data backfill.
        pass
    return {
        "symbol": symbol,
        "rows": len(merged),
        "first_date": merged["Date"].min(),
        "last_date": merged["Date"].max(),
        "files_updated": len(changed_files),
    }


def _download_batch(pairs):
    tickers = [ticker for _, ticker in pairs]
    return yf.download(
        tickers=tickers,
        period="10y",
        interval="1d",
        auto_adjust=True,
        progress=False,
        threads=True,
        group_by="ticker",
    )


def backfill_market(market, batch_size=40, resume=True):
    market = normalize_market(market)
    symbols_file = symbols_file_for_market(market)
    symbols = load_top_symbols(symbols_file, limit=1_000_000, market=market)
    completed = _load_checkpoint() if resume else set()
    pending = [
        symbol for symbol in symbols
        if f"{market}:{symbol}" not in completed
    ]
    failures = []
    updated = 0
    print(
        f"{market}: {len(symbols)} symbols, {len(pending)} pending; "
        f"reliable through {last_reliable_completed_candle(market=market):%Y-%m-%d}"
    )

    for offset in range(0, len(pending), max(1, int(batch_size))):
        batch_symbols = pending[offset:offset + max(1, int(batch_size))]
        pairs = [(symbol, yfinance_symbol(symbol, market)) for symbol in batch_symbols]
        try:
            batch_data = _download_batch(pairs)
        except Exception as exc:
            batch_data = pd.DataFrame()
            print(f"{market}: batch request failed at {offset + 1}: {exc}")

        missing = []
        for symbol, ticker in pairs:
            try:
                ticker_data = _ticker_frame(batch_data, ticker, len(pairs))
                result = _store_symbol(symbol, market, ticker_data)
            except Exception as exc:
                result = None
                failures.append({"market": market, "symbol": symbol, "error": str(exc)})
            if result is None:
                missing.append((symbol, ticker))
                continue
            completed.add(f"{market}:{symbol}")
            updated += 1

        # Retry missing batch members one at a time. Yahoo can omit an
        # individual ticker even when the surrounding batch succeeds.
        for symbol, ticker in missing:
            try:
                single_data = yf.download(
                    tickers=ticker,
                    period="10y",
                    interval="1d",
                    auto_adjust=True,
                    progress=False,
                    threads=False,
                )
                result = _store_symbol(symbol, market, single_data)
            except Exception as exc:
                result = None
                failures.append({"market": market, "symbol": symbol, "error": str(exc)})
            if result is not None:
                completed.add(f"{market}:{symbol}")
                updated += 1

        _save_checkpoint(completed)
        processed = min(offset + len(batch_symbols), len(pending))
        print(
            f"{market}: processed {processed}/{len(pending)} pending "
            f"({updated} saved, {len(failures)} errors)"
        )
        time.sleep(0.25)
    return updated, failures


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--market",
        choices=("INDIA", "US", "ALL"),
        default="ALL",
    )
    parser.add_argument("--batch-size", type=int, default=40)
    parser.add_argument("--restart", action="store_true")
    args = parser.parse_args()
    markets = (
        (MARKET_INDIA, MARKET_US)
        if args.market == "ALL"
        else (args.market,)
    )
    all_failures = []
    for market in markets:
        _, failures = backfill_market(
            market,
            batch_size=args.batch_size,
            resume=not args.restart,
        )
        all_failures.extend(failures)
    print(f"Backfill finished with {len(all_failures)} recorded errors.")
    if all_failures:
        failure_file = META_DIR / "ten_year_backfill_failures.json"
        failure_file.write_text(json.dumps(all_failures, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
