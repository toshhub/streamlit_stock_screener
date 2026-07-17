import unittest

import pandas as pd

from backtest import split_favorite_filter
from pattern import (
    build_ma_expression_context,
    evaluate_expression_filters_from_df,
    expression_uses_pe,
    validate_expression,
)


class MovingAverageExpressionTests(unittest.TestCase):
    def test_market_values_and_ma_statistics(self):
        df = pd.DataFrame({"Close": range(1, 11)})
        expressions = [
            "P == 10",
            "PE == 25",
            "SMA3 == 9",
            "round(ROI(3), 2) == 12.5",
            "MA_MIN(3, 4) == 6",
            "MA_MAX(3, 4) == 9",
            "round(MA_VAR(3, 4), 2) == 33.33",
        ]

        passed, error = evaluate_expression_filters_from_df(
            df,
            expressions,
            pe_ratio=25,
        )

        self.assertTrue(passed, error)

    def test_cross_days_returns_days_since_latest_bullish_cross(self):
        df = pd.DataFrame({"Close": [5, 4, 3, 2, 1, 2, 3, 4]})
        context = build_ma_expression_context(df)

        self.assertEqual(context["CD"](2, 3), 1.0)

    def test_decimal_function_arguments_are_supported(self):
        df = pd.DataFrame({"Close": range(1, 31)})
        expressions = [
            "MA_MAX(5.4, 10.2) >= MA_MIN(5.4, 10.2)",
            "ROI(5.4) > 0",
            "MA_VAR(5.4, 10.2) > 0",
        ]

        passed, error = evaluate_expression_filters_from_df(df, expressions)

        self.assertTrue(passed, error)

    def test_old_swing_variables_are_rejected(self):
        for expression in ("H1 > H2", "L1 > 0", "DH1 < 10", "DL1 < 10"):
            with self.subTest(expression=expression):
                valid, _ = validate_expression(expression)
                self.assertFalse(valid)

    def test_new_names_and_function_signatures_are_validated(self):
        valid_expressions = [
            "P > SMA200",
            "PE < 30",
            "CD(50, 200) < 40",
            "ROI(50) > 0",
            "MA_MIN(50, 120) > 0",
            "MA_MAX(50, 100) > 0",
            "MA_VAR(200, 150) > 15",
        ]
        invalid_expressions = [
            "SMA0 > 0",
            "CD(50) < 40",
            "ROI(-50) > 0",
            "MA_MIN(50, days) > 0",
            "H1 > H2",
        ]

        for expression in valid_expressions:
            with self.subTest(expression=expression):
                valid, error = validate_expression(expression)
                self.assertTrue(valid, error)

        for expression in invalid_expressions:
            with self.subTest(expression=expression):
                valid, _ = validate_expression(expression)
                self.assertFalse(valid)

    def test_pe_usage_detection(self):
        self.assertTrue(expression_uses_pe("P > SMA200 and PE < 25"))
        self.assertFalse(expression_uses_pe("P > SMA200"))

    def test_favorite_filter_round_trip_preserves_expressions(self):
        expressions = [
            "P > SMA200",
            "CD(50, 200) < 40",
            "MA_VAR(200, 150) > 15",
        ]
        favorite = {
            "ma_filter_set": [],
            "pattern": {
                "expressions": expressions,
            },
        }

        _, expression_settings = split_favorite_filter(favorite)

        self.assertEqual(expression_settings["expressions"], expressions)


if __name__ == "__main__":
    unittest.main()
