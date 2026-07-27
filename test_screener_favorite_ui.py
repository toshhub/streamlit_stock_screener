import unittest
from pathlib import Path


class ScreenerFavoriteUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app_source = (Path(__file__).parent / "app.py").read_text(
            encoding="utf-8"
        )

    def test_redundant_favorite_and_remove_last_controls_are_gone(self):
        for retired_label in (
            "− Remove Last",
            "⭐ Favorite Sets",
            "⭐ Save Favorite",
            "Remove Favorite",
            "Save Current Set",
            "Remove Saved Set",
        ):
            self.assertNotIn(retired_label, self.app_source)

    def test_custom_save_and_personal_strategy_remove_are_inline(self):
        self.assertIn('key="save_custom_strategy_popover"', self.app_source)
        self.assertIn('key="save_custom_strategy"', self.app_source)
        self.assertIn('key="remove_selected_saved_strategy"', self.app_source)
        self.assertIn(
            "is_personal_strategy = selected_fav in personal_favorite_keys",
            self.app_source,
        )


if __name__ == "__main__":
    unittest.main()
