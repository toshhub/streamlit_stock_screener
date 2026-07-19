import re

import streamlit as st


_ORIGINAL_SELECTBOX = st.selectbox
_ORIGINAL_EXPANDER = st.expander
_ORIGINAL_MARKDOWN = st.markdown
_STYLES_INJECTED = False


def _inject_force_grid_styles():
    global _STYLES_INJECTED
    if _STYLES_INJECTED:
        return

    _ORIGINAL_MARKDOWN(
        """
        <style>
        /*
         * Streamlit collapses columns into vertical rows at narrower container
         * widths. These selectors identify only the Add Filter and Current
         * Filter Set rows, then keep their direct children in a 50/50 grid.
         */
        div[data-testid="stHorizontalBlock"]:has([class*="st-key-_filter_card_add_"]),
        div[data-testid="stHorizontalBlock"]:has(.filter-tone-marker) {
            display: flex !important;
            flex-direction: row !important;
            flex-wrap: nowrap !important;
            align-items: stretch !important;
            gap: 0.75rem !important;
            width: 100% !important;
        }

        div[data-testid="stHorizontalBlock"]:has([class*="st-key-_filter_card_add_"])
            > div[data-testid="column"],
        div[data-testid="stHorizontalBlock"]:has(.filter-tone-marker)
            > div[data-testid="column"] {
            flex: 1 1 0 !important;
            width: 0 !important;
            min-width: 0 !important;
            max-width: 50% !important;
        }

        div[data-testid="stHorizontalBlock"]:has([class*="st-key-_filter_card_add_"])
            > div[data-testid="column"] .stButton,
        div[data-testid="stHorizontalBlock"]:has(.filter-tone-marker)
            > div[data-testid="column"] [data-testid="stExpander"] {
            width: 100% !important;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )
    _STYLES_INJECTED = True


def _patched_selectbox(label, options, *args, **kwargs):
    if label == "Filter Category":
        _inject_force_grid_styles()
    return _ORIGINAL_SELECTBOX(label, options, *args, **kwargs)


def _patched_expander(label, *args, **kwargs):
    if re.match(r"^\d+\.\s+", str(label)):
        _inject_force_grid_styles()
    return _ORIGINAL_EXPANDER(label, *args, **kwargs)


def install_force_filter_grid():
    if getattr(st, "_force_filter_grid_installed", False):
        return
    st.selectbox = _patched_selectbox
    st.expander = _patched_expander
    st._force_filter_grid_installed = True
