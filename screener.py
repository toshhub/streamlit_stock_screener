
import json

import pandas as pd

DEFAULT_FILTER_SET = {
    "ma_rising": {"enabled": True, "ma": 200},
    "short_above_long": {"enabled": False, "short_ma": 50, "long_ma": 200},
    "price_near_long": {"enabled": True, "long_ma": 200, "threshold_pct": 5.0},
    "golden_cross": {"enabled": False, "short_ma": 50, "long_ma": 200, "lookback_units": 20},
    "long_ma_down_from_max": {"enabled": False, "long_ma": 200, "down_pct": 5.0, "lookback_units": 50},
}

def filter_config(filter_set, filter_name):
    return filter_set.get(filter_name, DEFAULT_FILTER_SET[filter_name])

def enabled_filter_names(filter_set):
    return [name for name, config in filter_set.items() if config.get("enabled")]

def long_ma_rising_from_two_bars_back(series):
    values = series.dropna()
    if len(values) < 3:
        return False, None

    current_value = values.iloc[-1]
    two_bars_back_value = values.iloc[-3]
    if two_bars_back_value == 0:
        return current_value > two_bars_back_value, None

    rising_rate_pct = (current_value - two_bars_back_value) / two_bars_back_value * 100
    return current_value > two_bars_back_value, rising_rate_pct

def ma_rising_from_two_bars_back(df, ma_label):
    return long_ma_rising_from_two_bars_back(df[ma_label])

def pct_close_to_ma(price, moving_average):
    if pd.isna(moving_average) or moving_average == 0:
        return None
    return abs(price - moving_average) / moving_average * 100

def crossed_up(short_ma, long_ma, lookback_days):
    diff = short_ma - long_ma
    lookback = diff.tail(lookback_days + 1).dropna()
    if len(lookback) < 2:
        return False

    previous = lookback.shift(1)
    return ((previous <= 0) & (lookback > 0)).any()

def long_ma_down_from_max(series, down_pct, lookback_units):
    values = series.dropna().tail(lookback_units)
    if len(values) < 2:
        return False, None

    current_value = values.iloc[-1]
    max_value = values.max()
    if max_value == 0:
        return False, None

    down_from_max_pct = (max_value - current_value) / max_value * 100
    return down_from_max_pct >= down_pct, down_from_max_pct

def required_ma_periods(filter_set):
    periods = set()
    for name, config in filter_set.items():
        if not config.get("enabled"):
            continue

        if name == "ma_rising":
            periods.add(int(config["ma"]))
        elif name in {"short_above_long", "golden_cross"}:
            periods.add(int(config["short_ma"]))
            periods.add(int(config["long_ma"]))
        elif name in {"price_near_long", "long_ma_down_from_max"}:
            periods.add(int(config["long_ma"]))

    return sorted(periods)

def normalize_filter_set(filter_set=None):
    if not filter_set:
        return DEFAULT_FILTER_SET.copy()

    normalized = {}
    for name, defaults in DEFAULT_FILTER_SET.items():
        config = defaults.copy()
        config.update(filter_set.get(name, {}))
        normalized[name] = config
    return normalized

def screen_json_file(path, filter_set=None, **legacy_kwargs):
    if not path.exists():
        return None

    if filter_set is None and legacy_kwargs:
        short_ma = int(legacy_kwargs.get("short_ma", 50))
        long_ma = int(legacy_kwargs.get("long_ma", 200))
        filter_set = {
            "ma_rising": {"enabled": False, "ma": long_ma},
            "short_above_long": {"enabled": False, "short_ma": short_ma, "long_ma": long_ma},
            "price_near_long": {
                "enabled": True,
                "long_ma": long_ma,
                "threshold_pct": float(legacy_kwargs.get("support_threshold_pct", 5)),
            },
            "golden_cross": {
                "enabled": True,
                "short_ma": short_ma,
                "long_ma": long_ma,
                "lookback_units": int(legacy_kwargs.get("cross_lookback_days", 20)),
            },
            "long_ma_down_from_max": {"enabled": False, "long_ma": long_ma, "down_pct": 5.0, "lookback_units": 50},
        }

    filter_set = normalize_filter_set(filter_set)
    active_filters = enabled_filter_names(filter_set)
    if not active_filters:
        return None

    df = pd.DataFrame(json.loads(path.read_text()))
    ma_periods = required_ma_periods(filter_set)
    max_ma = max(ma_periods) if ma_periods else 0
    if len(df) < max_ma:
        return None

    if "Date" in df.columns:
        df["Date"] = pd.to_datetime(df["Date"], errors="coerce")
        df = df.sort_values("Date")

    df["Close"] = pd.to_numeric(df["Close"], errors="coerce")
    df = df.dropna(subset=["Close"])
    if len(df) < max_ma:
        return None

    for period in ma_periods:
        df[f"SMA{period}"] = df["Close"].rolling(period).mean()

    last = df.iloc[-1]
    price = last["Close"]

    result = {
        "Symbol": path.stem,
        "Price": round(price, 2),
        "MatchedFilters": ", ".join(active_filters),
    }

    for period in ma_periods:
        ma_label = f"SMA{period}"
        if pd.isna(last[ma_label]):
            return None
        result[ma_label] = round(last[ma_label], 2)

    ma_rising_config = filter_config(filter_set, "ma_rising")
    if ma_rising_config.get("enabled"):
        ma_label = f"SMA{int(ma_rising_config['ma'])}"
        passed, rising_rate_pct = ma_rising_from_two_bars_back(df, ma_label)
        result["MARising"] = passed
        result["MARisingRatePct"] = round(rising_rate_pct, 2) if rising_rate_pct is not None else None
        if not passed:
            return None

    short_above_config = filter_config(filter_set, "short_above_long")
    if short_above_config.get("enabled"):
        short_label = f"SMA{int(short_above_config['short_ma'])}"
        long_label = f"SMA{int(short_above_config['long_ma'])}"
        passed = last[short_label] > last[long_label]
        result["ShortMAAboveLongMA"] = passed
        if not passed:
            return None

    price_near_config = filter_config(filter_set, "price_near_long")
    if price_near_config.get("enabled"):
        long_label = f"SMA{int(price_near_config['long_ma'])}"
        distance_pct = pct_close_to_ma(price, last[long_label])
        passed = (
            distance_pct is not None
            and price >= last[long_label]
            and distance_pct <= float(price_near_config["threshold_pct"])
        )
        result["PercentCloseToLongMA"] = round(distance_pct, 2) if distance_pct is not None else None
        result["PriceNearAndAboveLongMA"] = passed
        if not passed:
            return None

    golden_cross_config = filter_config(filter_set, "golden_cross")
    if golden_cross_config.get("enabled"):
        short_label = f"SMA{int(golden_cross_config['short_ma'])}"
        long_label = f"SMA{int(golden_cross_config['long_ma'])}"
        passed = crossed_up(
            df[short_label],
            df[long_label],
            int(golden_cross_config["lookback_units"]),
        )
        result["GoldenCross"] = passed
        if not passed:
            return None

    down_config = filter_config(filter_set, "long_ma_down_from_max")
    if down_config.get("enabled"):
        long_label = f"SMA{int(down_config['long_ma'])}"
        passed, down_from_max_pct = long_ma_down_from_max(
            df[long_label],
            float(down_config["down_pct"]),
            int(down_config["lookback_units"]),
        )
        result["LongMADownFromMaxPct"] = round(down_from_max_pct, 2) if down_from_max_pct is not None else None
        result["LongMADownFromMax"] = passed
        if not passed:
            return None

    return result
