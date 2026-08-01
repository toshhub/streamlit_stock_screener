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
        self.assertIn("SUPABASE_URL", workflow)
        self.assertIn("SUPABASE_SERVICE_ROLE_KEY", workflow)
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
            "git add -- data/metadata/monthly_valuations.parquet "
            "data/metadata/screener_fundamentals.json",
            workflow,
        )
        self.assertIn("git push", workflow)

        gitignore = (Path(__file__).parent / ".gitignore").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            "!data/metadata/screener_fundamentals.json",
            gitignore,
        )

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
        backend = object()
        with (
            patch(
                "daily_update.cloud_storage_from_environment",
                return_value=backend,
            ),
            patch("daily_update.configure_cloud_alerts") as configure_alerts,
            patch("daily_update.run_update", return_value=expected) as update,
        ):
            result = daily_update.main()
        configure_alerts.assert_called_once_with(backend, require_auth=True)
        update.assert_called_once_with()
        self.assertEqual(result, expected)

    def test_alerts_run_only_after_r2_manifest_is_published(self):
        source = Path("r2_update.py").read_text(encoding="utf-8")

        self.assertLess(
            source.index("upload_manifest(store, manifest)"),
            source.index("check_price_alerts_for_market_candles(", source.index("def run_update")),
        )


if __name__ == "__main__":
    unittest.main()
