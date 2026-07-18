import json
import tempfile
import unittest
from pathlib import Path

import pandas as pd
from streamlit.testing.v1 import AppTest

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
                growth_metrics={
                    "Compounded Sales Growth": {
                        "10 Years": 11.0,
                        "5 Years": 12.0,
                        "3 Years": 7.0,
                        "TTM": 10.0,
                    },
                    "Stock Price CAGR": {
                        "10 Years": 7.0,
                        "5 Years": -7.0,
                        "3 Years": -9.0,
                        "1 Year": -31.0,
                    },
                },
                valuation_medians={
                    "Median PE": {
                        "10 Years": 30.0,
                        "5 Years": 25.0,
                        "3 Years": 20.0,
                    },
                    "Median Market Cap to Sales": {
                        "10 Years": 5.4,
                        "5 Years": 5.7,
                        "3 Years": 5.3,
                    },
                },
            )

        self.assertIn('class="chart-pe-badge"', result)
        self.assertIn('title="Price-to-Earnings ratio">PE 24.57</span>', result)
        self.assertIn("Interactive candlestick chart", result)
        self.assertIn("Selected stock", result)
        self.assertIn("Browse matches", result)
        self.assertIn("Time range", result)
        self.assertNotIn("Chart view", result)
        self.assertNotIn('id="zoom-out"', result)
        self.assertNotIn('id="zoom-in"', result)
        self.assertNotIn('id="reset-chart"', result)
        self.assertIn('aria-label="Previous matched stock"', result)
        self.assertIn('aria-label="Next matched stock"', result)
        self.assertIn('aria-label="Close interactive chart"', result)
        self.assertIn("2 / 8", result)
        self.assertIn("nse-interactive-chart", result)
        self.assertIn("range-change", result)
        self.assertIn("showBars(\"756\")", result)
        self.assertIn("CrosshairMode.Normal", result)
        self.assertNotIn("CrosshairMode.Magnet", result)
        self.assertNotIn("title: label", result)
        self.assertIn("item.label + ' ' + formatPrice(value)", result)
        self.assertIn("@media (max-width: 640px)", result)
        self.assertIn("grid-template-rows: auto auto minmax(280px, 1fr) auto", result)
        self.assertIn("padding: 0;", result)
        self.assertIn("Growth &amp; valuation snapshot", result)
        self.assertIn("Source: Screener.in", result)
        self.assertIn("<strong>-9%</strong>", result)
        self.assertIn("Median P/E", result)
        self.assertIn("Median Market Cap / Sales", result)
        self.assertIn("valuation-favorable", result)
        self.assertIn("Below historical median", result)

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

    def test_growth_section_is_hidden_when_values_are_unavailable(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "TEST.json"
            path.write_text(json.dumps(self._price_rows(300)), encoding="utf-8")
            result = interactive_stock_chart_html(
                "TEST",
                path,
                pe_ratio=20,
                growth_metrics={
                    "Compounded Sales Growth": {
                        "10 Years": None,
                        "5 Years": None,
                        "3 Years": None,
                        "TTM": None,
                    }
                },
                valuation_medians={
                    "Median PE": {
                        "10 Years": None,
                        "5 Years": None,
                        "3 Years": None,
                    }
                },
            )

        self.assertNotIn('class="growth-snapshot"', result)
        self.assertNotIn("Source: Screener.in", result)
        self.assertIn('<div class="chart-title">', result)
        self.assertNotIn('<div class="chart-title valuation-favorable">', result)
        self.assertNotIn('<div class="chart-title valuation-unfavorable">', result)

    def test_stock_box_is_red_when_current_pe_is_not_below_two_medians(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "TEST.json"
            path.write_text(json.dumps(self._price_rows(300)), encoding="utf-8")
            result = interactive_stock_chart_html(
                "TEST",
                path,
                pe_ratio=32,
                valuation_medians={
                    "Median PE": {
                        "10 Years": 30,
                        "5 Years": 25,
                        "3 Years": 20,
                    }
                },
            )

        self.assertIn("valuation-unfavorable", result)
        self.assertIn("Above historical median", result)

    def test_results_table_has_tiny_in_panel_interactive_button(self):
        df = pd.DataFrame(
            [
                {
                    "Symbol": "360ONE",
                    "PE Ratio": 20,
                    "Sales CAGR 3Y": 7,
                    "Profit CAGR 3Y": 8,
                    "Price CAGR 3Y": -9,
                    "ROE 3Y": 31,
                    "ValuationMedians": {
                        "Median PE": {
                            "3 Years": 30,
                            "5 Years": 25,
                            "10 Years": 15,
                        }
                    },
                    "ChartSource": "360ONE",
                    "FundamentalsRefreshToken": "completed-1",
                },
                {
                    "Symbol": "REDTEST",
                    "PE Ratio": 40,
                    "ValuationMedians": {
                        "Median PE": {
                            "3 Years": 30,
                            "5 Years": 25,
                            "10 Years": 20,
                        }
                    },
                    "ChartSource": "REDTEST",
                },
                {
                    "Symbol": "NEUTRAL",
                    "PE Ratio": 20,
                    "ValuationMedians": {},
                    "ChartSource": "NEUTRAL",
                },
            ]
        )

        result = results_hover_table_html(
            df,
            interactive_market="INDIA",
            interactive_ma_periods=[50, 200],
        )

        self.assertIn('<button class="interactive-chart-link"', result)
        self.assertIn('<button class="fundamentals-retry-link"', result)
        self.assertIn("retry_fundamentals=360ONE", result)
        self.assertNotIn('target="_top"', result)
        self.assertIn("fundamentals-retry-spin", result)
        self.assertIn("window.top.location.assign", result)
        self.assertIn("data-fundamentals-refresh-version='completed-1", result)
        self.assertNotIn("<th>FundamentalsRefreshToken</th>", result)
        self.assertLess(
            result.index('class="interactive-chart-link"'),
            result.index('class="fundamentals-retry-link"'),
        )
        self.assertIn("interactive_chart=360ONE", result)
        self.assertIn("market=INDIA", result)
        self.assertIn("ma=50%2C200", result)
        self.assertIn("pe=20", result)
        self.assertNotIn("<th>Sales CAGR 3Y</th>", result)
        self.assertNotIn("<th>Profit CAGR 3Y</th>", result)
        self.assertNotIn("<th>Price CAGR 3Y</th>", result)
        self.assertNotIn("<th>ROE 3Y</th>", result)
        self.assertIn('class="stock-symbol-label valuation-favorable"', result)
        self.assertIn('class="stock-symbol-label valuation-unfavorable"', result)
        self.assertIn('class="stock-symbol-label">NEUTRAL</span>', result)
        self.assertNotIn("<th>ValuationMedians</th>", result)
        self.assertIn('data-interactive-src="?', result)
        self.assertIn("embedded=1", result)
        self.assertIn("&position=", result)
        self.assertIn("&range=", result)
        self.assertIn("activeInteractiveRange", result)
        self.assertIn("message.action === 'range-change'", result)
        self.assertIn("message.action === 'close'", result)
        self.assertNotIn("data-interactive-close", result)
        self.assertIn("nse-interactive-chart", result)
        self.assertIn("position: sticky", result)
        self.assertIn("height: 1100px", result)
        self.assertIn("max-height: none", result)
        self.assertIn("border-width: 0", result)
        self.assertIn("revealInteractiveHeader", result)
        self.assertIn("embeddedFrame.addEventListener('load'", result)
        self.assertNotIn('target="_blank"', result)

        us_result = results_hover_table_html(
            df,
            interactive_market="US",
            interactive_ma_periods=[50, 200],
        )
        self.assertNotIn('class="fundamentals-retry-link"', us_result)


class InteractiveChartRouteTests(unittest.TestCase):
    def test_embedded_interactive_chart_route_renders_without_exception(self):
        stock_files = sorted(Path("data/daily").glob("*.json"))
        if not stock_files:
            self.skipTest("No daily stock fixture is available.")

        app = AppTest.from_file("app.py")
        app.query_params.update(
            {
                "interactive_chart": stock_files[0].stem,
                "market": "INDIA",
                "embedded": "1",
                "ma": "50,200",
            }
        )
        app.run(timeout=30)

        self.assertEqual(list(app.exception), [])

    def test_missing_fundamentals_retry_route_returns_without_exception(self):
        app = AppTest.from_file("app.py")
        app.query_params.update(
            {
                "retry_fundamentals": "NOT-IN-SAVED-RESULTS",
                "market": "INDIA",
            }
        )
        app.run(timeout=30)

        self.assertEqual(list(app.exception), [])


if __name__ == "__main__":
    unittest.main()
