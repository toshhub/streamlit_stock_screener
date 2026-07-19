import re
from contextlib import AbstractContextManager

import streamlit as _st


_FILTER_LABEL_TONES = {
    "MA Rising": 0,
    "Short MA Above Long MA": 1,
    "Current Price Near And Above Long MA": 2,
    "Short MA Crossed Long MA - Golden Cross": 3,
    "Long MA Down From Recent Max": 4,
    "Long MA Up From Recent Min": 5,
    "PE < N": 6,
    "Hitting All Time High": 7,
    "Price Near Very Old ATH": 8,
}

_FILTER_COLORS = [
    ("#2563eb", "#eff6ff", "#bfdbfe"),
    ("#7c3aed", "#f5f3ff", "#ddd6fe"),
    ("#0891b2", "#ecfeff", "#a5f3fc"),
    ("#ea580c", "#fff7ed", "#fed7aa"),
    ("#dc2626", "#fef2f2", "#fecaca"),
    ("#16a34a", "#f0fdf4", "#bbf7d0"),
    ("#ca8a04", "#fefce8", "#fef08a"),
    ("#be123c", "#fff1f2", "#fecdd3"),
    ("#4f46e5", "#eef2ff", "#c7d2fe"),
]

_STYLES_INJECTED = False


def _slug(value):
    return re.sub(r"[^a-z0-9]+", "_", str(value).lower()).strip("_")


def _card_key(label):
    return f"filter_card_{_slug(label)}"


def _inject_styles():
    global _STYLES_INJECTED
    if _STYLES_INJECTED:
        return

    card_rules = []
    expander_rules = []
    for label, tone in _FILTER_LABEL_TONES.items():
        accent, background, border = _FILTER_COLORS[tone]
        key = _card_key(label)
        card_rules.append(
            f"""
            div[class*="st-key-{key}"] button {{
                min-height: 76px !important;
                width: 100% !important;
                justify-content: flex-start !important;
                padding: 0.8rem 0.9rem !important;
                border: 1px solid {border} !important;
                border-left: 6px solid {accent} !important;
                border-radius: 14px !important;
                background: {background} !important;
                color: #172033 !important;
                font-weight: 750 !important;
                line-height: 1.25 !important;
                text-align: left !important;
                box-shadow: 0 4px 12px rgba(15, 23, 42, 0.05) !important;
            }}
            div[class*="st-key-{key}"] button:hover {{
                border-color: {accent} !important;
                color: {accent} !important;
                transform: translateY(-1px);
            }}
            div[class*="st-key-{key}"] button p {{
                color: inherit !important;
                white-space: normal !important;
                text-align: left !important;
            }}
            """
        )
        expander_rules.append(
            f"""
            div[data-testid="stExpander"]:has(.filter-tone-{tone}) {{
                border: 1px solid {border} !important;
                border-left: 6px solid {accent} !important;
                border-radius: 14px !important;
                background: linear-gradient(90deg, {background} 0%, #ffffff 40%) !important;
                box-shadow: 0 5px 16px rgba(15, 23, 42, 0.05) !important;
            }}
            div[data-testid="stExpander"]:has(.filter-tone-{tone}) summary p {{
                color: {accent} !important;
                font-weight: 800 !important;
            }}
            """
        )

    _st.markdown(
        """
        <style>
        .filter-tone-marker { display: none !important; }

        /* Keep the add-filter and active-filter Streamlit columns side by side. */
        div[data-testid="stHorizontalBlock"]:has([class*="st-key-filter_card_"]),
        div[data-testid="stHorizontalBlock"]:has(.filter-tone-marker) {
            display: flex !important;
            flex-direction: row !important;
            flex-wrap: nowrap !important;
            align-items: stretch !important;
            gap: 0.7rem !important;
            width: 100% !important;
        }

        div[data-testid="stHorizontalBlock"]:has([class*="st-key-filter_card_"])
            > div[data-testid="column"],
        div[data-testid="stHorizontalBlock"]:has(.filter-tone-marker)
            > div[data-testid="column"] {
            flex: 1 1 0 !important;
            width: 0 !important;
            min-width: 0 !important;
            max-width: 50% !important;
        }

        div[data-testid="stHorizontalBlock"]:has([class*="st-key-filter_card_"])
            > div[data-testid="column"] .stButton,
        div[data-testid="stHorizontalBlock"]:has(.filter-tone-marker)
            > div[data-testid="column"] [data-testid="stExpander"] {
            width: 100% !important;
        }
        """
        + "".join(card_rules)
        + "".join(expander_rules)
        + "</style>",
        unsafe_allow_html=True,
    )
    _STYLES_INJECTED = True


class _ColoredExpander(AbstractContextManager):
    def __init__(self, delegate, tone):
        self._delegate = delegate
        self._tone = tone

    def __enter__(self):
        entered = self._delegate.__enter__()
        _st.markdown(
            f'<span class="filter-tone-marker filter-tone-{self._tone}"></span>',
            unsafe_allow_html=True,
        )
        return entered

    def __exit__(self, exc_type, exc_value, traceback):
        return self._delegate.__exit__(exc_type, exc_value, traceback)


class StreamlitFilterProxy:
    def __getattr__(self, name):
        return getattr(_st, name)

    def selectbox(self, label, options, *args, **kwargs):
        if label != "Filter Category":
            return _st.selectbox(label, options, *args, **kwargs)

        _inject_styles()
        options = list(options)
        if not options:
            return None

        format_func = kwargs.get("format_func") or (lambda value: value)
        selected = _st.session_state.get("_filter_card_selected", options[0])
        if selected not in options:
            selected = options[0]

        # Create one Streamlit row per pair. This gives true row-major ordering:
        # 1,2 on row one; 3,4 on row two; and so on.
        for row_start in range(0, len(options), 2):
            row_options = options[row_start:row_start + 2]
            columns = _st.columns(2, gap="small")
            for column_index, option in enumerate(row_options):
                display_label = str(format_func(option))
                with columns[column_index]:
                    clicked = _st.button(
                        f"＋  {display_label}",
                        key=_card_key(display_label),
                        use_container_width=True,
                        help=f"Add {display_label} to the current filter set.",
                    )
                if clicked:
                    selected = option
                    _st.session_state["_filter_card_selected"] = option
                    _st.session_state["_filter_card_add_clicked"] = True

        return selected

    def button(self, label, *args, **kwargs):
        if label == "➕ Add":
            return bool(_st.session_state.pop("_filter_card_add_clicked", False))
        return _st.button(label, *args, **kwargs)

    def expander(self, label, *args, **kwargs):
        match = re.match(r"^\d+\.\s+(.+)$", str(label))
        if not match:
            return _st.expander(label, *args, **kwargs)

        tone = _FILTER_LABEL_TONES.get(match.group(1))
        if tone is None:
            return _st.expander(label, *args, **kwargs)

        _inject_styles()
        return _ColoredExpander(_st.expander(label, *args, **kwargs), tone)


st = StreamlitFilterProxy()
