"""Twice-daily R2 candle updater and annual monthly-to-Parquet rollover."""

from __future__ import annotations

import io
import json
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import pandas as pd
import yfinance as yf

from app_paths import symbols_file_for_market
from downloader import (
    MARKET_INDIA,
    MARKET_US,
    last_reliable_completed_candle,
    load_top_symbols,
    normalize_market,
    yfinance_symbol,
)
from r2_stock_data import (
    CANDLE_COLUMNS,
    R2DataError,
    dataframe_json_bytes,
    get_r2_store,
    manifest_entry,
    normalize_candles,
)


OVERLAP_BUSINESS_DAYS = 5
MAX_WORKERS = max(1, int(os.environ.get("STOCK_UPDATE_WORKERS", "3")))


def _market_key(market):
    return normalize_market(market).lower()


def _read_remote_json(store, key):
    try:
        payload = store._object_bytes(key)
    except Exception as exc:
        response = getattr(exc, "response", {})
        code = str((response.get("Error") or {}).get("Code", ""))
        if code in {"NoSuchKey", "404"}:
            return pd.DataFrame(columns=CANDLE_COLUMNS)
        raise
    try:
        rows = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise R2DataError(f"Invalid existing monthly JSON {key}: {exc}") from exc
    return normalize_candles(pd.DataFrame(rows))


def _download_symbol(symbol, market, start, end):
    frame = yf.download(
        yfinance_symbol(symbol, market),
        start=start.strftime("%Y-%m-%d"),
        end=(end + pd.Timedelta(days=1)).strftime("%Y-%m-%d"),
        interval="1d",
        auto_adjust=False,
        progress=False,
        threads=False,
    )
    if frame.empty:
        return pd.DataFrame(columns=CANDLE_COLUMNS)
    if isinstance(frame.columns, pd.MultiIndex):
        frame.columns = [
            column[0] if isinstance(column, tuple) else column
            for column in frame.columns
        ]
    frame = frame.reset_index()
    if "Date" not in frame.columns and "index" in frame.columns:
        frame = frame.rename(columns={"index": "Date"})
    frame["Symbol"] = str(symbol).strip().upper()
    return normalize_candles(frame)


def update_market_month(store, manifest, market, *, now=None, symbols=None):
    market = normalize_market(market)
    market_key = _market_key(market)
    reliable_date = last_reliable_completed_candle(now=now, market=market)
    month = reliable_date.strftime("%Y-%m")
    key = store.key(f"{market_key}/current/{month}.json")
    existing = _read_remote_json(store, key)
    month_start = reliable_date.replace(day=1)
    download_start = (
        month_start
        if existing.empty
        else max(
            month_start,
            reliable_date - pd.offsets.BDay(OVERLAP_BUSINESS_DAYS),
        )
    )
    symbols = list(symbols or [])
    if not symbols:
        symbols = load_top_symbols(
            symbols_file_for_market(market),
            limit=1_000_000,
            market=market,
        )
    if market == MARKET_INDIA and "NIFTY" not in symbols:
        symbols.append("NIFTY")
    if not symbols:
        raise R2DataError(f"No symbols configured for {market}.")

    frames = []
    failures = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {
            executor.submit(
                _download_symbol,
                symbol,
                market,
                download_start,
                reliable_date,
            ): symbol
            for symbol in symbols
        }
        for future in as_completed(futures):
            symbol = futures[future]
            try:
                frame = future.result()
                if not frame.empty:
                    frames.append(frame)
            except Exception as exc:
                failures.append({"Symbol": symbol, "Error": str(exc)})

    downloaded = (
        pd.concat(frames, ignore_index=True)
        if frames
        else pd.DataFrame(columns=CANDLE_COLUMNS)
    )
    combined = normalize_candles(
        pd.concat([existing, downloaded], ignore_index=True)
    )
    combined = combined[
        combined["Date"].dt.strftime("%Y-%m").eq(month)
        & combined["Date"].le(reliable_date)
    ].reset_index(drop=True)
    if combined.empty:
        raise R2DataError(f"No valid {market} candles were produced for {month}.")
    payload = dataframe_json_bytes(combined)
    store.client.put_object(
        Bucket=store.settings.bucket,
        Key=key,
        Body=payload,
        ContentType="application/json",
    )

    market_manifest = manifest.setdefault("markets", {}).setdefault(
        market_key,
        {},
    )
    market_manifest.setdefault("yearly", {})
    market_manifest.setdefault("current", {})[month] = manifest_entry(
        key,
        payload,
        rows=len(combined),
    )
    market_manifest["symbols"] = sorted(
        set(market_manifest.get("symbols") or []) | set(combined["Symbol"])
    )
    market_manifest["symbol_count"] = int(combined["Symbol"].nunique())
    market_manifest["latest_date"] = combined["Date"].max().strftime("%Y-%m-%d")
    return {
        "Market": market,
        "Month": month,
        "Rows": len(combined),
        "Symbols": int(combined["Symbol"].nunique()),
        "Failures": failures,
    }


def finalize_year(store, manifest, market, year):
    market_key = _market_key(market)
    market_manifest = manifest.setdefault("markets", {}).setdefault(
        market_key,
        {},
    )
    year_key = str(int(year))
    if year_key in (market_manifest.get("yearly") or {}):
        return None
    month_entries = {
        month: entry
        for month, entry in (market_manifest.get("current") or {}).items()
        if month.startswith(year_key + "-")
    }
    expected_months = {
        f"{year_key}-{month:02d}" for month in range(1, 13)
    }
    if set(month_entries) != expected_months:
        return None
    frames = []
    for month, entry in sorted(month_entries.items()):
        payload = store._object_bytes(store._entry_key(entry))
        frames.append(pd.DataFrame(json.loads(payload.decode("utf-8"))))
    combined = normalize_candles(pd.concat(frames, ignore_index=True))
    combined = combined[combined["Date"].dt.year.eq(int(year))].reset_index(drop=True)
    if combined.empty:
        raise R2DataError(f"Cannot finalize empty {market_key} year {year}.")
    buffer = io.BytesIO()
    combined.to_parquet(
        buffer,
        index=False,
        engine="pyarrow",
        compression="zstd",
        row_group_size=10_000,
    )
    payload = buffer.getvalue()
    key = store.key(f"{market_key}/yearly/{year_key}.parquet")
    store.client.put_object(
        Bucket=store.settings.bucket,
        Key=key,
        Body=payload,
        ContentType="application/octet-stream",
    )
    market_manifest.setdefault("yearly", {})[year_key] = manifest_entry(
        key,
        payload,
        rows=len(combined),
    )
    for month in month_entries:
        market_manifest.setdefault("current", {}).pop(month, None)
    return {"Market": normalize_market(market), "Year": int(year), "Rows": len(combined)}


def upload_manifest(store, manifest):
    manifest["version"] = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    manifest["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    payload = json.dumps(
        manifest,
        indent=2,
        sort_keys=True,
    ).encode("utf-8")
    store.client.put_object(
        Bucket=store.settings.bucket,
        Key=store.manifest_key,
        Body=payload,
        ContentType="application/json",
    )
    return manifest


def run_update(*, now=None, symbols_by_market=None):
    store = get_r2_store()
    if not store.settings.configured:
        raise R2DataError("Cloudflare R2 is not configured.")
    try:
        manifest = store.fetch_manifest(force=True)
    except R2DataError:
        manifest = {"schema_version": 1, "markets": {}}
    now_ts = pd.Timestamp(now or datetime.now(timezone.utc))
    previous_year = now_ts.year - 1
    rollovers = []
    for market in (MARKET_INDIA, MARKET_US):
        rollover = finalize_year(store, manifest, market, previous_year)
        if rollover:
            rollovers.append(rollover)
    summaries = []
    for market in (MARKET_INDIA, MARKET_US):
        summaries.append(
            update_market_month(
                store,
                manifest,
                market,
                now=now,
                symbols=(symbols_by_market or {}).get(market),
            )
        )
    upload_manifest(store, manifest)
    return {"markets": summaries, "rollovers": rollovers}
