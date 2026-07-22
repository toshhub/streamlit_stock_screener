import re
from contextlib import AbstractContextManager

import streamlit as st


_FILTER_LABEL_TONES = {
    "Custom Filter": 9,
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
    ("#db2777", "#fdf2f8", "#fbcfe8"),
]

_ORIGINAL_SELECTBOX = st.selectbox
_ORIGINAL_BUTTON = st.button
_ORIGINAL_EXPANDER = st.expander
_ORIGINAL_MARKDOWN = st.markdown
_STYLES_INJECTED = False


def _slug(value):
    return re.sub(r"[^a-z0-9]+", "_", str(value).lower()).strip("_")


def _card_widget_key(label):
    return f"_filter_card_add_{_slug(label)}"


def _expander_widget_key(label):
    return f"colored_filter_expander_{_slug(label)}"


def _inject_filter_styles():
    global _STYLES_INJECTED
    if _STYLES_INJECTED:
        return

    card_rules = []
    expander_rules = []
    for label, tone in _FILTER_LABEL_TONES.items():
        accent, background, border = _FILTER_COLORS[tone]
        card_key = _card_widget_key(label)
        expander_prefix = f"colored_filter_expander_"
        card_rules.append(
            f"""
            .st-key-{card_key} button {{
                min-height: 78px;
                width: 100%;
                justify-content: flex-start;
                padding: 0.85rem 0.95rem;
                border: 1px solid {border} !important;
                border-left: 6px solid {accent} !important;
                border-radius: 14px !important;
                background: {background} !important;
                color: #172033 !important;
                font-weight: 750 !important;
                line-height: 1.25;
                text-align: left;
                box-shadow: 0 4px 12px rgba(15, 23, 42, 0.05);
            }}
            .st-key-{card_key} button:hover {{
                border-color: {accent} !important;
                background: {background} !important;
                color: {accent} !important;
                transform: translateY(-1px);
            }}
            .st-key-{card_key} button p {{
                color: inherit !important;
                white-space: normal !important;
                text-align: left !important;
            }}
            """
        )
        expander_rules.append(
            f"""
            div[class*="st-key-{expander_prefix}"][data-testid="stExpander"]:has(.filter-tone-{tone}),
            div[class*="st-key-{expander_prefix}"]:has(> div[data-testid="stExpander"] .filter-tone-{tone}) > div[data-testid="stExpander"],
            div[data-testid="stExpander"]:has(.filter-tone-{tone}) {{
                border: 1px solid {border} !important;
                border-left: 6px solid {accent} !important;
                border-radius: 14px !important;
                background: linear-gradient(90deg, {background} 0%, #ffffff 38%) !important;
                box-shadow: 0 5px 16px rgba(15, 23, 42, 0.05);
            }}
            div[data-testid="stExpander"]:has(.filter-tone-{tone}) summary p {{
                color: {accent} !important;
                font-weight: 800 !important;
            }}
            """
        )

    _ORIGINAL_MARKDOWN(
        """
        <style>
        .filter-card-grid [data-testid="stHorizontalBlock"],
        .current-filter-grid [data-testid="stHorizontalBlock"] {
            gap: 0.75rem !important;
            align-items: stretch;
        }
        .filter-card-grid [data-testid="column"],
        .current-filter-grid [data-testid="column"] {
            min-width: 0;
        }
        .filter-card-grid .stButton {
            margin-bottom: 0.15rem;
        }
        .filter-tone-marker {
            display: none;
        }
        @media (max-width: 640px) {
            .filter-card-grid [data-testid="stHorizontalBlock"],
            .current-filter-grid [data-testid="stHorizontalBlock"] {
                flex-wrap: wrap;
            }
            .filter-card-grid [data-testid="column"],
            .current-filter-grid [data-testid="column"] {
                flex: 1 1 calc(50% - 0.75rem) !important;
                width: calc(50% - 0.75rem) !important;
            }
        }
        """
        + "".join(card_rules)
        + "".join(expander_rules)
        + "</style>",
        unsafe_allow_html=True,
    )
    _STYLES_INJECTED = True


def _filter_card_selectbox(label, options, *args, **kwargs):
    _inject_filter_styles()
    options = list(options)
    if not options:
        return None

    format_func = kwargs.get("format_func") or (lambda value: value)
    selected = st.session_state.get("_filter_card_selected", options[0])
    if selected not in options:
        selected = options[0]

    _ORIGINAL_MARKDOWN('<div class="filter-card-grid">', unsafe_allow_html=True)
    columns = st.columns(2, gap="small")
    for index, option in enumerate(options):
        display_label = str(format_func(option))
        with columns[index % 2]:
            clicked = _ORIGINAL_BUTTON(
                f"＋  {display_label}",
                key=_card_widget_key(display_label),
                use_container_width=True,
                help=f"Add {display_label} to the current filter set.",
            )
        if clicked:
            selected = option
            st.session_state["_filter_card_selected"] = option
            st.session_state["_filter_card_add_clicked"] = True
    _ORIGINAL_MARKDOWN("</div>", unsafe_allow_html=True)

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
    match = re.match(r"^\d+\.\s+(.+)$", str(label))
    if not match:
        return _ORIGINAL_EXPANDER(label, *args, **kwargs)

    tone = _FILTER_LABEL_TONES.get(match.group(1))
    if tone is None:
        return _ORIGINAL_EXPANDER(label, *args, **kwargs)

    _inject_filter_styles()
    keyed_kwargs = dict(kwargs)
    keyed_kwargs.setdefault("key", _expander_widget_key(label))
    try:
        delegate = _ORIGINAL_EXPANDER(label, *args, **keyed_kwargs)
    except TypeError:
        delegate = _ORIGINAL_EXPANDER(label, *args, **kwargs)
    return _ColoredExpander(delegate, tone)


def install_filter_card_ui():
    if getattr(st, "_colored_filter_cards_installed", False):
        return
    st.selectbox = _patched_selectbox
    st.button = _patched_button
    st.expander = _patched_expander
    st._colored_filter_cards_installed = True
