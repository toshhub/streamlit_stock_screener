import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from price_alerts import (
    check_price_alerts_for_symbol,
    create_price_alert,
    load_price_alerts,
    remove_price_alerts,
    sort_price_alerts,
)


class PriceAlertTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.alert_file = Path(self.temp_dir.name) / "price_alerts.json"
        self.stock_file = Path(self.temp_dir.name) / "TEST.json"
        self.file_patch = patch("price_alerts.PRICE_ALERTS_FILE", self.alert_file)
        self.file_patch.start()

    def tearDown(self):
        self.file_patch.stop()
        self.temp_dir.cleanup()

    def _write_candles(self, rows):
        self.stock_file.write_text(json.dumps(rows), encoding="utf-8")

    def test_duplicate_alert_is_not_created_twice(self):
        first, first_created = create_price_alert(
            "TEST", "INDIA", 110, current_price=100, current_candle_date="2026-01-01"
        )
        second, second_created = create_price_alert(
            "TEST", "INDIA", 110, current_price=100, current_candle_date="2026-01-01"
        )

        self.assertTrue(first_created)
        self.assertFalse(second_created)
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(len(load_price_alerts()), 1)

    def test_cross_above_triggers_once_across_repeated_checks(self):
        create_price_alert(
            "TEST", "INDIA", 110, current_price=100, current_candle_date="2026-01-01"
        )
        self._write_candles([
            {"Date": "2026-01-01", "Open": 98, "High": 105, "Low": 97, "Close": 100},
            {"Date": "2026-01-02", "Open": 101, "High": 112, "Low": 100, "Close": 111},
        ])

        first = check_price_alerts_for_symbol("TEST", "INDIA", self.stock_file)
        second = check_price_alerts_for_symbol("TEST", "INDIA", self.stock_file)

        self.assertEqual(len(first), 1)
        self.assertEqual(second, [])
        self.assertEqual(load_price_alerts()[0]["status"], "Triggered")
        self.assertEqual(load_price_alerts()[0]["triggered_candle_date"], "2026-01-02")

    def test_cross_below_uses_future_candle_low(self):
        alert, created = create_price_alert(
            "TEST", "US", 90, current_price=100, current_candle_date="2026-01-01"
        )
        self._write_candles([
            {"Date": "2026-01-01", "Open": 100, "High": 101, "Low": 85, "Close": 100},
            {"Date": "2026-01-02", "Open": 98, "High": 99, "Low": 89, "Close": 91},
        ])

        triggered = check_price_alerts_for_symbol("TEST", "US", self.stock_file)

        self.assertTrue(created)
        self.assertEqual(alert["direction"], "below")
        self.assertEqual(len(triggered), 1)

    def test_alert_can_be_removed(self):
        alert, _ = create_price_alert(
            "TEST", "INDIA", 110, current_price=100, current_candle_date="2026-01-01"
        )
        self.assertEqual(remove_price_alerts([alert["id"]]), 1)
        self.assertEqual(load_price_alerts(), [])

    def test_alerts_sort_active_first_then_triggered_by_trigger_date(self):
        alerts = [
            {
                "id": "triggered-old",
                "status": "Triggered",
                "created_at": "2026-07-20T10:00:00+05:30",
                "triggered_candle_date": "2026-07-21",
                "triggered_at": "2026-07-21T16:00:00+05:30",
            },
            {
                "id": "active-old",
                "status": "Active",
                "created_at": "2026-07-20T10:00:00+05:30",
            },
            {
                "id": "triggered-new",
                "status": "Triggered",
                "created_at": "2026-07-18T10:00:00+05:30",
                "triggered_candle_date": "2026-07-22",
                "triggered_at": "2026-07-22T16:00:00+05:30",
            },
            {
                "id": "active-new",
                "status": "Active",
                "created_at": "2026-07-22T10:00:00+05:30",
            },
        ]

        ordered = sort_price_alerts(alerts)

        self.assertEqual(
            [alert["id"] for alert in ordered],
            ["active-new", "active-old", "triggered-new", "triggered-old"],
        )


if __name__ == "__main__":
    unittest.main()
