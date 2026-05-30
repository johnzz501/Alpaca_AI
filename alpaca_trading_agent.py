import os
import threading
import time
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Callable, Optional, Tuple

from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading.requests import MarketOrderRequest, TrailingStopOrderRequest
from dotenv import load_dotenv


DEFAULT_TRAIL_PERCENT = 5.0


@dataclass
class BuyWithTrailingStopResult:
    entry_order: object
    filled_entry_order: object
    trailing_stop_order: object


class RateLimiter:
    """Small thread-safe rolling-window limiter for Alpaca API calls."""

    def __init__(
        self,
        max_calls: int = 180,
        period_seconds: float = 60.0,
        sleep_fn: Callable[[float], None] = time.sleep,
    ):
        if max_calls < 1:
            raise ValueError("max_calls must be at least 1")
        if period_seconds <= 0:
            raise ValueError("period_seconds must be greater than 0")
        self.max_calls = max_calls
        self.period_seconds = period_seconds
        self.sleep_fn = sleep_fn
        self._calls = deque()
        self._lock = threading.Lock()

    def wait(self) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                while self._calls and now - self._calls[0] >= self.period_seconds:
                    self._calls.popleft()

                if len(self._calls) < self.max_calls:
                    self._calls.append(now)
                    return

                sleep_for = self.period_seconds - (now - self._calls[0])

            self.sleep_fn(max(0.0, sleep_for))


class SymbolTradeGuard:
    """Prevent two local threads from opening/protecting the same symbol at once."""

    def __init__(self):
        self._active_symbols = set()
        self._lock = threading.Lock()

    @contextmanager
    def acquire(self, symbol: str):
        normalized = symbol.upper()
        with self._lock:
            if normalized in self._active_symbols:
                raise RuntimeError(f"trade already in progress for {normalized}")
            self._active_symbols.add(normalized)
        try:
            yield
        finally:
            with self._lock:
                self._active_symbols.discard(normalized)


def _is_rate_limit_error(exc: Exception) -> bool:
    status_code = getattr(exc, "status_code", None) or getattr(getattr(exc, "response", None), "status_code", None)
    return status_code == 429 or "rate limit" in str(exc).lower() or "too many requests" in str(exc).lower()


class RateLimitedTradingClient:
    """Proxy an Alpaca client so every API method is throttled and 429s are retried."""

    def __init__(
        self,
        client,
        limiter: Optional[RateLimiter] = None,
        max_retries: int = 2,
        retry_sleep_seconds: float = 1.0,
        sleep_fn: Callable[[float], None] = time.sleep,
    ):
        if max_retries < 0:
            raise ValueError("max_retries must be non-negative")
        self.client = client
        self.limiter = limiter or RateLimiter()
        self.max_retries = max_retries
        self.retry_sleep_seconds = retry_sleep_seconds
        self.sleep_fn = sleep_fn

    def __getattr__(self, name: str):
        attr = getattr(self.client, name)
        if not callable(attr):
            return attr

        def throttled_call(*args, **kwargs):
            attempts = 0
            while True:
                self.limiter.wait()
                try:
                    return attr(*args, **kwargs)
                except Exception as exc:
                    if not _is_rate_limit_error(exc) or attempts >= self.max_retries:
                        raise
                    attempts += 1
                    self.sleep_fn(self.retry_sleep_seconds * attempts)

        return throttled_call


class SafeTradeExecutor:
    """Wrap order workflows with symbol-level locking and process-wide throttling."""

    def __init__(
        self,
        client: Optional[TradingClient] = None,
        guard: Optional[SymbolTradeGuard] = None,
        rate_limiter: Optional[RateLimiter] = None,
    ):
        raw_client = client or create_trading_client()
        self.guard = guard or SymbolTradeGuard()
        self.rate_limiter = rate_limiter or RateLimiter()
        self.client = RateLimitedTradingClient(raw_client, limiter=self.rate_limiter)

    def buy_with_trailing_stop(
        self,
        symbol: str,
        qty: float,
        trail_percent: float = DEFAULT_TRAIL_PERCENT,
        **kwargs,
    ) -> BuyWithTrailingStopResult:
        normalized = symbol.upper()
        with self.guard.acquire(normalized):
            self.rate_limiter.wait()
            return buy_with_trailing_stop(
                normalized,
                qty,
                trail_percent=trail_percent,
                client=self.client,
                **kwargs,
            )


def load_credentials() -> Tuple[str, str]:
    """Load Alpaca credentials from .env or the current environment."""
    load_dotenv()

    api_key = os.getenv("ALPACA_API_KEY")
    secret_key = os.getenv("ALPACA_SECRET_KEY")

    missing = [
        name
        for name, value in (
            ("ALPACA_API_KEY", api_key),
            ("ALPACA_SECRET_KEY", secret_key),
        )
        if not value
    ]
    if missing:
        raise RuntimeError(f"Missing required environment variable(s): {', '.join(missing)}")

    return api_key, secret_key


def create_trading_client() -> TradingClient:
    api_key, secret_key = load_credentials()
    return TradingClient(api_key, secret_key, paper=True)


def print_account_status(client: TradingClient) -> None:
    account = client.get_account()
    print(f"Account status: {account.status}")
    print(f"Buying power: {account.buying_power}")


def place_market_order(
    symbol: str,
    qty: float,
    client: Optional[TradingClient] = None,
):
    """Submit a US stock market buy order in Alpaca paper trading."""
    if qty <= 0:
        raise ValueError("qty must be greater than 0")

    trading_client = client or create_trading_client()
    order_data = MarketOrderRequest(
        symbol=symbol.upper(),
        qty=float(qty),
        side=OrderSide.BUY,
        time_in_force=TimeInForce.DAY,
    )

    try:
        order = trading_client.submit_order(order_data=order_data)
        print(f"Order submitted: id={order.id}, symbol={symbol.upper()}, qty={qty}, status={order.status}")
        return order
    except Exception as exc:
        print(f"Failed to submit market order for {symbol.upper()}: {exc}")
        raise


def _order_status_value(status) -> str:
    return getattr(status, "value", str(status)).lower()


def wait_for_order_fill(
    client: TradingClient,
    order_id: str,
    timeout_seconds: int = 300,
    poll_interval_seconds: int = 5,
    sleep_fn: Callable[[float], None] = time.sleep,
):
    deadline = time.monotonic() + timeout_seconds
    terminal_failure_statuses = {"canceled", "expired", "rejected", "suspended"}

    while time.monotonic() <= deadline:
        order = client.get_order_by_id(order_id)
        status = _order_status_value(order.status)

        if status == "filled":
            return order
        if status in terminal_failure_statuses:
            raise RuntimeError(f"Order {order_id} ended with status: {status}")

        print(f"Waiting for order {order_id} to fill. Current status: {status}")
        sleep_fn(poll_interval_seconds)

    raise TimeoutError(f"Order {order_id} was not filled within {timeout_seconds} seconds")


def place_trailing_stop_order(
    symbol: str,
    qty: float,
    trail_percent: float = DEFAULT_TRAIL_PERCENT,
    client: Optional[TradingClient] = None,
):
    """Submit a GTC trailing stop sell order that trails the high-water mark by trail_percent."""
    if qty <= 0:
        raise ValueError("qty must be greater than 0")
    if trail_percent <= 0:
        raise ValueError("trail_percent must be greater than 0")

    trading_client = client or create_trading_client()
    order_data = TrailingStopOrderRequest(
        symbol=symbol.upper(),
        qty=float(qty),
        side=OrderSide.SELL,
        time_in_force=TimeInForce.GTC,
        trail_percent=float(trail_percent),
    )

    try:
        order = trading_client.submit_order(order_data=order_data)
        print(
            "Trailing stop submitted: "
            f"id={order.id}, symbol={symbol.upper()}, qty={qty}, trail_percent={trail_percent}, status={order.status}"
        )
        return order
    except Exception as exc:
        print(f"Failed to submit trailing stop order for {symbol.upper()}: {exc}")
        raise


def buy_with_trailing_stop(
    symbol: str,
    qty: float,
    trail_percent: float = DEFAULT_TRAIL_PERCENT,
    client: Optional[TradingClient] = None,
    fill_timeout_seconds: int = 300,
    fill_poll_interval_seconds: int = 5,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> BuyWithTrailingStopResult:
    trading_client = client or create_trading_client()
    entry_order = place_market_order(symbol, qty, client=trading_client)
    filled_entry_order = wait_for_order_fill(
        trading_client,
        str(entry_order.id),
        timeout_seconds=fill_timeout_seconds,
        poll_interval_seconds=fill_poll_interval_seconds,
        sleep_fn=sleep_fn,
    )
    trailing_stop_order = place_trailing_stop_order(
        symbol,
        qty,
        trail_percent=trail_percent,
        client=trading_client,
    )

    return BuyWithTrailingStopResult(
        entry_order=entry_order,
        filled_entry_order=filled_entry_order,
        trailing_stop_order=trailing_stop_order,
    )


def main() -> None:
    trading_client = create_trading_client()
    print_account_status(trading_client)

    # Example paper-trading order. Uncomment only when you intend to send it.
    # place_market_order("AAPL", 1, client=trading_client)
    # buy_with_trailing_stop("NVDA", 1, trail_percent=5, client=trading_client)


if __name__ == "__main__":
    main()
