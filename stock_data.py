"""Canonical yearly Parquet storage for daily stock candles.

Each symbol is represented by a directory and one file per calendar year:
``data/daily/RELIANCE/2026.parquet``.  Readers also understand the former
single JSON file so deployments can migrate lazily on the next download.
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
from pandas import Timestamp as PandasTimestamp
import pyarrow.parquet as pq


PRICE_COLUMNS = ("Date", "Open", "High", "Low", "Close", "Adj Close", "Volume")
SCREENING_HISTORY_YEARS = 5


def rolling_history_start(years=SCREENING_HISTORY_YEARS, as_of=None):
    """Return the inclusive calendar cutoff for a rolling history window."""
    reference = PandasTimestamp(as_of) if as_of is not None else PandasTimestamp.now()
    if reference.tzinfo is not None:
        reference = reference.tz_localize(None)
    return reference.normalize() - pd.DateOffset(years=max(1, int(years)))


def symbol_path(directory, symbol):
    return Path(directory) / str(symbol).strip().upper()


def symbol_from_path(path):
    path = Path(path)
    return path.stem


def legacy_json_path(path):
    path = Path(path)
    return path if path.suffix.lower() == ".json" else path.parent / f"{path.name}.json"


def stock_exists(path):
    path = Path(path)
    if path.is_dir() and any(path.glob("*.parquet")):
        return True
    return legacy_json_path(path).exists()


def list_symbol_paths(directory, include_index=True):
    directory = Path(directory)
    if not directory.exists():
        return []
    symbols = {
        child.name: child
        for child in directory.iterdir()
        if child.is_dir() and any(child.glob("*.parquet"))
    }
    for json_file in directory.glob("*.json"):
        symbols.setdefault(json_file.stem, symbol_path(directory, json_file.stem))
    paths = sorted(symbols.values(), key=lambda item: item.name)
    if include_index:
        return paths
    return [path for path in paths if path.name.upper() != "NIFTY"]


def normalize_price_dataframe(df):
    if df is None or df.empty or "Date" not in df.columns:
        return pd.DataFrame()
    normalized = df.copy()
    normalized["Date"] = pd.to_datetime(normalized["Date"], errors="coerce")
    normalized = normalized.dropna(subset=["Date"])
    if normalized.empty:
        return normalized
    # Daily candles are date values. Remove any timezone before normalization.
    try:
        normalized["Date"] = normalized["Date"].dt.tz_localize(None)
    except TypeError:
        pass
    normalized["Date"] = normalized["Date"].dt.normalize()
    for column in PRICE_COLUMNS[1:]:
        if column in normalized.columns:
            normalized[column] = pd.to_numeric(normalized[column], errors="coerce")
    if "Close" in normalized.columns:
        # Batched Yahoo responses share a union date index across tickers and
        # pad pre-listing/post-delisting dates with null OHLC rows. Those are
        # not candles and must not create artificial yearly partitions.
        normalized = normalized.dropna(subset=["Close"])
    normalized = normalized.sort_values("Date").drop_duplicates("Date", keep="last")
    return normalized.reset_index(drop=True)


def load_stock_dataframe(path, start=None, end=None):
    """Load all yearly files for a symbol, optionally pruning by year/date."""
    path = Path(path)
    frames = []
    if path.is_dir():
        start_year = PandasTimestamp(start).year if start is not None else None
        end_year = PandasTimestamp(end).year if end is not None else None
        for parquet_file in sorted(path.glob("*.parquet")):
            try:
                year = int(parquet_file.stem)
            except ValueError:
                continue
            if start_year is not None and year < start_year:
                continue
            if end_year is not None and year > end_year:
                continue
            try:
                frames.append(pd.read_parquet(parquet_file))
            except (OSError, ValueError):
                continue
    if not frames:
        json_file = legacy_json_path(path)
        if json_file.exists():
            try:
                rows = json.loads(json_file.read_text(encoding="utf-8"))
                if isinstance(rows, list):
                    frames.append(pd.DataFrame(rows))
            except (OSError, json.JSONDecodeError):
                pass
    if not frames:
        return pd.DataFrame()
    result = normalize_price_dataframe(pd.concat(frames, ignore_index=True))
    if start is not None:
        result = result[result["Date"] >= PandasTimestamp(start).normalize()]
    if end is not None:
        result = result[result["Date"] <= PandasTimestamp(end).normalize()]
    return result.reset_index(drop=True)


def latest_stock_date(path):
    path = Path(path)
    if path.is_dir():
        files = sorted(path.glob("*.parquet"), reverse=True)
        for parquet_file in files:
            try:
                parquet = pq.ParquetFile(parquet_file)
                date_index = parquet.schema.names.index("Date")
                maxima = []
                for group_index in range(parquet.metadata.num_row_groups):
                    stats = parquet.metadata.row_group(group_index).column(date_index).statistics
                    if stats is not None and stats.has_min_max:
                        maxima.append(stats.max)
                latest = max(maxima) if maxima else None
                if latest is None:
                    dates = pd.read_parquet(parquet_file, columns=["Date"])["Date"]
                    latest = pd.to_datetime(dates, errors="coerce").max()
            except (OSError, ValueError, KeyError, IndexError):
                continue
            if pd.notna(latest):
                return PandasTimestamp(latest).normalize()
    df = load_stock_dataframe(path)
    if df.empty:
        return None
    return PandasTimestamp(df["Date"].max()).normalize()


def earliest_stock_date(path):
    """Return the oldest candle date while avoiding full-history reads."""
    path = Path(path)
    if path.is_dir():
        for parquet_file in sorted(path.glob("*.parquet")):
            try:
                parquet = pq.ParquetFile(parquet_file)
                date_index = parquet.schema.names.index("Date")
                minima = []
                for group_index in range(parquet.metadata.num_row_groups):
                    stats = parquet.metadata.row_group(group_index).column(date_index).statistics
                    if stats is not None and stats.has_min_max:
                        minima.append(stats.min)
                earliest = min(minima) if minima else None
                if earliest is None:
                    dates = pd.read_parquet(parquet_file, columns=["Date"])["Date"]
                    earliest = pd.to_datetime(dates, errors="coerce").min()
            except (OSError, ValueError, KeyError, IndexError):
                continue
            if pd.notna(earliest):
                return PandasTimestamp(earliest).normalize()
    df = load_stock_dataframe(path)
    if df.empty:
        return None
    return PandasTimestamp(df["Date"].min()).normalize()


def latest_stock_row(path):
    """Return the newest candle without loading older yearly partitions."""
    path = Path(path)
    if path.is_dir():
        for parquet_file in sorted(path.glob("*.parquet"), reverse=True):
            try:
                df = normalize_price_dataframe(pd.read_parquet(parquet_file))
            except (OSError, ValueError):
                continue
            if not df.empty:
                return df.iloc[-1]
    df = load_stock_dataframe(path)
    return None if df.empty else df.iloc[-1]


def write_yearly_stock_data(path, df, keep_years=10):
    """Atomically update changed year files and prune data outside retention."""
    path = Path(path)
    normalized = normalize_price_dataframe(df)
    if normalized.empty:
        return []
    latest = PandasTimestamp(normalized["Date"].max()).normalize()
    cutoff = latest - pd.DateOffset(years=max(1, int(keep_years)))
    normalized = normalized[normalized["Date"] >= cutoff].copy()
    path.mkdir(parents=True, exist_ok=True)
    changed = []
    expected_years = set()
    for year, year_df in normalized.groupby(normalized["Date"].dt.year):
        expected_years.add(int(year))
        out_file = path / f"{int(year)}.parquet"
        year_df = year_df.reset_index(drop=True)
        existing = pd.DataFrame()
        if out_file.exists():
            try:
                existing = normalize_price_dataframe(pd.read_parquet(out_file))
            except (OSError, ValueError):
                pass
        if not existing.equals(year_df):
            tmp_file = path / f".{int(year)}.parquet.tmp"
            year_df.to_parquet(tmp_file, index=False, engine="pyarrow", compression="zstd")
            tmp_file.replace(out_file)
            changed.append(out_file)
    for old_file in path.glob("*.parquet"):
        try:
            year = int(old_file.stem)
        except ValueError:
            continue
        if year not in expected_years:
            old_file.unlink()
            changed.append(old_file)
    return changed


def migrate_legacy_json(path, keep_years=10):
    path = Path(path)
    legacy = legacy_json_path(path)
    if not legacy.exists():
        return []
    df = load_stock_dataframe(legacy)
    if df.empty:
        return []
    changed = write_yearly_stock_data(path, df, keep_years=keep_years)
    legacy.unlink()
    return changed


def remove_null_candle_rows(directory):
    """Remove Yahoo batch padding from existing yearly partitions in one pass."""
    directory = Path(directory).resolve()
    cleaned_files = 0
    removed_files = 0
    for parquet_file in directory.glob("*/*.parquet"):
        try:
            original = pd.read_parquet(parquet_file)
        except (OSError, ValueError):
            continue
        cleaned = normalize_price_dataframe(original)
        if len(cleaned) == len(original):
            continue
        if cleaned.empty:
            parquet_file.unlink()
            removed_files += 1
            continue
        temporary = parquet_file.with_name(f".{parquet_file.name}.tmp")
        cleaned.to_parquet(
            temporary,
            index=False,
            engine="pyarrow",
            compression="zstd",
        )
        temporary.replace(parquet_file)
        cleaned_files += 1
    return cleaned_files, removed_files
