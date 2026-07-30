"""Stock candle access with Cloudflare R2 as the source of truth.

Local JSON helpers remain for tests and one-time migration utilities. Paths
under ``data/{india,us}/daily`` are virtual symbol references backed by the
manifest-driven R2 cache.
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
from pandas import Timestamp as PandasTimestamp


PRICE_COLUMNS = ("Date", "Open", "High", "Low", "Close", "Adj Close", "Volume")
SCREENING_HISTORY_YEARS = 5


def rolling_history_start(years=SCREENING_HISTORY_YEARS, as_of=None):
    """Return the inclusive calendar cutoff for a rolling history window."""
    reference = PandasTimestamp(as_of) if as_of is not None else PandasTimestamp.now()
    if reference.tzinfo is not None:
        reference = reference.tz_localize(None)
    return reference.normalize() - pd.DateOffset(years=max(1, int(years)))


def symbol_path(directory, symbol):
    """Return the canonical JSON file for one stock symbol."""
    return Path(directory) / f"{str(symbol).strip().upper()}.json"


def symbol_from_path(path):
    return Path(path).stem


def _r2_market_for_path(path):
    path = Path(path)
    parts = [part.lower() for part in path.parts]
    for market in ("india", "us"):
        if market in parts and "daily" in parts:
            return market
    return None


def _r2_store_for_path(path):
    market = _r2_market_for_path(path)
    if not market:
        return None, None
    try:
        from r2_stock_data import get_r2_store, r2_configured

        return (get_r2_store(), market) if r2_configured() else (None, market)
    except Exception:
        return None, market


def stock_exists(path):
    path = Path(path)
    if path.is_file() and path.suffix.lower() == ".json":
        return True
    if path.suffix.lower() != ".json" and path.with_suffix(".json").is_file():
        return True
    # Transitional support for the one-time Parquet-to-JSON migration.
    if path.is_dir() and any(path.glob("*.parquet")):
        return True
    store, market = _r2_store_for_path(path)
    if store is None:
        return False
    try:
        return path.stem.upper() in set(store.list_symbols(market))
    except Exception:
        return False


def list_symbol_paths(directory, include_index=True):
    directory = Path(directory)
    store, market = _r2_store_for_path(directory)
    if store is not None:
        symbols = store.list_symbols(market)
        if not include_index:
            symbols = [
                symbol for symbol in symbols if symbol.upper() != "NIFTY"
            ]
        return [directory / f"{symbol}.json" for symbol in symbols]
    if not directory.exists():
        return []
    symbols = {
        json_file.stem: json_file
        for json_file in directory.glob("*.json")
        if json_file.is_file()
    }
    # Transitional discovery lets status and migration tools see old data.
    for child in directory.iterdir():
        if child.is_dir() and any(child.glob("*.parquet")):
            symbols.setdefault(child.name, child)
    paths = sorted(symbols.values(), key=lambda item: symbol_from_path(item))
    if include_index:
        return paths
    return [
        path
        for path in paths
        if symbol_from_path(path).upper() != "NIFTY"
    ]


def normalize_price_dataframe(df):
    if df is None or df.empty or "Date" not in df.columns:
        return pd.DataFrame()
    normalized = df.copy()
    normalized["Date"] = pd.to_datetime(normalized["Date"], errors="coerce")
    normalized = normalized.dropna(subset=["Date"])
    if normalized.empty:
        return normalized
    try:
        normalized["Date"] = normalized["Date"].dt.tz_localize(None)
    except TypeError:
        pass
    normalized["Date"] = normalized["Date"].dt.normalize()
    for column in PRICE_COLUMNS[1:]:
        if column in normalized.columns:
            normalized[column] = pd.to_numeric(normalized[column], errors="coerce")
    if "Close" in normalized.columns:
        normalized = normalized.dropna(subset=["Close"])
    normalized = normalized.sort_values("Date").drop_duplicates("Date", keep="last")
    return normalized.reset_index(drop=True)


def _canonical_json_path(path):
    path = Path(path)
    if path.suffix.lower() == ".json":
        return path
    if path.is_dir():
        sibling = path.parent / f"{path.name}.json"
        if sibling.exists():
            return sibling
    return path.with_suffix(".json")


def _load_json_dataframe(path):
    try:
        rows = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return pd.DataFrame()
    return pd.DataFrame(rows) if isinstance(rows, list) else pd.DataFrame()


def _edge_json_record(path, *, last):
    """Read only the first or last flat candle object from a JSON array."""
    path = Path(path)
    try:
        with path.open("rb") as handle:
            if last:
                size = handle.seek(0, 2)
                handle.seek(max(0, size - 65_536))
            chunk = handle.read().decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return None

    if last:
        end = chunk.rfind("}")
        start = chunk.rfind("{", 0, end + 1)
    else:
        start = chunk.find("{")
        end = chunk.find("}", start + 1)
    if start < 0 or end < start:
        return None
    try:
        record = json.loads(chunk[start : end + 1])
    except json.JSONDecodeError:
        return None
    return record if isinstance(record, dict) else None


def _edge_stock_row(path, *, last):
    json_file = _canonical_json_path(path)
    if json_file.is_file():
        record = _edge_json_record(json_file, last=last)
        if record is not None:
            row = pd.Series(record)
            if "Date" in row:
                row["Date"] = pd.to_datetime(row["Date"], errors="coerce")
            return row
    store, market = _r2_store_for_path(path)
    if store is not None:
        entries = store.market_entries(market)
        periods = sorted(entries["yearly"]) + sorted(entries["current"])
        if not periods:
            return None
        period = periods[-1 if last else 0]
        if len(period) == 4:
            start = f"{period}-01-01"
            end = f"{period}-12-31"
        else:
            start = f"{period}-01"
            end = (
                PandasTimestamp(start) + pd.offsets.MonthEnd(1)
            ).strftime("%Y-%m-%d")
        frame = store.load_symbol(
            market,
            path.stem,
            start=start,
            end=end,
        )
        if frame.empty:
            return None
        return frame.iloc[-1 if last else 0]
    df = load_stock_dataframe(path)
    if df.empty:
        return None
    return df.iloc[-1 if last else 0]


def _load_legacy_parquet_dataframe(path, start=None, end=None):
    """Read legacy yearly partitions only for conversion compatibility."""
    frames = []
    start_year = PandasTimestamp(start).year if start is not None else None
    end_year = PandasTimestamp(end).year if end is not None else None
    for parquet_file in sorted(Path(path).glob("*.parquet")):
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
    return (
        pd.concat(frames, ignore_index=True)
        if frames
        else pd.DataFrame()
    )


def load_stock_dataframe(path, start=None, end=None, columns=None):
    """Load one stock JSON file and optionally restrict its date window."""
    path = Path(path)
    requested_columns = list(dict.fromkeys(columns or []))
    for required in ("Date", "Close"):
        if requested_columns and required not in requested_columns:
            requested_columns.append(required)
    json_file = _canonical_json_path(path)
    if json_file.exists():
        frame = _load_json_dataframe(json_file)
    elif path.is_dir():
        frame = _load_legacy_parquet_dataframe(path, start=start, end=end)
    else:
        store, market = _r2_store_for_path(path)
        frame = (
            store.load_symbol(
                market,
                path.stem,
                start=start,
                end=end,
                columns=requested_columns or None,
            )
            if store is not None
            else pd.DataFrame()
        )
    result = normalize_price_dataframe(frame)
    if result.empty:
        return result
    if start is not None:
        result = result[result["Date"] >= PandasTimestamp(start).normalize()]
    if end is not None:
        result = result[result["Date"] <= PandasTimestamp(end).normalize()]
    if requested_columns:
        result = result[
            [column for column in requested_columns if column in result.columns]
        ]
    return result.reset_index(drop=True)


def latest_stock_date(path):
    store, market = _r2_store_for_path(path)
    if store is not None:
        try:
            manifest = store.fetch_manifest()
            latest = (
                ((manifest.get("markets") or {}).get(market, {}))
                .get("latest_date")
            )
            if latest:
                return PandasTimestamp(latest).normalize()
        except Exception:
            pass
    row = _edge_stock_row(path, last=True)
    if row is None or pd.isna(row.get("Date")):
        return None
    return PandasTimestamp(row["Date"]).normalize()


def earliest_stock_date(path):
    row = _edge_stock_row(path, last=False)
    if row is None or pd.isna(row.get("Date")):
        return None
    return PandasTimestamp(row["Date"]).normalize()


def latest_stock_row(path):
    return _edge_stock_row(path, last=True)


def _json_records(df):
    serializable = df.copy()
    serializable["Date"] = serializable["Date"].dt.strftime("%Y-%m-%d")
    serializable = serializable.where(pd.notna(serializable), None)
    return serializable.to_dict(orient="records")


def _frames_equal(left, right):
    left = normalize_price_dataframe(left)
    right = normalize_price_dataframe(right)
    if list(left.columns) != list(right.columns):
        return False
    try:
        pd.testing.assert_frame_equal(
            left,
            right,
            check_dtype=False,
            check_exact=False,
            rtol=1e-10,
            atol=1e-10,
        )
    except AssertionError:
        return False
    return True


def write_stock_data(path, df, keep_years=10):
    """Atomically update one stock JSON file and retain ten rolling years."""
    path = _canonical_json_path(path)
    normalized = normalize_price_dataframe(df)
    if normalized.empty:
        return []
    latest = PandasTimestamp(normalized["Date"].max()).normalize()
    cutoff = latest - pd.DateOffset(years=max(1, int(keep_years)))
    normalized = normalized[normalized["Date"] >= cutoff].reset_index(drop=True)
    existing = load_stock_dataframe(path) if path.exists() else pd.DataFrame()
    if not existing.empty:
        existing_latest = PandasTimestamp(existing["Date"].max()).normalize()
        existing_cutoff = existing_latest - pd.DateOffset(
            years=max(1, int(keep_years))
        )
        existing = existing[existing["Date"] >= existing_cutoff].reset_index(
            drop=True
        )
    if _frames_equal(existing, normalized):
        return []
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(_json_records(normalized), separators=(",", ":")),
        encoding="utf-8",
    )
    temporary.replace(path)
    return [path]


def migrate_parquet_symbol(path, destination=None, keep_years=10):
    """Convert one legacy yearly-Parquet directory after verifying JSON output."""
    path = Path(path)
    if not path.is_dir():
        return []
    parquet_files = sorted(path.glob("*.parquet"))
    if not parquet_files:
        return []
    source = normalize_price_dataframe(_load_legacy_parquet_dataframe(path))
    if source.empty:
        raise ValueError(f"No valid candles found in {path}")
    destination = Path(destination or path.parent) / f"{path.name}.json"
    if destination.exists():
        source = normalize_price_dataframe(
            pd.concat(
                [load_stock_dataframe(destination), source],
                ignore_index=True,
            )
        )
    changed = write_stock_data(destination, source, keep_years=keep_years)
    converted = load_stock_dataframe(destination)
    expected_latest = PandasTimestamp(source["Date"].max()).normalize()
    cutoff = expected_latest - pd.DateOffset(years=max(1, int(keep_years)))
    expected = source[source["Date"] >= cutoff].reset_index(drop=True)
    if not _frames_equal(converted, expected):
        raise ValueError(f"JSON verification failed for {path.name}")
    for parquet_file in parquet_files:
        parquet_file.unlink()
    path.rmdir()
    return changed


def remove_null_candle_rows(directory):
    """Remove invalid Yahoo padding rows from canonical JSON files."""
    cleaned_files = 0
    removed_files = 0
    for json_file in Path(directory).glob("*.json"):
        original = _load_json_dataframe(json_file)
        cleaned = normalize_price_dataframe(original)
        if len(cleaned) == len(original):
            continue
        if cleaned.empty:
            json_file.unlink()
            removed_files += 1
            continue
        write_stock_data(json_file, cleaned)
        cleaned_files += 1
    return cleaned_files, removed_files
