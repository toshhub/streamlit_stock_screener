import streamlit as _st


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

            div[data-testid="stHorizontalBlock"] {
                max-width: 100% !important;
            }

            div[data-testid="stHorizontalBlock"]
                > div:is([data-testid="column"], [data-testid="stColumn"]) {
                min-width: 0 !important;
            }
        }
        </style>
        """,
        unsafe_allow_html=True,
    )
    _MOBILE_STYLES_INJECTED = True


class MobileFilterProxy:
    """Transparent Streamlit proxy with only mobile-safe styling hooks.

    The previous proxy routed every widget through the filter-card proxy. That
    changed core Streamlit behaviour outside the filter builder, preventing
    buttons, searchable selectboxes, and custom-component tables from working
    reliably. Keep native Streamlit widgets everywhere and discard the two
    filter-card-only keyword arguments when the saved-strategy selector is
    rendered.
    """

    def __getattr__(self, name):
        return getattr(_st, name)

    def columns(self, spec, *args, **kwargs):
        _inject_mobile_filter_styles()
        return _st.columns(spec, *args, **kwargs)

    def selectbox(self, label, options, *args, **kwargs):
        _inject_mobile_filter_styles()
        kwargs.pop("removable_options", None)
        kwargs.pop("on_remove", None)
        return _st.selectbox(label, options, *args, **kwargs)

    def expander(self, label, *args, **kwargs):
        _inject_mobile_filter_styles()
        return _st.expander(label, *args, **kwargs)

    def button(self, label, *args, **kwargs):
        return _st.button(label, *args, **kwargs)


st = MobileFilterProxy()
