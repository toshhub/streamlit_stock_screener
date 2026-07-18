import json
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from charting import (
    interactive_chart_payload,
    interactive_stock_chart_html,
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

    def test_interactive_chart_header_contains_pe_ratio(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "TEST.json"
            path.write_text(json.dumps(self._price_rows(300)), encoding="utf-8")
            result = interactive_stock_chart_html(
                "TEST",
                path,
                ma_periods=[50, 200],
                pe_ratio=24.567,
                match_position=2,
                match_total=8,
                has_previous=True,
                has_next=True,
                initial_range="756",
            )

        self.assertIn("PE 24.57 · Daily interactive candlestick chart", result)
        self.assertIn('aria-label="Previous matched stock"', result)
        self.assertIn('aria-label="Next matched stock"', result)
        self.assertIn("2 / 8", result)
        self.assertIn("nse-interactive-chart", result)
        self.assertIn("range-change", result)
        self.assertIn("showBars(\"756\")", result)
        self.assertIn("CrosshairMode.Normal", result)
        self.assertNotIn("CrosshairMode.Magnet", result)

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

    def test_results_table_has_tiny_in_panel_interactive_button(self):
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

        self.assertIn('<button class="interactive-chart-link"', result)
        self.assertIn("interactive_chart=360ONE", result)
        self.assertIn("market=INDIA", result)
        self.assertIn("ma=50%2C200", result)
        self.assertIn("pe=20", result)
        self.assertIn('data-interactive-src="?', result)
        self.assertIn("embedded=1", result)
        self.assertIn("&position=", result)
        self.assertIn("&range=", result)
        self.assertIn("activeInteractiveRange", result)
        self.assertIn("message.action === 'range-change'", result)
        self.assertIn("nse-interactive-chart", result)
        self.assertIn("position: sticky", result)
        self.assertIn("revealInteractiveHeader", result)
        self.assertIn("embeddedFrame.addEventListener('load'", result)
        self.assertNotIn('target="_blank"', result)


if __name__ == "__main__":
    unittest.main()
