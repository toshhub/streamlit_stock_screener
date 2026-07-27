import unittest
from pathlib import Path


class LiveScreenerPipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = (Path(__file__).parent / "app.py").read_text(
            encoding="utf-8"
        )

    def test_match_is_emitted_before_optional_chart_generation(self):
        match_event = self.source.index('"type": "match"')
        chart_phase = self.source.index('"phase": "charts"')
        self.assertLess(match_event, chart_phase)

    def test_live_results_do_not_trigger_synchronous_repairs(self):
        self.assertIn(
            "not live_job_running\n"
            "            and result_metadata_for_repair.get(\"create_charts\")",
            self.source,
        )
        self.assertNotIn("repair_result_fundamentals(rows", self.source)

    def test_default_live_worker_pool_is_twelve(self):
        self.assertIn(
            'os.environ.get("SCREENER_MAX_WORKERS", "12")',
            self.source,
        )


if __name__ == "__main__":
    unittest.main()
