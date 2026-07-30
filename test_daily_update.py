import unittest
from pathlib import Path
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
        self.assertIn("R2_BUCKET_NAME", workflow)
        self.assertNotIn("git add -- data", workflow)
        self.assertNotIn("git push", workflow)

    def test_monthly_workflow_scrapes_and_commits_one_valuation_file(self):
        workflow = (
            Path(__file__).parent
            / ".github"
            / "workflows"
            / "monthly-valuations.yml"
        ).read_text(encoding="utf-8")

        self.assertIn('- cron: "15 2 2 * *"', workflow)
        self.assertIn("python monthly_valuation_update.py", workflow)
        self.assertIn(
            "git add -- data/metadata/monthly_valuations.parquet",
            workflow,
        )
        self.assertIn("git push", workflow)

    def test_daily_entrypoint_delegates_to_r2_update(self):
        from unittest.mock import patch

        expected = {
            "markets": [
                {
                    "Market": "INDIA",
                    "Rows": 1,
                    "Symbols": 1,
                    "Month": "2026-07",
                    "Failures": [],
                }
            ],
            "rollovers": [],
        }
        with patch("daily_update.run_update", return_value=expected) as update:
            result = daily_update.main()
        update.assert_called_once_with()
        self.assertEqual(result, expected)


if __name__ == "__main__":
    unittest.main()
