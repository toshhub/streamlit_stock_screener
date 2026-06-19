
import json
import urllib.parse
import urllib.request
from copy import deepcopy
from functools import lru_cache

import pandas as pd
import yfinance as yf

from storage import load_pe_ratios, save_pe_ratios

FILTER_TYPE_LABELS = {
    "ma_rising": "MA Rising",
    "short_above_long": "Short MA Above Long MA",
    "price_near_long": "Current Price Near And Above Long MA",
    "golden_cross": "Short MA Crossed Long MA - Golden Cross",
    "long_ma_down_from_max": "Long MA Down From Recent Max",
}

FILTER_TYPE_DEFAULTS = {
    "ma_rising": {"ma": 200},
    "short_above_long": {"short_ma": 50, "long_ma": 200},
    "price_near_long": {"long_ma": 200, "threshold_pct": 5.0},
    "golden_cross": {"short_ma": 50, "long_ma": 200, "lookback_units": 20},
    "long_ma_down_from_max": {"long_ma": 200, "down_pct": 5.0, "lookback_units": 50},
}

DEFAULT_FILTER_SET = [
    {"id": 1, "type": "ma_rising", "params": {"ma": 200}},
    {"id": 2, "type": "price_near_long", "params": {"long_ma": 200, "threshold_pct": 5.0}},
]

def filter_label(filter_item):
    return FILTER_TYPE_LABELS.get(filter_item["type"], filter_item["type"])

def clean_pe_ratio(pe):
    if pe is None:
        return ""

    try:
        pe = float(pe)
    except (TypeError, ValueError):
        return ""

    if pe <= 0:
        return ""

    return round(pe, 2)

def get_yfinance_pe_ratio(yahoo_symbol):
    ticker = yf.Ticker(yahoo_symbol)
    return clean_pe_ratio(ticker.info.get("trailingPE"))

def get_quote_api_pe_ratio(yahoo_symbol):
    params = urllib.parse.urlencode({"symbols": yahoo_symbol})
    url = f"https://query1.finance.yahoo.com/v7/finance/quote?{params}"
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0"},
    )

    with urllib.request.urlopen(request, timeout=10) as response:
        data = json.loads(response.read().decode("utf-8"))

    quotes = data.get("quoteResponse", {}).get("result", [])
    if not quotes:
        return ""

    return clean_pe_ratio(quotes[0].get("trailingPE"))

@lru_cache(maxsize=2048)
def get_pe_ratio(symbol):
    pe_cache = load_pe_ratios()
    if symbol in pe_cache:
        return pe_cache[symbol]

    yahoo_symbol = symbol + ".NS"
    errors = []
    for source_name, pe_lookup in [
        ("yfinance", get_yfinance_pe_ratio),
        ("Yahoo quote API", get_quote_api_pe_ratio),
    ]:
        try:
            pe = pe_lookup(yahoo_symbol)
            if pe != "":
                pe_cache[symbol] = pe
                save_pe_ratios(pe_cache)
                return pe
        except Exception as exc:
            errors.append(f"{source_name}: {exc}")

    if errors:
        print(f"PE not available for {symbol}: {'; '.join(errors)}")
    return ""

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
    for filter_item in filter_set:
        name = filter_item["type"]
        config = filter_item["params"]

        if name == "ma_rising":
            periods.add(int(config["ma"]))
        elif name in {"short_above_long", "golden_cross"}:
            periods.add(int(config["short_ma"]))
            periods.add(int(config["long_ma"]))
        elif name in {"price_near_long", "long_ma_down_from_max"}:
            periods.add(int(config["long_ma"]))

    return sorted(periods)

def normalize_filter_item(filter_item, fallback_id):
    filter_type = filter_item.get("type")
    if filter_type not in FILTER_TYPE_DEFAULTS:
        return None

    params = deepcopy(FILTER_TYPE_DEFAULTS[filter_type])
    params.update(filter_item.get("params", {}))

    return {
        "id": filter_item.get("id", fallback_id),
        "type": filter_type,
        "params": params,
    }

def normalize_filter_set(filter_set=None):
    if not filter_set:
        return deepcopy(DEFAULT_FILTER_SET)

    if isinstance(filter_set, list):
        normalized = []
        for index, filter_item in enumerate(filter_set, start=1):
            normalized_item = normalize_filter_item(filter_item, index)
            if normalized_item:
                normalized.append(normalized_item)
        return normalized

    if isinstance(filter_set, dict):
        normalized = []
        next_id = 1
        for filter_type, defaults in FILTER_TYPE_DEFAULTS.items():
            legacy_config = filter_set.get(filter_type, {})
            if legacy_config.get("enabled"):
                params = deepcopy(defaults)
                params.update({key: value for key, value in legacy_config.items() if key != "enabled"})
                normalized.append({"id": next_id, "type": filter_type, "params": params})
                next_id += 1
        return normalized

    return deepcopy(DEFAULT_FILTER_SET)

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
    if not filter_set:
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
        "PE Ratio": "",
        "Price": round(price, 2),
        "MatchedFilters": ", ".join(filter_label(filter_item) for filter_item in filter_set),
    }

    for period in ma_periods:
        ma_label = f"SMA{period}"
        if pd.isna(last[ma_label]):
            return None
        result[ma_label] = round(last[ma_label], 2)

    for filter_index, filter_item in enumerate(filter_set, start=1):
        filter_type = filter_item["type"]
        config = filter_item["params"]
        prefix = f"F{filter_index}_{filter_type}"

        if filter_type == "ma_rising":
            ma_label = f"SMA{int(config['ma'])}"
            passed, rising_rate_pct = ma_rising_from_two_bars_back(df, ma_label)
            result[f"{prefix}_Passed"] = passed
            result[f"{prefix}_RisingRatePct"] = round(rising_rate_pct, 2) if rising_rate_pct is not None else None
            if not passed:
                return None

        elif filter_type == "short_above_long":
            short_label = f"SMA{int(config['short_ma'])}"
            long_label = f"SMA{int(config['long_ma'])}"
            passed = last[short_label] > last[long_label]
            result[f"{prefix}_Passed"] = passed
            if not passed:
                return None

        elif filter_type == "price_near_long":
            long_label = f"SMA{int(config['long_ma'])}"
            distance_pct = pct_close_to_ma(price, last[long_label])
            passed = (
                distance_pct is not None
                and price >= last[long_label]
                and distance_pct <= float(config["threshold_pct"])
            )
            result[f"{prefix}_DistancePct"] = round(distance_pct, 2) if distance_pct is not None else None
            result[f"{prefix}_Passed"] = passed
            if not passed:
                return None

        elif filter_type == "golden_cross":
            short_label = f"SMA{int(config['short_ma'])}"
            long_label = f"SMA{int(config['long_ma'])}"
            passed = crossed_up(
                df[short_label],
                df[long_label],
                int(config["lookback_units"]),
            )
            result[f"{prefix}_Passed"] = passed
            if not passed:
                return None

        elif filter_type == "long_ma_down_from_max":
            long_label = f"SMA{int(config['long_ma'])}"
            passed, down_from_max_pct = long_ma_down_from_max(
                df[long_label],
                float(config["down_pct"]),
                int(config["lookback_units"]),
            )
            result[f"{prefix}_DownFromMaxPct"] = round(down_from_max_pct, 2) if down_from_max_pct is not None else None
            result[f"{prefix}_Passed"] = passed
            if not passed:
                return None

    result["PE Ratio"] = get_pe_ratio(path.stem)
    return result
