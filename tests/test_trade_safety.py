import threading
import unittest
from unittest.mock import MagicMock, patch

import alpaca_trading_agent


class TradeSafetyTests(unittest.TestCase):
    def test_symbol_guard_blocks_concurrent_trade_for_same_symbol(self):
        guard = alpaca_trading_agent.SymbolTradeGuard()
        entered = threading.Event()
        release = threading.Event()
        result = []

        def hold_symbol():
            with guard.acquire("NVDA"):
                entered.set()
                release.wait(timeout=2)

        worker = threading.Thread(target=hold_symbol)
        worker.start()
        entered.wait(timeout=2)

        try:
            with self.assertRaisesRegex(RuntimeError, "already in progress"):
                with guard.acquire("nvda"):
                    result.append("unexpected")
        finally:
            release.set()
            worker.join(timeout=2)

        self.assertEqual(result, [])

    @patch("alpaca_trading_agent.time.monotonic")
    def test_rate_limiter_sleeps_between_api_calls(self, monotonic):
        sleeps = []
        monotonic.side_effect = [100.0, 100.0, 100.2, 101.0]
        limiter = alpaca_trading_agent.RateLimiter(max_calls=2, period_seconds=1.0, sleep_fn=sleeps.append)

        limiter.wait()
        limiter.wait()
        limiter.wait()

        self.assertEqual(len(sleeps), 1)
        self.assertAlmostEqual(sleeps[0], 0.8)

    def test_safe_executor_serializes_buy_with_trailing_stop(self):
        client = MagicMock()
        executor = alpaca_trading_agent.SafeTradeExecutor(client=client)

        with patch("alpaca_trading_agent.buy_with_trailing_stop", return_value="ok") as buy:
            result = executor.buy_with_trailing_stop("aapl", 1, trail_percent=4)

        self.assertEqual(result, "ok")
        buy.assert_called_once()
        self.assertIs(buy.call_args.kwargs["client"].client, client)
        self.assertEqual(buy.call_args.args[:2], ("AAPL", 1))

    def test_rate_limited_client_waits_before_api_method(self):
        client = MagicMock()
        limiter = MagicMock()
        wrapped = alpaca_trading_agent.RateLimitedTradingClient(client, limiter=limiter)

        wrapped.get_account()

        limiter.wait.assert_called_once()
        client.get_account.assert_called_once()

    def test_rate_limited_client_retries_after_429(self):
        class FakeRateLimitError(Exception):
            status_code = 429

        client = MagicMock()
        client.submit_order.side_effect = [FakeRateLimitError("too many requests"), "order"]
        sleeps = []
        wrapped = alpaca_trading_agent.RateLimitedTradingClient(
            client,
            limiter=MagicMock(),
            max_retries=1,
            retry_sleep_seconds=0.25,
            sleep_fn=sleeps.append,
        )

        result = wrapped.submit_order(order_data="request")

        self.assertEqual(result, "order")
        self.assertEqual(client.submit_order.call_count, 2)
        self.assertEqual(sleeps, [0.25])


if __name__ == "__main__":
    unittest.main()
