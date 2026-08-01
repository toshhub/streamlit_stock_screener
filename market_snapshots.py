"""Consolidated latest-price and monthly valuation snapshots."""

from __future__ import annotations

from datetime import datetime
from functools import lru_cache
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading

import pandas as pd

from config import META_DIR
from downloader import MARKET_INDIA, normalize_market
from fundamentals import (
    _valuation_medians_from_history,
    fetch_screener_company_snapshot,
    fetch_screener_growth_metrics,
    save_company_fundamentals_snapshots,
)
from storage import load_fundamentals, load_pe_ratios
from stock_data import latest_stock_row, list_symbol_paths, symbol_from_path


LATEST_VALUES_FILE = META_DIR / "latest_stock_values.parquet"
MONTHLY_VALUATIONS_FILE = META_DIR / "monthly_valuations.parquet"
_FULL_SNAPSHOT_LIMIT = threading.BoundedSemaphore(2)


def _write_atomic(df, path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    df.to_parquet(tmp, index=False, engine="pyarrow", compression="zstd")
    tmp.replace(path)


def refresh_latest_stock_values(market_directories):
    stock_items = [
        (market, stock_path)
        for market, directory in market_directories.items()
        for stock_path in list_symbol_paths(directory)
    ]

    def latest_value(item):
        market, stock_path = item
        latest = latest_stock_row(stock_path)
        if latest is None:
            return None
        return {
            "Market": normalize_market(market),
            "Symbol": symbol_from_path(stock_path),
            "Date": pd.Timestamp(latest["Date"]).normalize(),
            "Close": float(latest["Close"]),
            "Volume": float(latest.get("Volume", 0) or 0),
            "UpdatedAt": datetime.now().astimezone().isoformat(
                timespec="seconds"
            ),
        }

    rows = []
    with ThreadPoolExecutor(max_workers=12) as executor:
        for row in executor.map(latest_value, stock_items):
            if row is not None:
                rows.append(row)
    result = pd.DataFrame(rows)
    if not result.empty:
        _write_atomic(result, LATEST_VALUES_FILE)
    return result


@lru_cache(maxsize=2)
def _read_monthly_snapshot(path, mtime_ns, size):
    del mtime_ns, size
    try:
        return pd.read_parquet(path)
    except (OSError, ValueError):
        return pd.DataFrame()


def _existing_monthly():
    try:
        stat = MONTHLY_VALUATIONS_FILE.stat()
    except OSError:
        return pd.DataFrame()
    return _read_monthly_snapshot(
        str(MONTHLY_VALUATIONS_FILE),
        stat.st_mtime_ns,
        stat.st_size,
    ).copy()


def _merge_valuation_rows(existing, new_rows):
    merged = pd.concat([existing, pd.DataFrame(new_rows)], ignore_index=True)
    merged["Month"] = pd.to_datetime(merged["Month"], errors="coerce")
    merged = merged.dropna(subset=["Month", "Market", "Symbol"])
    merged = merged.drop_duplicates(["Month", "Market", "Symbol"], keep="last")
    return merged.sort_values(["Market", "Symbol", "Month"]).reset_index(drop=True)


def collect_monthly_valuations(
    symbols_by_market,
    month=None,
    fundamentals_first_cut=False,
):
    """Refresh ten years of native Screener.in valuation history each month.

    The consolidated Parquet contains every Indian stock. Individual failures
    never abort other symbols, and successful symbols are checkpointed while
    the all-stock pass is still running.
    """
    month_date = pd.Timestamp(month or datetime.now()).to_period("M").to_timestamp()
    existing = _existing_monthly()
    fundamentals_cache = load_fundamentals()
    completed = set()
    local_valuation_symbols = set()
    local_valuation_medians = {}
    if not existing.empty:
        source = (
            existing["Source"].astype(str)
            if "Source" in existing.columns
            else pd.Series("", index=existing.index)
        )
        collected_month = (
            pd.to_datetime(existing["CollectedAt"], errors="coerce", utc=True)
            .dt.tz_convert(None)
            .dt.to_period("M")
            .dt.to_timestamp()
            if "CollectedAt" in existing.columns
            else pd.Series(pd.NaT, index=existing.index)
        )
        completed = {
            (str(row.Market), str(row.Symbol))
            for row in existing[
                (collected_month == month_date)
                & source.eq("Screener.in")
            ].itertuples()
        }
        local_rows = existing[source.eq("Screener.in")]
        for (row_market, row_symbol), symbol_rows in local_rows.groupby(
            ["Market", "Symbol"]
        ):
            key = (str(row_market), str(row_symbol).upper())
            local_valuation_symbols.add(key)
            local_valuation_medians[key] = _valuation_medians_from_history(
                symbol_rows.to_dict("records")
            )
    new_rows = []
    failures = []
    pending = []
    for market, symbols in symbols_by_market.items():
        market = normalize_market(market)
        if market != MARKET_INDIA:
            # Screener.in has no US-company chart source. Existing US rows are
            # preserved for backward compatibility but are not fabricated.
            continue
        for symbol in symbols:
            symbol = str(symbol).strip().upper()
            fundamentals_entry = fundamentals_cache.get(
                f"{market}:{symbol}",
                {},
            )
            fundamentals_complete = (
                isinstance(fundamentals_entry, dict)
                and bool(fundamentals_entry.get("metrics"))
                and bool(fundamentals_entry.get("valuation_medians"))
            )
            valuation_complete = (market, symbol) in completed
            if fundamentals_complete and (
                fundamentals_first_cut or valuation_complete
            ):
                continue
            # Before the first combined monthly cron, fill missing fundamentals
            # from the company page and reuse any existing valuation first cut,
            # even if that valuation history was collected last month.
            reuse_local_valuation = (
                not fundamentals_complete
                and (market, symbol) in local_valuation_symbols
            )
            pending.append((market, symbol, reuse_local_valuation))

    def fetch_one(market_symbol):
        market, symbol, reuse_local_valuation = market_symbol
        collected_at = datetime.now().astimezone().isoformat(timespec="seconds")
        if reuse_local_valuation:
            # First-cut fundamentals can reuse the valuation history already
            # committed locally. This avoids downloading the same ten-year
            # chart a second time before the first monthly combined refresh.
            metrics = fetch_screener_growth_metrics(symbol)
            valuation_medians = local_valuation_medians.get(
                (market, symbol),
                {},
            )
            valuation_rows = []
        else:
            # A full snapshot makes both page and chart requests. Keep that
            # heavier path at the original concurrency even when the lighter
            # first-cut fundamentals bootstrap uses extra workers.
            with _FULL_SNAPSHOT_LIMIT:
                rows, metrics, valuation_medians = fetch_screener_company_snapshot(
                    symbol
                )
            valuation_rows = [{
                **row,
                "Month": pd.Timestamp(row["Month"]),
                "Market": market,
                "Symbol": symbol,
                "Source": "Screener.in",
                "CollectedAt": collected_at,
            } for row in rows]
        fundamentals_snapshot = {
            "market": market,
            "symbol": symbol,
            "metrics": metrics,
            "valuation_medians": valuation_medians,
            "fetched_at": collected_at,
        }
        return valuation_rows, fundamentals_snapshot

    # Twelve workers keep the one-page fundamentals bootstrap moving. Full chart
    # snapshots remain capped at two above, while fundamentals.py's global
    # request-start limiter applies to both paths.
    checkpoint_rows = []
    checkpoint_fundamentals = []
    with ThreadPoolExecutor(max_workers=12) as executor:
        futures = {
            executor.submit(fetch_one, item): item
            for item in pending
        }
        processed = 0
        for future in as_completed(futures):
            market, symbol, _ = futures[future]
            try:
                symbol_rows, fundamentals_snapshot = future.result()
                new_rows.extend(symbol_rows)
                checkpoint_rows.extend(symbol_rows)
                checkpoint_fundamentals.append(fundamentals_snapshot)
                if (
                    len(checkpoint_rows) >= 5_000
                    or len(checkpoint_fundamentals) >= 75
                ):
                    if checkpoint_rows:
                        existing = _merge_valuation_rows(existing, checkpoint_rows)
                        _write_atomic(existing, MONTHLY_VALUATIONS_FILE)
                    save_company_fundamentals_snapshots(
                        checkpoint_fundamentals
                    )
                    checkpoint_rows.clear()
                    checkpoint_fundamentals.clear()
            except Exception as exc:
                failures.append({"Market": market, "Symbol": symbol, "Error": str(exc)})
            processed += 1
            if processed % 100 == 0 or processed == len(futures):
                print(
                    f"Valuation sync progress: {processed}/{len(futures)} symbols; "
                    f"{len(failures)} failures.",
                    flush=True,
                )
    if checkpoint_rows:
        existing = _merge_valuation_rows(existing, checkpoint_rows)
        _write_atomic(existing, MONTHLY_VALUATIONS_FILE)
    if checkpoint_fundamentals:
        save_company_fundamentals_snapshots(checkpoint_fundamentals)
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
            "eps": (
                None if not hasattr(row, "EPS") or pd.isna(row.EPS)
                else round(float(row.EPS), 4)
            ),
            "sales": (
                None if not hasattr(row, "Sales") or pd.isna(row.Sales)
                else round(float(row.Sales), 4)
            ),
            "medianPe": (
                None if not hasattr(row, "MedianPE") or pd.isna(row.MedianPE)
                else round(float(row.MedianPE), 4)
            ),
            "medianMarketCapToSales": (
                None
                if (
                    not hasattr(row, "MedianMarketCapToSales")
                    or pd.isna(row.MedianMarketCapToSales)
                )
                else round(float(row.MedianMarketCapToSales), 4)
            ),
        })
    return payload


def latest_monthly_pe_values(market, existing=None):
    """Return the newest locally stored PE per symbol without network calls."""
    existing = _existing_monthly() if existing is None else existing
    if existing.empty or "PE" not in existing.columns:
        return {}
    rows = existing[
        existing["Market"].astype(str).str.upper() == normalize_market(market)
    ].copy()
    if rows.empty:
        return {}
    rows["Month"] = pd.to_datetime(rows["Month"], errors="coerce")
    rows["PE"] = pd.to_numeric(rows["PE"], errors="coerce")
    rows = rows.dropna(subset=["Month", "PE"]).sort_values("Month")
    rows = rows.drop_duplicates("Symbol", keep="last")
    return {
        str(row.Symbol).strip().upper(): round(float(row.PE), 2)
        for row in rows.itertuples()
        if float(row.PE) > 0
    }


def historical_pe_medians_by_symbol(market, as_of=None, existing=None):
    """Return local 3Y/5Y/10Y PE medians in the table-coloring format."""
    existing = _existing_monthly() if existing is None else existing
    if existing.empty or "PE" not in existing.columns:
        return {}
    rows = existing[
        existing["Market"].astype(str).str.upper() == normalize_market(market)
    ].copy()
    if rows.empty:
        return {}
    rows["Month"] = pd.to_datetime(rows["Month"], errors="coerce")
    rows["PE"] = pd.to_numeric(rows["PE"], errors="coerce")
    rows = rows.dropna(subset=["Month", "PE"])
    rows = rows[rows["PE"] > 0]
    if rows.empty:
        return {}
    reference = pd.Timestamp(as_of or datetime.now()).normalize()
    period_years = {
        "3 Years": 3,
        "5 Years": 5,
        "10 Years": 10,
    }
    result = {}
    for symbol, symbol_rows in rows.groupby(
        rows["Symbol"].astype(str).str.strip().str.upper()
    ):
        period_values = {}
        for period, years in period_years.items():
            values = symbol_rows.loc[
                (
                    symbol_rows["Month"]
                    >= reference - pd.DateOffset(years=years)
                )
                & (symbol_rows["Month"] <= reference),
                "PE",
            ]
            if not values.empty:
                period_values[period] = round(float(values.median()), 4)
        if len(period_values) == len(period_years):
            result[symbol] = {"Median PE": period_values}
    return result


def hydrate_result_valuations(rows, market):
    """Fill result PE and median fields from local data without network calls."""
    market = normalize_market(market)
    pe_values = load_pe_ratios()
    fundamentals = load_fundamentals()
    monthly_rows = _existing_monthly()
    for symbol, pe_ratio in latest_monthly_pe_values(
        market,
        existing=monthly_rows,
    ).items():
        pe_values.setdefault(f"{market}:{symbol}", pe_ratio)
    monthly_medians = historical_pe_medians_by_symbol(
        market,
        existing=monthly_rows,
    )

    hydrated = []
    for row in rows:
        display_row = dict(row)
        symbol = str(display_row.get("Symbol", "") or "").strip().upper()
        if display_row.get("PE Ratio") in ("", None):
            display_row["PE Ratio"] = pe_values.get(
                f"{market}:{symbol}",
                pe_values.get(symbol, ""),
            )
        fundamentals_entry = fundamentals.get(f"{market}:{symbol}", {})
        local_medians = monthly_medians.get(symbol) or (
            fundamentals_entry.get("valuation_medians", {})
            if isinstance(fundamentals_entry, dict)
            else {}
        )
        if local_medians:
            display_row["ValuationMedians"] = local_medians
        hydrated.append(display_row)
    return hydrated
