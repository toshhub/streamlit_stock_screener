import json
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from charting import (
    interactive_chart_payload,
    normalize_interactive_ma_periods,
    results_hover_table_html,
)


class InteractiveChartTests(unittest.TestCase):
    @staticmethod
    def _price_rows(count):
        return [
            {
                "Date": (pd.Timestamp("2020-01-01") + pd.Timedelta(days=index)).strftime("%Y-%m-%d"),
                "Open": index,
                "High": index + 2,
                "Low": index - 1,
                "Close": index + 1,
                "Volume": index * 100,
            }
            for index in range(1, count + 1)
        ]

    def test_payload_is_capped_and_contains_candles_mas_and_volume(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "TEST.json"
            path.write_text(json.dumps(self._price_rows(1200)), encoding="utf-8")
            payload = interactive_chart_payload(path, [50, 200], max_points=1000)

        self.assertEqual(payload["pointCount"], 1000)
        self.assertEqual(payload["maPeriods"], [50, 200])
        self.assertEqual(len(payload["candles"]), 1000)
        self.assertEqual(len(payload["movingAverages"]["SMA50"]), 1000)
        self.assertEqual(len(payload["movingAverages"]["SMA200"]), 1000)
        self.assertEqual(len(payload["volume"]), 1000)

    def test_payload_uses_full_available_history_by_default(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "TEST.json"
            path.write_text(json.dumps(self._price_rows(1200)), encoding="utf-8")
            payload = interactive_chart_payload(path, [50, 200])

        self.assertEqual(payload["pointCount"], 1200)
        self.assertEqual(len(payload["candles"]), 1200)
        self.assertEqual(payload["firstDate"], "2020-01-02")
        self.assertEqual(payload["lastDate"], "2023-04-15")

    def test_ma_periods_are_sanitized_capped_and_defaulted(self):
        self.assertEqual(
            normalize_interactive_ma_periods([200, "50", 50.9, -1, 0, "bad", 1200]),
            [50, 200],
        )
        self.assertEqual(normalize_interactive_ma_periods([]), [50, 200])
        self.assertEqual(
            normalize_interactive_ma_periods(range(1, 20)),
            [1, 2, 3, 4, 5, 6, 7],
        )

    def test_results_table_has_tiny_new_tab_interactive_link(self):
        df = pd.DataFrame(
            [
                {
                    "Symbol": "360ONE",
                    "PE Ratio": 20,
                    "ChartSource": "360ONE",
                }
            ]
        )

        result = results_hover_table_html(
            df,
            interactive_market="INDIA",
            interactive_ma_periods=[50, 200],
        )

        self.assertIn('class="interactive-chart-link"', result)
        self.assertIn("interactive_chart=360ONE", result)
        self.assertIn("market=INDIA", result)
        self.assertIn("ma=50%2C200", result)
        self.assertIn('target="_blank"', result)


if __name__ == "__main__":
    unittest.main()
