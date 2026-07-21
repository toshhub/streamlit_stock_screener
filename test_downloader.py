import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from downloader import (
    DOWNLOAD_JOBS,
    DOWNLOAD_JOBS_LOCK,
    MARKET_INDIA,
    _date_after_latest,
    data_availability_summary,
    background_download_snapshot,
    download_symbol,
    start_background_download,
)


class IncrementalDownloaderTests(unittest.TestCase):
    def test_background_download_continues_without_browser_callback(self):
        with DOWNLOAD_JOBS_LOCK:
            DOWNLOAD_JOBS.clear()

        completed_rows = [{"Downloaded": True, "Rows Added": 2, "Status": "Updated"}]
        nifty_row = {"Downloaded": True, "Rows Added": 1, "Status": "Updated", "Error": ""}
        with (
            patch("downloader.download_top_stocks", return_value=completed_rows),
            patch("downloader.download_nifty_index", return_value=nifty_row),
        ):
            _, started = start_background_download(
                Path("symbols.xlsx"),
                "DAY",
                1,
                incremental=True,
                market=MARKET_INDIA,
            )
            deadline = time.monotonic() + 2
            snapshot = background_download_snapshot(MARKET_INDIA)
            while snapshot.get("running") and time.monotonic() < deadline:
                time.sleep(0.01)
                snapshot = background_download_snapshot(MARKET_INDIA)

        self.assertTrue(started)
        self.assertFalse(snapshot["running"])
        self.assertEqual(snapshot["status"], "Completed")
        self.assertEqual(snapshot["rows_added"], 2)

    def test_data_availability_counts_stocks_on_latest_date(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            (directory / "AAA.json").write_text(json.dumps([
                {"Date": "2026-07-20", "Close": 100.0},
                {"Date": "2026-07-21", "Close": 101.0},
            ]))
            (directory / "BBB.json").write_text(json.dumps([
                {"Date": "2026-07-21", "Close": 102.0},
            ]))
            (directory / "CCC.json").write_text(json.dumps([
                {"Date": "2026-07-18", "Close": 99.0},
            ]))
            (directory / "NIFTY.json").write_text(json.dumps([
                {"Date": "2026-07-21", "Close": 25000.0},
            ]))

            summary = data_availability_summary(directory)

        self.assertEqual(summary["Latest Date"], pd.Timestamp("2026-07-21"))
        self.assertEqual(summary["Stocks On Latest Date"], 2)
        self.assertEqual(summary["Stock Files"], 3)

    def test_daily_start_skips_weekend_dates(self):
        self.assertEqual(
            _date_after_latest(pd.Timestamp("2026-07-17"), "1d"),
            pd.Timestamp("2026-07-20"),
        )

    def test_each_stock_uses_its_own_latest_saved_date(self):
        fixed_today = pd.Timestamp("2026-07-21")
        downloaded = pd.DataFrame(
            {"Close": [107.0]},
            index=pd.DatetimeIndex(["2026-07-21"], name="Date"),
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            first_file = Path(temp_dir) / "FIRST.json"
            second_file = Path(temp_dir) / "SECOND.json"
            first_file.write_text(json.dumps([{"Date": "2026-07-16", "Close": 100.0}]))
            second_file.write_text(json.dumps([{"Date": "2026-07-17", "Close": 101.0}]))

            with (
                patch("downloader.pd.Timestamp") as timestamp,
                patch("downloader.yf.download", return_value=downloaded) as yf_download,
            ):
                timestamp.today.return_value = fixed_today
                download_symbol("FIRST", "1d", "5y", first_file, incremental=True)
                download_symbol("SECOND", "1d", "5y", second_file, incremental=True)

            self.assertEqual(yf_download.call_args_list[0].kwargs["start"], "2026-07-17")
            self.assertEqual(yf_download.call_args_list[1].kwargs["start"], "2026-07-20")

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
