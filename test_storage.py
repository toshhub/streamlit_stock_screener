import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from storage import configure_user_storage, load_settings, update_settings


class SettingsPersistenceTests(unittest.TestCase):
    def tearDown(self):
        configure_user_storage(None, None)

    def test_backtest_update_preserves_existing_settings_and_writes_values(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            settings_path = Path(temp_dir) / "session_settings.json"
            legacy_path = Path(temp_dir) / "app_settings.json"
            settings_path.write_text(json.dumps({"market": "INDIA"}), encoding="utf-8")

            with (
                patch("storage.SETTINGS_FILE", settings_path),
                patch("storage.LEGACY_SETTINGS_FILE", legacy_path),
            ):
                update_settings({
                    "backtest_selected_filters": ["50 Support"],
                    "backtest_target_expression": "10%",
                    "backtest_stop_loss_expression": "SMA50-1%",
                    "backtest_closing_basis": False,
                    "backtest_green_candle_only": True,
                })

            stored = json.loads(settings_path.read_text(encoding="utf-8"))

        self.assertEqual(stored["market"], "INDIA")
        self.assertEqual(stored["backtest_selected_filters"], ["50 Support"])
        self.assertEqual(stored["backtest_target_expression"], "10%")
        self.assertEqual(stored["backtest_stop_loss_expression"], "SMA50-1%")
        self.assertFalse(stored["backtest_closing_basis"])
        self.assertTrue(stored["backtest_green_candle_only"])

    def test_authenticated_settings_are_loaded_and_updated_in_cloud(self):
        class FakeUserStorage:
            def __init__(self):
                self.settings = {"google-user": {"theme": "dark"}}

            def load_settings(self, user_id):
                return dict(self.settings.get(user_id, {}))

            def update_settings(self, user_id, updates):
                self.settings.setdefault(user_id, {}).update(updates)
                return dict(self.settings[user_id])

        backend = FakeUserStorage()
        with tempfile.TemporaryDirectory() as temp_dir:
            settings_path = Path(temp_dir) / "session_settings.json"
            legacy_path = Path(temp_dir) / "app_settings.json"
            settings_path.write_text(json.dumps({"market": "INDIA"}), encoding="utf-8")

            with (
                patch("storage.SETTINGS_FILE", settings_path),
                patch("storage.LEGACY_SETTINGS_FILE", legacy_path),
            ):
                configure_user_storage(backend, "google-user")
                loaded = load_settings()
                update_settings({"selected_favorite_filter_set": "My setup"})

        self.assertEqual(loaded["market"], "INDIA")
        self.assertEqual(loaded["theme"], "dark")
        self.assertEqual(
            backend.settings["google-user"]["selected_favorite_filter_set"],
            "My setup",
        )


if __name__ == "__main__":
    unittest.main()
