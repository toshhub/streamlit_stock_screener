import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd
from streamlit.testing.v1 import AppTest

from charting import (
    interactive_chart_payload,
    interactive_stock_chart_html,
    normalize_interactive_ma_periods,
    results_hover_table_html,
)


class InteractiveChartTests(unittest.TestCase):
    def test_cursor_alert_component_relays_chart_controls_to_results_owner(self):
        component_html = (
            Path(__file__).parent / "cursor_alert_component" / "index.html"
        ).read_text(encoding="utf-8")

        self.assertIn("window.parent.parent.postMessage(message", component_html)
        self.assertIn("message.source === \"nse-interactive-chart\"", component_html)
        self.assertIn("allow-popups-to-escape-sandbox", component_html)
        self.assertIn('message.action === "add-to-watchlist"', component_html)

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
        self.assertIn("Interactive chart navigation", result)
        self.assertIn("Time range", result)
        self.assertNotIn("Chart view", result)
        self.assertNotIn('id="zoom-out"', result)
        self.assertNotIn('id="zoom-in"', result)
        self.assertNotIn('id="reset-chart"', result)
        self.assertIn('aria-label="Previous stock"', result)
        self.assertIn('aria-label="Next stock"', result)
        self.assertIn('aria-label="Close interactive chart"', result)
        self.assertIn('id="chart-symbol-input"', result)
        self.assertIn('id="chart-fullscreen"', result)
        self.assertIn("requestFullscreen", result)
        self.assertIn("webkitRequestFullscreen", result)
        self.assertIn("webkitExitFullscreen", result)
        self.assertIn("fullscreen-fallback", result)
        self.assertIn("chart-pseudo-fullscreen", result)
        self.assertIn("promoteAncestorFrames", result)
        self.assertIn('frame.style.setProperty("position", "fixed", "important")', result)
        self.assertIn('frame.style.setProperty("height", "100dvh", "important")', result)
        self.assertIn("ownerWindow.scrollTo(0, 0)", result)
        self.assertIn('screen.orientation.lock("landscape")', result)
        self.assertIn("fullscreen-exit-icon", result)
        self.assertIn("Not in table", result)
        self.assertIn("symbol-select", result)
        self.assertIn("2 / 8", result)
        self.assertIn("nse-interactive-chart", result)
        self.assertIn("range-change", result)
        self.assertIn("showBars(\"756\")", result)
        self.assertIn("CrosshairMode.Normal", result)
        self.assertNotIn("CrosshairMode.Magnet", result)
        self.assertIn("minimumBarSpacingForWidth", result)
        self.assertIn("minBarSpacing: minimumBarSpacingForWidth(initialChartWidth)", result)
        self.assertNotIn("minBarSpacing: 1.2", result)
        self.assertNotIn("title: label", result)
        self.assertIn("item.label + ' ' + formatPrice(value)", result)
        self.assertIn("Gain versus previous candle close", result)
        self.assertIn('class="chart-title__identity"', result)
        self.assertIn('class="chart-legend" id="chart-legend"', result)
        self.assertLess(
            result.index('class="chart-title__identity"'),
            result.index('class="chart-legend" id="chart-legend"'),
        )
        self.assertIn(
            "renderLegend(latestCandle.time, latestCandle, latestCandleIndex, null)",
            result,
        )
        self.assertIn("gainFromPreviousCandle", result)
        self.assertIn("candleAtOrBeforeCursor", result)
        self.assertIn("Math.floor(logical)", result)
        self.assertIn('class="legend-gain ', result)

        self.assertIn("@media (max-width: 640px)", result)
        self.assertIn("grid-template-rows: auto minmax(0, 1fr) auto", result)
        self.assertIn("padding: 0;", result)
        self.assertIn("Growth &amp; valuation snapshot", result)
        self.assertIn("Source: Screener.in", result)
        self.assertIn('class="fundamentals-drawer"', result)
        self.assertIn('class="fundamentals-toggle"', result)
        self.assertIn('aria-expanded="false"', result)
        self.assertIn('aria-hidden="true" inert', result)
        self.assertIn('class="fundamentals-panel"', result)
        self.assertIn("transform: translateX(calc(-100% - 18px))", result)
        self.assertIn("width: calc(100% - 42px)", result)
        self.assertIn("border-radius: 12px", result)
        self.assertIn("setFundamentalsOpen(false)", result)
        self.assertIn(
            '!fundamentalsDrawer.classList.contains("is-open")',
            result,
        )
        self.assertIn(
            '!valuationDrawer.classList.contains("is-open")',
            result,
        )
        self.assertIn(
            "if (isOpen && valuationDrawer) setValuationOpen(false)",
            result,
        )
        self.assertIn(
            "if (open && fundamentalsDrawer) setFundamentalsOpen(false)",
            result,
        )
        self.assertIn("left: 34px", result)
        self.assertIn("height:clamp(240px,44dvh,420px)", result)
        self.assertIn('id="price-alert-at-cursor"', result)
        self.assertIn('aria-label="Add price alert at cursor"', result)
        self.assertIn('data-symbol="TEST"', result)
        self.assertIn('data-market="INDIA"', result)
        self.assertIn('action: "create-price-alert"', result)
        self.assertIn("updateCursorPriceAlert(param)", result)
        self.assertIn("candleSeries.coordinateToPrice(param.point.y)", result)
        self.assertIn('priceAlertButton.style.top = safeY + "px"', result)
        self.assertNotIn('type="number"', result)
        self.assertNotIn('id="price-alert-dialog"', result)
        self.assertIn('event.key === "Escape"', result)
        self.assertLess(
            result.index('id="chart"'),
            result.index('id="fundamentals-drawer"'),
        )
        self.assertNotIn('class="growth-snapshot"', result)
        self.assertIn("<strong>-9%</strong>", result)
        self.assertIn("Median P/E", result)
        self.assertIn("Median Market Cap / Sales", result)
        self.assertIn("valuation-favorable", result)
        self.assertIn("Below historical median", result)

    def test_mobile_interactive_chart_keeps_time_axis_inside_viewport(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "TEST.json"
            path.write_text(
                json.dumps(self._price_rows(300)),
                encoding="utf-8",
            )
            result = interactive_stock_chart_html("TEST", path)

        self.assertIn(
            "grid-template-rows: auto minmax(0, 1fr) auto",
            result,
        )
        self.assertIn("#chart { min-height: 0; }", result)
        self.assertIn("function minimumChartHeight()", result)
        self.assertIn(
            'window.matchMedia("(max-width: 640px)").matches ? 120 : 280',
            result,
        )
        self.assertIn(
            "height: Math.max(minimumChartHeight(), container.clientHeight)",
            result,
        )
        self.assertIn(
            "height: Math.max(minimumChartHeight(), Math.floor(rect.height))",
            result,
        )
        self.assertIn(
            "@media (max-width: 640px) and (orientation: portrait)",
            result,
        )
        self.assertIn("display: contents", result)
        self.assertIn("order: 4", result)
        self.assertIn(
            "flex: 0 0 max(240px, calc(100dvh - 193px))",
            result,
        )
        self.assertIn("overflow-y: auto", result)
        self.assertIn("left: -1px", result)

    def test_interactive_chart_can_add_stock_to_a_watchlist(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "TEST.json"
            path.write_text(
                json.dumps(self._price_rows(300)),
                encoding="utf-8",
            )
            result = interactive_stock_chart_html(
                "TEST",
                path,
                watchlists=[
                    {"id": "growth-list", "name": "Growth & Quality"},
                    {"id": "income-list", "name": "Income"},
                ],
            )

        self.assertIn('id="chart-watchlist-select"', result)
        self.assertIn('value="growth-list"', result)
        self.assertIn("Growth &amp; Quality", result)
        self.assertIn('id="chart-watchlist-add"', result)
        self.assertIn('action: "add-to-watchlist"', result)
        self.assertIn("watchlistId: watchlistId", result)

    def test_new_price_alert_refreshes_cached_chart_markers(self):
        source = Path("charting.py").read_text(encoding="utf-8")

        self.assertIn(
            'st.session_state.pop("_cached_price_alerts", None)',
            source,
        )
        self.assertIn(
            'st.session_state.pop("_cached_price_alerts_at", None)',
            source,
        )
        self.assertIn("if created:", source)

    def test_valuation_drawer_has_screener_style_metric_and_range_controls(self):
        valuation_rows = [{
            "time": "2020-02-01",
            "pe": 18.2,
            "marketCapToSales": 2.4,
            "eps": 5.1,
            "sales": 720.0,
            "medianPe": 16.0,
            "medianMarketCapToSales": 2.0,
        }]
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "TEST.json"
            path.write_text(json.dumps(self._price_rows(300)), encoding="utf-8")
            with patch("charting.valuation_chart_payload", return_value=valuation_rows):
                result = interactive_stock_chart_html("TEST", path)

        self.assertIn('data-valuation-metric="pe"', result)
        self.assertIn('data-valuation-metric="sales"', result)
        self.assertIn('data-valuation-months="1"', result)
        self.assertIn('data-valuation-months="6"', result)
        self.assertIn('data-valuation-months="120"', result)
        self.assertNotIn('data-valuation-months="all"', result)
        self.assertNotIn(">Max</button>", result)
        self.assertIn('id="valuation-price-toggle"', result)
        self.assertIn('aria-pressed="true"', result)
        self.assertIn("valuationPriceEnabled = true", result)
        self.assertIn('id="valuation-price-legend"', result)
        self.assertIn('id="valuation-price-cursor-dot"', result)
        self.assertIn('"price":32.0', result)
        self.assertIn("Price:", result)
        self.assertIn("TTM EPS", result)
        self.assertIn("TTM Sales", result)
        self.assertIn("medianMarketCapToSales", result)
        self.assertIn("sortedLineValues", result)
        self.assertIn("Median PE", result)
        self.assertIn('id="valuation-crosshair"', result)
        self.assertIn('id="valuation-cursor-dot"', result)
        self.assertIn("svg.onpointermove", result)
        self.assertIn("svg.onpointerdown", result)
        self.assertIn("cursorIndexFromPointer", result)
        self.assertIn("valuation-tooltip__date", result)
        self.assertIn('event.key === "ArrowRight"', result)
        self.assertIn("requestAnimationFrame(drawValuationChart)", result)

    def test_interactive_trade_overlay_adds_levels_markers_and_window(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "TEST.json"
            path.write_text(json.dumps(self._price_rows(40)), encoding="utf-8")
            result = interactive_stock_chart_html(
                "TEST",
                path,
                trade_overlay={
                    "buyDate": "2020-01-12",
                    "exitDate": "2020-01-20",
                    "windowStart": "2020-01-02",
                    "windowEnd": "2020-01-30",
                    "buyPrice": 11,
                    "targetPrice": 12.1,
                    "stopPrice": 10.45,
                    "exitPrice": 12.1,
                    "exitReason": "Target",
                },
            )

        self.assertIn('"tradeOverlay":{', result)
        self.assertIn('"targetPrice":12.1', result)
        self.assertIn("candleSeries.createPriceLine", result)
        self.assertIn("LightweightCharts.createSeriesMarkers", result)
        self.assertIn("tradeWindowStart", result)

    def test_alert_overlay_adds_thin_price_line_and_created_date_arrow(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "TEST.json"
            path.write_text(json.dumps(self._price_rows(40)), encoding="utf-8")
            result = interactive_stock_chart_html(
                "TEST",
                path,
                trade_overlay={
                    "alertDate": "2020-01-12T10:00:00+05:30",
                    "alertPrice": 12.25,
                },
            )

        self.assertIn('"alertDate":"2020-01-12"', result)
        self.assertIn('"alertPrice":12.25', result)
        self.assertIn("const legacyAlertPrice", result)
        self.assertIn('title: isBelow ? "ALERT ↓" : "ALERT ↑"', result)
        self.assertIn("lineWidth: 1", result)
        self.assertIn("const alertAnchorSeries", result)
        self.assertIn("value: marker.price", result)
        self.assertIn('position: isBelow ? "aboveBar" : "belowBar"', result)
        self.assertIn('shape: isBelow ? "arrowDown" : "arrowUp"', result)
        self.assertIn('text: isBelow ? "A↓" : "A↑"', result)
        self.assertIn("[...candleTimes].reverse().find", result)
        self.assertIn("time <= marker.date", result)
        self.assertNotIn("time >= requestedAlertDate", result)

    def test_interactive_chart_renders_every_active_alert_marker(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "TEST.json"
            path.write_text(json.dumps(self._price_rows(40)), encoding="utf-8")
            result = interactive_stock_chart_html(
                "TEST",
                path,
                alert_markers=[
                    {
                        "id": "above",
                        "date": "2020-01-12",
                        "price": 12.25,
                        "direction": "above",
                    },
                    {
                        "id": "below",
                        "date": "2020-01-18",
                        "price": 9.75,
                        "direction": "below",
                    },
                ],
            )

        self.assertIn('"alertMarkers":[', result)
        self.assertIn(
            '"id":"above","date":"2020-01-12","price":12.25,'
            '"direction":"above"',
            result,
        )
        self.assertIn(
            '"id":"below","date":"2020-01-18","price":9.75,'
            '"direction":"below"',
            result,
        )
        self.assertIn("uniqueAlertMarkers.forEach", result)
        self.assertIn("const alertLinePrices = new Set()", result)

    def test_alert_table_stock_name_opens_embedded_chart_with_marker_params(self):
        result = results_hover_table_html(
            pd.DataFrame([{
                "Symbol": "TEST",
                "Market": "India",
                "Target Price": 123.45,
                "Interactive Market": "INDIA",
                "Alert Date": "2026-07-20T10:00:00+05:30",
                "Alert Price": 123.45,
            }]),
            interactive_symbol_click=True,
            table_title="Price Alert Charts",
        )

        self.assertIn("Price Alert Charts", result)
        self.assertIn("interactive-symbol-button", result)
        self.assertIn("flex: 0 1 auto", result)
        self.assertIn(".interactive-symbol-button .stock-symbol-label", result)
        self.assertIn(">TEST</span></button>", result)
        self.assertIn("interactive_chart=TEST", result)
        self.assertIn("market=INDIA", result)
        self.assertIn("alert_date=", result)
        self.assertIn("alert_marker_price=123.45", result)
        self.assertNotIn(">Interactive Market</th>", result)
        self.assertNotIn(">Alert Date</th>", result)
        self.assertNotIn(">Alert Price</th>", result)

    def test_ma_periods_are_sanitized_capped_and_defaulted(self):
        self.assertEqual(
            normalize_interactive_ma_periods([200, "50", 50.9, -1, 0, "bad", 1200]),
            [50, 200],
        )
        self.assertEqual(normalize_interactive_ma_periods([]), [50, 200])
        self.assertEqual(
            normalize_interactive_ma_periods(range(1, 20)),
            list(range(1, 20)),
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
        self.assertNotIn('class="fundamentals-drawer"', result)
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

    def test_stock_box_is_red_when_current_pe_is_unavailable_but_medians_exist(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "TEST.json"
            path.write_text(json.dumps(self._price_rows(300)), encoding="utf-8")
            result = interactive_stock_chart_html(
                "TEST",
                path,
                pe_ratio=None,
                valuation_medians={
                    "Median PE": {
                        "10 Years": 12.1,
                        "5 Years": 16.9,
                        "3 Years": 66.4,
                    }
                },
            )

        self.assertIn("valuation-unfavorable", result)
        self.assertIn("Current P/E unavailable", result)

    def test_results_table_has_tiny_in_panel_interactive_button(self):
        df = pd.DataFrame(
            [
                {
                    "Symbol": "360ONE",
                    "PE Ratio": 20,
                    "Market Cap Position": 2,
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
                },
                {
                    "Symbol": "REDTEST",
                    "PE Ratio": 40,
                    "Market Cap Position": 1,
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
                {
                    "Symbol": "COMPLETE",
                    "PE Ratio": 20,
                    "GrowthMetrics": {
                        "Compounded Sales Growth": {"3 Years": 5},
                        "Compounded Profit Growth": {"3 Years": 6},
                        "Stock Price CAGR": {"3 Years": 7},
                        "Return on Equity": {"3 Years": 8},
                    },
                    "ValuationMedians": {
                        "Median PE": {
                            "3 Years": 24,
                            "5 Years": 26,
                            "10 Years": 22,
                        },
                        "Median Market Cap to Sales": {
                            "3 Years": 3.1,
                            "5 Years": 3.4,
                            "10 Years": 2.8,
                        },
                    },
                    "ChartSource": "COMPLETE",
                },
                {
                    "Symbol": "LOSSMAKING",
                    "PE Ratio": "",
                    "GrowthMetrics": {
                        "Compounded Sales Growth": {"3 Years": 5},
                        "Compounded Profit Growth": {"TTM": 6},
                        "Stock Price CAGR": {"3 Years": 7},
                        "Return on Equity": {"3 Years": -2},
                    },
                    "ValuationMedians": {
                        "Median PE": {
                            "3 Years": 66.4,
                            "5 Years": 16.9,
                            "10 Years": 12.1,
                        },
                        "Median Market Cap to Sales": {
                            "3 Years": 1.2,
                            "5 Years": 1.3,
                            "10 Years": 1.3,
                        },
                    },
                    "ChartSource": "LOSSMAKING",
                },
            ]
        )

        result = results_hover_table_html(
            df,
            interactive_market="INDIA",
            interactive_ma_periods=[50, 200],
        )

        self.assertIn('<button class="interactive-chart-link"', result)
        self.assertIn('<a class="screener-company-link"', result)
        self.assertIn('<span aria-hidden="true">S</span></a>', result)
        self.assertEqual(
            result.count('class="screener-company-link"'),
            len(df),
        )
        self.assertIn(
            'href="https://www.screener.in/company/360ONE/"',
            result,
        )
        self.assertIn('target="_blank" rel="noopener noreferrer"', result)
        self.assertIn(
            'aria-label="Open COMPLETE on Screener.in"',
            result,
        )
        self.assertIn(
            'class="stock-symbol-label valuation-unfavorable" '
            'title="Current PE is unavailable or non-positive; '
            'historical median PE data is available">LOSSMAKING</span>',
            result,
        )
        self.assertIn(
            'aria-label="Open LOSSMAKING on Screener.in"',
            result,
        )
        self.assertLess(
            result.index('class="interactive-chart-link"'),
            result.index('class="screener-company-link"'),
        )
        self.assertIn("interactive_chart=360ONE", result)
        self.assertIn("market=INDIA", result)
        self.assertIn("ma=50%2C200", result)
        self.assertIn("pe=20", result)
        self.assertIn('onclick="toggleSymbolSort(0)"', result)
        self.assertIn('onclick="sortNumericColumn(1)"', result)
        self.assertIn('onclick="sortNumericColumn(2)"', result)
        self.assertIn("restoreOriginalOrder", result)
        self.assertIn("tap again for market-cap order", result)
        self.assertIn("<th class=\"sortable\" onclick=\"sortNumericColumn(2)\">Market Cap Position</th>", result)
        self.assertNotIn("<th>Sales CAGR 3Y</th>", result)
        self.assertNotIn("<th>Profit CAGR 3Y</th>", result)
        self.assertNotIn("<th>Price CAGR 3Y</th>", result)
        self.assertNotIn("<th>ROE 3Y</th>", result)
        self.assertIn('class="stock-symbol-label valuation-favorable"', result)
        self.assertIn('class="stock-symbol-label valuation-unfavorable"', result)
        self.assertIn('class="stock-symbol-label">NEUTRAL</span>', result)
        self.assertIn('<tr class="valuation-favorable"', result)
        self.assertIn('<tr class="valuation-unfavorable"', result)
        self.assertIn("tbody tr.valuation-favorable", result)
        self.assertIn("tbody tr.valuation-unfavorable", result)
        self.assertNotIn("<th>ValuationMedians</th>", result)
        self.assertIn('data-interactive-src="?', result)
        self.assertIn("embedded=1", result)
        self.assertIn("&position=", result)
        self.assertIn("&embed_height=", result)
        self.assertIn("&compact_landscape=", result)
        self.assertIn("availableEmbedHeight", result)
        self.assertIn("(orientation: landscape) and (max-height: 600px)", result)
        self.assertIn("window.visualViewport", result)
        self.assertNotIn("viewportHeight + 300", result)
        self.assertIn("var componentFrameHeight = viewportHeight", result)
        self.assertIn("Math.max(", result)
        self.assertIn("setComponentFrameHeight(componentFrameHeight)", result)
        self.assertIn("&range=", result)
        self.assertIn("activeInteractiveRange", result)
        self.assertIn("message.action === 'range-change'", result)
        self.assertIn("message.action === 'close'", result)
        self.assertNotIn("data-interactive-close", result)
        self.assertIn("nse-interactive-chart", result)
        self.assertIn("position: sticky", result)
        self.assertIn("position: fixed", result)
        self.assertIn(".chart-panel.interactive-mode::before", result)
        self.assertIn("height: var(--fixed-app-nav-clearance)", result)
        self.assertIn("--fixed-app-nav-clearance:", result)
        self.assertIn("top: var(--fixed-app-nav-clearance)", result)
        self.assertIn(
            "height: calc(100vh - var(--fixed-app-nav-clearance))",
            result,
        )
        self.assertIn(
            "height: calc(100dvh - var(--fixed-app-nav-clearance))",
            result,
        )
        self.assertIn("window.getComputedStyle(panel).top", result)
        self.assertIn("- navClearance", result)
        self.assertIn("flex: 1 1 auto", result)
        self.assertIn("window.frameElement.scrollIntoView", result)
        self.assertNotIn("height: 1100px", result)
        self.assertIn("border-width: 0", result)
        self.assertIn(".interactive-panel-header { display: none; }", result)
        self.assertIn("revealInteractiveHeader", result)
        self.assertIn("embeddedFrame.addEventListener('load'", result)
        self.assertIn('allow="fullscreen; screen-orientation" allowfullscreen', result)
        self.assertIn("message.action === 'fullscreen-fallback'", result)
        self.assertIn("source: 'nse-chart-host'", result)
        self.assertIn("symbols: items.map", result)
        self.assertIn("message.action === 'symbol-select'", result)
        self.assertIn("table-layout: fixed", result)
        self.assertIn("overflow-x: hidden", result)
        self.assertNotIn("min-width: 560px", result)

        roi_result = results_hover_table_html(
            pd.DataFrame([{"Symbol": "TEST", "ROI50": 1.25}]),
            interactive_market="INDIA",
        )
        self.assertIn(
            '<th class="sortable" onclick="sortNumericColumn(1)">ROI50</th>',
            roi_result,
        )

        us_result = results_hover_table_html(
            df,
            interactive_market="US",
            interactive_ma_periods=[50, 200],
        )
        self.assertNotIn('class="screener-company-link"', us_result)

    def test_results_table_supports_inline_alert_actions(self):
        alert_df = pd.DataFrame([
            {
                "Symbol": "TEST",
                "Market": "India",
                "Condition": "Cross below",
                "Prices": "Target 100 / Reference 110",
                "Dates": "Created 28 Jul 2026 / Triggered —",
                "Actions": "",
                "ChartSource": "TEST",
                "Interactive Market": "INDIA",
                "Alert Date": "2026-07-28T10:00:00+05:30",
                "Alert Price": 100,
                "Acknowledge Button Key": "alert_acknowledge_abc",
                "Remove Button Key": "alert_remove_abc",
            }
        ])

        result = results_hover_table_html(
            alert_df,
            table_title="New Alerts",
            row_actions=True,
            count_label="alert",
        )

        self.assertIn("New Alerts", result)
        self.assertIn("1 alert", result)
        self.assertIn('aria-label="Acknowledge alert"', result)
        self.assertIn('aria-label="Remove alert"', result)
        self.assertIn("window.confirm", result)
        self.assertIn(
            'data-streamlit-action-key="alert_acknowledge_abc"',
            result,
        )
        self.assertIn(
            'data-streamlit-action-key="alert_remove_abc"',
            result,
        )
        self.assertIn("triggerStreamlitAction", result)
        self.assertIn("source: 'alert-table-action'", result)
        self.assertIn("--component-nav-origin: 3.65rem", result)
        self.assertIn(
            "var(--component-nav-origin) + 3.15rem",
            result,
        )
        self.assertNotIn("__NAV_ORIGIN_", result)
        self.assertIn("source: 'alert-table-chart'", result)
        self.assertNotIn("window.parent.document.querySelector", result)
        self.assertNotIn('target="_top"', result)
        self.assertIn("streamlit:setFrameHeight", result)
        self.assertIn(
            "window.frameElement.style.height = requestedHeight + 'px'",
            result,
        )
        self.assertIn("data-default-height='700'", result)
        self.assertNotIn("<th>Acknowledge URL</th>", result)
        self.assertNotIn("<th>Remove URL</th>", result)
        self.assertNotIn("<th>Acknowledge Button Key</th>", result)
        self.assertNotIn("<th>Remove Button Key</th>", result)
        self.assertIn("alert_date=2026-07-28", result)
        self.assertIn("alert_marker_price=100", result)

    def test_alert_table_component_forwards_actions_to_streamlit(self):
        component_html = (
            Path(__file__).parent
            / "alert_table_component"
            / "index.html"
        ).read_text(encoding="utf-8")

        self.assertIn("streamlit:componentReady", component_html)
        self.assertIn("streamlit:setComponentValue", component_html)
        self.assertIn('message.source === "alert-table-action"', component_html)
        self.assertIn('message.source === "alert-table-chart"', component_html)
        self.assertIn('message.action === "reveal"', component_html)
        self.assertIn("window.frameElement.scrollIntoView", component_html)
        self.assertIn(
            'window.frameElement.style.height = safeHeight + "px"',
            component_html,
        )
        self.assertIn("actionKey", component_html)
        self.assertIn("<base href=", component_html)
        self.assertIn("document.referrer", component_html)


class InteractiveChartRouteTests(unittest.TestCase):
    def test_embedded_interactive_chart_route_renders_without_exception(self):
        stock_files = sorted(Path("data/india/daily").glob("*.json"))
        if not stock_files:
            self.skipTest("No daily stock fixture is available.")

        app = AppTest.from_file("app.py")
        app.query_params.update(
            {
                "interactive_chart": stock_files[0].stem,
                "market": "INDIA",
                "embedded": "1",
                "embed_height": "630",
                "compact_landscape": "1",
                "ma": "50,200",
            }
        )
        with patch("fundamentals.get_company_fundamentals", return_value=({}, {})):
            app.run(timeout=30)

        self.assertEqual(list(app.exception), [])

if __name__ == "__main__":
    unittest.main()
