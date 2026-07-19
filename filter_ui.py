import re
from contextlib import AbstractContextManager

import streamlit as st


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

_ORIGINAL_SELECTBOX = st.selectbox
_ORIGINAL_BUTTON = st.button
_ORIGINAL_EXPANDER = st.expander
_ORIGINAL_MARKDOWN = st.markdown
_ORIGINAL_CONTAINER = st.container


def _slug(value):
    return re.sub(r"[^a-z0-9]+", "_", str(value).lower()).strip("_")


def _inject_filter_styles():
    card_rules = []
    expander_rules = []
    for label, tone in _FILTER_LABEL_TONES.items():
        accent, background, border = _FILTER_COLORS[tone]
        card_key = f"filter_card_{_slug(label)}"
        card_rules.append(
            f"""
            .st-key-{card_key} button {{
                min-height: 74px;
                justify-content: flex-start;
                padding: 0.8rem 0.9rem;
                border: 1px solid {border};
                border-left: 5px solid {accent};
                border-radius: 14px;
                background: {background};
                color: #172033;
                font-weight: 750;
                line-height: 1.25;
                text-align: left;
                box-shadow: 0 4px 12px rgba(15, 23, 42, 0.05);
            }}
            .st-key-{card_key} button:hover {{
                border-color: {accent};
                background: {background};
                color: {accent};
                transform: translateY(-1px);
            }}
            .st-key-{card_key} button p {{
                color: inherit !important;
                white-space: normal;
            }}
            """
        )
        expander_rules.append(
            f"""
            div[data-testid="stExpander"]:has(.filter-tone-{tone}) {{
                border: 1px solid {border};
                border-left: 6px solid {accent};
                border-radius: 14px;
                background: linear-gradient(90deg, {background} 0%, #ffffff 34%);
                box-shadow: 0 5px 16px rgba(15, 23, 42, 0.05);
            }}
            div[data-testid="stExpander"]:has(.filter-tone-{tone}) summary p {{
                color: {accent} !important;
                font-weight: 800;
            }}
            """
        )

    _ORIGINAL_MARKDOWN(
        """
        <style>
        .st-key-filter_card_grid [data-testid="stHorizontalBlock"] {
            row-gap: 0.55rem;
        }
        .st-key-filter_card_grid .stButton {
            margin-bottom: 0.25rem;
        }
        .filter-tone-marker {
            display: none;
        }
        """
        + "".join(card_rules)
        + "".join(expander_rules)
        + "</style>",
        unsafe_allow_html=True,
    )


def _keyed_container(key):
    try:
        return _ORIGINAL_CONTAINER(key=key)
    except TypeError:
        return _ORIGINAL_CONTAINER()


def _filter_card_selectbox(label, options, *args, **kwargs):
    options = list(options)
    if not options:
        return None

    format_func = kwargs.get("format_func") or (lambda value: value)
    selected = st.session_state.get("_filter_card_selected", options[0])
    if selected not in options:
        selected = options[0]

    with _keyed_container("filter_card_grid"):
        columns = st.columns(2)
        for index, option in enumerate(options):
            display_label = str(format_func(option))
            with columns[index % 2]:
                with _keyed_container(f"filter_card_{_slug(display_label)}"):
                    clicked = _ORIGINAL_BUTTON(
                        f"＋  {display_label}",
                        key=f"_filter_card_add_{_slug(option)}",
                        use_container_width=True,
                        help=f"Add {display_label} to the current filter set.",
                    )
            if clicked:
                selected = option
                st.session_state["_filter_card_selected"] = option
                st.session_state["_filter_card_add_clicked"] = True

    return selected


def _patched_selectbox(label, options, *args, **kwargs):
    if label == "Filter Category":
        return _filter_card_selectbox(label, options, *args, **kwargs)
    return _ORIGINAL_SELECTBOX(label, options, *args, **kwargs)


def _patched_button(label, *args, **kwargs):
    if label == "➕ Add":
        return bool(st.session_state.pop("_filter_card_add_clicked", False))
    return _ORIGINAL_BUTTON(label, *args, **kwargs)


class _ColoredExpander(AbstractContextManager):
    def __init__(self, delegate, tone):
        self.delegate = delegate
        self.tone = tone

    def __enter__(self):
        entered = self.delegate.__enter__()
        _ORIGINAL_MARKDOWN(
            f'<span class="filter-tone-marker filter-tone-{self.tone}"></span>',
            unsafe_allow_html=True,
        )
        return entered

    def __exit__(self, exc_type, exc_value, traceback):
        return self.delegate.__exit__(exc_type, exc_value, traceback)


def _patched_expander(label, *args, **kwargs):
    delegate = _ORIGINAL_EXPANDER(label, *args, **kwargs)
    match = re.match(r"^\d+\.\s+(.+)$", str(label))
    if not match:
        return delegate
    tone = _FILTER_LABEL_TONES.get(match.group(1))
    if tone is None:
        return delegate
    return _ColoredExpander(delegate, tone)


def install_filter_card_ui():
    if getattr(st, "_colored_filter_cards_installed", False):
        return
    _inject_filter_styles()
    st.selectbox = _patched_selectbox
    st.button = _patched_button
    st.expander = _patched_expander
    st._colored_filter_cards_installed = True
