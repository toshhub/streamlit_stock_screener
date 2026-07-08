from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd

from pattern import evaluate_pattern_filters_from_df
from screener import load_price_dataframe, normalize_filter_set, screen_dataframe


def split_favorite_filter(saved_filter):
    if isinstance(saved_filter, list):
        return normalize_filter_set(saved_filter, use_default=False), {}

    ma_filter_set = normalize_filter_set(saved_filter.get("ma_filter_set", []), use_default=False)
    pattern_settings = saved_filter.get("pattern", {}) if isinstance(saved_filter, dict) else {}
    expressions = [
        str(expression).strip()
        for expression in pattern_settings.get("expressions", [])
        if str(expression).strip()
    ]

    return ma_filter_set, {
        "lookback_days": int(pattern_settings.get("lookback_days", 120)),
        "reversal_pct": float(pattern_settings.get("reversal_pct", 5.0)),
        "expressions": expressions,
    }


def _screen_backtest_signal(df, symbol, position, filter_set, pattern_settings):
    window = df.iloc[: position + 1].copy()
    needs_pe = any(filter_item["type"] == "pe_less_than" for filter_item in filter_set)
    result = screen_dataframe(window, symbol, filter_set=filter_set, include_pe=needs_pe)
    if not result:
        return False

    expressions = pattern_settings.get("expressions", [])
    if not expressions:
        return True

    passed, _, _ = evaluate_pattern_filters_from_df(
        window,
        pattern_settings.get("lookback_days", 120),
        pattern_settings.get("reversal_pct", 5.0),
        expressions,
    )
    return passed


def _backtest_stock_file(path, favorite_configs, backtest_candles, gain_candles):
    df = load_price_dataframe(path)
    if df.empty or len(df) <= backtest_candles:
        return {name: [] for name in favorite_configs}

    start_position = len(df) - 1 - backtest_candles
    end_position = len(df) - 1 - gain_candles
    if start_position < 0 or end_position < start_position:
        return {name: [] for name in favorite_configs}

    events_by_filter = {name: [] for name in favorite_configs}

    for position in range(start_position, end_position + 1):
        signal_close = float(df.iloc[position]["Close"])
        exit_close = float(df.iloc[position + gain_candles]["Close"])
        if signal_close == 0:
            continue

        gain_pct = (exit_close - signal_close) / signal_close * 100
        signal_date = df.iloc[position]["Date"] if "Date" in df.columns else position

        for filter_name, config in favorite_configs.items():
            if _screen_backtest_signal(
                df,
                path.stem,
                position,
                config["filter_set"],
                config["pattern"],
            ):
                events_by_filter[filter_name].append({
                    "Filter Name": filter_name,
                    "Symbol": path.stem,
                    "Date": signal_date,
                    "Gain %": round(gain_pct, 2),
                })

    return events_by_filter


def run_backtest(stock_files, favorite_filter_sets, selected_filter_names, backtest_candles, gain_candles):
    favorite_configs = {}
    for name in selected_filter_names:
        filter_set, pattern_settings = split_favorite_filter(favorite_filter_sets[name])
        favorite_configs[name] = {
            "filter_set": filter_set,
            "pattern": pattern_settings,
        }

    all_events = {name: [] for name in selected_filter_names}
    if not stock_files or not favorite_configs:
        return [], {}

    max_workers = min(8, max(1, len(stock_files)))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [
            executor.submit(
                _backtest_stock_file,
                path,
                favorite_configs,
                int(backtest_candles),
                int(gain_candles),
            )
            for path in stock_files
        ]
        for future in as_completed(futures):
            file_events = future.result()
            for filter_name, events in file_events.items():
                all_events[filter_name].extend(events)

    summary_rows = []
    series_by_filter = {}
    for filter_name in selected_filter_names:
        events = all_events[filter_name]
        if events:
            events_df = pd.DataFrame(events)
            events_df["Date"] = pd.to_datetime(events_df["Date"], errors="coerce")
            stocks_found = int(events_df["Symbol"].nunique())
            daily_series = (
                events_df
                .groupby("Date", dropna=False)
                .agg(**{"Average Gain %": ("Gain %", "mean"), "Matches": ("Gain %", "count")})
                .reset_index()
                .sort_values("Date")
            )
            daily_series["Average Gain %"] = daily_series["Average Gain %"].round(2)
            average_gain = round(float(events_df["Gain %"].mean()), 2)
            peak_average_gain = round(float(daily_series["Average Gain %"].max()), 2)
            match_count = int(len(events_df))
        else:
            daily_series = pd.DataFrame(columns=["Date", "Average Gain %", "Matches"])
            average_gain = None
            peak_average_gain = None
            match_count = 0
            stocks_found = 0

        summary_rows.append({
            "Filter Name": filter_name,
            "Gain for next M days": average_gain,
            "Peak Average Gain %": peak_average_gain,
            "Stocks Found": stocks_found,
            "Matches": match_count,
        })
        series_by_filter[filter_name] = daily_series.to_dict("records")

    return summary_rows, series_by_filter
