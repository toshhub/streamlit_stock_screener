import streamlit as _st

from streamlit_filter_proxy import _is_screener_top_layout, st as _filter_st


_MOBILE_STYLES_INJECTED = False


def _inject_mobile_filter_styles():
    """Apply mobile-safe layout rules without replacing Streamlit widgets."""
    global _MOBILE_STYLES_INJECTED
    if _MOBILE_STYLES_INJECTED:
        return

    _st.markdown(
        """
        <style>
        @media (max-width: 768px) {
            html, body, [data-testid="stAppViewContainer"], .stApp,
            .stMain, .stMainBlockContainer {
                max-width: 100% !important;
                overflow-x: hidden !important;
            }

            div[data-testid="stHorizontalBlock"]:has([class*="st-key-filter_card_"]),
            div[data-testid="stHorizontalBlock"]:has([class*="st-key-favorite_filter_card_"]),
            div[data-testid="stHorizontalBlock"]:has(.filter-tone-marker) {
                display: flex !important;
                flex-direction: row !important;
                flex-wrap: nowrap !important;
                gap: 0.4rem !important;
                width: 100% !important;
                max-width: 100% !important;
                overflow: hidden !important;
            }

            div[data-testid="stHorizontalBlock"]:has([class*="st-key-filter_card_"])
                > div:is([data-testid="column"], [data-testid="stColumn"]),
            div[data-testid="stHorizontalBlock"]:has([class*="st-key-favorite_filter_card_"])
                > div:is([data-testid="column"], [data-testid="stColumn"]),
            div[data-testid="stHorizontalBlock"]:has(.filter-tone-marker)
                > div:is([data-testid="column"], [data-testid="stColumn"]) {
                flex: 0 0 calc(50% - 0.2rem) !important;
                width: calc(50% - 0.2rem) !important;
                min-width: 0 !important;
                max-width: calc(50% - 0.2rem) !important;
            }

            div[class*="st-key-favorite_filter_card_"] button {
                min-height: 54px !important;
                padding: 0.5rem 0.35rem !important;
            }

            div[class*="st-key-favorite_filter_card_"] button p {
                font-size: 0.72rem !important;
                line-height: 1.14 !important;
                overflow-wrap: anywhere !important;
            }

            div[data-testid="stHorizontalBlock"]:has(.filter-tone-marker)
                > div:is([data-testid="column"], [data-testid="stColumn"]) [data-testid="stExpander"] {
                width: 100% !important;
                max-width: 100% !important;
                margin: 0 !important;
            }

            div[data-testid="stHorizontalBlock"]:has(.filter-tone-marker)
                [data-testid="stExpander"] summary p {
                font-size: 0.84rem !important;
                line-height: 1.2 !important;
                white-space: normal !important;
                overflow-wrap: anywhere !important;
            }
        }
        </style>
        """,
        unsafe_allow_html=True,
    )
    _MOBILE_STYLES_INJECTED = True


class MobileFilterProxy:
    """Use card widgets only for the Screener filter builder.

    All unrelated widgets continue to call Streamlit directly. This preserves
    native interactions elsewhere while retaining the established card-based
    Screener UI.
    """

    def __getattr__(self, name):
        return getattr(_st, name)

    def columns(self, spec, *args, **kwargs):
        _inject_mobile_filter_styles()
        if _is_screener_top_layout(spec):
            return _filter_st.columns(spec, *args, **kwargs)
        return _st.columns(spec, *args, **kwargs)

    def selectbox(self, label, options, *args, **kwargs):
        _inject_mobile_filter_styles()
        if label == "Filter Category" or "Filter Set To Run" in str(label):
            return _filter_st.selectbox(label, options, *args, **kwargs)
        return _st.selectbox(label, options, *args, **kwargs)

    def expander(self, label, *args, **kwargs):
        _inject_mobile_filter_styles()
        if str(label).split(".", 1)[0].isdigit():
            return _filter_st.expander(label, *args, **kwargs)
        return _st.expander(label, *args, **kwargs)

    def button(self, label, *args, **kwargs):
        if label == "➕ Add":
            return _filter_st.button(label, *args, **kwargs)
        return _st.button(label, *args, **kwargs)


st = MobileFilterProxy()
