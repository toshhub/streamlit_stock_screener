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
import shutil
import threading
import time
import uuid
import zlib
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
LOCAL_CACHE_HISTORY_YEARS = 10
# Schema 3 already stores one Parquet file per symbol.  The faster bucketed
# builder only changes how those files are produced, not their on-disk format,
# so bumping this number would needlessly rebuild every deployed cache.
LOCAL_CACHE_SCHEMA_VERSION = 3


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
        self._materialize_locks = {
            market: threading.RLock() for market in MARKETS
        }
        self._materialize_threads = {}
        self._materialize_errors = {}
        self._materialize_progress = {}
        self._startup_sync_markets = set()

    def _set_materialize_progress(self, market, **values):
        market = _clean_market(market)
        current = dict(self._materialize_progress.get(market) or {})
        current.update(values)
        current["updated_at"] = datetime.now(timezone.utc).isoformat(
            timespec="seconds"
        )
        self._materialize_progress[market] = current
        return current

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

    @property
    def materialized_cache_dir(self):
        return self.cache_dir / "materialized"

    def _materialized_market_dir(self, market):
        return self.materialized_cache_dir / _clean_market(market)

    def _active_cache_state_path(self, market):
        return self._materialized_market_dir(market) / "active.json"

    def _read_active_cache_state(self, market):
        try:
            state = json.loads(
                self._active_cache_state_path(market).read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(state, dict):
            return None
        generation = str(state.get("generation", "")).strip()
        market_file = (
            self._materialized_market_dir(market)
            / "generations"
            / generation
            / "market.parquet"
        )
        if not generation or not market_file.is_file():
            return None
        return state

    def _generation_dir(self, market, generation):
        return (
            self._materialized_market_dir(market)
            / "generations"
            / str(generation)
        )

    def _market_cache_revision(self, market, manifest):
        market = _clean_market(market)
        entries = self.market_entries(market, manifest=manifest)
        identities = {
            f"yearly/{period}": self._entry_identity(entry)
            for period, entry in entries["yearly"].items()
        }
        identities.update({
            f"current/{period}": self._entry_identity(entry)
            for period, entry in entries["current"].items()
        })
        payload = {
            "schema": LOCAL_CACHE_SCHEMA_VERSION,
            "market": market,
            "history_years": LOCAL_CACHE_HISTORY_YEARS,
            "entries": identities,
        }
        revision = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            )
        ).hexdigest()
        return revision, identities

    @staticmethod
    def _cache_cutoff(manifest, market):
        market_data = (manifest.get("markets") or {}).get(market, {})
        latest = pd.Timestamp(
            market_data.get("latest_date") or pd.Timestamp.now()
        ).normalize()
        return latest - pd.DateOffset(years=LOCAL_CACHE_HISTORY_YEARS)

    def _write_active_cache_state(self, market, state):
        path = self._active_cache_state_path(market)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        temporary.write_text(
            json.dumps(state, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        temporary.replace(path)

    @staticmethod
    def _write_parquet_atomic(frame, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        frame.to_parquet(
            temporary,
            index=False,
            engine="pyarrow",
            compression="zstd",
            row_group_size=10_000,
        )
        temporary.replace(path)

    def _materialize_generation(self, market, manifest, revision, identities):
        market = _clean_market(market)
        generation = revision[:20]
        market_dir = self._materialized_market_dir(market)
        generations_dir = market_dir / "generations"
        final_dir = self._generation_dir(market, generation)
        if (
            (final_dir / "market.parquet").is_file()
            and (final_dir / "metadata.json").is_file()
            and (final_dir / "symbols").is_dir()
        ):
            return generation
        if final_dir.exists():
            resolved_generations = generations_dir.resolve()
            if final_dir.resolve().parent != resolved_generations:
                raise R2DataError(
                    f"Unsafe incomplete cache generation: {final_dir}"
                )
            shutil.rmtree(final_dir, ignore_errors=False)

        cutoff = self._cache_cutoff(manifest, market)
        selected_entries = self.entries_for_window(
            market,
            start=cutoff,
            manifest=manifest,
        )
        partition_paths = []
        total_download_bytes = sum(
            max(0, int(entry.get("bytes", 0) or 0))
            for _file_type, _period, entry in selected_entries
            if isinstance(entry, dict)
        )
        completed_download_bytes = 0
        download_started = time.monotonic()
        self._set_materialize_progress(
            market,
            phase="download",
            percent=0.0,
            completed=0,
            total=total_download_bytes,
            unit="bytes",
            eta_seconds=None,
            message="Checking and downloading R2 partitions",
        )
        for file_type, _period, entry in selected_entries:
            partition_path = self.ensure_entry(entry)
            if file_type == "json":
                partition_path = self._indexed_json_path(partition_path)
            partition_paths.append(partition_path)
            entry_bytes = (
                max(0, int(entry.get("bytes", 0) or 0))
                if isinstance(entry, dict)
                else 0
            )
            completed_download_bytes += entry_bytes
            elapsed = max(0.001, time.monotonic() - download_started)
            fraction = (
                completed_download_bytes / total_download_bytes
                if total_download_bytes
                else len(partition_paths) / max(1, len(selected_entries))
            )
            rate = completed_download_bytes / elapsed
            eta_seconds = (
                (total_download_bytes - completed_download_bytes) / rate
                if rate > 0 and completed_download_bytes < total_download_bytes
                else 0
            )
            self._set_materialize_progress(
                market,
                phase="download",
                percent=min(0.2, max(0.0, fraction * 0.2)),
                completed=completed_download_bytes,
                total=total_download_bytes,
                unit="bytes",
                eta_seconds=eta_seconds,
                message=(
                    "Checking and downloading R2 partitions: "
                    f"{len(partition_paths)}/{len(selected_entries)}"
                ),
            )
        market_data = (manifest.get("markets") or {}).get(market, {})
        symbols = sorted({
            str(symbol).strip().upper()
            for symbol in (market_data.get("symbols") or [])
            if str(symbol).strip()
        })
        if not symbols:
            raise R2DataError(
                f"The R2 manifest has no symbols for the {market} cache."
            )
        generations_dir.mkdir(parents=True, exist_ok=True)
        # The generation is invisible to readers until active.json is replaced.
        # Build directly at its deterministic final path instead of renaming a
        # populated directory; Windows can retain short-lived Parquet handles
        # and reject that directory rename with WinError 5.
        staging = final_dir
        symbols_dir = staging / "symbols"
        symbols_dir.mkdir(parents=True, exist_ok=True)
        market_temporary = staging / f".market.{uuid.uuid4().hex}.tmp"
        parquet_writer = None
        bucket_writers = {}
        row_count = 0
        symbol_count = 0
        latest_date = None
        try:
            import pyarrow as pa
            import pyarrow.compute as pc
            import pyarrow.dataset as ds
            import pyarrow.parquet as pq

            market_schema = pa.schema([
                pa.field("Symbol", pa.large_string()),
                pa.field("Date", pa.timestamp("ns")),
                *[
                    pa.field(column, pa.float64())
                    for column in CANDLE_COLUMNS[2:]
                ],
            ])
            expected_rows = sum(
                max(0, int(entry.get("rows", 0) or 0))
                for _file_type, _period, entry in selected_entries
                if isinstance(entry, dict)
            )
            market_started = time.monotonic()
            self._set_materialize_progress(
                market,
                phase="market",
                percent=0.2,
                completed=0,
                total=expected_rows,
                unit="rows",
                eta_seconds=None,
                message="Building local market cache",
            )
            source_dataset = ds.dataset(
                [str(path) for path in partition_paths],
                format="parquet",
                schema=market_schema,
            )
            cutoff_scalar = pa.scalar(
                cutoff.to_pydatetime(),
                type=pa.timestamp("ns"),
            )
            scanner = source_dataset.scanner(
                columns=list(CANDLE_COLUMNS),
                filter=ds.field("Date") >= cutoff_scalar,
                batch_size=65_536,
                use_threads=True,
            )
            parquet_writer = pq.ParquetWriter(
                market_temporary,
                market_schema,
                compression="zstd",
            )
            for batch in scanner.to_batches():
                if batch.num_rows == 0:
                    continue
                parquet_writer.write_batch(batch, row_group_size=10_000)
                row_count += batch.num_rows
                batch_latest = pc.max(
                    batch.column(batch.schema.get_field_index("Date"))
                ).as_py()
                if batch_latest is not None:
                    batch_latest = pd.Timestamp(batch_latest)
                    if latest_date is None or batch_latest > latest_date:
                        latest_date = batch_latest
                elapsed = max(0.001, time.monotonic() - market_started)
                fraction = (
                    min(1.0, row_count / expected_rows)
                    if expected_rows
                    else 0.0
                )
                rate = row_count / elapsed
                eta_seconds = (
                    (expected_rows - row_count) / rate
                    if rate > 0 and expected_rows > row_count
                    else None
                )
                self._set_materialize_progress(
                    market,
                    phase="market",
                    percent=0.2 + 0.3 * fraction,
                    completed=row_count,
                    total=expected_rows,
                    unit="rows",
                    eta_seconds=eta_seconds,
                    message="Building local market cache",
                )
            if parquet_writer is None or row_count == 0:
                raise R2DataError(
                    f"Cannot build an empty local {market} market cache."
                )
            parquet_writer.close()
            parquet_writer = None
            market_temporary.replace(staging / "market.parquet")

            bucket_count = min(64, max(1, len(symbols)))
            symbol_buckets = {
                symbol: zlib.crc32(symbol.encode("utf-8")) % bucket_count
                for symbol in symbols
            }
            bucket_dir = staging / ".symbol-buckets"
            bucket_dir.mkdir(parents=True, exist_ok=True)
            partition_started = time.monotonic()
            partitioned_rows = 0
            self._set_materialize_progress(
                market,
                phase="partition",
                percent=0.5,
                completed=0,
                total=row_count,
                unit="rows",
                eta_seconds=None,
                message="Partitioning local symbol data",
            )
            market_dataset = ds.dataset(
                str(staging / "market.parquet"),
                format="parquet",
                schema=market_schema,
            )
            for batch in market_dataset.scanner(
                batch_size=65_536,
                use_threads=True,
            ).to_batches():
                batch_frame = batch.to_pandas()
                batch_frame["_bucket"] = batch_frame["Symbol"].map(
                    symbol_buckets
                )
                unknown = batch_frame["_bucket"].isna()
                if unknown.any():
                    batch_frame.loc[unknown, "_bucket"] = batch_frame.loc[
                        unknown,
                        "Symbol",
                    ].map(
                        lambda value: zlib.crc32(
                            str(value).encode("utf-8")
                        ) % bucket_count
                    )
                for bucket, bucket_frame in batch_frame.groupby(
                    "_bucket",
                    sort=False,
                ):
                    bucket = int(bucket)
                    table = pa.Table.from_pandas(
                        bucket_frame.drop(columns=["_bucket"]),
                        schema=market_schema,
                        preserve_index=False,
                        safe=False,
                    )
                    writer = bucket_writers.get(bucket)
                    if writer is None:
                        writer = pq.ParquetWriter(
                            bucket_dir / f"bucket-{bucket:02d}.parquet",
                            market_schema,
                            compression="zstd",
                        )
                        bucket_writers[bucket] = writer
                    writer.write_table(table, row_group_size=10_000)
                partitioned_rows += batch.num_rows
                elapsed = max(0.001, time.monotonic() - partition_started)
                rate = partitioned_rows / elapsed
                eta_seconds = (
                    (row_count - partitioned_rows) / rate
                    if rate > 0 and partitioned_rows < row_count
                    else 0
                )
                self._set_materialize_progress(
                    market,
                    phase="partition",
                    percent=0.5 + 0.2 * partitioned_rows / row_count,
                    completed=partitioned_rows,
                    total=row_count,
                    unit="rows",
                    eta_seconds=eta_seconds,
                    message="Partitioning local symbol data",
                )
            for writer in bucket_writers.values():
                writer.close()
            bucket_writers.clear()

            bucket_files = sorted(bucket_dir.glob("bucket-*.parquet"))
            if not bucket_files:
                raise R2DataError(
                    f"No local {market} symbol buckets were produced."
                )
            compact_started = time.monotonic()
            compact_index = 0
            self._set_materialize_progress(
                market,
                phase="compact",
                percent=0.7,
                completed=0,
                total=len(symbols),
                unit="symbols",
                eta_seconds=None,
                message=(
                    "Compacting symbol files: "
                    f"0/{len(symbols)}"
                ),
            )
            for bucket_file in bucket_files:
                bucket_frame = normalize_candles(
                    pd.read_parquet(bucket_file),
                    market=market,
                )
                for symbol, symbol_frame in bucket_frame.groupby(
                    "Symbol",
                    sort=True,
                ):
                    compact_index += 1
                    self._write_parquet_atomic(
                        symbol_frame.drop(columns=["Symbol"]).reset_index(
                            drop=True
                        ),
                        symbols_dir / f"{symbol}.parquet",
                    )
                    elapsed = max(0.001, time.monotonic() - compact_started)
                    rate = compact_index / elapsed
                    eta_seconds = (
                        (len(symbols) - compact_index) / rate
                        if compact_index >= 10 and rate > 0
                        else None
                    )
                    completed = min(compact_index, len(symbols))
                    self._set_materialize_progress(
                        market,
                        phase="compact",
                        percent=0.7 + 0.3 * completed / len(symbols),
                        completed=completed,
                        total=len(symbols),
                        unit="symbols",
                        eta_seconds=eta_seconds,
                        message=(
                            "Compacting symbol files: "
                            f"{completed}/{len(symbols)}"
                        ),
                    )
                bucket_file.unlink()
            bucket_dir.rmdir()
            symbol_count = sum(
                1 for path in symbols_dir.glob("*.parquet") if path.is_file()
            )
            cache_metadata = {
                "schema_version": LOCAL_CACHE_SCHEMA_VERSION,
                "market": market,
                "revision": revision,
                "manifest_version": str(manifest.get("version", "")),
                "history_years": LOCAL_CACHE_HISTORY_YEARS,
                "cutoff": cutoff.strftime("%Y-%m-%d"),
                "latest_date": latest_date.strftime("%Y-%m-%d"),
                "rows": int(row_count),
                "symbols": int(symbol_count),
                "entries": identities,
                "built_at": datetime.now(timezone.utc).isoformat(
                    timespec="seconds"
                ),
            }
            (staging / "metadata.json").write_text(
                json.dumps(cache_metadata, indent=2, sort_keys=True),
                encoding="utf-8",
            )
        except Exception:
            if parquet_writer is not None:
                parquet_writer.close()
            for writer in bucket_writers.values():
                writer.close()
            if staging.is_dir():
                shutil.rmtree(staging, ignore_errors=True)
            raise
        return generation

    def _cleanup_old_generations(self, market, active_generation, keep=2):
        generations_dir = self._materialized_market_dir(market) / "generations"
        if not generations_dir.is_dir():
            return
        candidates = sorted(
            (
                path for path in generations_dir.iterdir()
                if path.is_dir() and not path.name.startswith(".")
            ),
            key=lambda path: path.stat().st_mtime_ns,
            reverse=True,
        )
        retained = {active_generation}
        retained.update(path.name for path in candidates[: max(1, int(keep))])
        resolved_parent = generations_dir.resolve()
        for path in candidates:
            if path.name in retained:
                continue
            resolved = path.resolve()
            if resolved.parent == resolved_parent:
                shutil.rmtree(resolved, ignore_errors=True)

    def sync_local_cache(self, market, *, force_manifest=False):
        """Synchronize one ten-year local cache and atomically activate it."""
        market = _clean_market(market)
        with self._materialize_locks[market]:
            manifest = self.fetch_manifest(force=force_manifest)
            revision, identities = self._market_cache_revision(market, manifest)
            active = self._read_active_cache_state(market)
            if active and active.get("revision") == revision:
                return dict(active)
            try:
                generation = self._materialize_generation(
                    market,
                    manifest,
                    revision,
                    identities,
                )
            except R2DataError:
                raise
            except Exception as exc:
                raise R2DataError(
                    f"Could not build the local {market} cache: {exc}"
                ) from exc
            metadata_path = self._generation_dir(
                market,
                generation,
            ) / "metadata.json"
            state = json.loads(metadata_path.read_text(encoding="utf-8"))
            state["generation"] = generation
            self._write_active_cache_state(market, state)
            self._materialize_errors.pop(market, None)
            self._cleanup_old_generations(market, generation)
            self._set_materialize_progress(
                market,
                phase="complete",
                percent=1.0,
                completed=int(state.get("symbols", 0)),
                total=int(state.get("symbols", 0)),
                unit="symbols",
                eta_seconds=0,
                message="Server cache refresh complete",
            )
            return state

    def _start_background_cache_sync(self, market, *, force_manifest=False):
        market = _clean_market(market)
        with self._lock:
            current = self._materialize_threads.get(market)
            if current is not None and current.is_alive():
                return False

            def refresh():
                try:
                    self.sync_local_cache(
                        market,
                        force_manifest=force_manifest,
                    )
                except Exception as exc:
                    self._materialize_errors[market] = str(exc)
                    self._set_materialize_progress(
                        market,
                        phase="failed",
                        eta_seconds=None,
                        message=f"Server cache refresh failed: {exc}",
                    )

            thread = threading.Thread(
                target=refresh,
                name=f"r2-local-cache-{market}",
                daemon=True,
            )
            self._materialize_threads[market] = thread
            thread.start()
            return True

    def ensure_local_cache(self, market):
        """Return the active local cache without touching R2."""
        market = _clean_market(market)
        active = self._read_active_cache_state(market)
        if active is None:
            raise R2DataError(
                f"The local {market} server cache is not ready. "
                "Refresh it from the Server Data Cache panel first."
            )
        return active

    def request_local_cache_sync(self, market, *, force_manifest=False):
        """Start a non-blocking cache warm-up when missing or out of date."""
        market = _clean_market(market)
        # Manifest retrieval and first-time materialization must not block the
        # Streamlit request that opened the app.
        return self._start_background_cache_sync(
            market,
            force_manifest=force_manifest,
        )

    def request_startup_cache_sync_once(self, market):
        """Check R2 once per server process, never from a data read path."""
        market = _clean_market(market)
        with self._lock:
            if market in self._startup_sync_markets:
                return False
            self._startup_sync_markets.add(market)
        return self._start_background_cache_sync(market)

    def cached_symbol_exists(self, market, symbol):
        """Check the active generation without consulting R2."""
        market = _clean_market(market)
        clean_symbol = str(symbol).strip().upper()
        active = self._read_active_cache_state(market)
        if not active or not clean_symbol:
            return False
        base = (
            self._generation_dir(market, active["generation"])
            / "symbols"
            / clean_symbol
        )
        return base.is_dir() or base.with_suffix(".parquet").is_file()

    def list_cached_symbols(self, market):
        """List symbols in the active generation without consulting R2."""
        market = _clean_market(market)
        active = self._read_active_cache_state(market)
        if not active:
            return []
        symbols_dir = (
            self._generation_dir(market, active["generation"])
            / "symbols"
        )
        if not symbols_dir.is_dir():
            return []
        symbols = {
            path.stem if path.is_file() else path.name
            for path in symbols_dir.iterdir()
            if path.is_dir() or path.suffix.lower() == ".parquet"
        }
        return sorted(symbol for symbol in symbols if symbol)

    def local_cache_status(self, market):
        market = _clean_market(market)
        active = self._read_active_cache_state(market) or {}
        thread = self._materialize_threads.get(market)
        return {
            **active,
            "market": market,
            "ready": bool(active),
            "syncing": bool(thread is not None and thread.is_alive()),
            "error": self._materialize_errors.get(market, ""),
            "progress": dict(self._materialize_progress.get(market) or {}),
        }

    def load_cached_market(
        self,
        market,
        *,
        start=None,
        end=None,
        symbols=None,
        columns=None,
    ):
        market = _clean_market(market)
        state = self.ensure_local_cache(market)
        path = self._generation_dir(
            market,
            state["generation"],
        ) / "market.parquet"
        symbol_values = sorted({
            str(symbol).strip().upper() for symbol in (symbols or []) if symbol
        })
        requested = list(dict.fromkeys(columns or CANDLE_COLUMNS))
        for required in ("Symbol", "Date"):
            if required not in requested:
                requested.insert(0, required)
        filters = []
        if symbol_values:
            filters.append(("Symbol", "in", symbol_values))
        if start is not None:
            filters.append(("Date", ">=", pd.Timestamp(start).to_pydatetime()))
        if end is not None:
            filters.append(("Date", "<=", pd.Timestamp(end).to_pydatetime()))
        try:
            frame = pd.read_parquet(
                path,
                columns=requested,
                filters=filters or None,
            )
        except (KeyError, TypeError, ValueError):
            frame = pd.read_parquet(path)
        frame = normalize_candles(frame, market=market)
        if symbol_values:
            frame = frame[frame["Symbol"].isin(symbol_values)]
        if start is not None:
            frame = frame[frame["Date"].ge(pd.Timestamp(start).normalize())]
        if end is not None:
            frame = frame[frame["Date"].le(pd.Timestamp(end).normalize())]
        available = [column for column in requested if column in frame.columns]
        return frame[available].reset_index(drop=True)

    def load_cached_symbol(
        self,
        market,
        symbol,
        *,
        start=None,
        end=None,
        columns=None,
    ):
        market = _clean_market(market)
        clean_symbol = str(symbol).strip().upper()
        state = self.ensure_local_cache(market)
        path = (
            self._generation_dir(market, state["generation"])
            / "symbols"
            / clean_symbol
        )
        legacy_path = path.with_suffix(".parquet")
        if not path.is_dir() and legacy_path.is_file():
            path = legacy_path
        price_columns = CANDLE_COLUMNS[1:]
        if not path.exists():
            return pd.DataFrame(columns=list(columns or price_columns))
        requested = list(dict.fromkeys(columns or price_columns))
        for required in ("Date", "Close"):
            if required not in requested:
                requested.insert(0, required)
        try:
            frame = pd.read_parquet(path, columns=requested)
        except (KeyError, ValueError):
            frame = pd.read_parquet(path)
        frame = normalize_candles(
            frame.assign(Symbol=clean_symbol),
            market=market,
        ).drop(columns=["Symbol"], errors="ignore")
        if start is not None:
            frame = frame[frame["Date"].ge(pd.Timestamp(start).normalize())]
        if end is not None:
            frame = frame[frame["Date"].le(pd.Timestamp(end).normalize())]
        available = [column for column in requested if column in frame.columns]
        return frame[available].reset_index(drop=True)

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
        explicit_settings_changed = (
            values is not None
            and _STORE is not None
            and _STORE.settings != settings
        )
        explicit_client_changed = (
            client is not None
            and _STORE is not None
            and _STORE._client is not client
        )
        if (
            force
            or _STORE is None
            or explicit_settings_changed
            or explicit_client_changed
        ):
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
