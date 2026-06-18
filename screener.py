
import json

import pandas as pd

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

def screen_json_file(
    path,
    short_ma=50,
    long_ma=200,
    support_threshold_pct=5,
    cross_lookback_days=20,
    cross_threshold_pct=5,
):
    if not path.exists():
        return None

    df = pd.DataFrame(json.loads(path.read_text()))
    if len(df) < long_ma:
        return None

    if "Date" in df.columns:
        df["Date"] = pd.to_datetime(df["Date"], errors="coerce")
        df = df.sort_values("Date")

    df["Close"] = pd.to_numeric(df["Close"], errors="coerce")
    df = df.dropna(subset=["Close"])
    if len(df) < long_ma:
        return None

    short_label = f"SMA{short_ma}"
    long_label = f"SMA{long_ma}"

    df[short_label] = df["Close"].rolling(short_ma).mean()
    df[long_label] = df["Close"].rolling(long_ma).mean()

    last = df.iloc[-1]

    if pd.isna(last[long_label]):
        return None

    price = last["Close"]
    current_long_ma = last[long_label]
    support_pct = pct_close_to_ma(price, current_long_ma)

    long_ma_rising, rising_rate_pct = long_ma_rising_from_two_bars_back(df[long_label])

    ma_support = (
        support_pct is not None
        and price >= current_long_ma
        and support_pct <= support_threshold_pct
    )
    golden_cross = (
        long_ma_rising
        and price >= current_long_ma
        and support_pct is not None
        and support_pct <= cross_threshold_pct
        and crossed_up(df[short_label], df[long_label], cross_lookback_days)
    )

    if ma_support or golden_cross:
        return {
            "Symbol": path.stem,
            "Price": round(price, 2),
            f"SMA{short_ma}": round(last[short_label], 2),
            f"SMA{long_ma}": round(current_long_ma, 2),
            "PercentCloseToLongMA": round(support_pct, 2),
            "LongMARising": long_ma_rising,
            "LongMARisingRatePct": round(rising_rate_pct, 2) if rising_rate_pct is not None else None,
            "MASupport": ma_support,
            "GoldenCross": golden_cross,
        }

    return None
