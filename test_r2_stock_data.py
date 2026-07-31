import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from config import STOCK_CACHE_DIR
from r2_stock_data import (
    R2DataError,
    R2Settings,
    R2StockDataStore,
    configure_r2,
)


class _Body:
    def __init__(self, value):
        self.value = value

    def read(self):
        return self.value


class FakeR2:
    def __init__(self, objects):
        self.objects = dict(objects)
        self.get_calls = []

    def get_object(self, Bucket, Key):
        self.get_calls.append((Bucket, Key))
        return {"Body": _Body(self.objects[Key])}


class R2StockDataTests(unittest.TestCase):
    def _store(self, cache_dir):
        yearly = pd.DataFrame(
            [
                {
                    "Symbol": "TEST",
                    "Date": "2025-12-31",
                    "Open": 9,
                    "High": 11,
                    "Low": 8,
                    "Close": 10,
                    "Volume": 100,
                }
            ]
        )
        yearly_buffer = io.BytesIO()
        yearly.to_parquet(yearly_buffer, index=False)
        monthly = json.dumps(
            [
                {
                    "Symbol": "TEST",
                    "Date": "2026-01-02",
                    "Open": 10,
                    "High": 12,
                    "Low": 9,
                    "Close": 11,
                    "Volume": 110,
                },
                {
                    "Symbol": "TEST",
                    "Date": "2026-01-02",
                    "Open": 10,
                    "High": 13,
                    "Low": 9,
                    "Close": 12,
                    "Volume": 120,
                },
            ]
        ).encode()
        objects = {
            "stock-data/india/yearly/2025.parquet": yearly_buffer.getvalue(),
            "stock-data/india/current/2026-01.json": monthly,
        }

        def entry(key):
            payload = objects[key]
            return {
                "key": key,
                "sha256": hashlib.sha256(payload).hexdigest(),
            }

        manifest = {
            "version": "test-v1",
            "markets": {
                "india": {
                    "symbols": ["TEST"],
                    "yearly": {
                        "2025": entry(
                            "stock-data/india/yearly/2025.parquet"
                        )
                    },
                    "current": {
                        "2026-01": entry(
                            "stock-data/india/current/2026-01.json"
                        )
                    },
                }
            },
        }
        objects["stock-data/manifest.json"] = json.dumps(manifest).encode()
        client = FakeR2(objects)
        settings = R2Settings(
            bucket="test",
            endpoint_url="https://example.invalid",
            access_key_id="key",
            secret_access_key="secret",
            cache_dir=Path(cache_dir),
        )
        return R2StockDataStore(settings, client=client), client

    def test_default_cache_is_inside_application_data_directory(self):
        with patch.dict(
            "os.environ",
            {
                "R2_CACHE_DIR": "",
                "R2_MANIFEST_REFRESH_SECONDS": "",
            },
            clear=False,
        ):
            settings = R2Settings.from_mapping()

        self.assertEqual(settings.cache_dir, STOCK_CACHE_DIR / "r2")
        self.assertEqual(settings.manifest_refresh_seconds, 60.0)

    def test_manifest_refresh_detects_remote_version_changes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store, client = self._store(temp_dir)
            store.settings = R2Settings(
                bucket=store.settings.bucket,
                endpoint_url=store.settings.endpoint_url,
                access_key_id=store.settings.access_key_id,
                secret_access_key=store.settings.secret_access_key,
                cache_dir=store.settings.cache_dir,
                manifest_refresh_seconds=0,
            )
            first = store.fetch_manifest()
            changed = dict(first)
            changed["version"] = "test-v2"
            client.objects["stock-data/manifest.json"] = json.dumps(changed).encode()
            second = store.fetch_manifest()

        self.assertEqual(first["version"], "test-v1")
        self.assertEqual(second["version"], "test-v2")
        manifest_calls = [
            key for _, key in client.get_calls
            if key == "stock-data/manifest.json"
        ]
        self.assertEqual(len(manifest_calls), 2)

    def test_explicit_valid_settings_replace_an_unconfigured_store(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            empty_store = configure_r2(
                {
                    "cache_dir": temp_dir,
                    "manifest_refresh_seconds": "60",
                },
                force=True,
            )
            configured_values = {
                "account_id": "account",
                "access_key_id": "key",
                "secret_access_key": "secret",
                "bucket": "bucket",
                "cache_dir": temp_dir,
                "manifest_refresh_seconds": "60",
            }
            configured_store = configure_r2(configured_values)
            reused_store = configure_r2(configured_values)

        self.assertFalse(empty_store.settings.configured)
        self.assertTrue(configured_store.settings.configured)
        self.assertIsNot(configured_store, empty_store)
        self.assertIs(reused_store, configured_store)

    def test_loads_only_required_periods_and_deduplicates(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store, client = self._store(temp_dir)
            current = store.load_symbol(
                "india",
                "TEST",
                start="2026-01-01",
            )

        self.assertEqual(len(current), 1)
        self.assertEqual(current.iloc[0]["Close"], 12)
        downloaded_keys = [key for _, key in client.get_calls]
        self.assertNotIn(
            "stock-data/india/yearly/2025.parquet",
            downloaded_keys,
        )

    def test_merges_yearly_parquet_with_current_json(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store, _client = self._store(temp_dir)
            result = store.load_symbol(
                "india",
                "TEST",
                start="2025-01-01",
                columns=["Date", "Close"],
            )

        self.assertEqual(
            result["Date"].dt.strftime("%Y-%m-%d").tolist(),
            ["2025-12-31", "2026-01-02"],
        )
        self.assertEqual(list(result.columns), ["Date", "Close"])

    def test_materializes_market_and_symbol_caches_once(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store, client = self._store(temp_dir)
            store.sync_local_cache("india")

            first = store.load_cached_symbol(
                "india",
                "TEST",
                start="2025-01-01",
                columns=["Date", "Close"],
            )
            partition_calls_after_build = [
                key for _, key in client.get_calls
                if key != "stock-data/manifest.json"
            ]
            second = store.load_cached_symbol(
                "india",
                "TEST",
                start="2026-01-01",
                columns=["Date", "Close"],
            )
            partition_calls_after_second_read = [
                key for _, key in client.get_calls
                if key != "stock-data/manifest.json"
            ]
            status = store.local_cache_status("india")
            symbols_dir = (
                Path(temp_dir)
                / "materialized"
                / "india"
                / "generations"
                / status["generation"]
                / "symbols"
            )
            symbol_files = sorted(symbols_dir.glob("*.parquet"))
            symbol_directories = [
                path for path in symbols_dir.iterdir() if path.is_dir()
            ]
            bucket_directory_exists = (
                symbols_dir.parent / ".symbol-buckets"
            ).exists()

        self.assertEqual(
            first["Date"].dt.strftime("%Y-%m-%d").tolist(),
            ["2025-12-31", "2026-01-02"],
        )
        self.assertEqual(second["Close"].tolist(), [12])
        self.assertEqual(
            partition_calls_after_second_read,
            partition_calls_after_build,
        )
        self.assertTrue(status["ready"])
        self.assertEqual(status["schema_version"], 3)
        self.assertEqual(status["history_years"], 10)
        self.assertEqual(status["symbols"], 1)
        self.assertEqual(status["progress"]["phase"], "complete")
        self.assertEqual(status["progress"]["percent"], 1.0)
        self.assertEqual([path.name for path in symbol_files], ["TEST.parquet"])
        self.assertEqual(symbol_directories, [])
        self.assertFalse(bucket_directory_exists)

    def test_active_cache_read_does_not_wait_for_manifest(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store, _client = self._store(temp_dir)
            expected = store.sync_local_cache("india")
            with (
                patch.object(
                    store,
                    "fetch_manifest",
                    side_effect=AssertionError(
                        "active cache reads must not fetch R2 synchronously"
                    ),
                ),
                patch.object(
                    store,
                    "_start_background_cache_sync",
                    side_effect=AssertionError(
                        "local reads must not trigger background sync"
                    ),
                ),
            ):
                actual = store.ensure_local_cache("india")

        self.assertEqual(actual["generation"], expected["generation"])

    def test_missing_local_cache_does_not_fall_back_to_r2(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store, _client = self._store(temp_dir)
            with patch.object(
                store,
                "fetch_manifest",
                side_effect=AssertionError(
                    "local reads must not fetch R2"
                ),
            ):
                with self.assertRaisesRegex(R2DataError, "server cache is not ready"):
                    store.load_cached_symbol("india", "TEST")

    def test_startup_sync_is_requested_only_once_per_process(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store, _client = self._store(temp_dir)
            with patch.object(
                store,
                "_start_background_cache_sync",
                return_value=True,
            ) as background_sync:
                first = store.request_startup_cache_sync_once("india")
                second = store.request_startup_cache_sync_once("india")

        self.assertTrue(first)
        self.assertFalse(second)
        background_sync.assert_called_once_with("india")

    def test_cached_market_supports_local_symbol_filtering(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store, _client = self._store(temp_dir)
            store.sync_local_cache("india")

            result = store.load_cached_market(
                "india",
                start="2026-01-01",
                symbols=["TEST"],
                columns=["Symbol", "Date", "Close"],
            )

        self.assertEqual(result["Symbol"].tolist(), ["TEST"])
        self.assertEqual(result["Close"].tolist(), [12])

    def test_changed_manifest_builds_and_activates_a_new_generation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store, client = self._store(temp_dir)
            original = store.sync_local_cache("india")
            calls_before_update = len(client.get_calls)

            updated_month = json.dumps([
                {
                    "Symbol": "TEST",
                    "Date": "2026-01-02",
                    "Open": 10,
                    "High": 14,
                    "Low": 9,
                    "Close": 13,
                    "Volume": 130,
                }
            ]).encode()
            month_key = "stock-data/india/current/2026-01.json"
            client.objects[month_key] = updated_month
            manifest = json.loads(
                client.objects["stock-data/manifest.json"].decode()
            )
            manifest["version"] = "test-v2"
            manifest["markets"]["india"]["current"]["2026-01"] = {
                "key": month_key,
                "sha256": hashlib.sha256(updated_month).hexdigest(),
            }
            client.objects["stock-data/manifest.json"] = json.dumps(
                manifest
            ).encode()

            updated = store.sync_local_cache(
                "india",
                force_manifest=True,
            )
            result = store.load_cached_symbol("india", "TEST")
            update_calls = [
                key for _, key in client.get_calls[calls_before_update:]
            ]
            generations = list(
                (
                    Path(temp_dir)
                    / "materialized"
                    / "india"
                    / "generations"
                ).iterdir()
            )

        self.assertNotEqual(original["generation"], updated["generation"])
        self.assertEqual(result.iloc[-1]["Close"], 13)
        self.assertIn(month_key, update_calls)
        self.assertNotIn("stock-data/india/yearly/2025.parquet", update_calls)
        self.assertLessEqual(len(generations), 2)

    def test_materialization_normalizes_mixed_timestamp_units(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store, client = self._store(temp_dir)
            microsecond_frame = pd.DataFrame({
                "Symbol": ["MICRO"],
                "Date": pd.Series(["2024-12-31"], dtype="datetime64[us]"),
                "Open": [19.0],
                "High": [21.0],
                "Low": [18.0],
                "Close": [20.0],
                "Volume": [200.0],
            })
            microsecond_buffer = io.BytesIO()
            microsecond_frame.to_parquet(microsecond_buffer, index=False)
            microsecond_payload = microsecond_buffer.getvalue()
            microsecond_key = "stock-data/india/yearly/2024.parquet"
            client.objects[microsecond_key] = microsecond_payload
            manifest = json.loads(
                client.objects["stock-data/manifest.json"].decode()
            )
            manifest["markets"]["india"]["symbols"] = ["MICRO", "TEST"]
            manifest["markets"]["india"]["yearly"]["2024"] = {
                "key": microsecond_key,
                "sha256": hashlib.sha256(microsecond_payload).hexdigest(),
                "bytes": len(microsecond_payload),
            }
            client.objects["stock-data/manifest.json"] = json.dumps(
                manifest
            ).encode()

            state = store.sync_local_cache(
                "india",
                force_manifest=True,
            )
            result = store.load_cached_market(
                "india",
                columns=["Symbol", "Date", "Close"],
            )

        self.assertEqual(state["symbols"], 2)
        self.assertEqual(set(result["Symbol"]), {"MICRO", "TEST"})
        self.assertEqual(str(result["Date"].dtype), "datetime64[ns]")


if __name__ == "__main__":
    unittest.main()
