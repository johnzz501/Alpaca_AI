import unittest

import pandas as pd


class StrategyBacktestTests(unittest.TestCase):
    def test_generate_signals_marks_breakout_entries_and_exit_risk_levels(self):
        import strategy_backtest

        prices = pd.DataFrame(
            {
                "close": [10, 10.2, 10.4, 10.6, 10.8, 12.0],
                "high": [10.1, 10.3, 10.5, 10.7, 10.9, 12.2],
                "low": [9.8, 10.0, 10.2, 10.4, 10.6, 11.8],
                "volume": [100, 105, 110, 120, 130, 400],
            },
            index=pd.date_range("2026-01-01", periods=6, freq="D"),
        )
        config = strategy_backtest.BreakoutConfig(
            fast_window=2,
            slow_window=3,
            breakout_window=3,
            volume_window=3,
            volume_multiplier=2.0,
            stop_loss_pct=0.05,
            take_profit_pct=0.1,
        )

        signals = strategy_backtest.generate_breakout_signals(prices, config)

        self.assertEqual(signals["signal"].iloc[-1], 1)
        self.assertEqual(signals["entry_price"].iloc[-1], 12.0)
        self.assertEqual(signals["stop_loss"].iloc[-1], 11.4)
        self.assertEqual(signals["take_profit"].iloc[-1], 13.2)

    def test_backtest_signals_returns_closed_trade_and_equity_curve(self):
        import strategy_backtest

        signals = pd.DataFrame(
            {
                "close": [100.0, 103.0, 111.0, 109.0],
                "signal": [1, 0, 0, 0],
                "entry_price": [100.0, None, None, None],
                "stop_loss": [95.0, None, None, None],
                "take_profit": [110.0, None, None, None],
            },
            index=pd.date_range("2026-01-01", periods=4, freq="D"),
        )

        result = strategy_backtest.backtest_signals(signals, initial_cash=10_000, risk_per_trade=0.01)

        self.assertEqual(len(result.trades), 1)
        trade = result.trades.iloc[0]
        self.assertEqual(trade["exit_reason"], "take_profit")
        self.assertEqual(trade["qty"], 20)
        self.assertEqual(trade["pnl"], 200.0)
        self.assertEqual(result.equity_curve["equity"].iloc[-1], 10_200.0)

    def test_backtest_uses_intrabar_stop_and_trade_costs(self):
        import strategy_backtest

        signals = pd.DataFrame(
            {
                "open": [100.0, 100.0],
                "high": [101.0, 101.0],
                "low": [99.0, 94.0],
                "close": [100.0, 99.0],
                "signal": [1, 0],
                "entry_price": [100.0, None],
                "stop_loss": [95.0, None],
                "take_profit": [110.0, None],
            },
            index=pd.date_range("2026-01-01", periods=2, freq="D"),
        )

        result = strategy_backtest.backtest_signals(
            signals,
            initial_cash=10_000,
            risk_per_trade=0.01,
            slippage_pct=0.001,
            commission_per_share=0.01,
        )

        trade = result.trades.iloc[0]
        self.assertEqual(trade["exit_reason"], "stop_loss")
        self.assertGreater(trade["entry_price"], 100.0)
        self.assertLess(trade["exit_price"], 95.0)
        self.assertLess(trade["pnl"], -98.0)

    def test_multi_symbol_backtest_combines_symbol_results(self):
        import strategy_backtest

        dates = pd.date_range("2026-01-01", periods=3, freq="D")
        signals_by_symbol = {
            "AAA": pd.DataFrame(
                {
                    "open": [100.0, 100.0, 112.0],
                    "high": [101.0, 111.0, 113.0],
                    "low": [99.0, 99.0, 111.0],
                    "close": [100.0, 111.0, 112.0],
                    "signal": [1, 0, 0],
                    "entry_price": [100.0, None, None],
                    "stop_loss": [95.0, None, None],
                    "take_profit": [110.0, None, None],
                },
                index=dates,
            ),
            "BBB": pd.DataFrame(
                {
                    "open": [50.0, 50.0, 47.0],
                    "high": [51.0, 51.0, 48.0],
                    "low": [49.0, 47.0, 46.0],
                    "close": [50.0, 47.0, 47.0],
                    "signal": [1, 0, 0],
                    "entry_price": [50.0, None, None],
                    "stop_loss": [47.5, None, None],
                    "take_profit": [55.0, None, None],
                },
                index=dates,
            ),
        }

        result = strategy_backtest.backtest_multi_symbol(signals_by_symbol, initial_cash=20_000, risk_per_trade=0.005)

        self.assertEqual(set(result.trades["symbol"]), {"AAA", "BBB"})
        self.assertEqual(len(result.equity_curve), 3)


if __name__ == "__main__":
    unittest.main()
