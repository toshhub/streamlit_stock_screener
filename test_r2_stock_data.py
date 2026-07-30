import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from config import STOCK_CACHE_DIR
from r2_stock_data import R2Settings, R2StockDataStore, configure_r2


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


if __name__ == "__main__":
    unittest.main()
