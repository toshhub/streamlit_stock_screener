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


def is_pattern_variable(name):
    if name == "P":
        return True
    if name.startswith(("H", "L")) and name[1:].isdigit():
        return True
    if name.startswith(("DH", "DL")) and name[2:].isdigit():
        return True
    return False


def load_price_data(path):
    df = pd.DataFrame(json.loads(path.read_text()))
    if "Date" in df.columns:
        df["Date"] = pd.to_datetime(df["Date"], errors="coerce")
        df = df.sort_values("Date")
    else:
        df["Date"] = range(1, len(df) + 1)

    df["Close"] = pd.to_numeric(df["Close"], errors="coerce")
    return df.dropna(subset=["Close"]).reset_index(drop=True)


def detect_swings_from_df(df, lookback_days, reversal_pct):
    window = df.tail(int(lookback_days)).reset_index(drop=True)
    if window.empty:
        return []

    reversal = float(reversal_pct) / 100
    prices = window["Close"].tolist()
    swings = []
    extreme_idx = len(prices) - 1
    extreme_price = prices[-1]
    trend = None

    def add_swing(swing_type, index, price):
        swings.append({
            "type": swing_type,
            "index": index,
            "date": window.iloc[index]["Date"],
            "price": float(price),
            "days_since": len(window) - 1 - index,
        })

    for index in range(len(prices) - 2, -1, -1):
        price = prices[index]
        if trend is None:
            if price >= extreme_price * (1 + reversal):
                add_swing("L", extreme_idx, extreme_price)
                trend = "up"
                extreme_idx = index
                extreme_price = price
            elif price <= extreme_price * (1 - reversal):
                add_swing("H", extreme_idx, extreme_price)
                trend = "down"
                extreme_idx = index
                extreme_price = price
            else:
                if price > extreme_price:
                    extreme_idx = index
                    extreme_price = price
                elif price < extreme_price:
                    extreme_idx = index
                    extreme_price = price
            continue

        if trend == "up":
            if price > extreme_price:
                extreme_idx = index
                extreme_price = price
            elif price <= extreme_price * (1 - reversal):
                add_swing("H", extreme_idx, extreme_price)
                trend = "down"
                extreme_idx = index
                extreme_price = price
        elif trend == "down":
            if price < extreme_price:
                extreme_idx = index
                extreme_price = price
            elif price >= extreme_price * (1 + reversal):
                add_swing("L", extreme_idx, extreme_price)
                trend = "up"
                extreme_idx = index
                extreme_price = price

    return list(reversed(swings))


def build_swing_context(df, swings):
    context = {"P": float(df.iloc[-1]["Close"])} if not df.empty else {}

    for swing_type in ["H", "L"]:
        typed_swings = [swing for swing in reversed(swings) if swing["type"] == swing_type]
        for index, swing in enumerate(typed_swings, start=1):
            context[f"{swing_type}{index}"] = swing["price"]
            context[f"D{swing_type}{index}"] = swing["days_since"]

    return context


def validate_expression(expression, available_names=None):
    expression = expression.strip()
    if not expression:
        return False, "Expression cannot be blank."

    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        return False, exc.msg

    allowed_names = set(available_names or [])
    allowed_names.update(SAFE_FUNCTIONS.keys())

    for node in ast.walk(tree):
        if not isinstance(node, SAFE_NODE_TYPES):
            return False, f"Unsupported syntax: {type(node).__name__}"
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in SAFE_FUNCTIONS:
                return False, "Only abs(), min(), max(), and round() are supported."
        if isinstance(node, ast.Name) and node.id not in allowed_names and not is_pattern_variable(node.id):
            return False, f"Unknown variable: {node.id}"

    return True, ""


def evaluate_expression(expression, context):
    is_valid, error = validate_expression(expression, context.keys())
    if not is_valid:
        return False, error

    try:
        result = eval(
            compile(ast.parse(expression, mode="eval"), "<pattern-filter>", "eval"),
            {"__builtins__": {}},
            {**SAFE_FUNCTIONS, **context},
        )
    except (ArithmeticError, NameError, TypeError, ValueError, ZeroDivisionError) as exc:
        return False, str(exc)

    if isinstance(result, (bool, int, float)) and not isinstance(result, bool):
        if math.isnan(float(result)):
            return False, "Expression returned NaN."

    return bool(result), ""


def evaluate_pattern_filters(path, lookback_days, reversal_pct, expressions):
    expressions = [expression.strip() for expression in expressions if expression.strip()]
    df = load_price_data(path)
    return evaluate_pattern_filters_from_df(df, lookback_days, reversal_pct, expressions)


def evaluate_pattern_filters_from_df(df, lookback_days, reversal_pct, expressions):
    expressions = [expression.strip() for expression in expressions if expression.strip()]
    swings = detect_swings_from_df(df, lookback_days, reversal_pct)
    context = build_swing_context(df, swings)

    for expression in expressions:
        passed, error = evaluate_expression(expression, context)
        if not passed:
            return False, swings, error

    return True, swings, ""
