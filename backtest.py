from concurrent.futures import ThreadPoolExecutor, as_completed
import re

import pandas as pd

from charting import create_stock_chart
from pattern import (
    evaluate_numeric_expression_from_df,
    evaluate_pattern_filters_from_df,
    expression_uses_pe,
    validate_expression,
)
from screener import (
    custom_filter_expressions,
    load_price_dataframe,
    merge_legacy_expression_filters,
    normalize_filter_set,
    required_ma_periods,
    screen_dataframe,
)


def split_favorite_filter(saved_filter):
    if isinstance(saved_filter, list):
        filter_set = normalize_filter_set(saved_filter, use_default=False)
        return filter_set, {
            "lookback_days": 120,
            "reversal_pct": 5.0,
            "expressions": custom_filter_expressions(filter_set),
        }

    ma_filter_set = normalize_filter_set(saved_filter.get("ma_filter_set", []), use_default=False)
    pattern_settings = saved_filter.get("pattern", {}) if isinstance(saved_filter, dict) else {}
    legacy_expressions = [
        str(expression).strip()
        for expression in pattern_settings.get("expressions", [])
        if str(expression).strip()
    ]
    ma_filter_set = merge_legacy_expression_filters(ma_filter_set, legacy_expressions)
    expressions = custom_filter_expressions(ma_filter_set)

    return ma_filter_set, {
        "lookback_days": int(pattern_settings.get("lookback_days", 120)),
        "reversal_pct": float(pattern_settings.get("reversal_pct", 5.0)),
        "expressions": expressions,
    }


_PERCENT_PRICE_PATTERN = re.compile(r"^([+-]?\d+(?:\.\d+)?)\s*%$")
_PERCENT_ADJUSTMENT_PATTERN = re.compile(
    r"^(.+?)\s*([+-])\s*(\d+(?:\.\d+)?)\s*%$"
)


def _sell_expression_parts(expression):
    expression = str(expression or "").strip()
    if not expression:
        return {"kind": "blank"}

    percent_match = _PERCENT_PRICE_PATTERN.fullmatch(expression)
    if percent_match:
        return {"kind": "buy_percent", "percent": float(percent_match.group(1))}

    adjustment_match = _PERCENT_ADJUSTMENT_PATTERN.fullmatch(expression)
    if adjustment_match:
        percent = float(adjustment_match.group(3))
        if adjustment_match.group(2) == "-":
            percent = -percent
        return {
            "kind": "adjusted_expression",
            "expression": adjustment_match.group(1).strip(),
            "percent": percent,
        }

    if "%" in expression:
        return {
            "kind": "error",
            "error": "Use a percentage by itself (10% or -10%), or after a price expression (price - 1%).",
        }
    return {"kind": "expression", "expression": expression}


def validate_sell_price_expression(expression):
    """Validate a Target or Stop Loss expression without requiring price data."""
    parts = _sell_expression_parts(expression)
    if parts["kind"] == "error":
        return False, parts["error"]
    if parts["kind"] == "blank":
        return True, ""
    if parts.get("percent", 0) <= -100:
        return False, "A percentage adjustment must leave a price greater than zero."
    if parts["kind"] == "buy_percent":
        return True, ""
    return validate_expression(parts["expression"])


def evaluate_sell_price_expression(
    expression,
    evaluation_window,
    buy_price,
    candle_anchor_position=None,
):
    """Resolve a sell expression using current MA data and buy-anchored Candle references."""
    parts = _sell_expression_parts(expression)
    if parts["kind"] == "blank":
        return None, ""
    if parts["kind"] == "error":
        return None, parts["error"]
    if parts["kind"] == "buy_percent":
        price = float(buy_price) * (1 + parts["percent"] / 100)
    else:
        price, error = evaluate_numeric_expression_from_df(
            evaluation_window,
            parts["expression"],
            candle_anchor_position=candle_anchor_position,
        )
        if error:
            return None, error
        if parts["kind"] == "adjusted_expression":
            price *= 1 + parts["percent"] / 100

    if price is None or price <= 0:
        return None, "Sell strategy expressions must evaluate to a price greater than zero."
    return float(price), ""


def sell_expression_uses_dynamic_market_value(expression):
    parts = _sell_expression_parts(expression)
    base_expression = str(parts.get("expression", ""))
    return bool(re.search(
        r"\b(?:P|SMA\d+|CD|ROI|MA_MIN|MA_MAX|MA_VAR)\b",
        base_expression,
        re.IGNORECASE,
    ))


def _build_trade_gain_path(df, normalized_calendar, date_to_position, sell_strategy=None):
    reference_position = date_to_position[normalized_calendar[0]]
    end_position = date_to_position[normalized_calendar[-1]]
    chart_start_position = max(0, reference_position - 10)
    chart_end_position = min(len(df) - 1, end_position + 10)
    buy_price = float(df.iloc[reference_position]["Close"])
    if buy_price <= 0:
        raise ValueError("Buy price must be greater than zero.")

    strategy = sell_strategy or {}
    buy_window = df.iloc[: reference_position + 1].copy()
    target_price, target_error = evaluate_sell_price_expression(
        strategy.get("target", ""), buy_window, buy_price
    )
    stop_price, stop_error = evaluate_sell_price_expression(
        strategy.get("stop_loss", ""), buy_window, buy_price
    )
    if target_error:
        raise ValueError(f"Invalid Target expression: {target_error}")
    if stop_error:
        raise ValueError(f"Invalid Stop Loss expression: {stop_error}")

    closing_basis = bool(strategy.get("closing_basis", False))
    dynamic_stop = sell_expression_uses_dynamic_market_value(
        strategy.get("stop_loss", "")
    )
    effective_stop_price = stop_price
    exit_price = None
    exit_reason = ""
    exit_recorded = False
    gain_path = []

    for offset, calendar_date in enumerate(normalized_calendar):
        position = date_to_position[calendar_date]
        row = df.iloc[position]
        market_close = float(row["Close"])

        # The position is entered at the buy candle's close, so exit checks
        # start with the next candle to avoid using pre-entry OHLC movement.
        if offset > 0 and exit_price is None:
            if dynamic_stop:
                effective_stop_price, dynamic_stop_error = evaluate_sell_price_expression(
                    strategy.get("stop_loss", ""),
                    df.iloc[: position + 1].copy(),
                    buy_price,
                    candle_anchor_position=reference_position,
                )
                if dynamic_stop_error:
                    raise ValueError(
                        f"Invalid Stop Loss expression on {calendar_date.date()}: "
                        f"{dynamic_stop_error}"
                    )
            if closing_basis:
                stop_hit = effective_stop_price is not None and market_close <= effective_stop_price
                target_hit = target_price is not None and market_close >= target_price
            else:
                low = float(row["Low"]) if "Low" in df.columns and pd.notna(row["Low"]) else market_close
                high = float(row["High"]) if "High" in df.columns and pd.notna(row["High"]) else market_close
                stop_hit = effective_stop_price is not None and low <= effective_stop_price
                target_hit = target_price is not None and high >= target_price

            # OHLC data cannot reveal which threshold was touched first when
            # both occur in one candle, so use the conservative stop-first rule.
            if stop_hit:
                exit_price = market_close if closing_basis else effective_stop_price
                exit_reason = "Stop Loss"
            elif target_hit:
                exit_price = market_close if closing_basis else target_price
                exit_reason = "Target"

        is_last_candle = offset == len(normalized_calendar) - 1
        if is_last_candle and exit_price is None:
            exit_price = market_close
            exit_reason = "End Date"

        valuation_price = exit_price if exit_price is not None else market_close
        gain_pct = (valuation_price - buy_price) / buy_price * 100
        path_point = {
            "Candle": offset,
            "Portfolio Gain %": round(gain_pct, 2),
            "Date": calendar_date,
            "Close": market_close,
        }
        if effective_stop_price is not None:
            path_point["Stop Loss Price"] = float(effective_stop_price)
        if exit_reason and exit_price is not None and not exit_recorded:
            path_point.update({
                "Exit Reason": exit_reason,
                "Exit Price": float(exit_price),
            })
            exit_recorded = True
        gain_path.append(path_point)

    exit_point = next(
        (point for point in gain_path if point.get("Exit Reason")),
        gain_path[-1],
    )
    return gain_path, {
        "Buy Price": buy_price,
        "Target Price": target_price,
        "Stop Loss Price": effective_stop_price,
        "Dynamic Stop Loss": dynamic_stop,
        "Exit Date": exit_point["Date"],
        "Exit Price": float(exit_point.get("Exit Price", exit_price)),
        "Exit Reason": exit_point.get("Exit Reason", exit_reason),
        "Chart Start Date": pd.Timestamp(df.iloc[chart_start_position]["Date"]).normalize(),
        "Chart End Date": pd.Timestamp(df.iloc[chart_end_position]["Date"]).normalize(),
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
    exit_point = next(
        (point for point in gain_path if point.get("Exit Reason")),
        gain_path[-1],
    )
    annotations = []

    if "Close" in start_point:
        annotations.append({
            "type": "BUY",
            "date": pd.Timestamp(start_point["Date"]).normalize(),
            "price": float(start_point["Close"]),
            "label": "BUY",
        })

    if exit_point is not start_point:
        reason = exit_point.get("Exit Reason", "End Date")
        label = {"Target": "TARGET", "Stop Loss": "STOP", "End Date": "END"}.get(reason, "SELL")
        annotations.append({
            "type": label,
            "date": pd.Timestamp(exit_point["Date"]).normalize(),
            "price": float(exit_point.get("Exit Price", exit_point["Close"])),
            "label": label,
        })

    return annotations


def _backtest_stock_file(
    path,
    favorite_configs,
    calendar_dates,
    market,
    sell_strategy=None,
    green_candle_only=False,
):
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
    signal_date = normalized_calendar[0]
    trade_result = None

    if green_candle_only:
        buy_row = df.iloc[reference_position]
        try:
            is_green_buy_candle = (
                pd.notna(buy_row.get("Open"))
                and pd.notna(buy_row.get("Close"))
                and float(buy_row["Close"]) > float(buy_row["Open"])
            )
        except (TypeError, ValueError):
            is_green_buy_candle = False
        if not is_green_buy_candle:
            return events_by_filter

    for filter_name, config in favorite_configs.items():
        if _screen_backtest_signal(
            df,
            path.stem,
            reference_position,
            config["filter_set"],
            config["pattern"],
            market,
        ):
            if trade_result is None:
                trade_result = _build_trade_gain_path(
                    df,
                    normalized_calendar,
                    date_to_position,
                    sell_strategy=sell_strategy,
                )
            gain_path, exit_details = trade_result
            events_by_filter[filter_name].append({
                "Filter Name": filter_name,
                "Symbol": path.stem,
                "JsonPath": str(path),
                "Date": signal_date,
                "Final Gain %": gain_path[-1]["Portfolio Gain %"],
                "Gain Path": gain_path,
                **exit_details,
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
    sell_strategy=None,
    green_candle_only=False,
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
                sell_strategy,
                green_candle_only,
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
                            date_markers=_build_backtest_chart_annotations(gain_path),
                            window_start_date=event.get("Chart Start Date"),
                            window_end_date=event.get("Chart End Date"),
                        )
                    except Exception:
                        chart_paths[chart_key] = None
                stock_row = {
                    "Symbol": event["Symbol"],
                    "Gain at End Date": round(float(event["Final Gain %"]), 2),
                    "Peak Gain %": round(max(stock_gain_values), 2),
                    "Exit Reason": event.get("Exit Reason", "End Date"),
                    "Buy Date": pd.Timestamp(event.get("Date")).strftime("%d-%m-%Y"),
                    "Buy Price": round(float(event.get("Buy Price")), 2),
                    "Exit Date": pd.Timestamp(event.get("Exit Date")).strftime("%d-%m-%Y"),
                    "Exit Price": round(float(event.get("Exit Price")), 2),
                    "Target Price": (
                        round(float(event["Target Price"]), 2)
                        if event.get("Target Price") is not None else None
                    ),
                    "Stop Loss Price": (
                        round(float(event["Stop Loss Price"]), 2)
                        if event.get("Stop Loss Price") is not None else None
                    ),
                    "Chart Start Date": pd.Timestamp(event.get("Chart Start Date")).strftime("%Y-%m-%d"),
                    "Chart End Date": pd.Timestamp(event.get("Chart End Date")).strftime("%Y-%m-%d"),
                    "Chart Buy Price": float(event.get("Buy Price")),
                    "Chart Exit Price": float(event.get("Exit Price")),
                    "Chart Target Price": event.get("Target Price"),
                    "Chart Stop Loss Price": event.get("Stop Loss Price"),
                    "Chart MA Periods": required_ma_periods(
                        favorite_configs[filter_name]["filter_set"]
                    ),
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
