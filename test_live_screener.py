import unittest
from pathlib import Path


class LiveScreenerPipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = (Path(__file__).parent / "app.py").read_text(
            encoding="utf-8"
        )

    def test_match_is_emitted_before_optional_chart_generation(self):
        match_event = self.source.index('"type": "match"')
        chart_phase = self.source.index('"phase": "charts"')
        self.assertLess(match_event, chart_phase)

    def test_screener_workspace_uses_fragment_reruns_for_fast_selection(self):
        self.assertIn(
            "@st.fragment\n"
            "def render_screener_workspace():",
            self.source,
        )
        self.assertIn(
            'st.session_state["_fast_favorite_selection"] = True',
            self.source,
        )
        self.assertIn(
            "if not fragment_fast_favorite_selection:",
            self.source,
        )
        self.assertIn(
            'on_change="ignore"',
            self.source,
        )

    def test_live_results_do_not_trigger_synchronous_repairs(self):
        self.assertIn(
            "not live_job_running\n"
            "            and result_metadata_for_repair.get(\"create_charts\")",
            self.source,
        )
        self.assertNotIn("repair_result_fundamentals(rows", self.source)

    def test_default_live_worker_pool_is_twelve(self):
        self.assertIn(
            'os.environ.get("SCREENER_MAX_WORKERS", "12")',
            self.source,
        )

    def test_progress_stays_in_screener_until_background_job_completes(self):
        run_section = self.source[
            self.source.index("# ===== RUN SCREENER LOGIC ====="):
            self.source.index("with tab2:")
        ]
        self.assertIn(
            "@st.fragment(run_every=0.75)\n"
            "def render_active_screener_progress():",
            self.source,
        )
        self.assertIn(
            'st.rerun(scope="fragment")',
            run_section,
        )
        self.assertNotIn(
            'st.session_state["switch_to_results_tab"]',
            run_section,
        )
        self.assertIn(
            'st.session_state["switch_to_results_tab"] = True',
            self.source[
                self.source.index("def render_active_screener_progress():"):
                self.source.index("def chart_file_needs_regeneration")
            ],
        )

    def test_progress_region_is_rendered_above_run_button(self):
        quick_run_section = self.source[
            self.source.index(
                "active_screener_job = attach_registered_screener_job()"
            ):
            self.source.index(
                "# Read current_filter_set from session state"
            )
        ]
        self.assertLess(
            quick_run_section.index(
                "screener_progress_placeholder = st.empty()"
            ),
            quick_run_section.index(
                'run_combined = st.button('
            ),
        )

    def test_results_are_hidden_and_do_not_force_reruns_while_screening(self):
        results_section = self.source[
            self.source.index("# TAB 4: RESULTS"):
            self.source.index("# TAB 5: PRICE ALERTS")
        ]
        self.assertIn(
            "rows\n"
            "        and not live_job_running",
            results_section,
        )
        self.assertNotIn("time.sleep(0.75)", results_section)
        self.assertNotIn(
            'st.session_state["switch_to_results_tab"] = True',
            results_section,
        )

    def test_background_job_can_be_recovered_without_persisting_results(self):
        self.assertIn("SCREENER_JOBS[owner_key] = job", self.source)
        self.assertNotIn("completion_callback", self.source)
        self.assertNotIn("persist_user_results", self.source)
        self.assertNotIn("load_results(", self.source)
        self.assertNotIn("save_results(", self.source)


if __name__ == "__main__":
    unittest.main()
