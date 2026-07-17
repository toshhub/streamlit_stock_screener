from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd

from charting import create_stock_chart
from pattern import evaluate_pattern_filters_from_df, expression_uses_pe
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


def _screen_backtest_signal(df, symbol, position, filter_set, pattern_settings, market):
    window = df.iloc[: position + 1].copy()
    expressions = pattern_settings.get("expressions", [])
    needs_pe = (
        any(filter_item["type"] == "pe_less_than" for filter_item in filter_set)
        or any(expression_uses_pe(expression) for expression in expressions)
    )
    result = screen_dataframe(window, symbol, filter_set=filter_set, include_pe=needs_pe, market=market)
    if not result:
        return False

    if not expressions:
        return True

    passed, _, _ = evaluate_pattern_filters_from_df(
        window,
        pattern_settings.get("lookback_days", 120),
        pattern_settings.get("reversal_pct", 5.0),
        expressions,
        pe_ratio=result.get("PE Ratio"),
    )
    return passed


def get_backtest_calendar_dates(stock_files, sample_size=25):
    best_dates = []
    best_last_date = None
    sampled_files = sorted(
        stock_files,
        key=lambda path: path.stat().st_mtime if path.exists() else 0,
        reverse=True,
    )[:sample_size]

    for path in sampled_files:
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

    if not best_dates or best_last_date is None:
        return []

    three_year_cutoff = (best_last_date - pd.DateOffset(years=3)).normalize()
    return [date.normalize() for date in best_dates if date.normalize() >= three_year_cutoff]


def _build_backtest_calendar(stock_files, start_date, end_date):
    best_dates = get_backtest_calendar_dates(stock_files)
    if not best_dates:
        return []

    start_ts = pd.Timestamp(start_date).normalize()
    end_ts = pd.Timestamp(end_date).normalize()
    if end_ts < start_ts:
        return []

    return [date for date in best_dates if start_ts <= date <= end_ts]


def _build_backtest_chart_annotations(gain_path):
    if not gain_path:
        return []

    start_point = gain_path[0]
    end_point = gain_path[-1]
    annotations = []

    if "Close" in start_point:
        annotations.append({
            "type": "BUY",
            "date": pd.Timestamp(start_point["Date"]).normalize(),
            "price": float(start_point["Close"]),
            "label": "BUY",
        })

    if end_point is not start_point and "Close" in end_point:
        annotations.append({
            "type": "END",
            "date": pd.Timestamp(end_point["Date"]).normalize(),
            "price": float(end_point["Close"]),
            "label": "END",
        })

    return annotations


def _backtest_stock_file(path, favorite_configs, calendar_dates, market):
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
            "Close": close_at_offset,
        })

    signal_date = normalized_calendar[0]

    for filter_name, config in favorite_configs.items():
        if _screen_backtest_signal(
            df,
            path.stem,
            reference_position,
            config["filter_set"],
            config["pattern"],
            market,
        ):
            events_by_filter[filter_name].append({
                "Filter Name": filter_name,
                "Symbol": path.stem,
                "JsonPath": str(path),
                "Date": signal_date,
                "Final Gain %": gain_path[-1]["Portfolio Gain %"],
                "Gain Path": gain_path,
            })

    return events_by_filter


def _build_benchmark_gain_series(benchmark_file, calendar_dates):
    if not benchmark_file or not benchmark_file.exists() or not calendar_dates:
        return []

    df = load_price_dataframe(benchmark_file)
    if df.empty or "Date" not in df.columns or "Close" not in df.columns:
        return []

    normalized_calendar = [pd.Timestamp(date).normalize() for date in calendar_dates]
    df["Date"] = pd.to_datetime(df["Date"], errors="coerce")
    close_series = (
        df.dropna(subset=["Date", "Close"])
        .assign(Date=lambda data: data["Date"].dt.normalize())
        .drop_duplicates(subset=["Date"], keep="last")
        .set_index("Date")["Close"]
        .sort_index()
        .astype(float)
    )
    if close_series.empty:
        return []

    aligned_closes = close_series.reindex(normalized_calendar, method="ffill")
    if aligned_closes.isna().any():
        return []

    start_close = float(aligned_closes.iloc[0])
    if start_close == 0:
        return []

    benchmark_rows = []
    for offset, calendar_date in enumerate(normalized_calendar):
        close_at_offset = float(aligned_closes.iloc[offset])
        gain_pct = (close_at_offset - start_close) / start_close * 100
        benchmark_rows.append({
            "Candle": offset,
            "Portfolio Gain %": round(gain_pct, 2),
            "Date": calendar_date.strftime("%d-%m-%Y"),
            "Benchmark": True,
        })

    return benchmark_rows


def run_backtest(
    stock_files,
    favorite_filter_sets,
    selected_filter_names,
    start_date,
    end_date,
    progress_callback=None,
    benchmark_file=None,
    market="INDIA",
):
    favorite_configs = {}
    for name in selected_filter_names:
        filter_set, pattern_settings = split_favorite_filter(favorite_filter_sets[name])
        favorite_configs[name] = {
            "filter_set": filter_set,
            "pattern": pattern_settings,
        }

    all_events = {name: [] for name in selected_filter_names}
    if not stock_files or not favorite_configs:
        return [], {}, {}

    calendar_dates = _build_backtest_calendar(stock_files, start_date, end_date)
    if len(calendar_dates) < 2:
        return [], {}, {}

    max_workers = min(8, max(1, len(stock_files)))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [
            executor.submit(
                _backtest_stock_file,
                path,
                favorite_configs,
                calendar_dates,
                market,
            )
            for path in stock_files
        ]
        total = len(futures)
        for done, future in enumerate(as_completed(futures), start=1):
            file_events = future.result()
            for filter_name, events in file_events.items():
                all_events[filter_name].extend(events)
            if progress_callback:
                progress_callback(done, total)

    summary_rows = []
    series_by_filter = {}
    stock_details_by_filter = {}
    chart_paths = {}
    date_markers = [
        {"label": "Start", "date": calendar_dates[0]},
        {"label": "End", "date": calendar_dates[-1]},
    ]
    for filter_name in selected_filter_names:
        events = all_events[filter_name]
        if events:
            events_df = pd.DataFrame(events)
            events_df["Date"] = pd.to_datetime(events_df["Date"], errors="coerce")
            stocks_found = int(events_df["Symbol"].nunique())
            path_rows = []
            stock_rows = []
            for event in events:
                gain_path = event["Gain Path"]
                for path_point in event["Gain Path"]:
                    path_rows.append({
                        "Candle": int(path_point["Candle"]),
                        "Gain %": float(path_point.get("Portfolio Gain %", path_point.get("Average Gain %"))),
                        "Date": path_point["Date"],
                    })
                stock_gain_values = [
                    float(path_point.get("Portfolio Gain %", path_point.get("Average Gain %")))
                    for path_point in gain_path
                ]
                chart_key = (filter_name, event["JsonPath"])
                if chart_key not in chart_paths:
                    try:
                        chart_paths[chart_key] = create_stock_chart(
                            pd.io.common.stringify_path(event["JsonPath"]),
                            favorite_configs[filter_name]["filter_set"],
                            swing_annotations=_build_backtest_chart_annotations(gain_path),
                            date_markers=date_markers,
                        )
                    except Exception:
                        chart_paths[chart_key] = None
                stock_row = {
                    "Symbol": event["Symbol"],
                    "Gain at End Date": round(float(event["Final Gain %"]), 2),
                    "Peak Gain %": round(max(stock_gain_values), 2),
                }
                if chart_paths[chart_key]:
                    stock_row["ChartPath"] = chart_paths[chart_key]
                    stock_row["ChartSource"] = event["Symbol"]
                stock_rows.append(stock_row)

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
            stock_rows = sorted(
                stock_rows,
                key=lambda row: (row["Gain at End Date"], row["Peak Gain %"], row["Symbol"]),
                reverse=True,
            )
        else:
            gain_series = pd.DataFrame(columns=["Candle", "Portfolio Gain %", "Stocks Found"])
            average_gain = None
            peak_portfolio_gain = None
            stocks_found = 0
            stock_rows = []

        summary_rows.append({
            "Filter Name": filter_name,
            "Portfolio Gain at End Date": average_gain,
            "Peak Portfolio Gain %": peak_portfolio_gain,
            "Stocks Found": stocks_found,
        })
        series_by_filter[filter_name] = gain_series.to_dict("records")
        stock_details_by_filter[filter_name] = stock_rows

    benchmark_series = _build_benchmark_gain_series(benchmark_file, calendar_dates)
    if benchmark_series:
        series_by_filter["Nifty 50"] = benchmark_series

    return summary_rows, series_by_filter, stock_details_by_filter
