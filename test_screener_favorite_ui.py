import unittest
from pathlib import Path

from streamlit.testing.v1 import AppTest


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
        self.assertIn(
            "from mobile_filter_proxy import st",
            self.app_source,
        )
        self.assertIn(
            'with st.popover(\n                    "Save Filters"',
            self.app_source,
        )
        self.assertIn('key="save_custom_strategy_popover"', self.app_source)
        self.assertIn('key="save_custom_strategy"', self.app_source)
        self.assertIn(
            "removable_options=personal_favorite_keys.keys()",
            self.app_source,
        )
        self.assertIn(
            "on_remove=request_saved_strategy_removal",
            self.app_source,
        )
        self.assertNotIn(
            '"removable_options" in inspect.signature(st.selectbox).parameters',
            self.app_source,
        )
        self.assertIn('@st.dialog("Remove saved strategy?")', self.app_source)
        self.assertIn('"Remove strategy"', self.app_source)
        self.assertIn(
            "This saved filter setup cannot be recovered.",
            self.app_source,
        )

        proxy_source = (
            Path(__file__).parent / "streamlit_filter_proxy.py"
        ).read_text(encoding="utf-8")
        self.assertIn('f"favorite_filter_remove_', proxy_source)
        self.assertIn("if option in removable_options:", proxy_source)
        self.assertIn(
            "with _st.container(\n                            key=_favorite_card_key",
            proxy_source,
        )

    def test_only_personal_strategy_card_renders_a_remove_button(self):
        test_app = AppTest.from_string(
            """
from streamlit_filter_proxy import st

def request_remove(name):
    st.session_state["removed"] = name

st.selectbox(
    "⭐ Filter Set To Run",
    ["100 Support", "200 Support"],
    key="favorite",
    removable_options=["100 Support"],
    on_remove=request_remove,
)
"""
        ).run(timeout=30)

        self.assertFalse(test_app.exception)
        labels = [button.label for button in test_app.button]
        self.assertEqual(labels.count("−"), 1)
        self.assertIn("☆  100 Support", labels)
        self.assertIn("☆  200 Support", labels)
        test_app.button(key="favorite_filter_remove_0_100_support").click().run()
        self.assertEqual(test_app.session_state["removed"], "100 Support")


if __name__ == "__main__":
    unittest.main()
