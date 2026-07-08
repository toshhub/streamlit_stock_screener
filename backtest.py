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


def get_backtest_calendar_dates(stock_files):
    best_dates = []
    best_last_date = None

    for path in stock_files:
        try:
            df = load_price_dataframe(path)
        except Exception:
            continue
        if df.empty or "Date" not in df.columns:
            continue

        dates = list(pd.to_datetime(df["Date"], errors="coerce").dropna())
        if len(dates) < 2:
            continue

        last_date = dates[-1]
        if best_last_date is None or last_date > best_last_date or (
            last_date == best_last_date and len(dates) > len(best_dates)
        ):
            best_dates = dates
            best_last_date = last_date

    return [date.normalize() for date in best_dates]


def _build_backtest_calendar(stock_files, start_date, end_date):
    best_dates = get_backtest_calendar_dates(stock_files)
    if not best_dates:
        return []

    start_ts = pd.Timestamp(start_date).normalize()
    end_ts = pd.Timestamp(end_date).normalize()
    if end_ts < start_ts:
        return []

    return [date for date in best_dates if start_ts <= date <= end_ts]


def _backtest_stock_file(path, favorite_configs, calendar_dates):
    df = load_price_dataframe(path)
    if df.empty or "Date" not in df.columns or not calendar_dates:
        return {name: [] for name in favorite_configs}

    df["Date"] = pd.to_datetime(df["Date"], errors="coerce")
    date_to_position = {
        date.normalize(): position
        for position, date in enumerate(df["Date"])
        if pd.notna(date)
    }
    normalized_calendar = [pd.Timestamp(date).normalize() for date in calendar_dates]
    if any(date not in date_to_position for date in normalized_calendar):
        return {name: [] for name in favorite_configs}

    events_by_filter = {name: [] for name in favorite_configs}

    reference_position = date_to_position[normalized_calendar[0]]
    signal_close = float(df.iloc[reference_position]["Close"])
    if signal_close == 0:
        return events_by_filter

    gain_path = []
    for offset, calendar_date in enumerate(normalized_calendar):
        position = date_to_position[calendar_date]
        close_at_offset = float(df.iloc[position]["Close"])
        gain_pct = (close_at_offset - signal_close) / signal_close * 100
        gain_path.append({
            "Candle": offset,
            "Portfolio Gain %": round(gain_pct, 2),
            "Date": calendar_date,
        })

    signal_date = normalized_calendar[0]

    for filter_name, config in favorite_configs.items():
        if _screen_backtest_signal(
            df,
            path.stem,
            reference_position,
            config["filter_set"],
            config["pattern"],
        ):
            events_by_filter[filter_name].append({
                "Filter Name": filter_name,
                "Symbol": path.stem,
                "Date": signal_date,
                "Final Gain %": gain_path[-1]["Portfolio Gain %"],
                "Gain Path": gain_path,
            })

    return events_by_filter


def run_backtest(stock_files, favorite_filter_sets, selected_filter_names, start_date, end_date):
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

    calendar_dates = _build_backtest_calendar(stock_files, start_date, end_date)
    if len(calendar_dates) < 2:
        return [], {}

    max_workers = min(8, max(1, len(stock_files)))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [
            executor.submit(
                _backtest_stock_file,
                path,
                favorite_configs,
                calendar_dates,
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
            path_rows = []
            for event in events:
                for path_point in event["Gain Path"]:
                    path_rows.append({
                        "Candle": int(path_point["Candle"]),
                        "Gain %": float(path_point.get("Portfolio Gain %", path_point.get("Average Gain %"))),
                        "Date": path_point["Date"],
                    })

            path_df = pd.DataFrame(path_rows)
            path_df["Date"] = pd.to_datetime(path_df["Date"], errors="coerce")
            gain_series = (
                path_df
                .groupby("Candle", dropna=False)
                .agg(**{
                    "Portfolio Gain %": ("Gain %", "mean"),
                    "Stocks Found": ("Gain %", "count"),
                    "Date": ("Date", "first"),
                })
                .reset_index()
                .sort_values("Candle")
            )
            gain_series["Portfolio Gain %"] = gain_series["Portfolio Gain %"].round(2)
            gain_series["Date"] = gain_series["Date"].dt.strftime("%d-%m-%Y")
            final_candle = int(gain_series["Candle"].max())
            final_gain_rows = gain_series[gain_series["Candle"] == final_candle]
            average_gain = (
                round(float(final_gain_rows.iloc[0]["Portfolio Gain %"]), 2)
                if not final_gain_rows.empty
                else None
            )
            peak_portfolio_gain = round(float(gain_series["Portfolio Gain %"].max()), 2)
        else:
            gain_series = pd.DataFrame(columns=["Candle", "Portfolio Gain %", "Stocks Found"])
            average_gain = None
            peak_portfolio_gain = None
            stocks_found = 0

        summary_rows.append({
            "Filter Name": filter_name,
            "Portfolio Gain at End Date": average_gain,
            "Peak Portfolio Gain %": peak_portfolio_gain,
            "Stocks Found": stocks_found,
        })
        series_by_filter[filter_name] = gain_series.to_dict("records")

    return summary_rows, series_by_filter
