import unittest
from pathlib import Path


class WatchlistUiTests(unittest.TestCase):
    def test_watchlist_rows_are_simple_and_directly_removable(self):
        app_source = Path("app.py").read_text(encoding="utf-8")

        self.assertIn('stock_header.markdown("**Stock**")', app_source)
        self.assertIn('remove_header.markdown("**Remove**")', app_source)
        self.assertIn('help=f"Remove {item_symbol}"', app_source)
        self.assertNotIn("Save order and notes", app_source)
        self.assertNotIn("Personal note (optional)", app_source)
        self.assertNotIn("watchlist_item_note_", app_source)
        self.assertNotIn("watchlist_order_", app_source)

    def test_chart_watchlist_add_is_validated_and_saved(self):
        app_source = Path("app.py").read_text(encoding="utf-8")

        self.assertIn("def add_chart_stock_to_watchlist(event):", app_source)
        self.assertIn('event.get("watchlistId", "")', app_source)
        self.assertIn("event_symbol != symbol.upper()", app_source)
        self.assertIn("cloud_store.save_watchlist_item(", app_source)
        self.assertIn(
            "watchlist_add_callback=add_chart_stock_to_watchlist",
            app_source,
        )


if __name__ == "__main__":
    unittest.main()
