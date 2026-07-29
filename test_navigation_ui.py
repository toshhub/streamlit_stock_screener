import unittest
import re
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

    def test_global_product_banner_is_removed(self):
        self.assertNotIn('key="app_hero_shell"', self.app_source)
        self.assertNotIn('class="app-hero__title"', self.app_source)
        self.assertNotIn('class="workflow-rail"', self.app_source)

    def test_every_tab_starts_with_its_workspace_banner(self):
        for tab_number in (1, 3, 4, 5, 6):
            self.assertRegex(
                self.app_source,
                re.compile(
                    rf"with tab{tab_number}:\n"
                    rf"(?:    [^\n]+\n){{0,3}}"
                    rf"    render_workspace_banner\("
                ),
            )
        self.assertIn(
            "def render_screener_workspace():\n"
            "    fragment_fast_favorite_selection",
            self.app_source,
        )
        screener_fragment = self.app_source[
            self.app_source.index("def render_screener_workspace():"):
            self.app_source.index("with tab2:")
        ]
        self.assertLess(
            screener_fragment.index("render_workspace_banner("),
            screener_fragment.index("st.markdown("),
        )
        self.assertIn(
            "with tab2:\n"
            "    render_screener_workspace()",
            self.app_source,
        )

    def test_workspace_banner_contains_compact_account_controls(self):
        auth_source = Path("user_auth.py").read_text(encoding="utf-8")

        self.assertIn(
            'with st.container(key=f"workspace_banner_shell_{tone}")',
            self.app_source,
        )
        self.assertIn(
            "render_workspace_account_controls(",
            self.app_source,
        )
        self.assertIn("Signed in", auth_source)
        self.assertIn('"Log out"', auth_source)
        self.assertIn("workspace_sign_out_", auth_source)

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

    def test_fixed_navigation_reserves_space_before_all_page_content(self):
        self.assertIn("--primary-nav-top:", self.app_source)
        self.assertIn("--primary-nav-height:", self.app_source)
        self.assertIn("--primary-nav-clearance:", self.app_source)
        self.assertIn("top: var(--primary-nav-top);", self.app_source)
        self.assertIn(
            "var(--primary-nav-height)\n"
            "            + var(--primary-nav-clearance)",
            self.app_source,
        )
        self.assertNotIn("padding-top: 1.25rem;", self.app_source)


if __name__ == "__main__":
    unittest.main()
