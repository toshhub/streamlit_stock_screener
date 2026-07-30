import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from stock_data import (
    earliest_stock_date,
    latest_stock_date,
    latest_stock_row,
    load_stock_dataframe,
    migrate_parquet_symbol,
    symbol_path,
    write_stock_data,
)


class StockJsonStorageTests(unittest.TestCase):
    def test_r2_latest_date_uses_manifest_without_loading_candles(self):
        class FakeStore:
            def fetch_manifest(self):
                return {
                    "markets": {
                        "india": {"latest_date": "2026-07-29"},
                    }
                }

        with (
            patch(
                "stock_data._r2_store_for_path",
                return_value=(FakeStore(), "india"),
            ),
            patch(
                "stock_data._edge_stock_row",
                side_effect=AssertionError("candle data should not be loaded"),
            ),
        ):
            latest = latest_stock_date("data/india/daily/TEST.json")

        self.assertEqual(latest, pd.Timestamp("2026-07-29"))

    def test_symbol_path_is_one_json_file_per_stock(self):
        self.assertEqual(
            symbol_path(Path("data/india/daily"), "reliance"),
            Path("data/india/daily/RELIANCE.json"),
        )

    def test_json_writer_merges_to_a_single_atomic_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            stock_file = Path(temp_dir) / "TEST.json"
            rows = pd.DataFrame([
                {"Date": "2025-01-02", "Close": 100.0},
                {"Date": "2026-01-02", "Close": 110.0},
            ])
            changed = write_stock_data(stock_file, rows)
            unchanged = write_stock_data(stock_file, rows)
            stored = load_stock_dataframe(stock_file)

        self.assertEqual(changed, [stock_file])
        self.assertEqual(unchanged, [])
        self.assertEqual(len(stored), 2)

    def test_edge_helpers_do_not_load_the_full_json_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            stock_file = Path(temp_dir) / "TEST.json"
            rows = pd.DataFrame([
                {"Date": "2026-01-01", "Close": 100.0},
                {"Date": "2026-01-02", "Close": 110.0},
            ])
            write_stock_data(stock_file, rows)

            with patch(
                "stock_data.load_stock_dataframe",
                side_effect=AssertionError("full JSON load should not be needed"),
            ):
                earliest = earliest_stock_date(stock_file)
                latest = latest_stock_date(stock_file)
                latest_row = latest_stock_row(stock_file)

        self.assertEqual(earliest, pd.Timestamp("2026-01-01"))
        self.assertEqual(latest, pd.Timestamp("2026-01-02"))
        self.assertEqual(latest_row["Close"], 110.0)

    def test_parquet_conversion_verifies_json_before_removing_source(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            legacy = root / "legacy" / "TEST"
            destination = root / "json"
            legacy.mkdir(parents=True)
            pd.DataFrame([
                {"Date": "2025-01-02", "Close": 100.0},
            ]).to_parquet(legacy / "2025.parquet", index=False)
            pd.DataFrame([
                {"Date": "2026-01-02", "Close": 110.0},
            ]).to_parquet(legacy / "2026.parquet", index=False)

            migrate_parquet_symbol(legacy, destination=destination)
            stored = load_stock_dataframe(destination / "TEST.json")

        self.assertEqual(len(stored), 2)
        self.assertFalse(legacy.exists())


if __name__ == "__main__":
    unittest.main()
