import unittest
import urllib.error
from datetime import datetime, timezone
from unittest.mock import patch

from fundamentals import (
    _merge_valuation_medians,
    _read_url_with_retries,
    get_company_fundamentals,
    growth_summary_fields,
    has_complete_company_fundamentals,
    parse_screener_company_chart_context,
    parse_screener_growth_html,
    parse_screener_valuation_chart_payload,
    refresh_company_fundamentals,
)
from downloader import MARKET_US


SAMPLE_HTML = """
<div class="ranges">
  <div class="ranges-table">
    <h3>Compounded Sales Growth</h3>
    <table>
      <tr><td>10 Years:</td><td>11%</td></tr>
      <tr><td>5 Years:</td><td>12%</td></tr>
      <tr><td>3 Years:</td><td>7%</td></tr>
      <tr><td>TTM:</td><td>10%</td></tr>
    </table>
  </div>
  <div class="ranges-table">
    <h3>Compounded Profit Growth</h3>
    <table>
      <tr><td>10 Years:</td><td>8%</td></tr>
      <tr><td>5 Years:</td><td>9%</td></tr>
      <tr><td>3 Years:</td><td>8%</td></tr>
      <tr><td>TTM:</td><td>13%</td></tr>
    </table>
  </div>
  <div class="ranges-table">
    <h3>Stock Price CAGR</h3>
    <table>
      <tr><td>10 Years:</td><td>7%</td></tr>
      <tr><td>5 Years:</td><td>-7%</td></tr>
      <tr><td>3 Years:</td><td>-9%</td></tr>
      <tr><td>1 Year:</td><td>-31%</td></tr>
    </table>
  </div>
  <div class="ranges-table">
    <h3>Return on Equity</h3>
    <table>
      <tr><td>10 Years:</td><td>28%</td></tr>
      <tr><td>5 Years:</td><td>31%</td></tr>
      <tr><td>3 Years:</td><td>31%</td></tr>
      <tr><td>Last Year:</td><td>32%</td></tr>
    </table>
  </div>
</div>
"""

CURRENT_TABLE_HTML = """
<div>
  <table class="ranges-table">
    <tr><th colspan="2">Compounded Sales Growth</th></tr>
    <tr><td>10 Years:</td><td>9%</td></tr>
    <tr><td>5 Years:</td><td>10%</td></tr>
    <tr><td>3 Years:</td><td>6%</td></tr>
    <tr><td>TTM:</td><td>8%</td></tr>
  </table>
</div>
"""


class ScreenerFundamentalsTests(unittest.TestCase):
    class _Response:
        def __init__(self, body):
            self.body = body

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def read(self):
            return self.body

    def test_growth_sections_and_negative_values_are_parsed(self):
        metrics = parse_screener_growth_html(SAMPLE_HTML)

        self.assertEqual(metrics["Compounded Sales Growth"]["10 Years"], 11.0)
        self.assertEqual(metrics["Compounded Profit Growth"]["TTM"], 13.0)
        self.assertEqual(metrics["Stock Price CAGR"]["3 Years"], -9.0)
        self.assertEqual(metrics["Return on Equity"]["Last Year"], 32.0)

    def test_summary_fields_use_three_year_values(self):
        metrics = parse_screener_growth_html(SAMPLE_HTML)

        self.assertEqual(
            growth_summary_fields(metrics),
            {
                "Sales CAGR 3Y": 7.0,
                "Profit CAGR 3Y": 8.0,
                "Price CAGR 3Y": -9.0,
                "ROE 3Y": 31.0,
            },
        )

    def test_current_ranges_table_markup_is_parsed(self):
        metrics = parse_screener_growth_html(CURRENT_TABLE_HTML)

        self.assertEqual(metrics["Compounded Sales Growth"]["3 Years"], 6.0)
        self.assertEqual(metrics["Compounded Sales Growth"]["TTM"], 8.0)

    def test_company_chart_context_and_median_payload_are_parsed(self):
        context = parse_screener_company_chart_context(
            '<div data-company-id="3365" data-warehouse-id="6599230" '
            'data-consolidated="true" id="company-info"></div>'
        )
        medians = parse_screener_valuation_chart_payload(
            {
                "datasets": [
                    {
                        "metric": "Median PE",
                        "label": "Median PE = 28.2",
                        "values": [["2023-01-01", "28.2"], ["2026-01-01", "28.2"]],
                    },
                    {
                        "metric": "Median Market Cap to Sales",
                        "label": "Median Market Cap to Sales = 5.3",
                        "values": [["2023-01-01", "5.3"], ["2026-01-01", "5.3"]],
                    },
                ]
            }
        )

        self.assertEqual(context, {"company_id": "3365", "consolidated": True})
        self.assertEqual(
            medians,
            {"Median PE": 28.2, "Median Market Cap to Sales": 5.3},
        )

    def test_us_market_skips_screener_fundamentals(self):
        self.assertEqual(get_company_fundamentals("AAPL", MARKET_US), ({}, {}))

    def test_transient_screener_error_is_retried(self):
        throttled = urllib.error.HTTPError(
            "https://www.screener.in/test",
            429,
            "Too Many Requests",
            {},
            None,
        )
        with (
            patch(
                "fundamentals.urllib.request.urlopen",
                side_effect=[throttled, self._Response(b"recovered")],
            ) as urlopen,
            patch("fundamentals.time.sleep") as sleep,
        ):
            body = _read_url_with_retries("request")

        self.assertEqual(body, b"recovered")
        self.assertEqual(urlopen.call_count, 2)
        sleep.assert_called_once()

    def test_fundamentals_completeness_requires_all_three_median_periods(self):
        metrics = parse_screener_growth_html(SAMPLE_HTML)
        incomplete = {
            "Median PE": {"10 Years": 12.9},
            "Median Market Cap to Sales": {"10 Years": 0.8},
        }
        two_periods = {
            "Median PE": {"3 Years": 22.0, "5 Years": 12.8},
            "Median Market Cap to Sales": {"3 Years": 0.7, "5 Years": 0.7},
        }
        complete = _merge_valuation_medians(
            incomplete,
            two_periods,
        )

        self.assertFalse(has_complete_company_fundamentals(metrics, incomplete))
        self.assertFalse(has_complete_company_fundamentals(metrics, two_periods))
        self.assertTrue(has_complete_company_fundamentals(metrics, complete))

    def test_manual_refresh_bypasses_cached_values_and_saves_new_data(self):
        cache = {
            "INDIA:TEST": {
                "fetched_at": datetime.now(timezone.utc).isoformat(),
                "metrics": {"Old metric": {"3 Years": 1}},
                "valuation_fetched_at": datetime.now(timezone.utc).isoformat(),
                "valuation_medians": {"Median PE": {"3 Years": 99}},
            }
        }
        refreshed_medians = {
            "Median PE": {"3 Years": 24.5},
            "Median Market Cap to Sales": {"3 Years": 4.2},
        }

        with (
            patch("fundamentals.load_fundamentals", return_value=cache),
            patch("fundamentals.save_fundamentals") as save_fundamentals,
            patch("fundamentals._fetch_screener_page", return_value=SAMPLE_HTML) as fetch_page,
            patch(
                "fundamentals._fetch_valuation_medians",
                return_value=refreshed_medians,
            ),
        ):
            metrics, medians, refreshed = refresh_company_fundamentals(
                "TEST",
                include_status=True,
            )

        fetch_page.assert_called_once_with("TEST")
        self.assertEqual(metrics["Compounded Sales Growth"]["3 Years"], 7.0)
        self.assertEqual(medians, refreshed_medians)
        self.assertTrue(refreshed)
        saved_entry = save_fundamentals.call_args.args[0]["INDIA:TEST"]
        self.assertEqual(saved_entry["metrics"], metrics)
        self.assertEqual(saved_entry["valuation_medians"], refreshed_medians)


if __name__ == "__main__":
    unittest.main()
