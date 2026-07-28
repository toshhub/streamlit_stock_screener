import unittest
from pathlib import Path


class NavigationUiTests(unittest.TestCase):
    def setUp(self):
        self.app_source = Path("app.py").read_text(encoding="utf-8")

    def test_primary_tabs_use_fixed_square_icon_navigation(self):
        self.assertIn("position: fixed;", self.app_source)
        self.assertIn('div.stTabs [role="tablist"]', self.app_source)
        self.assertIn("flex: 0 0 3.15rem;", self.app_source)
        self.assertIn('p::before { content: "📥"; }', self.app_source)
        self.assertIn('p::before { content: "🔔"; }', self.app_source)
        self.assertIn("font-size: 0 !important;", self.app_source)

    def test_workflow_is_rendered_inside_the_main_hero(self):
        hero_start = self.app_source.index(
            'with st.container(key="app_hero_shell"):'
        )
        next_function = self.app_source.index(
            "def sync_pattern_lookback_from_slider():",
            hero_start,
        )
        hero_source = self.app_source[hero_start:next_function]

        self.assertIn('class="workflow-rail"', hero_source)
        self.assertIn("Prepare data", hero_source)
        self.assertIn("Build a screen", hero_source)
        self.assertIn("Validate strategy", hero_source)
        self.assertIn("Review results", hero_source)

    def test_alert_badge_is_attached_to_alert_icon(self):
        self.assertIn(
            '[role="tablist"] > [role="tab"]:nth-child(6)::after',
            self.app_source,
        )

    def test_navigation_has_phone_and_landscape_layouts(self):
        self.assertIn("@media (max-width: 768px)", self.app_source)
        self.assertIn(
            "width: calc(100vw - 1rem);",
            self.app_source,
        )
        self.assertIn(
            "@media (orientation: landscape) and (max-height: 600px)",
            self.app_source,
        )
        self.assertIn("flex-basis: 2.45rem;", self.app_source)


if __name__ == "__main__":
    unittest.main()
