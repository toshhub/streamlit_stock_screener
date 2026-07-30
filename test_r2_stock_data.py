import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from r2_stock_data import R2Settings, R2StockDataStore


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
