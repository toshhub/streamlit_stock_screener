import unittest
from pathlib import Path


class CursorAlertComponentTests(unittest.TestCase):
    def test_nested_chart_iframe_allows_fullscreen(self):
        source = (
            Path(__file__).parent
            / "cursor_alert_component"
            / "index.html"
        ).read_text(encoding="utf-8")

        self.assertIn('allow="fullscreen; screen-orientation"', source)
        self.assertIn("allowfullscreen", source)
        self.assertIn("webkitallowfullscreen", source)


if __name__ == "__main__":
    unittest.main()
