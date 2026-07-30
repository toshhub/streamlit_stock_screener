"""Cloudflare R2 source-of-truth and local cache for aggregate candle data.

Remote layout::

    stock-data/{india,us}/yearly/YYYY.parquet
    stock-data/{india,us}/current/YYYY-MM.json
    stock-data/manifest.json

The public helpers are intentionally independent of Streamlit.  The app,
GitHub Actions, and tests can therefore share the same manifest and validation
rules without importing the UI.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from config import STOCK_CACHE_DIR


CANDLE_COLUMNS = (
    "Symbol",
    "Date",
    "Open",
    "High",
    "Low",
    "Close",
    "Adj Close",
    "Volume",
)
MARKETS = ("india", "us")
DEFAULT_R2_CACHE_DIR = STOCK_CACHE_DIR / "r2"
DEFAULT_MANIFEST_REFRESH_SECONDS = 60.0


class R2ConfigurationError(RuntimeError):
    pass


class R2DataError(RuntimeError):
    pass


def _clean_market(market):
    clean = str(market or "").strip().lower()
    if clean not in MARKETS:
        raise ValueError(f"Unsupported stock market: {market}")
    return clean


def normalize_candles(frame, *, market=None):
    """Normalize and validate aggregate Symbol + Date candle rows."""
    if frame is None or frame.empty:
        return pd.DataFrame(columns=CANDLE_COLUMNS)
    if "Symbol" not in frame.columns or "Date" not in frame.columns:
        raise R2DataError("Candle data must contain Symbol and Date columns.")
    result = frame.copy()
    result["Symbol"] = result["Symbol"].astype(str).str.strip().str.upper()
    result["Date"] = pd.to_datetime(result["Date"], errors="coerce")
    try:
        result["Date"] = result["Date"].dt.tz_localize(None)
    except TypeError:
        pass
    result["Date"] = result["Date"].dt.normalize()
    result = result.dropna(subset=["Date"])
    result = result[result["Symbol"].ne("")]
    for column in CANDLE_COLUMNS[2:]:
        if column in result.columns:
            result[column] = pd.to_numeric(result[column], errors="coerce")
    if "Close" not in result.columns:
        raise R2DataError("Candle data must contain Close.")
    result = result.dropna(subset=["Close"])
    result = (
        result.sort_values(["Symbol", "Date"])
        .drop_duplicates(["Symbol", "Date"], keep="last")
        .reset_index(drop=True)
    )
    if result.duplicated(["Symbol", "Date"]).any():
        raise R2DataError("Duplicate Symbol + Date rows remain after validation.")
    if market is not None and _clean_market(market) not in MARKETS:
        raise R2DataError(f"Invalid market: {market}")
    return result


def dataframe_json_bytes(frame):
    clean = normalize_candles(frame)
    serializable = clean.copy()
    serializable["Date"] = serializable["Date"].dt.strftime("%Y-%m-%d")
    serializable = serializable.where(pd.notna(serializable), None)
    return json.dumps(
        serializable.to_dict(orient="records"),
        separators=(",", ":"),
    ).encode("utf-8")


def sha256_bytes(payload):
    return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True)
class R2Settings:
    bucket: str
    endpoint_url: str
    access_key_id: str
    secret_access_key: str
    prefix: str = "stock-data"
    cache_dir: Path = DEFAULT_R2_CACHE_DIR
    manifest_refresh_seconds: float = DEFAULT_MANIFEST_REFRESH_SECONDS

    @classmethod
    def from_mapping(cls, values=None):
        values = dict(values or {})

        def setting(name, environment, default=""):
            return str(values.get(name) or os.environ.get(environment, default)).strip()

        account_id = setting("account_id", "R2_ACCOUNT_ID")
        endpoint = setting("endpoint_url", "R2_ENDPOINT_URL")
        if not endpoint and account_id:
            endpoint = f"https://{account_id}.r2.cloudflarestorage.com"
        cache_value = setting("cache_dir", "R2_CACHE_DIR")
        refresh_value = setting(
            "manifest_refresh_seconds",
            "R2_MANIFEST_REFRESH_SECONDS",
            str(DEFAULT_MANIFEST_REFRESH_SECONDS),
        )
        try:
            manifest_refresh_seconds = max(0.0, float(refresh_value))
        except ValueError:
            manifest_refresh_seconds = DEFAULT_MANIFEST_REFRESH_SECONDS
        return cls(
            bucket=setting("bucket", "R2_BUCKET_NAME"),
            endpoint_url=endpoint,
            access_key_id=setting("access_key_id", "R2_ACCESS_KEY_ID"),
            secret_access_key=setting(
                "secret_access_key",
                "R2_SECRET_ACCESS_KEY",
            ),
            prefix=setting("prefix", "R2_PREFIX", "stock-data").strip("/"),
            cache_dir=(
                Path(cache_value).expanduser()
                if cache_value
                else DEFAULT_R2_CACHE_DIR
            ),
            manifest_refresh_seconds=manifest_refresh_seconds,
        )

    @property
    def configured(self):
        return all(
            (
                self.bucket,
                self.endpoint_url,
                self.access_key_id,
                self.secret_access_key,
            )
        )


class R2StockDataStore:
    def __init__(self, settings, client=None):
        self.settings = settings
        self.cache_dir = Path(settings.cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._client = client
        self._manifest = None
        self._manifest_fetched_at = 0.0
        self._lock = threading.RLock()

    @property
    def client(self):
        if self._client is None:
            if not self.settings.configured:
                raise R2ConfigurationError(
                    "Configure R2_BUCKET_NAME, R2_ACCOUNT_ID (or "
                    "R2_ENDPOINT_URL), R2_ACCESS_KEY_ID, and "
                    "R2_SECRET_ACCESS_KEY."
                )
            try:
                import boto3
            except ImportError as exc:
                raise R2ConfigurationError(
                    "The boto3 package is required for Cloudflare R2."
                ) from exc
            self._client = boto3.client(
                "s3",
                endpoint_url=self.settings.endpoint_url,
                aws_access_key_id=self.settings.access_key_id,
                aws_secret_access_key=self.settings.secret_access_key,
                region_name="auto",
            )
        return self._client

    def key(self, relative):
        relative = str(relative).strip("/")
        prefix = self.settings.prefix
        return f"{prefix}/{relative}" if prefix else relative

    @property
    def manifest_key(self):
        return self.key("manifest.json")

    @property
    def manifest_cache_path(self):
        return self.cache_dir / "manifest.json"

    @property
    def cache_index_path(self):
        return self.cache_dir / ".cache-index.json"

    def _read_cache_index(self):
        try:
            value = json.loads(self.cache_index_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    def _write_cache_index(self, value):
        temporary = self.cache_index_path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(value, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )
        temporary.replace(self.cache_index_path)

    def _object_bytes(self, key):
        response = self.client.get_object(Bucket=self.settings.bucket, Key=key)
        return response["Body"].read()

    def fetch_manifest(self, *, force=False):
        with self._lock:
            manifest_age = time.monotonic() - self._manifest_fetched_at
            if (
                self._manifest is not None
                and not force
                and manifest_age < self.settings.manifest_refresh_seconds
            ):
                return self._manifest
            try:
                payload = self._object_bytes(self.manifest_key)
                manifest = json.loads(payload.decode("utf-8"))
            except Exception as exc:
                if self.manifest_cache_path.exists() and not force:
                    try:
                        manifest = json.loads(
                            self.manifest_cache_path.read_text(encoding="utf-8")
                        )
                    except (OSError, json.JSONDecodeError):
                        raise R2DataError(f"Could not load the R2 manifest: {exc}") from exc
                else:
                    raise R2DataError(f"Could not load the R2 manifest: {exc}") from exc
            if not isinstance(manifest, dict):
                raise R2DataError("R2 manifest must be a JSON object.")
            temporary = self.manifest_cache_path.with_suffix(".tmp")
            temporary.write_text(
                json.dumps(manifest, indent=2, sort_keys=True),
                encoding="utf-8",
            )
            temporary.replace(self.manifest_cache_path)
            self._manifest = manifest
            self._manifest_fetched_at = time.monotonic()
            return manifest

    @staticmethod
    def _entry_key(entry):
        return entry.get("key", "") if isinstance(entry, dict) else str(entry or "")

    @staticmethod
    def _entry_identity(entry):
        if isinstance(entry, dict):
            return str(
                entry.get("sha256")
                or entry.get("etag")
                or entry.get("version")
                or entry.get("updated_at")
                or entry.get("key")
                or ""
            )
        return str(entry or "")

    def market_entries(self, market, *, manifest=None):
        market = _clean_market(market)
        manifest = manifest or self.fetch_manifest()
        market_data = (manifest.get("markets") or {}).get(market, {})
        yearly = market_data.get("yearly") or {}
        current = market_data.get("current") or {}
        return {
            "yearly": {str(key): value for key, value in yearly.items()},
            "current": {str(key): value for key, value in current.items()},
        }

    def entries_for_window(self, market, start=None, end=None, *, manifest=None):
        entries = self.market_entries(market, manifest=manifest)
        start_ts = pd.Timestamp(start).normalize() if start is not None else None
        end_ts = pd.Timestamp(end).normalize() if end is not None else pd.Timestamp.now().normalize()
        start_year = start_ts.year if start_ts is not None else min(
            [int(year) for year in entries["yearly"]] + [end_ts.year]
        )
        selected = []
        for year, entry in entries["yearly"].items():
            if start_year <= int(year) <= end_ts.year:
                selected.append(("parquet", year, entry))
        for month, entry in entries["current"].items():
            try:
                month_start = pd.Timestamp(f"{month}-01")
            except ValueError:
                continue
            month_end = month_start + pd.offsets.MonthEnd(1)
            if month_start <= end_ts and (start_ts is None or month_end >= start_ts):
                selected.append(("json", month, entry))
        return selected

    def _cache_path(self, key):
        prefix = self.settings.prefix.strip("/")
        relative = key
        if prefix and key.startswith(prefix + "/"):
            relative = key[len(prefix) + 1 :]
        path = self.cache_dir.joinpath(*Path(relative).parts)
        resolved = path.resolve()
        if self.cache_dir.resolve() not in (resolved, *resolved.parents):
            raise R2DataError(f"Unsafe R2 cache key: {key}")
        return path

    def ensure_entry(self, entry, *, force=False):
        key = self._entry_key(entry)
        if not key:
            raise R2DataError("Manifest file entry is missing its key.")
        path = self._cache_path(key)
        identity = self._entry_identity(entry)
        with self._lock:
            index = self._read_cache_index()
            current = index.get(key)
            if path.exists() and not force and current == identity:
                return path
            try:
                payload = self._object_bytes(key)
            except Exception as exc:
                raise R2DataError(f"Could not download {key} from R2: {exc}") from exc
            expected = entry.get("sha256") if isinstance(entry, dict) else None
            if expected and sha256_bytes(payload) != str(expected):
                raise R2DataError(f"Checksum validation failed for {key}.")
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_name(f".{path.name}.tmp")
            temporary.write_bytes(payload)
            temporary.replace(path)
            index[key] = identity
            self._write_cache_index(index)
            return path

    def _indexed_json_path(self, path):
        """Build a local Parquet index for efficient per-symbol JSON reads."""
        path = Path(path)
        indexed = path.with_suffix(path.suffix + ".parquet")
        try:
            current = indexed.exists() and indexed.stat().st_mtime_ns >= path.stat().st_mtime_ns
        except OSError:
            current = False
        if current:
            return indexed
        with self._lock:
            try:
                current = indexed.exists() and indexed.stat().st_mtime_ns >= path.stat().st_mtime_ns
            except OSError:
                current = False
            if current:
                return indexed
            try:
                frame = pd.read_json(path, orient="records")
            except (ValueError, OSError) as exc:
                raise R2DataError(f"Invalid monthly JSON {path.name}: {exc}") from exc
            frame = normalize_candles(frame)
            temporary = indexed.with_name(f".{indexed.name}.tmp")
            frame.to_parquet(
                temporary,
                index=False,
                engine="pyarrow",
                compression="zstd",
                row_group_size=10_000,
            )
            temporary.replace(indexed)
            return indexed

    def load_market(
        self,
        market,
        *,
        start=None,
        end=None,
        symbols=None,
        columns=None,
    ):
        market = _clean_market(market)
        symbol_values = {
            str(symbol).strip().upper() for symbol in (symbols or []) if symbol
        }
        requested = list(dict.fromkeys(columns or CANDLE_COLUMNS))
        for required in ("Symbol", "Date"):
            if required not in requested:
                requested.insert(0, required)
        frames = []
        for file_type, _period, entry in self.entries_for_window(
            market,
            start=start,
            end=end,
        ):
            path = self.ensure_entry(entry)
            if file_type == "parquet":
                try:
                    filters = (
                        [("Symbol", "in", sorted(symbol_values))]
                        if symbol_values
                        else None
                    )
                    frame = pd.read_parquet(
                        path,
                        columns=requested,
                        filters=filters,
                    )
                except (KeyError, ValueError):
                    frame = pd.read_parquet(path)
            else:
                indexed_path = self._indexed_json_path(path)
                try:
                    filters = (
                        [("Symbol", "in", sorted(symbol_values))]
                        if symbol_values
                        else None
                    )
                    frame = pd.read_parquet(
                        indexed_path,
                        columns=requested,
                        filters=filters,
                    )
                except (KeyError, ValueError):
                    frame = pd.read_parquet(indexed_path)
            if symbol_values and "Symbol" in frame.columns:
                frame = frame[
                    frame["Symbol"].astype(str).str.upper().isin(symbol_values)
                ]
            frames.append(frame)
        result = normalize_candles(
            pd.concat(frames, ignore_index=True)
            if frames
            else pd.DataFrame(columns=CANDLE_COLUMNS),
            market=market,
        )
        if start is not None:
            result = result[result["Date"] >= pd.Timestamp(start).normalize()]
        if end is not None:
            result = result[result["Date"] <= pd.Timestamp(end).normalize()]
        available = [column for column in requested if column in result.columns]
        return result[available].reset_index(drop=True)

    def load_symbol(self, market, symbol, *, start=None, end=None, columns=None):
        result = self.load_market(
            market,
            start=start,
            end=end,
            symbols=[symbol],
            columns=columns,
        )
        return result.drop(columns=["Symbol"], errors="ignore")

    def list_symbols(self, market):
        market = _clean_market(market)
        manifest = self.fetch_manifest()
        market_data = (manifest.get("markets") or {}).get(market, {})
        symbols = market_data.get("symbols") or []
        if symbols:
            return sorted({str(value).strip().upper() for value in symbols if value})
        entries = self.market_entries(market, manifest=manifest)
        candidates = (
            [entries["current"][key] for key in sorted(entries["current"])[-1:]]
            or [entries["yearly"][key] for key in sorted(entries["yearly"])[-1:]]
        )
        if not candidates:
            return []
        path = self.ensure_entry(candidates[-1])
        if path.suffix.lower() == ".parquet":
            frame = pd.read_parquet(path, columns=["Symbol"])
        else:
            frame = pd.read_json(path, orient="records")
        return sorted(frame["Symbol"].astype(str).str.strip().str.upper().unique())

    def market_status(self, market):
        market = _clean_market(market)
        manifest = self.fetch_manifest()
        data = (manifest.get("markets") or {}).get(market, {})
        latest = data.get("latest_date")
        symbols = data.get("symbol_count")
        if latest and symbols is not None:
            return {
                "Latest Date": pd.Timestamp(latest).normalize(),
                "Stocks On Latest Date": int(symbols),
                "Current Stock Files": int(symbols),
                "Stock Files": int(symbols),
            }
        frame = self.load_market(
            market,
            start=pd.Timestamp.now().normalize() - pd.Timedelta(days=14),
            columns=["Symbol", "Date", "Close"],
        )
        if frame.empty:
            return {
                "Latest Date": None,
                "Stocks On Latest Date": 0,
                "Current Stock Files": 0,
                "Stock Files": 0,
            }
        latest_date = frame["Date"].max()
        latest_symbols = int(frame.loc[frame["Date"].eq(latest_date), "Symbol"].nunique())
        return {
            "Latest Date": latest_date,
            "Stocks On Latest Date": latest_symbols,
            "Current Stock Files": latest_symbols,
            "Stock Files": int(frame["Symbol"].nunique()),
        }

    def sync(self, market=None, *, force=True):
        manifest = self.fetch_manifest(force=force)
        markets = [_clean_market(market)] if market else list(MARKETS)
        downloaded = []
        for market_name in markets:
            entries = self.market_entries(market_name, manifest=manifest)
            current_entries = list(entries["current"].values())
            if current_entries:
                downloaded.append(
                    self.ensure_entry(current_entries[-1], force=force)
                )
        return {
            "version": manifest.get("version", ""),
            "files": downloaded,
        }


_STORE = None
_STORE_LOCK = threading.RLock()


def configure_r2(values=None, *, client=None, force=False):
    global _STORE
    settings = R2Settings.from_mapping(values)
    with _STORE_LOCK:
        if force or _STORE is None:
            _STORE = R2StockDataStore(settings, client=client)
    return _STORE


def get_r2_store():
    return configure_r2()


def r2_configured():
    return get_r2_store().settings.configured or get_r2_store()._client is not None


def manifest_entry(key, payload, *, rows=None):
    entry = {
        "key": key,
        "sha256": sha256_bytes(payload),
        "bytes": len(payload),
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    if rows is not None:
        entry["rows"] = int(rows)
    return entry
