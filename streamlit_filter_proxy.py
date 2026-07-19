import html
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


def _favorite_card_key(label, tone, selected, index):
    state = "selected" if selected else "idle"
    return f"favorite_filter_card_tone_{tone}_{state}_{index}_{_slug(label)}"


def _is_screener_top_layout(spec):
    if not isinstance(spec, (list, tuple)) or len(spec) != 2:
        return False
    try:
        return abs(float(spec[0]) - 1.35) < 0.001 and abs(float(spec[1]) - 1.0) < 0.001
    except (TypeError, ValueError):
        return False


def _inject_styles(force=False):
    global _STYLES_INJECTED
    if _STYLES_INJECTED and not force:
        return

    card_rules = []
    expander_rules = []
    for label, tone in _FILTER_LABEL_TONES.items():
        accent, background, border = _FILTER_COLORS[tone]
        key = _card_key(label)
        card_rules.append(
            f"""
            div[class*="st-key-{key}"] {{
                position: relative;
            }}
            div[class*="st-key-{key}"] .filter-card-label {{
                display: flex;
                align-items: center;
                min-height: 58px !important;
                padding: 0.7rem 3.1rem 0.7rem 0.85rem;
                border: 1px solid {border} !important;
                border-left: 4px solid {accent} !important;
                border-radius: 11px !important;
                background: linear-gradient(135deg, {background}, #ffffff 88%) !important;
                color: #172033 !important;
                font-weight: 700 !important;
                line-height: 1.2 !important;
                text-align: left !important;
                box-shadow: 0 2px 7px rgba(15, 23, 42, 0.045) !important;
                transition: border-color 140ms ease, box-shadow 140ms ease, transform 140ms ease !important;
            }}
            div[class*="st-key-{key}"]:has(button:hover) .filter-card-label {{
                border-color: {accent} !important;
                color: {accent} !important;
                box-shadow: 0 6px 14px rgba(15, 23, 42, 0.08) !important;
            }}
            div[class*="st-key-{key}"] [data-testid="stButton"] {{
                width: 2rem !important;
            }}
            div[class*="st-key-{key}"] [data-testid="stButton"] button {{
                display: grid !important;
                place-items: center !important;
                min-height: 2rem !important;
                width: 2rem !important;
                height: 2rem !important;
                padding: 0 !important;
                border: 1px solid {border} !important;
                border-radius: 9px !important;
                background: #ffffff !important;
                color: {accent} !important;
                font-size: 1.15rem !important;
                font-weight: 900 !important;
                line-height: 1 !important;
                box-shadow: 0 2px 6px rgba(15, 23, 42, 0.08) !important;
            }}
            div[class*="st-key-{key}"] [data-testid="stButton"] button:hover {{
                border-color: {accent} !important;
                background: {background} !important;
                transform: scale(1.04);
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

    favorite_card_rules = []
    for tone, (accent, background, border) in enumerate(_FILTER_COLORS):
        favorite_card_rules.append(
            f"""
            div[class*="st-key-favorite_filter_card_tone_{tone}_"] button {{
                min-height: 54px !important;
                width: 100% !important;
                justify-content: flex-start !important;
                padding: 0.65rem 0.8rem !important;
                border: 1px solid {border} !important;
                border-left: 4px solid {accent} !important;
                border-radius: 11px !important;
                background: linear-gradient(135deg, {background}, #ffffff 90%) !important;
                color: #172033 !important;
                font-weight: 700 !important;
                line-height: 1.15 !important;
                text-align: left !important;
                box-shadow: 0 2px 7px rgba(15, 23, 42, 0.045) !important;
                transition: border-color 140ms ease, box-shadow 140ms ease, transform 140ms ease !important;
            }}
            div[class*="st-key-favorite_filter_card_tone_{tone}_"] button:hover {{
                border-color: {accent} !important;
                color: {accent} !important;
                transform: translateY(-1px);
                box-shadow: 0 6px 14px rgba(15, 23, 42, 0.08) !important;
            }}
            div[class*="st-key-favorite_filter_card_tone_{tone}_"] button p {{
                color: inherit !important;
                white-space: normal !important;
                text-align: left !important;
                overflow-wrap: anywhere !important;
            }}
            div[class*="st-key-favorite_filter_card_tone_{tone}_selected_"] button {{
                border-color: {accent} !important;
                background: {background} !important;
                color: {accent} !important;
                box-shadow: 0 0 0 2px {border}, 0 5px 13px rgba(15, 23, 42, 0.08) !important;
            }}
            """
        )

    _st.markdown(
        """
        <style>
        .filter-tone-marker { display: none !important; }

        .quick-run-section-label {
            margin: 0.85rem 0 0.42rem;
            color: #263b50;
            font-size: 0.78rem;
            font-weight: 800;
            letter-spacing: 0.065em;
            text-transform: uppercase;
        }

        .quick-run-section-label span {
            margin-left: 0.35rem;
            color: #7b8b9b;
            font-size: 0.74rem;
            font-weight: 500;
            letter-spacing: 0;
            text-transform: none;
        }

        div[class*="st-key-quick_run_options"] {
            padding: 0.7rem 0.8rem 0.35rem;
            border: 1px solid #e1e9ef;
            border-radius: 12px;
            background: #f8fafc;
        }

        div[class*="st-key-quick_run_action"] {
            margin-top: 0.75rem;
            padding-top: 0.75rem;
            border-top: 1px solid #e5ebf0;
        }

        div[class*="st-key-quick_run_action"] button {
            min-height: 48px !important;
            border-radius: 11px !important;
            font-weight: 800 !important;
            box-shadow: 0 6px 16px rgba(31, 102, 125, 0.18) !important;
        }

        /*
         * Streamlit wraps buttons in tooltip elements whose intrinsic width
         * otherwise shrinks the colored surface to the label text.
         */
        div[class*="st-key-favorite_filter_card_"] .stButton,
        div[class*="st-key-favorite_filter_card_"] [data-testid="stTooltipIcon"],
        div[class*="st-key-favorite_filter_card_"] [data-testid="stTooltipHoverTarget"] {
            width: 100% !important;
        }

        div[class*="st-key-current_filter_card_"] {
            position: relative;
        }

        div[class*="st-key-filter_add_action_"] {
            position: absolute !important;
            top: 50%;
            right: 0.6rem;
            z-index: 6;
            width: 2rem !important;
            transform: translateY(-50%);
        }

        div[class*="st-key-current_filter_card_"] [data-testid="stExpander"] summary {
            min-height: 3.65rem !important;
            padding-right: 3.15rem !important;
        }

        div[class*="st-key-current_filter_card_"] [data-testid="stExpander"] summary p {
            box-sizing: border-box;
            width: 100%;
            max-width: 100%;
            padding-right: 0.2rem;
            white-space: normal !important;
            overflow-wrap: anywhere !important;
        }

        div[class*="st-key-current_filter_remove_"] {
            position: absolute !important;
            top: 50%;
            right: 0.55rem;
            z-index: 6;
            width: 1.9rem !important;
            transform: translateY(-50%);
        }

        div[class*="st-key-current_filter_remove_"] [data-testid="stButton"],
        div[class*="st-key-current_filter_remove_"] [data-testid="stTooltipIcon"],
        div[class*="st-key-current_filter_remove_"] [data-testid="stTooltipHoverTarget"] {
            width: 1.9rem !important;
        }

        div[class*="st-key-current_filter_remove_"] button {
            display: grid !important;
            place-items: center !important;
            min-height: 1.9rem !important;
            width: 1.9rem !important;
            height: 1.9rem !important;
            padding: 0 !important;
            border: 1px solid #fecaca !important;
            border-radius: 9px !important;
            background: #fff7f7 !important;
            color: #dc2626 !important;
            font-size: 1.15rem !important;
            font-weight: 900 !important;
            line-height: 1 !important;
            box-shadow: 0 2px 6px rgba(127, 29, 29, 0.08) !important;
        }

        div[class*="st-key-current_filter_remove_"] button:hover {
            border-color: #ef4444 !important;
            background: #fee2e2 !important;
        }

        /* Keep filter rows in a two-column grid. */
        div[data-testid="stHorizontalBlock"]:has([class*="st-key-filter_card_"]),
        div[data-testid="stHorizontalBlock"]:has([class*="st-key-favorite_filter_card_"]),
        div[data-testid="stHorizontalBlock"]:has(.filter-tone-marker) {
            display: flex !important;
            flex-direction: row !important;
            flex-wrap: nowrap !important;
            align-items: stretch !important;
            gap: 0.7rem !important;
            width: 100% !important;
        }

        div[data-testid="stHorizontalBlock"]:has([class*="st-key-filter_card_"])
            > div:is([data-testid="column"], [data-testid="stColumn"]),
        div[data-testid="stHorizontalBlock"]:has([class*="st-key-favorite_filter_card_"])
            > div:is([data-testid="column"], [data-testid="stColumn"]),
        div[data-testid="stHorizontalBlock"]:has(.filter-tone-marker)
            > div:is([data-testid="column"], [data-testid="stColumn"]) {
            flex: 1 1 0 !important;
            width: 0 !important;
            min-width: 0 !important;
            max-width: 50% !important;
        }

        div[data-testid="stHorizontalBlock"]:has([class*="st-key-filter_card_"])
            > div:is([data-testid="column"], [data-testid="stColumn"]) .stButton,
        div[data-testid="stHorizontalBlock"]:has([class*="st-key-favorite_filter_card_"])
            > div:is([data-testid="column"], [data-testid="stColumn"]) .stButton,
        div[data-testid="stHorizontalBlock"]:has(.filter-tone-marker)
            > div:is([data-testid="column"], [data-testid="stColumn"]) [data-testid="stExpander"] {
            width: 100% !important;
        }

        @media (max-width: 768px) {
            .stMainBlockContainer {
                padding-left: 0.65rem !important;
                padding-right: 0.65rem !important;
            }

            div[data-testid="stVerticalBlockBorderWrapper"] {
                border-radius: 14px !important;
            }

            div[data-testid="stHorizontalBlock"]:has([class*="st-key-filter_card_"]),
            div[data-testid="stHorizontalBlock"]:has([class*="st-key-favorite_filter_card_"]),
            div[data-testid="stHorizontalBlock"]:has(.filter-tone-marker) {
                gap: 0.45rem !important;
            }

            div[class*="st-key-favorite_filter_card_"] button {
                min-height: 54px !important;
                padding: 0.58rem 0.52rem !important;
                border-left-width: 4px !important;
                border-radius: 10px !important;
                font-size: 0.78rem !important;
            }

            div[class*="st-key-favorite_filter_card_"] button p {
                font-size: 0.78rem !important;
                line-height: 1.18 !important;
            }

            div[class*="st-key-filter_card_"] .filter-card-label {
                min-height: 58px !important;
                padding: 0.56rem 2.55rem 0.56rem 0.58rem;
                font-size: 0.72rem;
                line-height: 1.14;
                overflow-wrap: anywhere;
            }

            div[class*="st-key-filter_add_action_"] {
                right: 0.4rem;
                width: 1.8rem !important;
            }

            div[class*="st-key-filter_card_"] [data-testid="stButton"] button {
                min-height: 1.8rem !important;
                width: 1.8rem !important;
                height: 1.8rem !important;
            }

            div[data-testid="stHorizontalBlock"]:has(.filter-tone-marker)
                > div:is([data-testid="column"], [data-testid="stColumn"]) [data-testid="stExpander"] summary {
                min-height: 4.1rem !important;
                padding-left: 0.45rem !important;
                padding-right: 3rem !important;
            }

            div[data-testid="stHorizontalBlock"]:has(.filter-tone-marker)
                > div:is([data-testid="column"], [data-testid="stColumn"]) [data-testid="stExpander"] summary p {
                font-size: 0.72rem !important;
                line-height: 1.15 !important;
            }

            div[class*="st-key-current_filter_remove_"] {
                right: 0.42rem;
            }

            .data-panel-heading {
                font-size: 1rem !important;
            }

            .data-panel-subtitle {
                font-size: 0.82rem !important;
                line-height: 1.35 !important;
            }

            .quick-run-section-label span {
                display: block;
                margin: 0.12rem 0 0;
            }
        }
        """
        + "".join(card_rules)
        + "".join(favorite_card_rules)
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

    def columns(self, spec, *args, **kwargs):
        if _is_screener_top_layout(spec):
            # Streamlit removes prior markdown elements on a rerun while module
            # globals remain alive, so emit the stylesheet once on every run.
            _inject_styles(force=True)
            # Return two full-width containers. app.py renders Quick Run into the
            # first and Add a Filter into the second, placing them vertically.
            return _st.container(), _st.container()
        return _st.columns(spec, *args, **kwargs)

    def selectbox(self, label, options, *args, **kwargs):
        if "Filter Set To Run" in str(label):
            _inject_styles()
            options = list(options)
            if not options:
                return None

            widget_key = kwargs.get("key", "_favorite_filter_card_selected")
            selected = _st.session_state.get(widget_key)
            if selected not in options:
                # A custom working set intentionally has no favorite selected.
                selected = None

            callback = kwargs.get("on_change")
            callback_args = kwargs.get("args") or ()
            callback_kwargs = kwargs.get("kwargs") or {}

            def select_favorite(option):
                # Button callbacks run before Streamlit renders the page again,
                # so the newly selected card and loaded filters appear on the
                # first click instead of one rerun later.
                _st.session_state[widget_key] = option
                if callback:
                    callback(*callback_args, **callback_kwargs)

            for row_start in range(0, len(options), 2):
                row_options = options[row_start:row_start + 2]
                columns = _st.columns(2, gap="small")
                for column_index, option in enumerate(row_options):
                    tone = (row_start + column_index) % len(_FILTER_COLORS)
                    is_selected = option == selected
                    prefix = "✓  " if is_selected else "☆  "
                    button_kwargs = {}
                    if not is_selected:
                        button_kwargs = {
                            "on_click": select_favorite,
                            "args": (option,),
                        }
                    with columns[column_index]:
                        _st.button(
                            f"{prefix}{option}",
                            key=_favorite_card_key(
                                option,
                                tone,
                                is_selected,
                                row_start + column_index,
                            ),
                            use_container_width=True,
                            help=f"Load and run the {option} filter set.",
                            **button_kwargs,
                        )

            return selected

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

        for row_start in range(0, len(options), 2):
            row_options = options[row_start:row_start + 2]
            columns = _st.columns(2, gap="small")
            for column_index, option in enumerate(row_options):
                display_label = str(format_func(option))
                with columns[column_index]:
                    with _st.container(key=_card_key(display_label)):
                        _st.markdown(
                            f'<div class="filter-card-label">{html.escape(display_label)}</div>',
                            unsafe_allow_html=True,
                        )
                        clicked = _st.button(
                            "＋",
                            key=f"filter_add_action_{row_start + column_index}_{_slug(display_label)}",
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
