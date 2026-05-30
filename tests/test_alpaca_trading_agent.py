import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

from alpaca.trading.enums import OrderSide, OrderStatus, OrderType, TimeInForce

import alpaca_trading_agent


class PaperTradeFromScanTests(unittest.TestCase):
    def test_daily_buy_count_resets_after_us_market_close(self):
        import paper_trade_from_scan

        ny = ZoneInfo("America/New_York")
        before_close = paper_trade_from_scan._ny_trade_day_start(
            paper_trade_from_scan.datetime(2026, 5, 22, 10, 0, tzinfo=ny)
        )
        self.assertEqual(before_close, paper_trade_from_scan.datetime(2026, 5, 21, 16, 0, tzinfo=ny))

        after_close = paper_trade_from_scan._ny_trade_day_start(
            paper_trade_from_scan.datetime(2026, 5, 22, 21, 0, tzinfo=ny)
        )
        self.assertEqual(after_close, paper_trade_from_scan.datetime(2026, 5, 22, 16, 0, tzinfo=ny))

    def test_parse_scan_markdown_extracts_trade_setups(self):
        import paper_trade_from_scan

        markdown = """
| 市場 (US/TW) | 股票代號 | 公司名稱 | 偵測型態 | 進場價格 | 停損 (SL) | 停利 (TP) | 風險報酬比 | 設定說明與理由 |
|---|---|---|---|---:|---:|---:|---:|---|
| US | [MASI](https://www.tradingview.com/symbols/NASDAQ-MASI/) | Masimo Corporation | VCP 波動收縮 | 179.21 | 177.36 | 215.06 | 1:19.38 | 測試 |
"""

        setups = paper_trade_from_scan.parse_scan_markdown(markdown)

        self.assertEqual(len(setups), 1)
        self.assertEqual(setups[0].symbol, "MASI")
        self.assertEqual(setups[0].entry, 179.21)
        self.assertEqual(setups[0].stop_loss, 177.36)
        self.assertEqual(setups[0].take_profit, 215.06)
        self.assertEqual(setups[0].reward_risk, 19.38)

    def test_builds_paper_bracket_stop_limit_order(self):
        import paper_trade_from_scan
        from alpaca.trading.enums import OrderClass, OrderSide, OrderType, TimeInForce

        setup = paper_trade_from_scan.TradeSetup(
            market="US",
            symbol="MASI",
            company_name="Masimo Corporation",
            pattern="VCP",
            entry=100.0,
            stop_loss=96.0,
            take_profit=112.0,
            reward_risk=3.0,
            rationale="test",
        )

        request = paper_trade_from_scan.build_bracket_stop_limit_order(setup, qty=2, limit_buffer=0.002)

        self.assertEqual(request.symbol, "MASI")
        self.assertEqual(request.qty, 2.0)
        self.assertEqual(request.side, OrderSide.BUY)
        self.assertEqual(request.type, OrderType.STOP_LIMIT)
        self.assertEqual(request.time_in_force, TimeInForce.DAY)
        self.assertEqual(request.order_class, OrderClass.BRACKET)
        self.assertEqual(request.stop_price, 100.0)
        self.assertEqual(request.limit_price, 100.2)
        self.assertEqual(request.take_profit.limit_price, 112.0)
        self.assertEqual(request.stop_loss.stop_price, 96.0)

    def test_execute_paper_trades_can_enforce_optional_daily_trade_limit(self):
        import paper_trade_from_scan

        fake_client = MagicMock()
        fake_client.get_account.return_value = MagicMock(equity="100000", last_equity="100100")
        fake_client.get_orders.return_value = []
        fake_client.get_all_positions.return_value = []
        fake_client.submit_order.side_effect = [
            MagicMock(id=f"order-{index}", status="accepted") for index in range(10)
        ]
        fake_client.get_order_by_id.return_value = MagicMock(status=OrderStatus.FILLED, filled_qty="1")
        setups = [
            paper_trade_from_scan.TradeSetup("US", f"SYM{index}", "Company", "VCP", 100, 96, 112, 3, "ok")
            for index in range(6)
        ]

        results = paper_trade_from_scan.execute_paper_trades(
            fake_client,
            setups,
            qty=1,
            max_qty=1,
            max_new_trades=5,
        )

        self.assertEqual(fake_client.submit_order.call_count, 5)
        self.assertEqual([result.action for result in results].count("BUY_BRACKET_SUBMITTED"), 5)
        self.assertEqual(results[-1].action, "HOLD")
        self.assertIn("每日最大交易檔數", results[-1].reason)

    def test_execute_paper_trades_has_default_daily_trade_limit(self):
        import paper_trade_from_scan

        fake_client = MagicMock()
        fake_client.get_account.return_value = MagicMock(equity="100000", last_equity="100100")
        fake_client.get_orders.return_value = []
        fake_client.get_all_positions.return_value = []
        fake_client.submit_order.side_effect = [
            MagicMock(id=f"order-{index}", status="accepted") for index in range(6)
        ]
        fake_client.get_order_by_id.return_value = MagicMock(status=OrderStatus.FILLED, filled_qty="1")
        setups = [
            paper_trade_from_scan.TradeSetup("US", f"SYM{index}", "Company", "VCP", 100, 96, 112, 3, "ok")
            for index in range(6)
        ]

        results = paper_trade_from_scan.execute_paper_trades(fake_client, setups, qty=1, max_qty=1)

        self.assertEqual([result.action for result in results].count("BUY_BRACKET_SUBMITTED"), 5)
        self.assertIn("每日最大交易檔數", results[-1].reason)

    def test_execute_paper_trades_blocks_when_daily_loss_limit_is_reached(self):
        import paper_trade_from_scan

        fake_client = MagicMock()
        fake_client.get_account.return_value = MagicMock(equity="99499.99", last_equity="100000")
        fake_client.get_orders.return_value = []
        fake_client.get_all_positions.return_value = []
        setup = paper_trade_from_scan.TradeSetup("US", "MASI", "Masimo", "VCP", 100, 96, 112, 3, "ok")

        results = paper_trade_from_scan.execute_paper_trades(fake_client, [setup], qty=1)

        fake_client.submit_order.assert_not_called()
        self.assertEqual(results[0].action, "HOLD")
        self.assertIn("每日最大虧損限制", results[0].reason)

    def test_recommended_qty_scales_from_one_to_twenty_by_setup_quality(self):
        import paper_trade_from_scan

        weak_setup = paper_trade_from_scan.TradeSetup(
            "US",
            "WEAK",
            "Weak Setup",
            "多頭反轉收復",
            100,
            96,
            109,
            2.25,
            "以最新收盤價作為可成交進場基準",
        )
        strong_setup = paper_trade_from_scan.TradeSetup(
            "US",
            "STRONG",
            "Strong Setup",
            "VCP 波動收縮 + 底部突破",
            100,
            98,
            125,
            12.5,
            "以最新收盤價作為可成交進場基準; 成交量為 20 日均量 2.4 倍",
        )

        self.assertEqual(paper_trade_from_scan.recommended_qty(weak_setup, max_qty=20), 1)
        self.assertEqual(paper_trade_from_scan.recommended_qty(strong_setup, max_qty=20), 20)

    def test_execute_paper_trades_uses_recommended_qty_for_market_buy(self):
        import paper_trade_from_scan

        fake_client = MagicMock()
        fake_client.get_account.return_value = MagicMock(equity="100000", last_equity="100100")
        fake_client.get_orders.return_value = []
        fake_client.get_all_positions.return_value = []
        fake_client.submit_order.return_value = MagicMock(id="buy-1", status=OrderStatus.ACCEPTED)
        fake_client.get_order_by_id.return_value = MagicMock(status=OrderStatus.FILLED, filled_qty="20")
        setup = paper_trade_from_scan.TradeSetup(
            "US",
            "STRONG",
            "Strong Setup",
            "VCP 波動收縮 + 底部突破",
            100,
            98,
            125,
            12.5,
            "以最新收盤價作為可成交進場基準; 成交量為 20 日均量 2.4 倍",
        )

        paper_trade_from_scan.execute_paper_trades(fake_client, [setup], qty=1, max_qty=20)

        buy_request = fake_client.submit_order.call_args.kwargs["order_data"]
        self.assertEqual(buy_request.qty, 20.0)
        self.assertEqual(buy_request.order_class.value, "bracket")

    def test_risk_based_qty_caps_position_value_and_total_portfolio_risk(self):
        import paper_trade_from_scan

        setup = paper_trade_from_scan.TradeSetup("US", "RISK", "Risk", "VCP", 100, 95, 115, 3, "ok")
        account = MagicMock(equity="100000")
        qty = paper_trade_from_scan.risk_based_qty(
            setup,
            account,
            risk_per_trade=0.01,
            max_qty=500,
            max_position_value_pct=0.10,
            remaining_portfolio_risk=350,
        )

        self.assertEqual(qty, 70)

    def test_market_order_flattens_position_when_protection_order_fails(self):
        import paper_trade_from_scan

        fake_client = MagicMock()
        fake_client.get_account.return_value = MagicMock(equity="100000", last_equity="100100")
        fake_client.get_orders.return_value = []
        fake_client.get_all_positions.return_value = []
        fake_client.submit_order.side_effect = [
            MagicMock(id="buy-1", status=OrderStatus.ACCEPTED),
            RuntimeError("trailing stop rejected"),
            MagicMock(id="flatten-1", status=OrderStatus.ACCEPTED),
        ]
        fake_client.get_order_by_id.return_value = MagicMock(status=OrderStatus.FILLED, filled_qty="2")
        setup = paper_trade_from_scan.TradeSetup("US", "SAFE", "Safe", "VCP", 100, 95, 115, 3, "ok")

        results = paper_trade_from_scan.execute_paper_trades(
            fake_client,
            [setup],
            max_qty=2,
            order_mode="market",
            flatten_on_protection_error=True,
        )

        self.assertEqual([result.action for result in results], ["BUY_MARKET_SUBMITTED", "PROTECTION_ERROR", "FLATTEN_SUBMITTED"])
        flatten_request = fake_client.submit_order.call_args_list[-1].kwargs["order_data"]
        self.assertEqual(flatten_request.side, OrderSide.SELL)
        self.assertEqual(flatten_request.symbol, "SAFE")

    def test_execute_paper_trades_writes_trade_audit_log(self):
        import paper_trade_from_scan

        fake_client = MagicMock()
        fake_client.get_account.return_value = MagicMock(equity="100000", last_equity="100100")
        fake_client.get_orders.return_value = []
        fake_client.get_all_positions.return_value = []
        fake_client.submit_order.return_value = MagicMock(id="bracket-1", status=OrderStatus.ACCEPTED)
        setup = paper_trade_from_scan.TradeSetup("US", "LOG", "Log", "VCP", 100, 95, 115, 3, "ok")

        with TemporaryDirectory() as tmpdir:
            log_path = Path(tmpdir) / "trades.csv"
            paper_trade_from_scan.execute_paper_trades(fake_client, [setup], max_qty=1, audit_log_path=log_path)
            text = log_path.read_text(encoding="utf-8")

        self.assertIn("event,symbol,action", text)
        self.assertIn("order_submitted,LOG,BUY_BRACKET_SUBMITTED", text)

    def test_reconcile_missing_trailing_stops_submits_for_unprotected_positions(self):
        import paper_trade_from_scan

        fake_client = MagicMock()
        fake_client.get_all_positions.return_value = [
            MagicMock(symbol="WELL", qty="2"),
            MagicMock(symbol="CWAN", qty="4"),
        ]
        fake_client.get_orders.return_value = [
            MagicMock(symbol="CWAN", side=OrderSide.SELL, type=OrderType.TRAILING_STOP)
        ]
        fake_client.submit_order.return_value = MagicMock(id="trail-well", status=OrderStatus.NEW)

        results = paper_trade_from_scan.reconcile_missing_trailing_stops(
            fake_client,
            trail_percent=5.0,
        )

        self.assertEqual([result.symbol for result in results], ["WELL"])
        self.assertEqual(results[0].action, "TRAILING_STOP_SUBMITTED")
        fake_client.submit_order.assert_called_once()
        request = fake_client.submit_order.call_args.kwargs["order_data"]
        self.assertEqual(request.symbol, "WELL")
        self.assertEqual(request.qty, 2.0)
        self.assertEqual(request.side, OrderSide.SELL)
        self.assertEqual(request.type, OrderType.TRAILING_STOP)
        self.assertEqual(request.time_in_force, TimeInForce.GTC)
        self.assertEqual(request.trail_percent, 5.0)

    def test_profit_taker_sells_half_at_twenty_percent_and_reprotects_remaining_qty(self):
        import paper_trade_from_scan

        fake_client = MagicMock()
        fake_client.get_all_positions.return_value = [
            MagicMock(symbol="GAIN", qty="10", unrealized_plpc="0.21")
        ]
        fake_client.get_orders.return_value = [
            MagicMock(id="old-trail", symbol="GAIN", side=OrderSide.SELL, type=OrderType.TRAILING_STOP)
        ]
        fake_client.submit_order.side_effect = [
            MagicMock(id="sell-half", status=OrderStatus.ACCEPTED),
            MagicMock(id="new-trail", status=OrderStatus.NEW),
        ]
        state = {}

        results = paper_trade_from_scan.execute_profit_takes(fake_client, state=state)

        self.assertEqual([result.action for result in results], ["PROFIT_TAKE_SUBMITTED", "TRAILING_STOP_SUBMITTED"])
        fake_client.cancel_order_by_id.assert_called_once_with("old-trail")
        sell_request = fake_client.submit_order.call_args_list[0].kwargs["order_data"]
        trail_request = fake_client.submit_order.call_args_list[1].kwargs["order_data"]
        self.assertEqual(sell_request.symbol, "GAIN")
        self.assertEqual(sell_request.qty, 5.0)
        self.assertEqual(sell_request.side, OrderSide.SELL)
        self.assertEqual(sell_request.time_in_force, TimeInForce.DAY)
        self.assertEqual(trail_request.qty, 5.0)
        self.assertEqual(state["GAIN"], 20)

    def test_profit_taker_sells_remaining_at_fifty_percent_once(self):
        import paper_trade_from_scan

        fake_client = MagicMock()
        fake_client.get_all_positions.return_value = [
            MagicMock(symbol="MOON", qty="5", unrealized_plpc="0.52")
        ]
        fake_client.get_orders.return_value = [
            MagicMock(id="moon-trail", symbol="MOON", side=OrderSide.SELL, type=OrderType.TRAILING_STOP)
        ]
        fake_client.submit_order.return_value = MagicMock(id="sell-rest", status=OrderStatus.ACCEPTED)
        state = {"MOON": 20}

        results = paper_trade_from_scan.execute_profit_takes(fake_client, state=state)

        self.assertEqual([result.action for result in results], ["PROFIT_TAKE_SUBMITTED"])
        fake_client.cancel_order_by_id.assert_called_once_with("moon-trail")
        sell_request = fake_client.submit_order.call_args.kwargs["order_data"]
        self.assertEqual(sell_request.symbol, "MOON")
        self.assertEqual(sell_request.qty, 5.0)
        self.assertEqual(sell_request.side, OrderSide.SELL)
        self.assertEqual(state["MOON"], 50)

        fake_client.submit_order.reset_mock()
        results = paper_trade_from_scan.execute_profit_takes(fake_client, state=state)

        fake_client.submit_order.assert_not_called()
        self.assertEqual(results, [])

    def test_premarket_fill_timeout_extends_until_after_regular_open(self):
        import paper_trade_from_scan

        ny = ZoneInfo("America/New_York")
        now = paper_trade_from_scan.datetime(2026, 5, 26, 4, 1, tzinfo=ny)

        timeout = paper_trade_from_scan.fill_timeout_seconds(now=now)

        self.assertEqual(timeout, 21540)

    def test_main_can_run_reconcile_only_without_scan(self):
        import paper_trade_from_scan

        fake_client = MagicMock()
        fake_results = [paper_trade_from_scan.PaperTradeResult("WELL", "TRAILING_STOP_SUBMITTED", "ok")]

        with patch("alpaca_trading_agent.create_trading_client", return_value=fake_client), patch(
            "paper_trade_from_scan.reconcile_missing_trailing_stops", return_value=fake_results
        ) as reconcile, patch("paper_trade_from_scan.run_scan") as run_scan, patch(
            "paper_trade_from_scan.print_results"
        ) as print_results:
            paper_trade_from_scan.main(["--reconcile-only"])

        run_scan.assert_not_called()
        reconcile.assert_called_once_with(fake_client, trail_percent=5.0)
        print_results.assert_called_once_with(fake_results)

    def test_main_regular_hours_only_skips_scan_outside_us_market_hours(self):
        import paper_trade_from_scan

        fake_client = MagicMock()
        fake_results = []
        ny = ZoneInfo("America/New_York")
        outside_hours = paper_trade_from_scan.datetime(2026, 5, 26, 8, 0, tzinfo=ny)

        with patch("alpaca_trading_agent.create_trading_client", return_value=fake_client), patch(
            "paper_trade_from_scan.reconcile_missing_trailing_stops", return_value=fake_results
        ) as reconcile, patch("paper_trade_from_scan.run_scan") as run_scan, patch(
            "paper_trade_from_scan.print_results"
        ) as print_results, patch("paper_trade_from_scan.now_new_york", return_value=outside_hours):
            paper_trade_from_scan.main(["--regular-hours-only"])

        run_scan.assert_not_called()
        reconcile.assert_called_once_with(fake_client, trail_percent=5.0)
        printed_results = print_results.call_args.args[0]
        self.assertEqual(printed_results[0].action, "HOLD")
        self.assertIn("美股 regular hours 尚未開盤", printed_results[0].reason)

    def test_main_includes_intraday_scanner_setups(self):
        import intraday_scanner
        import paper_trade_from_scan

        fake_client = MagicMock()
        fake_client.get_account.return_value = MagicMock(equity="100000", last_equity="100100")
        fake_client.get_orders.return_value = []
        fake_client.get_all_positions.return_value = []
        fake_client.submit_order.side_effect = [
            MagicMock(id="buy-intraday", status=OrderStatus.ACCEPTED),
            MagicMock(id="trail-intraday", status=OrderStatus.NEW),
        ]
        fake_client.get_order_by_id.return_value = MagicMock(status=OrderStatus.FILLED, filled_qty="1")
        intraday_setup = intraday_scanner.IntradaySetup(
            symbol="MOMO",
            entry=50.0,
            stop_loss=47.5,
            take_profit=55.0,
            reward_risk=2.0,
            gap_percent=2.5,
            momentum_percent=1.8,
            volume_spike=3.2,
            score=7.5,
            rationale="gap 2.50%, intraday momentum 1.80%, volume spike 3.20x",
        )

        with patch("alpaca_trading_agent.create_trading_client", return_value=fake_client), patch(
            "paper_trade_from_scan.run_scan", return_value=""
        ), patch("paper_trade_from_scan.reconcile_missing_trailing_stops", return_value=[]), patch(
            "paper_trade_from_scan.print_results"
        ), patch("intraday_scanner.scan_intraday_setups", return_value=[intraday_setup]) as intraday_scan:
            paper_trade_from_scan.main(["--intraday-scan"])

        intraday_scan.assert_called_once()
        buy_request = fake_client.submit_order.call_args_list[0].kwargs["order_data"]
        self.assertEqual(buy_request.symbol, "MOMO")


class LoadCredentialsTests(unittest.TestCase):
    @patch.dict(os.environ, {}, clear=True)
    @patch("alpaca_trading_agent.load_dotenv")
    def test_load_credentials_requires_api_key_and_secret(self, _load_dotenv):
        with self.assertRaisesRegex(RuntimeError, "ALPACA_API_KEY"):
            alpaca_trading_agent.load_credentials()


class PlaceMarketOrderTests(unittest.TestCase):
    @patch("builtins.print")
    def test_place_market_order_submits_day_buy_market_order(self, _print):
        fake_client = MagicMock()
        fake_order = MagicMock(id="order-123", status="accepted")
        fake_client.submit_order.return_value = fake_order

        order = alpaca_trading_agent.place_market_order("AAPL", 2, client=fake_client)

        self.assertIs(order, fake_order)
        fake_client.submit_order.assert_called_once()
        request = fake_client.submit_order.call_args.kwargs["order_data"]
        self.assertEqual(request.symbol, "AAPL")
        self.assertEqual(request.qty, 2.0)
        self.assertEqual(request.side, OrderSide.BUY)
        self.assertEqual(request.time_in_force, TimeInForce.DAY)


class TrailingStopOrderTests(unittest.TestCase):
    @patch("builtins.print")
    def test_place_trailing_stop_order_submits_gtc_sell_order_with_five_percent_trail(self, _print):
        fake_client = MagicMock()
        fake_order = MagicMock(id="exit-123", status="new")
        fake_client.submit_order.return_value = fake_order

        order = alpaca_trading_agent.place_trailing_stop_order("nvda", 1, client=fake_client)

        self.assertIs(order, fake_order)
        fake_client.submit_order.assert_called_once()
        request = fake_client.submit_order.call_args.kwargs["order_data"]
        self.assertEqual(request.symbol, "NVDA")
        self.assertEqual(request.qty, 1.0)
        self.assertEqual(request.side, OrderSide.SELL)
        self.assertEqual(request.type, OrderType.TRAILING_STOP)
        self.assertEqual(request.time_in_force, TimeInForce.GTC)
        self.assertEqual(request.trail_percent, 5.0)

    @patch("builtins.print")
    def test_buy_with_trailing_stop_waits_for_fill_then_places_exit_order(self, _print):
        fake_client = MagicMock()
        entry_order = MagicMock(id="entry-123", status=OrderStatus.NEW)
        filled_order = MagicMock(id="entry-123", status=OrderStatus.FILLED)
        exit_order = MagicMock(id="exit-123", status=OrderStatus.NEW)
        fake_client.submit_order.side_effect = [entry_order, exit_order]
        fake_client.get_order_by_id.return_value = filled_order

        result = alpaca_trading_agent.buy_with_trailing_stop(
            "NVDA",
            1,
            client=fake_client,
            fill_timeout_seconds=1,
            fill_poll_interval_seconds=0,
            sleep_fn=lambda _seconds: None,
        )

        self.assertEqual(result.entry_order, entry_order)
        self.assertEqual(result.trailing_stop_order, exit_order)
        fake_client.get_order_by_id.assert_called_once_with("entry-123")
        self.assertEqual(fake_client.submit_order.call_count, 2)
        entry_request = fake_client.submit_order.call_args_list[0].kwargs["order_data"]
        exit_request = fake_client.submit_order.call_args_list[1].kwargs["order_data"]
        self.assertEqual(entry_request.side, OrderSide.BUY)
        self.assertEqual(exit_request.side, OrderSide.SELL)
        self.assertEqual(exit_request.trail_percent, 5.0)


if __name__ == "__main__":
    unittest.main()
