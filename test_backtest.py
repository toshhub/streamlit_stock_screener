import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from backtest import (
    _build_backtest_chart_annotations,
    _build_trade_gain_path,
    evaluate_sell_price_expression,
    run_backtest,
    validate_sell_price_expression,
)


class SellStrategyTests(unittest.TestCase):
    def setUp(self):
        self.df = pd.DataFrame({
            "Date": pd.to_datetime(["2026-01-01", "2026-01-02", "2026-01-03", "2026-01-04"]),
            "Open": [96, 101, 105, 110],
            "High": [102, 111, 113, 115],
            "Low": [94, 95, 103, 108],
            "Close": [100, 105, 111, 112],
        })
        self.calendar = [date.normalize() for date in self.df["Date"]]
        self.positions = {date: index for index, date in enumerate(self.calendar)}

    def test_percentage_and_candle_sell_prices_use_buy_date_context(self):
        buy_window = self.df.iloc[:2]

        target, error = evaluate_sell_price_expression("10%", buy_window, 105)
        self.assertEqual(error, "")
        self.assertAlmostEqual(target, 115.5)

        stop, error = evaluate_sell_price_expression("-10%", buy_window, 105)
        self.assertEqual(error, "")
        self.assertAlmostEqual(stop, 94.5)

        candle_stop, error = evaluate_sell_price_expression(
            "min(Candle[0..-1].Low) - 1%",
            buy_window,
            105,
        )
        self.assertEqual(error, "")
        self.assertAlmostEqual(candle_stop, 94 * 0.99)

    def test_intraday_target_books_at_target_and_locks_gain(self):
        gain_path, details = _build_trade_gain_path(
            self.df,
            self.calendar,
            self.positions,
            {"target": "10%", "stop_loss": "-20%", "closing_basis": False},
        )

        self.assertEqual(details["Exit Reason"], "Target")
        self.assertEqual(details["Exit Date"], self.calendar[1])
        self.assertAlmostEqual(details["Exit Price"], 110)
        self.assertEqual([point["Portfolio Gain %"] for point in gain_path], [0.0, 10.0, 10.0, 10.0])

    def test_closing_basis_waits_for_close_and_books_at_close(self):
        gain_path, details = _build_trade_gain_path(
            self.df,
            self.calendar,
            self.positions,
            {"target": "10%", "stop_loss": "", "closing_basis": True},
        )

        self.assertEqual(details["Exit Reason"], "Target")
        self.assertEqual(details["Exit Date"], self.calendar[2])
        self.assertEqual(details["Exit Price"], 111)
        self.assertEqual(gain_path[-1]["Portfolio Gain %"], 11.0)

        markers = _build_backtest_chart_annotations(gain_path)
        self.assertEqual([marker["label"] for marker in markers], ["BUY", "TARGET"])
        self.assertEqual(markers[-1]["date"], self.calendar[2])
        self.assertEqual(markers[-1]["price"], 111)

    def test_stop_loss_wins_when_both_levels_touch_same_candle(self):
        volatile_df = self.df.copy()
        volatile_df.loc[1, ["High", "Low"]] = [112, 89]

        _, details = _build_trade_gain_path(
            volatile_df,
            self.calendar,
            self.positions,
            {"target": "10%", "stop_loss": "-10%", "closing_basis": False},
        )

        self.assertEqual(details["Exit Reason"], "Stop Loss")
        self.assertEqual(details["Exit Price"], 90)

    def test_sma_stop_is_recalculated_for_each_future_candle_intraday(self):
        dates = pd.date_range("2026-03-01", periods=6, freq="D")
        df = pd.DataFrame({
            "Date": dates,
            "Open": [79, 89, 99, 118, 114, 109],
            "High": [82, 92, 102, 122, 116, 111],
            "Low": [78, 88, 98, 105, 108, 104],
            "Close": [80, 90, 100, 120, 115, 110],
        })
        positions = {date.normalize(): index for index, date in enumerate(dates)}
        calendar = [date.normalize() for date in dates[2:]]

        gain_path, details = _build_trade_gain_path(
            df,
            calendar,
            positions,
            {"target": "", "stop_loss": "SMA2", "closing_basis": False},
        )

        self.assertTrue(details["Dynamic Stop Loss"])
        self.assertEqual(details["Exit Reason"], "Stop Loss")
        self.assertEqual(details["Exit Date"], dates[3].normalize())
        self.assertAlmostEqual(details["Stop Loss Price"], 110.0)
        self.assertAlmostEqual(details["Exit Price"], 110.0)
        self.assertAlmostEqual(gain_path[1]["Stop Loss Price"], 110.0)

        anchored_price, anchored_error = evaluate_sell_price_expression(
            "SMA2 - Candle[0].Low + 90",
            df.iloc[:4],
            100,
            candle_anchor_position=2,
        )
        self.assertEqual(anchored_error, "")
        self.assertAlmostEqual(anchored_price, 102.0)

    def test_dynamic_sma_stop_uses_close_and_books_close_on_closing_basis(self):
        dates = pd.date_range("2026-04-01", periods=6, freq="D")
        df = pd.DataFrame({
            "Date": dates,
            "Open": [79, 89, 99, 104, 94, 94],
            "High": [82, 92, 102, 108, 96, 97],
            "Low": [78, 88, 98, 100, 89, 92],
            "Close": [80, 90, 100, 105, 90, 95],
        })
        positions = {date.normalize(): index for index, date in enumerate(dates)}
        calendar = [date.normalize() for date in dates[2:]]

        _, details = _build_trade_gain_path(
            df,
            calendar,
            positions,
            {"target": "", "stop_loss": "SMA2", "closing_basis": True},
        )

        self.assertEqual(details["Exit Reason"], "Stop Loss")
        self.assertEqual(details["Exit Date"], dates[4].normalize())
        self.assertAlmostEqual(details["Stop Loss Price"], 97.5)
        self.assertAlmostEqual(details["Exit Price"], 90.0)

    def test_chart_window_has_ten_trading_candles_on_each_side(self):
        dates = pd.bdate_range("2026-01-01", periods=35)
        df = pd.DataFrame({
            "Date": dates,
            "Open": range(100, 135),
            "High": range(102, 137),
            "Low": range(99, 134),
            "Close": range(101, 136),
        })
        positions = {date.normalize(): index for index, date in enumerate(dates)}
        calendar = [date.normalize() for date in dates[12:19]]

        _, details = _build_trade_gain_path(df, calendar, positions)

        self.assertEqual(details["Chart Start Date"], dates[2].normalize())
        self.assertEqual(details["Chart End Date"], dates[28].normalize())

    def test_sell_expression_validation_rejects_bad_percent_syntax(self):
        valid, error = validate_sell_price_expression("Candle[0].Low - percent")
        self.assertFalse(valid)
        self.assertTrue(error)

        valid, error = validate_sell_price_expression("Candle[0].Low - 1%")
        self.assertTrue(valid, error)

        valid, error = validate_sell_price_expression("-100%")
        self.assertFalse(valid)
        self.assertIn("greater than zero", error)

    def test_portfolio_final_gain_is_equal_weight_average(self):
        dates = pd.date_range("2026-01-01", periods=3, freq="D")
        stock_rows = {
            "AAA": [
                {"Date": str(dates[0]), "Open": 99, "High": 101, "Low": 98, "Close": 100},
                {"Date": str(dates[1]), "Open": 100, "High": 111, "Low": 99, "Close": 109},
                {"Date": str(dates[2]), "Open": 109, "High": 112, "Low": 108, "Close": 111},
            ],
            "BBB": [
                {"Date": str(dates[0]), "Open": 99, "High": 101, "Low": 98, "Close": 100},
                {"Date": str(dates[1]), "Open": 98, "High": 99, "Low": 94, "Close": 95},
                {"Date": str(dates[2]), "Open": 94, "High": 96, "Low": 89, "Close": 90},
            ],
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            paths = []
            for symbol, rows in stock_rows.items():
                path = Path(temp_dir) / f"{symbol}.json"
                path.write_text(json.dumps(rows), encoding="utf-8")
                paths.append(path)

            with patch("backtest.create_stock_chart", return_value=None):
                summary, series, _ = run_backtest(
                    paths,
                    {"All": []},
                    ["All"],
                    dates[0].date(),
                    dates[-1].date(),
                    sell_strategy={"target": "10%", "stop_loss": "", "closing_basis": False},
                )

        self.assertEqual(summary[0]["Portfolio Gain at End Date"], 0.0)
        self.assertEqual(series["All"][-1]["Stocks Found"], 2)

    def test_green_candle_only_applies_to_every_selected_filter(self):
        dates = pd.date_range("2026-02-02", periods=3, freq="D")
        stock_rows = {
            "GREEN": [
                {"Date": str(dates[0]), "Open": 99, "High": 102, "Low": 98, "Close": 100},
                {"Date": str(dates[1]), "Open": 100, "High": 106, "Low": 99, "Close": 105},
                {"Date": str(dates[2]), "Open": 105, "High": 111, "Low": 104, "Close": 110},
            ],
            "RED": [
                {"Date": str(dates[0]), "Open": 101, "High": 102, "Low": 98, "Close": 100},
                {"Date": str(dates[1]), "Open": 100, "High": 106, "Low": 99, "Close": 105},
                {"Date": str(dates[2]), "Open": 105, "High": 111, "Low": 104, "Close": 110},
            ],
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            paths = []
            for symbol, rows in stock_rows.items():
                path = Path(temp_dir) / f"{symbol}.json"
                path.write_text(json.dumps(rows), encoding="utf-8")
                paths.append(path)

            with patch("backtest.create_stock_chart", return_value=None):
                summary, _, details = run_backtest(
                    paths,
                    {"First": [], "Second": []},
                    ["First", "Second"],
                    dates[0].date(),
                    dates[-1].date(),
                    green_candle_only=True,
                )

        self.assertEqual([row["Stocks Found"] for row in summary], [1, 1])
        self.assertEqual([row["Symbol"] for row in details["First"]], ["GREEN"])
        self.assertEqual([row["Symbol"] for row in details["Second"]], ["GREEN"])


if __name__ == "__main__":
    unittest.main()
