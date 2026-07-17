import ast
import json
import math

import pandas as pd


SAFE_FUNCTIONS = {
    "abs": abs,
    "min": min,
    "max": max,
    "round": round,
}

MA_FUNCTION_ARGUMENT_COUNTS = {
    "CD": 2,
    "ROI": 1,
    "MA_MIN": 2,
    "MA_MAX": 2,
    "MA_VAR": 2,
}

SAFE_NODE_TYPES = (
    ast.Expression,
    ast.BoolOp,
    ast.BinOp,
    ast.UnaryOp,
    ast.Compare,
    ast.Call,
    ast.Name,
    ast.Load,
    ast.Constant,
    ast.And,
    ast.Or,
    ast.Add,
    ast.Sub,
    ast.Mult,
    ast.Div,
    ast.Mod,
    ast.Pow,
    ast.USub,
    ast.UAdd,
    ast.Eq,
    ast.NotEq,
    ast.Lt,
    ast.LtE,
    ast.Gt,
    ast.GtE,
)


def is_sma_variable(name):
    return name.startswith("SMA") and name[3:].isdigit() and int(name[3:]) > 0


def expression_uses_pe(expression):
    try:
        tree = ast.parse(str(expression).strip(), mode="eval")
    except SyntaxError:
        return False
    return any(isinstance(node, ast.Name) and node.id == "PE" for node in ast.walk(tree))


def _positive_numeric_literal(node):
    if not isinstance(node, ast.Constant):
        return False
    value = node.value
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
        and float(value) > 0
    )


def validate_expression(expression, available_names=None):
    expression = str(expression).strip()
    if not expression:
        return False, "Expression cannot be blank."

    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        return False, exc.msg

    allowed_names = {"P", "PE", *SAFE_FUNCTIONS, *MA_FUNCTION_ARGUMENT_COUNTS}
    allowed_names.update(available_names or [])

    for node in ast.walk(tree):
        if not isinstance(node, SAFE_NODE_TYPES):
            return False, f"Unsupported syntax: {type(node).__name__}"

        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name):
                return False, "Only supported named functions can be called."

            function_name = node.func.id
            if node.keywords:
                return False, "Keyword arguments are not supported."

            if function_name in MA_FUNCTION_ARGUMENT_COUNTS:
                expected_count = MA_FUNCTION_ARGUMENT_COUNTS[function_name]
                if len(node.args) != expected_count:
                    return False, f"{function_name}() expects {expected_count} numeric argument(s)."
                if not all(_positive_numeric_literal(argument) for argument in node.args):
                    return False, f"{function_name}() arguments must be positive numbers."
            elif function_name not in SAFE_FUNCTIONS:
                return False, (
                    "Supported functions: CD(), ROI(), MA_MIN(), MA_MAX(), "
                    "MA_VAR(), abs(), min(), max(), and round()."
                )

        if isinstance(node, ast.Name):
            if node.id not in allowed_names and not is_sma_variable(node.id):
                return False, f"Unknown variable: {node.id}"

    return True, ""


def load_price_data(path):
    df = pd.DataFrame(json.loads(path.read_text()))
    return prepare_price_dataframe(df)


def prepare_price_dataframe(df):
    df = df.copy()
    if "Date" in df.columns:
        df["Date"] = pd.to_datetime(df["Date"], errors="coerce")
        df = df.sort_values("Date")
    else:
        df["Date"] = range(1, len(df) + 1)

    if "Close" not in df.columns:
        return pd.DataFrame(columns=["Date", "Close"])

    df["Close"] = pd.to_numeric(df["Close"], errors="coerce")
    return df.dropna(subset=["Close"]).reset_index(drop=True)


def _normalized_period(value):
    numeric_value = float(value)
    if not math.isfinite(numeric_value) or numeric_value <= 0:
        raise ValueError("MA periods and lookbacks must be positive numbers.")
    return max(1, int(math.floor(numeric_value + 0.5)))


def _numeric_pe(pe_ratio):
    try:
        value = float(pe_ratio)
    except (TypeError, ValueError):
        return math.nan
    return value if math.isfinite(value) else math.nan


def build_ma_expression_context(df, pe_ratio=None, expressions=None):
    df = prepare_price_dataframe(df)
    close = df["Close"] if "Close" in df.columns else pd.Series(dtype=float)
    ma_cache = {}

    def ma_series(period_value):
        period = _normalized_period(period_value)
        if period not in ma_cache:
            ma_cache[period] = close.rolling(period).mean()
        return ma_cache[period]

    def latest_sma(period_value):
        values = ma_series(period_value).dropna()
        return float(values.iloc[-1]) if not values.empty else math.nan

    def days_since_cross(short_period, long_period):
        short_ma = ma_series(short_period)
        long_ma = ma_series(long_period)
        difference = short_ma - long_ma
        crossed = ((difference.shift(1) <= 0) & (difference > 0)).fillna(False)
        cross_positions = [
            position
            for position, did_cross in enumerate(crossed.tolist())
            if bool(did_cross)
        ]
        if not cross_positions:
            return math.inf
        return float(len(difference) - 1 - cross_positions[-1])

    def ma_roi(period_value):
        values = ma_series(period_value).dropna()
        if len(values) < 2:
            return math.nan
        previous_value = float(values.iloc[-2])
        if previous_value == 0:
            return math.nan
        return (float(values.iloc[-1]) - previous_value) / abs(previous_value) * 100

    def ma_window_values(period_value, lookback_value):
        lookback = _normalized_period(lookback_value)
        return ma_series(period_value).dropna().tail(lookback)

    def ma_min(period_value, lookback_value):
        values = ma_window_values(period_value, lookback_value)
        return float(values.min()) if not values.empty else math.nan

    def ma_max(period_value, lookback_value):
        values = ma_window_values(period_value, lookback_value)
        return float(values.max()) if not values.empty else math.nan

    def ma_variation(period_value, lookback_value):
        values = ma_window_values(period_value, lookback_value)
        if values.empty:
            return math.nan
        maximum = float(values.max())
        minimum = float(values.min())
        if maximum == 0:
            return math.nan
        return (maximum - minimum) / abs(maximum) * 100

    context = {
        "P": float(close.iloc[-1]) if not close.empty else math.nan,
        "PE": _numeric_pe(pe_ratio),
        "CD": days_since_cross,
        "ROI": ma_roi,
        "MA_MIN": ma_min,
        "MA_MAX": ma_max,
        "MA_VAR": ma_variation,
        **SAFE_FUNCTIONS,
    }

    for expression in expressions or []:
        try:
            tree = ast.parse(str(expression).strip(), mode="eval")
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and is_sma_variable(node.id):
                context[node.id] = latest_sma(int(node.id[3:]))

    return context


def evaluate_expression(expression, context):
    is_valid, error = validate_expression(expression, context.keys())
    if not is_valid:
        return False, error

    try:
        result = eval(
            compile(ast.parse(str(expression).strip(), mode="eval"), "<expression-filter>", "eval"),
            {"__builtins__": {}},
            context,
        )
    except (ArithmeticError, NameError, TypeError, ValueError, ZeroDivisionError) as exc:
        return False, str(exc)

    if isinstance(result, float) and math.isnan(result):
        return False, "Expression returned NaN because the required data is unavailable."

    return bool(result), ""


def evaluate_expression_filters_from_df(df, expressions, pe_ratio=None):
    expressions = [str(expression).strip() for expression in expressions if str(expression).strip()]
    context = build_ma_expression_context(df, pe_ratio=pe_ratio, expressions=expressions)

    for expression in expressions:
        passed, error = evaluate_expression(expression, context)
        if not passed:
            return False, error

    return True, ""


def evaluate_pattern_filters(path, lookback_days, reversal_pct, expressions, pe_ratio=None):
    del lookback_days, reversal_pct
    df = load_price_data(path)
    passed, error = evaluate_expression_filters_from_df(df, expressions, pe_ratio=pe_ratio)
    return passed, [], error


def evaluate_pattern_filters_from_df(
    df,
    lookback_days,
    reversal_pct,
    expressions,
    pe_ratio=None,
):
    del lookback_days, reversal_pct
    passed, error = evaluate_expression_filters_from_df(df, expressions, pe_ratio=pe_ratio)
    return passed, [], error
