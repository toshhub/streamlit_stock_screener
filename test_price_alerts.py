import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from price_alerts import (
    acknowledge_price_alerts,
    check_price_alerts_for_market_candles,
    check_price_alerts_for_symbol,
    configure_cloud_alerts,
    create_price_alert,
    load_price_alerts,
    refresh_price_alerts_from_cache,
    remove_price_alerts,
    set_current_alert_user,
    sort_price_alerts,
)


class PriceAlertTests(unittest.TestCase):
    def setUp(self):
        configure_cloud_alerts(None, require_auth=False)
        set_current_alert_user(None)
        self.temp_dir = tempfile.TemporaryDirectory()
        self.alert_file = Path(self.temp_dir.name) / "price_alerts.json"
        self.stock_file = Path(self.temp_dir.name) / "TEST.json"
        self.file_patch = patch("price_alerts.PRICE_ALERTS_FILE", self.alert_file)
        self.file_patch.start()

    def tearDown(self):
        configure_cloud_alerts(None, require_auth=False)
        set_current_alert_user(None)
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
        self.assertFalse(load_price_alerts()[0]["acknowledged"])

    def test_triggered_alert_can_be_acknowledged(self):
        alert, _ = create_price_alert(
            "TEST", "INDIA", 110, current_price=100, current_candle_date="2026-01-01"
        )
        self._write_candles([
            {"Date": "2026-01-01", "Open": 98, "High": 105, "Low": 97, "Close": 100},
            {"Date": "2026-01-02", "Open": 101, "High": 112, "Low": 100, "Close": 111},
        ])
        check_price_alerts_for_symbol("TEST", "INDIA", self.stock_file)

        acknowledged = acknowledge_price_alerts([alert["id"]])
        stored = load_price_alerts()[0]

        self.assertEqual(acknowledged, 1)
        self.assertTrue(stored["acknowledged"])
        self.assertTrue(stored["acknowledged_at"])
        self.assertEqual(acknowledge_price_alerts([alert["id"]]), 0)

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

    def test_guest_cannot_create_alert_when_accounts_are_required(self):
        configure_cloud_alerts(object(), require_auth=True)

        with self.assertRaises(PermissionError):
            create_price_alert(
                "TEST", "INDIA", 110, current_price=100, current_candle_date="2026-01-01"
            )

    def test_guest_cannot_remove_alerts_when_accounts_are_required(self):
        configure_cloud_alerts(object(), require_auth=True)

        with self.assertRaises(PermissionError):
            remove_price_alerts(["guest-alert"])

    def test_guest_cannot_acknowledge_alerts_when_accounts_are_required(self):
        configure_cloud_alerts(object(), require_auth=True)

        with self.assertRaises(PermissionError):
            acknowledge_price_alerts(["guest-alert"])

    def test_cloud_alerts_are_scoped_to_the_current_user(self):
        class FakeCloudAlerts:
            def __init__(self):
                self.rows = {}

            def load_alerts(self, user_id):
                return list(self.rows.get(user_id, {}).values())

            def create_alert(self, user_id, alert):
                user_rows = self.rows.setdefault(user_id, {})
                if alert["id"] in user_rows:
                    return dict(user_rows[alert["id"]]), False
                user_rows[alert["id"]] = dict(alert)
                return dict(alert), True

        backend = FakeCloudAlerts()
        configure_cloud_alerts(backend, require_auth=True)
        set_current_alert_user("google-user-a")
        first, created = create_price_alert(
            "TEST", "INDIA", 110, current_price=100, current_candle_date="2026-01-01"
        )

        set_current_alert_user("google-user-b")
        self.assertEqual(load_price_alerts(), [])
        second, second_created = create_price_alert(
            "TEST", "INDIA", 110, current_price=100, current_candle_date="2026-01-01"
        )

        self.assertTrue(created)
        self.assertTrue(second_created)
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(len(backend.rows["google-user-a"]), 1)
        self.assertEqual(len(backend.rows["google-user-b"]), 1)

    def test_cron_evaluates_all_market_alerts_in_one_cloud_batch(self):
        class FakeCronAlerts:
            def __init__(self):
                self.load_calls = []
                self.updated = []

            def load_active_alerts_for_market(self, market):
                self.load_calls.append(market)
                return [{
                    "user_id": "user-1",
                    "id": "alert-1",
                    "symbol": "TEST",
                    "market": "INDIA",
                    "target_price": 110,
                    "direction": "above",
                    "status": "Active",
                    "created_candle_date": "2026-01-01",
                    "last_checked_date": "2026-01-01",
                }]

            def update_alerts(self, alerts):
                self.updated.extend(alerts)

        backend = FakeCronAlerts()
        configure_cloud_alerts(backend, require_auth=True)
        candles = pd.DataFrame([
            {
                "Symbol": "TEST",
                "Date": "2026-01-01",
                "Open": 98,
                "High": 105,
                "Low": 97,
                "Close": 100,
            },
            {
                "Symbol": "TEST",
                "Date": "2026-01-02",
                "Open": 101,
                "High": 112,
                "Low": 100,
                "Close": 111,
            },
        ])

        triggered = check_price_alerts_for_market_candles(candles, "INDIA")

        self.assertEqual(backend.load_calls, ["INDIA"])
        self.assertEqual([row["id"] for row in triggered], ["alert-1"])
        self.assertEqual(len(backend.updated), 1)
        self.assertEqual(backend.updated[0]["status"], "Triggered")
        self.assertEqual(
            backend.updated[0]["triggered_candle_date"],
            "2026-01-02",
        )

    def test_manual_refresh_checks_only_current_users_alerts_from_cache(self):
        class FakeUserAlerts:
            def __init__(self):
                self.updated_user = None
                self.updated = []

            def load_alerts(self, user_id):
                self.loaded_user = user_id
                return [{
                    "id": "alert-1",
                    "symbol": "TEST",
                    "market": "INDIA",
                    "target_price": 110,
                    "direction": "above",
                    "status": "Active",
                    "created_candle_date": "2026-01-01",
                    "last_checked_date": "2026-01-01",
                }]

            def update_user_alerts(self, user_id, alerts):
                self.updated_user = user_id
                self.updated.extend(alerts)

        self._write_candles([
            {"Date": "2026-01-01", "Open": 98, "High": 105, "Low": 97, "Close": 100},
            {"Date": "2026-01-02", "Open": 101, "High": 112, "Low": 100, "Close": 111},
        ])
        backend = FakeUserAlerts()
        configure_cloud_alerts(backend, require_auth=True)
        set_current_alert_user("google-user-a")

        with patch("price_alerts._stock_file", return_value=self.stock_file):
            result = refresh_price_alerts_from_cache()

        self.assertEqual(backend.loaded_user, "google-user-a")
        self.assertEqual(backend.updated_user, "google-user-a")
        self.assertEqual(result["active_alerts"], 1)
        self.assertEqual(len(result["triggered"]), 1)
        self.assertEqual(backend.updated[0]["status"], "Triggered")
        self.assertEqual(
            backend.updated[0]["triggered_candle_date"],
            "2026-01-02",
        )


class AlertTabPerformanceTests(unittest.TestCase):
    def test_alert_tab_has_manual_server_cache_refresh(self):
        app_source = Path("app.py").read_text(encoding="utf-8")

        self.assertIn('"Refresh Alerts"', app_source)
        self.assertIn("refresh_price_alerts_from_cache()", app_source)

    def test_alert_tab_reuses_session_snapshot_without_click_rerun(self):
        app_source = Path("app.py").read_text(encoding="utf-8")

        self.assertIn("def session_price_alerts(max_age_seconds=60):", app_source)
        self.assertIn("price_alerts_snapshot = session_price_alerts()", app_source)
        self.assertIn("alerts = session_price_alerts()", app_source)
        self.assertLess(
            app_source.index("def session_price_alerts(max_age_seconds=60):"),
            app_source.index("def run_interactive_chart_view():"),
        )
        self.assertIn(
            "trade_overlay, alert_markers = chart_alert_context(",
            app_source,
        )
        self.assertIn("alert_markers=alert_markers", app_source)
        self.assertNotIn("refresh_alerts_when_tab_is_clicked()", app_source)
        self.assertNotIn("alerts_refresh_trigger", app_source)

    def test_inline_alert_actions_are_consumed_in_the_alerts_tab(self):
        app_source = Path("app.py").read_text(encoding="utf-8")

        self.assertIn("action_event = sortable_results_table(", app_source)
        self.assertIn(
            "static_chart_path = alert_static_chart_path(",
            app_source,
        )
        self.assertIn("generate=generate_static_charts", app_source)
        self.assertIn("valuation_rows = hydrate_result_valuations(", app_source)
        self.assertIn('"PE Ratio": pe_ratio', app_source)
        self.assertIn('"ValuationMedians": valuation.get(', app_source)
        self.assertIn("pe_ratio=pe_ratio", app_source)
        self.assertIn(
            "== MAIN_TAB_LABELS[ALERTS_TAB_INDEX]",
            app_source,
        )
        self.assertIn('"ChartPath": static_chart_path', app_source)
        self.assertIn(
            'st.session_state.setdefault("_alert_static_chart_paths", {})',
            app_source,
        )
        self.assertIn("run_alert_row_action(*action_map[action_key])", app_source)
        self.assertIn('st.rerun()', app_source)
        self.assertNotIn('key="alert_action_bridge"', app_source)


if __name__ == "__main__":
    unittest.main()
