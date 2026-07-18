import unittest

from fundamentals import (
    get_company_fundamentals,
    growth_summary_fields,
    parse_screener_company_chart_context,
    parse_screener_growth_html,
    parse_screener_valuation_chart_payload,
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


if __name__ == "__main__":
    unittest.main()
