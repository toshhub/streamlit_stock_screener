import streamlit as _st

from streamlit_filter_proxy import st as _base_st


_MOBILE_STYLES_INJECTED = False


def _inject_mobile_filter_styles():
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

            div[data-testid="stHorizontalBlock"]:has([class*="st-key-filter_card_"]) {
                display: flex !important;
                flex-direction: row !important;
                flex-wrap: nowrap !important;
                gap: 0.4rem !important;
                width: 100% !important;
                max-width: 100% !important;
                overflow: hidden !important;
            }

            div[data-testid="stHorizontalBlock"]:has([class*="st-key-filter_card_"])
                > div[data-testid="column"] {
                flex: 0 0 calc(50% - 0.2rem) !important;
                width: calc(50% - 0.2rem) !important;
                min-width: 0 !important;
                max-width: calc(50% - 0.2rem) !important;
            }

            div[class*="st-key-filter_card_"] button {
                min-height: 70px !important;
                padding: 0.5rem 0.35rem !important;
            }

            div[class*="st-key-filter_card_"] button p {
                font-size: 0.72rem !important;
                line-height: 1.14 !important;
                overflow-wrap: anywhere !important;
            }

            div[data-testid="stHorizontalBlock"]:has(.filter-tone-marker) {
                display: flex !important;
                flex-direction: column !important;
                flex-wrap: nowrap !important;
                gap: 0.55rem !important;
                width: 100% !important;
                max-width: 100% !important;
            }

            div[data-testid="stHorizontalBlock"]:has(.filter-tone-marker)
                > div[data-testid="column"] {
                flex: 0 0 100% !important;
                width: 100% !important;
                min-width: 0 !important;
                max-width: 100% !important;
            }

            div[data-testid="stHorizontalBlock"]:has(.filter-tone-marker)
                > div[data-testid="column"] [data-testid="stExpander"] {
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
    def __getattr__(self, name):
        return getattr(_base_st, name)

    def columns(self, spec, *args, **kwargs):
        return _base_st.columns(spec, *args, **kwargs)

    def selectbox(self, label, options, *args, **kwargs):
        if label == "Filter Category":
            _inject_mobile_filter_styles()
        return _base_st.selectbox(label, options, *args, **kwargs)

    def expander(self, label, *args, **kwargs):
        if str(label).split(".", 1)[0].isdigit():
            _inject_mobile_filter_styles()
        return _base_st.expander(label, *args, **kwargs)

    def button(self, label, *args, **kwargs):
        return _base_st.button(label, *args, **kwargs)


st = MobileFilterProxy()
