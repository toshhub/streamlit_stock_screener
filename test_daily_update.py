import unittest
from pathlib import Path
from unittest.mock import patch

from downloader import MARKET_INDIA, MARKET_US
import daily_update


class DailyUpdateWorkflowTests(unittest.TestCase):
    def test_workflow_runs_both_post_close_passes_every_calendar_day(self):
        workflow = (
            Path(__file__).parent
            / ".github"
            / "workflows"
            / "streamlit-cron.yml"
        ).read_text(encoding="utf-8")

        self.assertIn('- cron: "30 11 * * *"', workflow)
        self.assertIn('- cron: "30 23 * * *"', workflow)
        self.assertIn("python daily_update.py", workflow)
        self.assertIn("git add -- data", workflow)
        self.assertIn("git push", workflow)

    def test_one_market_failure_does_not_stop_the_other_market(self):
        def symbols_for_market(_path, limit, market):
            return ["INDIA1"] if market == MARKET_INDIA else ["US1"]

        def download_for_market(
            _path,
            _timeframe,
            limit,
            incremental,
            market,
        ):
            if market == MARKET_INDIA:
                raise RuntimeError("India source temporarily failed")
            return [{
                "Symbol": "US1",
                "Downloaded": True,
                "Rows Added": 1,
                "Status": "Updated",
            }]

        with (
            patch("daily_update.configure_cloud_alerts"),
            patch("daily_update.load_top_symbols", side_effect=symbols_for_market),
            patch("daily_update.download_top_stocks", side_effect=download_for_market) as download,
            patch("daily_update.refresh_latest_stock_values"),
            patch(
                "daily_update.collect_monthly_valuations",
                return_value=([], []),
            ),
        ):
            result = daily_update.main()

        self.assertEqual(download.call_count, 2)
        self.assertEqual(
            [call.kwargs["market"] for call in download.call_args_list],
            [MARKET_INDIA, MARKET_US],
        )
        self.assertEqual(result["markets"][0]["Market"], MARKET_US)
        self.assertEqual(
            result["candle_failures"][0]["Market"],
            MARKET_INDIA,
        )


if __name__ == "__main__":
    unittest.main()
