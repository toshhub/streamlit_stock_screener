import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from downloader import MARKET_INDIA, _date_after_latest, _sample_download_start, download_symbol


class IncrementalDownloaderTests(unittest.TestCase):
    def test_daily_start_skips_weekend_dates(self):
        self.assertEqual(
            _date_after_latest(pd.Timestamp("2026-07-17"), "1d"),
            pd.Timestamp("2026-07-20"),
        )

    def test_five_company_sample_selects_one_conservative_start_date(self):
        sample_dates = [
            "2026-07-17",
            "2026-07-17",
            "2026-07-16",
            "2026-07-17",
            "2026-07-17",
        ]

        with tempfile.TemporaryDirectory() as temp_dir:
            target_dir = Path(temp_dir)
            symbols = [f"TEST{index}" for index in range(5)]
            for symbol, latest_date in zip(symbols, sample_dates):
                (target_dir / f"{symbol}.json").write_text(
                    json.dumps([{"Date": latest_date, "Close": 100.0}])
                )

            start = _sample_download_start(symbols, target_dir, "1d")

        self.assertEqual(start, pd.Timestamp("2026-07-17"))

    def test_download_starts_after_latest_saved_date_and_appends_new_candle(self):
        existing_records = [
            {
                "Date": "2026-07-16",
                "Open": 100.0,
                "High": 105.0,
                "Low": 99.0,
                "Close": 103.0,
                "Volume": 1000,
            }
        ]
        downloaded = pd.DataFrame(
            {
                "Open": [104.0],
                "High": [108.0],
                "Low": [102.0],
                "Close": [107.0],
                "Volume": [1200],
            },
            index=pd.DatetimeIndex(["2026-07-17"], name="Date"),
        )
        fixed_today = pd.Timestamp("2026-07-19")

        with tempfile.TemporaryDirectory() as temp_dir:
            out_file = Path(temp_dir) / "TEST.json"
            out_file.write_text(json.dumps(existing_records))

            with (
                patch("downloader.pd.Timestamp") as timestamp,
                patch("downloader.yf.download", return_value=downloaded) as yf_download,
            ):
                timestamp.today.return_value = fixed_today
                result = download_symbol(
                    "TEST",
                    "1d",
                    "5y",
                    out_file,
                    incremental=True,
                    market=MARKET_INDIA,
                )

            kwargs = yf_download.call_args.kwargs
            self.assertEqual(kwargs["start"], "2026-07-17")
            self.assertEqual(kwargs["end"], "2026-07-20")
            self.assertNotIn("period", kwargs)
            self.assertEqual(result["Rows Added"], 1)
            self.assertEqual(result["Status"], "Updated")
            self.assertEqual(
                [row["Date"] for row in json.loads(out_file.read_text())],
                ["2026-07-16", "2026-07-17"],
            )

    def test_current_file_skips_yahoo_request_and_is_not_rewritten(self):
        existing_records = [{"Date": "2026-07-19", "Close": 103.0}]
        fixed_today = pd.Timestamp("2026-07-19")

        with tempfile.TemporaryDirectory() as temp_dir:
            out_file = Path(temp_dir) / "TEST.json"
            original_text = json.dumps(existing_records)
            out_file.write_text(original_text)

            with (
                patch("downloader.pd.Timestamp") as timestamp,
                patch("downloader.yf.download") as yf_download,
            ):
                timestamp.today.return_value = fixed_today
                result = download_symbol(
                    "TEST",
                    "1d",
                    "5y",
                    out_file,
                    incremental=True,
                    market=MARKET_INDIA,
                )

            yf_download.assert_not_called()
            self.assertEqual(result["Rows Added"], 0)
            self.assertEqual(result["Status"], "Already current")
            self.assertEqual(out_file.read_text(), original_text)


if __name__ == "__main__":
    unittest.main()
