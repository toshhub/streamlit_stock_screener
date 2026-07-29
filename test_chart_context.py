import unittest
from urllib.parse import parse_qs, urlparse

from chart_context import (
    active_alert_markers,
    chart_alert_context,
    interactive_chart_query,
)


class InteractiveChartContextTests(unittest.TestCase):
    def test_active_alert_markers_filter_by_user_snapshot_symbol_and_market(self):
        alerts = [
            {
                "id": "one",
                "status": "Active",
                "symbol": "test.ns",
                "market": "india",
                "target_price": 125,
                "direction": "above",
                "created_candle_date": "2026-07-28",
            },
            {
                "id": "two",
                "status": "Active",
                "symbol": "TEST",
                "market": "INDIA",
                "target_price": 95,
                "direction": "below",
                "created_at": "2026-07-27T12:30:00+05:30",
            },
            {
                "id": "triggered",
                "status": "Triggered",
                "symbol": "TEST",
                "market": "INDIA",
                "target_price": 130,
                "direction": "above",
                "created_candle_date": "2026-07-28",
            },
            {
                "id": "other",
                "status": "Active",
                "symbol": "OTHER",
                "market": "INDIA",
                "target_price": 50,
                "direction": "above",
                "created_candle_date": "2026-07-28",
            },
        ]

        markers = active_alert_markers(alerts, "TEST", "INDIA")

        self.assertEqual(
            markers,
            [
                {
                    "id": "two",
                    "date": "2026-07-27",
                    "price": 95.0,
                    "direction": "below",
                },
                {
                    "id": "one",
                    "date": "2026-07-28",
                    "price": 125.0,
                    "direction": "above",
                },
            ],
        )

    def test_active_selected_alert_is_not_duplicated(self):
        overlay, markers = chart_alert_context(
            [{
                "id": "one",
                "status": "Active",
                "symbol": "TEST",
                "market": "INDIA",
                "target_price": 125,
                "direction": "above",
                "created_candle_date": "2026-07-28",
            }],
            "TEST",
            "INDIA",
            {
                "alertDate": "2026-07-28T12:30:00+05:30",
                "alertPrice": 125,
            },
        )

        self.assertNotIn("alertDate", overlay)
        self.assertNotIn("alertPrice", overlay)
        self.assertEqual(len(markers), 1)

    def test_shared_query_builder_carries_chart_and_trade_context(self):
        href = interactive_chart_query(
            "TEST",
            "india",
            ma_periods=[50, 200],
            pe_ratio=20.5,
            embedded=True,
            initial_range="all",
            trade_overlay={
                "buyDate": "2026-01-02",
                "targetPrice": 125,
                "alertDate": "2026-07-28",
                "alertPrice": 120,
            },
        )
        query = parse_qs(urlparse(href).query)

        self.assertEqual(query["interactive_chart"], ["TEST"])
        self.assertEqual(query["market"], ["INDIA"])
        self.assertEqual(query["ma"], ["50,200"])
        self.assertEqual(query["embedded"], ["1"])
        self.assertEqual(query["range"], ["all"])
        self.assertEqual(query["buy_date"], ["2026-01-02"])
        self.assertEqual(query["target_price"], ["125"])
        self.assertEqual(query["alert_marker_price"], ["120"])


if __name__ == "__main__":
    unittest.main()
