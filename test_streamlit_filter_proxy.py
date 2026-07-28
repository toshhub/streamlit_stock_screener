import unittest
from pathlib import Path


class SavedStrategyTooltipTests(unittest.TestCase):
    def test_saved_strategy_cards_do_not_define_hover_help(self):
        source = (Path(__file__).parent / "streamlit_filter_proxy.py").read_text(
            encoding="utf-8"
        )
        saved_strategy_section = source[
            source.index('if "Filter Set To Run" in str(label):'):
            source.index('if label != "Filter Category":')
        ]

        self.assertNotIn("Load and run the", saved_strategy_section)
        self.assertNotIn("Selected strategy:", saved_strategy_section)
        self.assertNotIn("Remove your saved strategy:", saved_strategy_section)


if __name__ == "__main__":
    unittest.main()
