import unittest

import pandas as pd

from backtest import split_favorite_filter
from pattern import (
    build_ma_expression_context,
    evaluate_expression_filters_from_df,
    expression_uses_pe,
    validate_expression,
)
from screener import (
    custom_filter_expressions,
    merge_legacy_expression_filters,
    required_ma_periods,
    screen_dataframe,
)


class MovingAverageExpressionTests(unittest.TestCase):
    def test_price_near_filter_adds_custom_expression_compatible_roi_for_its_sma(self):
        df = pd.DataFrame({
            "Close": [1.0, 2.0, 3.0, 4.0],
            "Open": [0.9, 1.9, 2.9, 3.9],
        })
        filter_set = [{
            "id": 1,
            "type": "price_near_long",
            "params": {"long_ma": 2, "threshold_pct": 20.0},
        }]

        result = screen_dataframe(
            df,
            "TEST",
            filter_set=filter_set,
            include_pe=False,
        )

        self.assertIsNotNone(result)
        self.assertEqual(result["ROI2"], 40.0)

    def test_custom_filter_reports_every_ma_period_for_charting(self):
        filter_set = [
            {
                "id": 1,
                "type": "custom_expression",
                "params": {
                    "expression": (
                        "SMA29 > SMA50 and CD(50, 200) < 20 and "
                        "ROI(100) > 0 and MA_MIN(150, 30) > 0"
                    )
                },
            }
        ]

        self.assertEqual(required_ma_periods(filter_set), [29, 50, 100, 150, 200])

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

    def test_candle_offsets_ranges_fields_and_is_green(self):
        df = pd.DataFrame({
            "Open": [9, 11, 10, 13, 14, 14],
            "High": [11, 13, 14, 15, 16, 18],
            "Low": [8, 10, 9, 12, 13, 13],
            "Close": [10, 12, 13, 14, 15, 17],
        })
        expressions = [
            "Candle[0].Close == 17",
            "Candle[-1].High == 16",
            "Candle[-2].Low == 12",
            "IsGreen(Candle[0])",
            "len(Candle[0..-4]) == 5",
        ]

        # len() intentionally remains unavailable; range lists are consumed
        # through the already-safe min()/max() functions instead.
        valid, _ = validate_expression(expressions[-1])
        self.assertFalse(valid)
        passed, error = evaluate_expression_filters_from_df(df, expressions[:-1])
        self.assertTrue(passed, error)

        passed, error = evaluate_expression_filters_from_df(
            df,
            [
                "min(Candle[0..-4].Low) == 9",
                "max(Candle[0..4].Close) == 17",
            ],
        )
        self.assertTrue(passed, error)

    def test_candle_dsl_rejects_future_offsets_and_unsafe_attributes(self):
        invalid_expressions = [
            "Candle[1].Close > 0",
            "Candle[-4..0].Low",
            "Candle[0].__class__",
            "IsGreen(Candle[0..-2])",
        ]

        for expression in invalid_expressions:
            with self.subTest(expression=expression):
                valid, _ = validate_expression(expression)
                self.assertFalse(valid)

    def test_unavailable_historical_candle_returns_a_clear_error(self):
        df = pd.DataFrame({
            "Open": [10],
            "High": [12],
            "Low": [9],
            "Close": [11],
        })

        passed, error = evaluate_expression_filters_from_df(
            df,
            ["Candle[-1].Close > 0"],
        )

        self.assertFalse(passed)
        self.assertIn("unavailable", error)

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

    def test_custom_filters_round_trip_as_regular_filter_rows(self):
        favorite = [
            {
                "id": 7,
                "type": "custom_expression",
                "params": {"expression": "P > SMA200"},
            },
            {
                "id": 8,
                "type": "custom_expression",
                "params": {"expression": "ROI(50) > 0"},
            },
        ]

        filter_set, pattern_settings = split_favorite_filter(favorite)

        self.assertEqual(len(filter_set), 2)
        self.assertEqual(
            pattern_settings["expressions"],
            ["P > SMA200", "ROI(50) > 0"],
        )

    def test_legacy_expressions_are_migrated_without_duplicate_rows(self):
        filter_set = [
            {
                "id": 3,
                "type": "custom_expression",
                "params": {"expression": "P > SMA200"},
            }
        ]

        migrated = merge_legacy_expression_filters(
            filter_set,
            ["P > SMA200", "PE < 30"],
        )

        self.assertEqual(
            custom_filter_expressions(migrated),
            ["P > SMA200", "PE < 30"],
        )
        self.assertEqual([item["id"] for item in migrated], [3, 4])


if __name__ == "__main__":
    unittest.main()
