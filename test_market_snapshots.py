import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

import market_snapshots
from downloader import MARKET_INDIA, MARKET_US


class MonthlyValuationTests(unittest.TestCase):
    def test_screener_history_is_upserted_and_us_is_not_requested(self):
        history = [
            {
                "Month": "2026-01-01",
                "PE": 18.0,
                "MarketCapToSales": 2.2,
                "EPS": 4.5,
                "Sales": 600.0,
                "MedianPE": 16.0,
                "MedianMarketCapToSales": 2.0,
            },
            {
                "Month": "2026-02-01",
                "PE": 19.0,
                "MarketCapToSales": 2.3,
                "EPS": 4.5,
                "Sales": 600.0,
                "MedianPE": 16.0,
                "MedianMarketCapToSales": 2.0,
            },
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "monthly.parquet"
            with (
                patch.object(market_snapshots, "MONTHLY_VALUATIONS_FILE", output),
                patch(
                    "market_snapshots.fetch_screener_valuation_history",
                    return_value=history,
                ) as fetch,
            ):
                rows, failures = market_snapshots.collect_monthly_valuations(
                    {
                        MARKET_INDIA: ["TEST"],
                        MARKET_US: ["AAPL"],
                    },
                    month="2026-02-15",
                )
                stored = pd.read_parquet(output)

        self.assertEqual(fetch.call_args.args, ("TEST",))
        self.assertEqual(len(rows), 2)
        self.assertFalse(failures)
        self.assertEqual(set(stored["Symbol"]), {"TEST"})
        self.assertEqual(set(stored["Source"]), {"Screener.in"})
        self.assertIn("EPS", stored.columns)
        self.assertIn("Sales", stored.columns)

    def test_local_pe_medians_support_result_table_coloring(self):
        rows = pd.DataFrame([
            {
                "Month": month,
                "Market": MARKET_INDIA,
                "Symbol": "TEST",
                "PE": pe,
            }
            for month, pe in (
                ("2017-01-01", 8.0),
                ("2022-01-01", 10.0),
                ("2024-01-01", 20.0),
                ("2026-01-01", 30.0),
            )
        ])
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "monthly.parquet"
            rows.to_parquet(output, index=False)
            with patch.object(
                market_snapshots,
                "MONTHLY_VALUATIONS_FILE",
                output,
            ):
                medians = market_snapshots.historical_pe_medians_by_symbol(
                    MARKET_INDIA,
                    as_of="2026-07-01",
                )

        self.assertEqual(
            medians["TEST"]["Median PE"],
            {
                "3 Years": 25.0,
                "5 Years": 20.0,
                "10 Years": 15.0,
            },
        )


if __name__ == "__main__":
    unittest.main()
