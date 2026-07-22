import json
import re
import threading
import urllib.parse
import urllib.request
from copy import deepcopy
from functools import lru_cache

import pandas as pd
import yfinance as yf

from downloader import MARKET_INDIA, normalize_market, yfinance_symbol
from storage import load_pe_ratios, save_pe_ratios

FILTER_TYPE_LABELS = {
    "custom_expression": "Custom Filter",
    "ma_rising": "MA Rising",
    "short_above_long": "Short MA Above Long MA",
    "price_near_long": "Current Price Near And Above Long MA",
    "golden_cross": "Short MA Crossed Long MA - Golden Cross",
    "long_ma_down_from_max": "Long MA Down From Recent Max",
    "long_ma_up_from_min": "Long MA Up From Recent Min",
    "green_candle_today": "Green Candle Today",
    "pe_less_than": "PE < N",
    "hitting_all_time_high": "Hitting All Time High",
    "price_near_old_ath": "Price Near Very Old ATH",
}

FILTER_TYPE_DEFAULTS = {
    "custom_expression": {"expression": ""},
    "ma_rising": {"ma": 200},
    "short_above_long": {"short_ma": 50, "long_ma": 200},
    "price_near_long": {"long_ma": 200, "threshold_pct": 5.0},
    "golden_cross": {"short_ma": 50, "long_ma": 200, "lookback_units": 20},
    "long_ma_down_from_max": {"long_ma": 200, "down_pct": 5.0, "lookback_units": 50},
    "long_ma_up_from_min": {"long_ma": 200, "up_pct": 5.0, "lookback_units": 50},
    "green_candle_today": {"min_gain_pct": 1.0},
    "pe_less_than": {"max_pe": 30.0},
    "hitting_all_time_high": {"ts_lookback": 200, "recent_n": 10},
    "price_near_old_ath": {"n_bars": 200, "range_low": -5.0, "range_high": 10.0},
}

PE_CACHE_LOCK = threading.RLock()

DEFAULT_FILTER_SET = [
    {"id": 1, "type": "ma_rising", "params": {"ma": 200}},
    {"id": 2, "type": "price_near_long", "params": {"long_ma": 200, "threshold_pct": 5.0}},
]

def filter_label(filter_item):
    return FILTER_TYPE_LABELS.get(filter_item["type"], filter_item["type"])


def custom_filter_expressions(filter_set):
    """Return non-blank expressions stored in Custom Filter rows."""
    return [
        str(item.get("params", {}).get("expression", "")).strip()
        for item in normalize_filter_set(filter_set, use_default=False)
        if item["type"] == "custom_expression"
        and str(item.get("params", {}).get("expression", "")).strip()
    ]


def merge_legacy_expression_filters(filter_set, expressions):
    """Migrate separately stored expressions into the unified filter list."""
    normalized = normalize_filter_set(filter_set, use_default=False)
    existing = custom_filter_expressions(normalized)
    next_id = max((int(item.get("id", 0)) for item in normalized), default=0) + 1
    for expression in expressions or []:
        expression = str(expression).strip()
        if not expression or expression in existing:
            continue
        normalized.append({
            "id": next_id,
            "type": "custom_expression",
            "params": {"expression": expression},
        })
        existing.append(expression)
        next_id += 1
    return normalized

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

def get_screener_in_pe_ratio(symbol):
    screener_symbol = urllib.parse.quote(symbol, safe="")
    urls = (
        f"https://www.screener.in/company/{screener_symbol}/",
        f"https://www.screener.in/company/{screener_symbol}/consolidated/",
    )
    for url in urls:
        request = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                html = response.read().decode("utf-8", errors="ignore")
        except urllib.error.HTTPError as exc:
            if exc.code in {404, 410}:
                continue
            raise

        match = re.search(
            r"Stock P/E\s*</span>\s*<span[^>]*class=\"[^\"]*number[^\"]*\"[^>]*>\s*([0-9,.]+)",
            html,
            re.IGNORECASE,
        )
        if not match:
            match = re.search(
                r"Stock P/E\s+([0-9,.]+)",
                re.sub(r"<[^>]+>", " ", html),
                re.IGNORECASE,
            )
        if match:
            return clean_pe_ratio(match.group(1).replace(",", ""))
    return ""

@lru_cache(maxsize=2048)
def get_pe_ratio(symbol, market=MARKET_INDIA):
    with PE_CACHE_LOCK:
        market = normalize_market(market)
        cache_key = f"{market}:{symbol}"
        pe_cache = load_pe_ratios()
        if cache_key in pe_cache:
            return pe_cache[cache_key]
        if market == MARKET_INDIA and symbol in pe_cache:
            return pe_cache[symbol]

        yahoo_symbol = yfinance_symbol(symbol, market)
        errors = []
        pe_sources = [
            ("yfinance", get_yfinance_pe_ratio),
            ("Yahoo quote API", get_quote_api_pe_ratio),
        ]
        if market == MARKET_INDIA:
            pe_sources.append(("Screener.in", get_screener_in_pe_ratio))

        for source_name, pe_lookup in pe_sources:
            try:
                pe = pe_lookup(symbol if source_name == "Screener.in" else yahoo_symbol)
                if pe != "":
                    pe_cache[cache_key] = pe
                    if market == MARKET_INDIA:
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


def signed_pct_diff_to_ma(price, moving_average):
    if pd.isna(moving_average) or moving_average == 0:
        return None
    return (price - moving_average) / moving_average * 100


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

def long_ma_up_from_min(series, up_pct, lookback_units):
    values = series.dropna().tail(lookback_units)
    if len(values) < 2:
        return False, None

    current_value = values.iloc[-1]
    min_value = values.min()
    if min_value == 0:
        return False, None

    up_from_min_pct = (current_value - min_value) / min_value * 100
    return up_from_min_pct >= up_pct, up_from_min_pct

def hitting_all_time_high(df, ts_lookback, recent_n):
    """Return True only if the ATH (max close within ts_lookback data frames)
    was hit within the most recent recent_n data frames.

    ts_lookback: Number of previous data frames to search for the All-Time High.
    recent_n: Check if ATH was hit in any of the last N data frames.
    """
    if len(df) < 2:
        return False, None

    ts_closes = df["Close"].dropna().tail(ts_lookback)
    if len(ts_closes) < 2:
        return False, None

    max_close = ts_closes.max()
    current_close = ts_closes.iloc[-1]
    if max_close == 0:
        return False, None

    # Check if ATH was hit (close >= max_close) in the last recent_n bars
    recent_closes = ts_closes.tail(recent_n)
    ath_hit = (recent_closes >= max_close).any()

    distance_pct = (current_close - max_close) / max_close * 100
    return ath_hit, distance_pct

def price_near_old_ath(df, n_bars, range_low, range_high):
    """Return True if current Close is within [range_low, range_high] % of
    the All-Time High found BEFORE the most recent n_bars (i.e. the ATH is
    searched in data excluding the last n_bars).

    n_bars: Number of recent bars to exclude when searching for the old ATH.
    range_low: Lower bound % (e.g. -4 means price can be 4% below old ATH).
    range_high: Upper bound % (e.g. +10 means price can be 10% above old ATH).
    """
    closes = df["Close"].dropna()
    if len(closes) <= n_bars:
        return False, None

    # Exclude the last n_bars and find the ATH in the older data
    old_closes = closes.iloc[:-n_bars]
    if len(old_closes) == 0:
        return False, None

    old_ath = old_closes.max()
    current_close = closes.iloc[-1]
    if old_ath == 0:
        return False, None

    distance_pct = (current_close - old_ath) / old_ath * 100
    passed = float(range_low) <= distance_pct <= float(range_high)
    return passed, round(distance_pct, 2)


def green_candle_today(df, min_gain_pct):
    if len(df) < 2 or "Open" not in df.columns:
        return False, None

    latest = df.iloc[-1]
    previous = df.iloc[-2]
    open_price = latest["Open"]
    close_price = latest["Close"]
    previous_close = previous["Close"]

    if pd.isna(open_price) or pd.isna(close_price) or pd.isna(previous_close) or previous_close == 0:
        return False, None

    gain_pct = (close_price - previous_close) / previous_close * 100
    passed = close_price > open_price and gain_pct >= float(min_gain_pct)
    return passed, gain_pct

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
        elif name in {"price_near_long", "long_ma_down_from_max", "long_ma_up_from_min"}:
            periods.add(int(config["long_ma"]))
        elif name == "custom_expression":
            expression = str(config.get("expression", ""))
            periods.update(int(value) for value in re.findall(r"\bSMA(\d+)\b", expression, re.IGNORECASE))
            for match in re.finditer(
                r"\b(CD|ROI|MA_MIN|MA_MAX|MA_VAR)\s*\(\s*(\d+(?:\.\d+)?)"
                r"(?:\s*,\s*(\d+(?:\.\d+)?))?",
                expression,
                re.IGNORECASE,
            ):
                periods.add(max(1, int(float(match.group(2)) + 0.5)))
                if match.group(1).upper() == "CD" and match.group(3):
                    periods.add(max(1, int(float(match.group(3)) + 0.5)))

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

def normalize_filter_set(filter_set=None, use_default=True):
    if not filter_set:
        return deepcopy(DEFAULT_FILTER_SET) if use_default else []

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

    return deepcopy(DEFAULT_FILTER_SET) if use_default else []

def legacy_kwargs_to_filter_set(legacy_kwargs):
    short_ma = int(legacy_kwargs.get("short_ma", 50))
    long_ma = int(legacy_kwargs.get("long_ma", 200))
    return {
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


def load_price_dataframe(path):
    df = pd.DataFrame(json.loads(path.read_text()))
    if "Date" in df.columns:
        df["Date"] = pd.to_datetime(df["Date"], errors="coerce")
        df = df.sort_values("Date")

    df["Close"] = pd.to_numeric(df["Close"], errors="coerce")
    if "Open" in df.columns:
        df["Open"] = pd.to_numeric(df["Open"], errors="coerce")
    return df.dropna(subset=["Close"]).reset_index(drop=True)


def screen_dataframe(df, symbol, filter_set=None, include_pe=True, market=MARKET_INDIA):
    filter_set = normalize_filter_set(filter_set, use_default=False)

    df = df.copy()
    ma_periods = required_ma_periods(filter_set)
    max_ma = max(ma_periods) if ma_periods else 0
    if len(df) < max_ma:
        return None

    if "Date" in df.columns:
        df["Date"] = pd.to_datetime(df["Date"], errors="coerce")
        df = df.sort_values("Date")

    df["Close"] = pd.to_numeric(df["Close"], errors="coerce")
    if "Open" in df.columns:
        df["Open"] = pd.to_numeric(df["Open"], errors="coerce")
    df = df.dropna(subset=["Close"]).reset_index(drop=True)
    if len(df) < max_ma:
        return None

    for period in ma_periods:
        df[f"SMA{period}"] = df["Close"].rolling(period).mean()

    last = df.iloc[-1]
    price = last["Close"]

    result = {
        "Symbol": symbol,
        "PE Ratio": "",
        "Price": round(price, 2),
        "MatchedFilters": ", ".join(filter_label(filter_item) for filter_item in filter_set),
    }

    for period in ma_periods:
        ma_label = f"SMA{period}"
        if pd.isna(last[ma_label]):
            return None
        sma_value = round(last[ma_label], 2)
        result[ma_label] = sma_value
        pct_diff = signed_pct_diff_to_ma(price, sma_value)
        result[f"Diff{ma_label}"] = round(pct_diff, 2) if pct_diff is not None else None
        # Rate of Change of the MA from 2 bars back
        _, roc_pct = long_ma_rising_from_two_bars_back(df[ma_label])
        result[f"Roc{ma_label}"] = round(roc_pct, 2) if roc_pct is not None else None

    indexed_filter_set = list(enumerate(filter_set, start=1))
    ordered_filter_set = [
        item for item in indexed_filter_set if item[1]["type"] != "pe_less_than"
    ] + [
        item for item in indexed_filter_set if item[1]["type"] == "pe_less_than"
    ]

    for filter_index, filter_item in ordered_filter_set:
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

        elif filter_type == "long_ma_up_from_min":
            long_label = f"SMA{int(config['long_ma'])}"
            passed, up_from_min_pct = long_ma_up_from_min(
                df[long_label],
                float(config["up_pct"]),
                int(config["lookback_units"]),
            )
            result[f"{prefix}_UpFromMinPct"] = round(up_from_min_pct, 2) if up_from_min_pct is not None else None
            result[f"{prefix}_Passed"] = passed
            if not passed:
                return None

        elif filter_type == "hitting_all_time_high":
            passed, distance_pct = hitting_all_time_high(df, int(config["ts_lookback"]), int(config["recent_n"]))
            result[f"{prefix}_DistancePct"] = round(distance_pct, 2) if distance_pct is not None else None
            result[f"{prefix}_Passed"] = passed
            if not passed:
                return None

        elif filter_type == "price_near_old_ath":
            passed, distance_pct = price_near_old_ath(
                df,
                int(config["n_bars"]),
                float(config["range_low"]),
                float(config["range_high"]),
            )
            result[f"{prefix}_DistancePct"] = round(distance_pct, 2) if distance_pct is not None else None
            result[f"{prefix}_Passed"] = passed
            if not passed:
                return None

        elif filter_type == "green_candle_today":
            passed, gain_pct = green_candle_today(df, float(config["min_gain_pct"]))
            result[f"{prefix}_GainPct"] = round(gain_pct, 2) if gain_pct is not None else None
            result[f"{prefix}_Passed"] = passed
            if not passed:
                return None

        elif filter_type == "pe_less_than":
            if result["PE Ratio"] == "":
                if not include_pe:
                    return None
                result["PE Ratio"] = get_pe_ratio(symbol, market)
            pe = result["PE Ratio"]
            passed = pe != "" and float(pe) < float(config["max_pe"])
            result[f"{prefix}_Passed"] = passed
            if not passed:
                return None

    if include_pe and result["PE Ratio"] == "":
        result["PE Ratio"] = get_pe_ratio(symbol, market)
    return result


def screen_json_file(path, filter_set=None, market=MARKET_INDIA, **legacy_kwargs):
    if not path.exists():
        return None

    if filter_set is None and legacy_kwargs:
        filter_set = legacy_kwargs_to_filter_set(legacy_kwargs)

    df = load_price_dataframe(path)
    return screen_dataframe(df, path.stem, filter_set=filter_set, market=market)
