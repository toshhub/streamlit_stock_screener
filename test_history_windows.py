import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from charting import interactive_chart_payload, interactive_stock_chart_html
from screener import load_price_dataframe, screen_json_file


def candle(date, close):
    return {
        "Date": pd.Timestamp(date),
        "Open": close - 1,
        "High": close + 1,
        "Low": close - 2,
        "Close": close,
        "Volume": 1000,
    }


class HistoryWindowTests(unittest.TestCase):
    def test_screener_loads_only_five_year_window_from_current_date(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            symbol_dir = Path(temp_dir) / "TEST"
            symbol_dir.mkdir()
            pd.DataFrame([candle("2020-07-27", 10)]).to_parquet(
                symbol_dir / "2020.parquet",
                index=False,
            )
            pd.DataFrame(
                [candle("2021-07-26", 20), candle("2021-07-27", 30)]
            ).to_parquet(symbol_dir / "2021.parquet", index=False)
            pd.DataFrame(
                [candle("2026-07-27", 40)]
            ).to_parquet(symbol_dir / "2026.parquet", index=False)

            result = load_price_dataframe(
                symbol_dir,
                years=5,
                as_of="2026-07-27",
            )

        self.assertEqual(
            result["Date"].dt.strftime("%Y-%m-%d").tolist(),
            ["2021-07-27", "2026-07-27"],
        )

    def test_chart_starts_at_five_years_and_reports_older_history(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            symbol_dir = Path(temp_dir) / "TEST"
            symbol_dir.mkdir()
            pd.DataFrame([candle("2016-07-27", 10)]).to_parquet(
                symbol_dir / "2016.parquet",
                index=False,
            )
            pd.DataFrame([candle("2021-07-27", 20)]).to_parquet(
                symbol_dir / "2021.parquet",
                index=False,
            )
            pd.DataFrame([candle("2026-07-27", 30)]).to_parquet(
                symbol_dir / "2026.parquet",
                index=False,
            )

            payload = interactive_chart_payload(
                symbol_dir,
                [50],
                history_years=5,
            )
            chart_html = interactive_stock_chart_html(
                "TEST",
                symbol_dir,
                ma_periods=[50],
            )

        self.assertEqual(payload["firstDate"], "2021-07-27")
        self.assertTrue(payload["hasEarlierHistory"])
        self.assertEqual(payload["historyYears"], 5)
        self.assertIn('action: "load-history"', chart_html)
        self.assertIn("subscribeVisibleLogicalRangeChange", chart_html)

    def test_plain_ma_screen_reads_a_smaller_window_than_five_years(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            symbol_dir = Path(temp_dir) / "TEST"
            symbol_dir.mkdir()
            for year in range(2021, 2027):
                pd.DataFrame([
                    candle(f"{year}-07-27", float(year))
                ]).to_parquet(symbol_dir / f"{year}.parquet", index=False)

            result = load_price_dataframe(
                symbol_dir,
                as_of="2026-07-27",
                filter_set=[{
                    "id": 1,
                    "type": "price_near_long",
                    "params": {"long_ma": 100, "threshold_pct": 5.0},
                }],
            )

        self.assertEqual(
            result["Date"].dt.year.unique().tolist(),
            [2026],
        )

    def test_technical_screen_does_not_wait_for_network_pe(self):
        dates = pd.bdate_range(end="2026-07-27", periods=260)
        rows = [
            candle(date, 100 + index * 0.1)
            for index, date in enumerate(dates)
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            symbol_dir = Path(temp_dir) / "TEST"
            symbol_dir.mkdir()
            frame = pd.DataFrame(rows)
            for year, year_frame in frame.groupby(frame["Date"].dt.year):
                year_frame.to_parquet(
                    symbol_dir / f"{year}.parquet",
                    index=False,
                )
            with patch(
                "screener.get_pe_ratio",
                side_effect=AssertionError("network PE should not be requested"),
            ) as get_pe:
                result = screen_json_file(
                    symbol_dir,
                    filter_set=[{
                        "id": 1,
                        "type": "price_near_long",
                        "params": {
                            "long_ma": 100,
                            "threshold_pct": 100.0,
                        },
                    }],
                )

        get_pe.assert_not_called()
        self.assertIsNotNone(result)
        self.assertEqual(result["PE Ratio"], "")


if __name__ == "__main__":
    unittest.main()
