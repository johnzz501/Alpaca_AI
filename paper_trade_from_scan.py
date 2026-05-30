#!/usr/bin/env python3
import argparse
import csv
import json
import os
import re
import subprocess
import sys
import time as time_module
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

from alpaca.trading.enums import OrderClass, OrderSide, OrderType, QueryOrderStatus, TimeInForce
from alpaca.trading.requests import (
    GetOrdersRequest,
    MarketOrderRequest,
    StopLimitOrderRequest,
    TrailingStopOrderRequest,
    StopLossRequest,
    TakeProfitRequest,
)

import alpaca_trading_agent
import intraday_scanner


DEFAULT_SCAN_SCRIPT = (
    "/Users/xuanren/Documents/Codex/2026-05-18/"
    "role-you-are-a-quant-trading-2/scan_tradingview_setups.py"
)
DEFAULT_SCAN_ARGS = ["--markets", "US", "--entry-mode", "executable"]
DEFAULT_QTY = 1.0
DEFAULT_MAX_QTY = 20
DEFAULT_MAX_NEW_TRADES = 5
DEFAULT_MAX_OPEN_POSITIONS = 8
DEFAULT_DAILY_LOSS_LIMIT = 500.0
DEFAULT_LIMIT_BUFFER = 0.002
DEFAULT_TRAIL_PERCENT = 5.0
DEFAULT_RISK_PER_TRADE = 0.01
DEFAULT_MAX_POSITION_VALUE_PCT = 0.10
DEFAULT_MAX_PORTFOLIO_RISK_PCT = 0.06
DEFAULT_FILL_TIMEOUT_SECONDS = 90
DEFAULT_POST_OPEN_FILL_TIMEOUT_SECONDS = 30 * 60
DEFAULT_POLL_SECONDS = 2
DEFAULT_SCAN_TIMEOUT_SECONDS = 180
DEFAULT_PROFIT_TAKE_STATE_FILE = Path(__file__).with_name("profit_take_state.json")
FIRST_PROFIT_TAKE_PERCENT = 20
SECOND_PROFIT_TAKE_PERCENT = 50
MIN_STOP_LOSS_ENTRY_RATIO = 0.95
MIN_REWARD_RISK = 2.0
NEW_YORK_TZ = ZoneInfo("America/New_York")
NY_MARKET_OPEN = time(9, 30)
NY_MARKET_CLOSE = time(16, 0)


@dataclass
class TradeSetup:
    market: str
    symbol: str
    company_name: str
    pattern: str
    entry: float
    stop_loss: float
    take_profit: float
    reward_risk: float
    rationale: str


@dataclass
class PaperTradeResult:
    symbol: str
    action: str
    reason: str
    order_id: Optional[str] = None
    status: Optional[str] = None


def _plain_cell(cell: str) -> str:
    value = cell.strip()
    match = re.match(r"\[([^\]]+)\]\([^)]+\)", value)
    if match:
        return match.group(1).strip()
    return value


def _parse_price(value: str) -> float:
    return float(_plain_cell(value).replace("$", "").replace(",", "").strip())


def _parse_reward_risk(value: str) -> float:
    text = _plain_cell(value)
    if ":" in text:
        return float(text.split(":", 1)[1])
    return float(text)


def parse_scan_markdown(markdown: str) -> List[TradeSetup]:
    setups = []
    for raw_line in markdown.splitlines():
        line = raw_line.strip()
        if not line.startswith("|") or "---" in line or "股票代號" in line:
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) < 9:
            continue
        market = _plain_cell(cells[0])
        if market != "US":
            continue
        setups.append(
            TradeSetup(
                market=market,
                symbol=_plain_cell(cells[1]).upper(),
                company_name=_plain_cell(cells[2]),
                pattern=_plain_cell(cells[3]),
                entry=_parse_price(cells[4]),
                stop_loss=_parse_price(cells[5]),
                take_profit=_parse_price(cells[6]),
                reward_risk=_parse_reward_risk(cells[7]),
                rationale=_plain_cell(cells[8]),
            )
        )
    return setups


def trade_setup_from_intraday(setup: intraday_scanner.IntradaySetup) -> TradeSetup:
    return TradeSetup(
        market="US",
        symbol=setup.symbol,
        company_name=setup.symbol,
        pattern="Intraday momentum / volume spike / gap",
        entry=setup.entry,
        stop_loss=setup.stop_loss,
        take_profit=setup.take_profit,
        reward_risk=setup.reward_risk,
        rationale=setup.rationale,
    )


def parse_symbols_csv(value: str) -> List[str]:
    return [symbol.strip().upper() for symbol in value.split(",") if symbol.strip()]


def validate_setup(setup: TradeSetup) -> Optional[str]:
    if setup.market != "US":
        return "非 US 標的"
    if not (setup.stop_loss < setup.entry < setup.take_profit):
        return "Entry/SL/TP 價格結構不合法"
    if setup.stop_loss < setup.entry * MIN_STOP_LOSS_ENTRY_RATIO:
        return "停損低於進場價 95%"
    reward_risk = (setup.take_profit - setup.entry) / (setup.entry - setup.stop_loss)
    if reward_risk < MIN_REWARD_RISK:
        return "風險報酬比低於 1:2"
    return None


def recommended_qty(setup: TradeSetup, max_qty: int = DEFAULT_MAX_QTY) -> int:
    if max_qty < 1:
        raise ValueError("max_qty must be at least 1")

    score = 0
    if setup.reward_risk >= 10:
        score += 2
    elif setup.reward_risk >= 5:
        score += 1

    if "VCP" in setup.pattern or "波動收縮" in setup.pattern:
        score += 1
    if "底部突破" in setup.pattern:
        score += 1
    if "成交量" in setup.rationale and "倍" in setup.rationale:
        score += 1

    max_score = 5
    scaled_qty = 1 + round((min(score, max_score) / max_score) * (max_qty - 1))
    return min(max_qty, max(1, scaled_qty))


def _account_equity(account) -> float:
    return float(getattr(account, "equity", 0) or 0)


def risk_based_qty(
    setup: TradeSetup,
    account,
    risk_per_trade: float = DEFAULT_RISK_PER_TRADE,
    max_qty: int = DEFAULT_MAX_QTY,
    max_position_value_pct: float = DEFAULT_MAX_POSITION_VALUE_PCT,
    remaining_portfolio_risk: Optional[float] = None,
) -> int:
    if max_qty < 1:
        raise ValueError("max_qty must be at least 1")
    if not (0 < risk_per_trade <= 1):
        raise ValueError("risk_per_trade must be between 0 and 1")
    if not (0 < max_position_value_pct <= 1):
        raise ValueError("max_position_value_pct must be between 0 and 1")

    equity = _account_equity(account)
    per_share_risk = setup.entry - setup.stop_loss
    if equity <= 0 or setup.entry <= 0 or per_share_risk <= 0:
        return 0

    trade_risk_budget = equity * risk_per_trade
    if remaining_portfolio_risk is not None:
        trade_risk_budget = min(trade_risk_budget, max(0.0, float(remaining_portfolio_risk)))

    risk_qty = int(trade_risk_budget // per_share_risk)
    value_qty = int((equity * max_position_value_pct) // setup.entry)
    return max(0, min(int(max_qty), risk_qty, value_qty))


def build_bracket_stop_limit_order(
    setup: TradeSetup,
    qty: float,
    limit_buffer: float = DEFAULT_LIMIT_BUFFER,
) -> StopLimitOrderRequest:
    limit_price = round(setup.entry * (1 + limit_buffer), 2)
    return StopLimitOrderRequest(
        symbol=setup.symbol,
        qty=float(qty),
        side=OrderSide.BUY,
        type=OrderType.STOP_LIMIT,
        time_in_force=TimeInForce.DAY,
        order_class=OrderClass.BRACKET,
        stop_price=round(setup.entry, 2),
        limit_price=limit_price,
        take_profit=TakeProfitRequest(limit_price=round(setup.take_profit, 2)),
        stop_loss=StopLossRequest(stop_price=round(setup.stop_loss, 2)),
    )


def _order_side_value(order) -> str:
    side = getattr(order, "side", "")
    return getattr(side, "value", str(side)).lower()


def _order_status_value(order) -> str:
    status = getattr(order, "status", "")
    return getattr(status, "value", str(status)).lower()


def _order_type_value(order) -> str:
    order_type = getattr(order, "type", "")
    return getattr(order_type, "value", str(order_type)).lower()


def _order_symbol(order) -> str:
    return str(getattr(order, "symbol", "")).upper()


def _ny_trade_day_start(now: Optional[datetime] = None) -> datetime:
    """
    Define the "daily" boundary for max-new-BUY counting.

    We reset the counter right after the US cash market close (16:00 New York time),
    so runs in Asia evening/next morning won't be blocked by the prior session's buys.
    """
    current = now or datetime.now(NEW_YORK_TZ)
    local = current.astimezone(NEW_YORK_TZ)
    today_close = datetime.combine(local.date(), NY_MARKET_CLOSE, tzinfo=NEW_YORK_TZ)
    if local >= today_close:
        return today_close
    return today_close - timedelta(days=1)


def now_new_york() -> datetime:
    return datetime.now(NEW_YORK_TZ)


def is_regular_market_hours(now: Optional[datetime] = None) -> bool:
    current = now or now_new_york()
    local = current.astimezone(NEW_YORK_TZ)
    open_at = datetime.combine(local.date(), NY_MARKET_OPEN, tzinfo=NEW_YORK_TZ)
    close_at = datetime.combine(local.date(), NY_MARKET_CLOSE, tzinfo=NEW_YORK_TZ)
    return local.weekday() < 5 and open_at <= local < close_at


def daily_buy_count(client, now: Optional[datetime] = None) -> int:
    request = GetOrdersRequest(
        status=QueryOrderStatus.ALL,
        after=_ny_trade_day_start(now),
        side=OrderSide.BUY,
        limit=500,
    )
    orders = client.get_orders(filter=request) or []
    return sum(1 for order in orders if _order_side_value(order) == "buy")


def open_symbols(client) -> set:
    symbols = set()
    try:
        positions = client.get_all_positions() or []
        symbols.update(_order_symbol(position) for position in positions if _order_symbol(position))
    except Exception:
        pass

    request = GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=500)
    for order in client.get_orders(filter=request) or []:
        symbol = _order_symbol(order)
        if symbol:
            symbols.add(symbol)
    return symbols


def open_order_symbols(client) -> set:
    symbols = set()
    request = GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=500)
    for order in client.get_orders(filter=request) or []:
        symbol = _order_symbol(order)
        if symbol:
            symbols.add(symbol)
    return symbols


def position_qty_by_symbol(client) -> dict:
    qty_map = {}
    try:
        for position in client.get_all_positions() or []:
            symbol = _order_symbol(position)
            if not symbol:
                continue
            try:
                qty_map[symbol] = float(getattr(position, "qty", 0) or 0)
            except (TypeError, ValueError):
                qty_map[symbol] = 0.0
    except Exception:
        pass
    return qty_map


def open_position_count(client) -> int:
    try:
        return sum(1 for position in client.get_all_positions() or [] if _position_qty_decimal(position) > 0)
    except Exception:
        return 0


def remaining_portfolio_risk_budget(
    account,
    max_portfolio_risk_pct: float = DEFAULT_MAX_PORTFOLIO_RISK_PCT,
    used_risk: float = 0.0,
) -> float:
    if not (0 < max_portfolio_risk_pct <= 1):
        raise ValueError("max_portfolio_risk_pct must be between 0 and 1")
    return max(0.0, _account_equity(account) * max_portfolio_risk_pct - used_risk)


def daily_loss(client) -> float:
    account = client.get_account()
    equity = float(getattr(account, "equity", 0) or 0)
    last_equity = float(getattr(account, "last_equity", 0) or 0)
    return max(0.0, last_equity - equity)


def write_audit_event(
    path: Optional[Path],
    event: str,
    result: PaperTradeResult,
    setup: Optional[TradeSetup] = None,
) -> None:
    if not path:
        return
    audit_path = Path(path)
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    exists = audit_path.exists()
    with audit_path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "event",
                "symbol",
                "action",
                "order_id",
                "status",
                "reason",
                "entry",
                "stop_loss",
                "take_profit",
                "reward_risk",
            ],
        )
        if not exists:
            writer.writeheader()
        writer.writerow(
            {
                "event": event,
                "symbol": result.symbol,
                "action": result.action,
                "order_id": result.order_id or "",
                "status": result.status or "",
                "reason": result.reason,
                "entry": "" if setup is None else setup.entry,
                "stop_loss": "" if setup is None else setup.stop_loss,
                "take_profit": "" if setup is None else setup.take_profit,
                "reward_risk": "" if setup is None else setup.reward_risk,
            }
        )


def fill_timeout_seconds(now: Optional[datetime] = None) -> int:
    current = now or datetime.now(NEW_YORK_TZ)
    local = current.astimezone(NEW_YORK_TZ)
    open_at = datetime.combine(local.date(), NY_MARKET_OPEN, tzinfo=NEW_YORK_TZ)
    close_at = datetime.combine(local.date(), NY_MARKET_CLOSE, tzinfo=NEW_YORK_TZ)

    if local.weekday() < 5 and open_at <= local < close_at:
        return DEFAULT_FILL_TIMEOUT_SECONDS

    days_ahead = 0
    if local.weekday() >= 5 or local >= close_at:
        days_ahead = 1
        while (local + timedelta(days=days_ahead)).weekday() >= 5:
            days_ahead += 1

    next_open = datetime.combine(
        (local + timedelta(days=days_ahead)).date(),
        NY_MARKET_OPEN,
        tzinfo=NEW_YORK_TZ,
    )
    return max(
        DEFAULT_FILL_TIMEOUT_SECONDS,
        int((next_open - local).total_seconds()) + DEFAULT_POST_OPEN_FILL_TIMEOUT_SECONDS,
    )


def wait_for_fill(
    client,
    order_id: str,
    timeout_seconds: int = DEFAULT_FILL_TIMEOUT_SECONDS,
    poll_seconds: int = DEFAULT_POLL_SECONDS,
):
    deadline = time_module.monotonic() + timeout_seconds
    terminal_failure_statuses = {"canceled", "expired", "rejected", "suspended"}

    while time_module.monotonic() <= deadline:
        order = client.get_order_by_id(order_id)
        status = _order_status_value(order)
        if status == "filled":
            return order
        if status in terminal_failure_statuses:
            raise RuntimeError(f"market buy ended with status {status}")
        time_module.sleep(poll_seconds)

    raise TimeoutError(f"market buy did not fill within {timeout_seconds}s")


def submit_trailing_stop(client, symbol: str, qty: float, trail_percent: float) -> PaperTradeResult:
    if qty <= 0:
        raise ValueError(f"qty must be positive for trailing stop: {qty}")
    if _is_fractional(qty):
        raise ValueError(f"Alpaca does not support fractional trailing stop qty={qty} symbol={symbol}")
    time_in_force = TimeInForce.GTC
    trailing_order = client.submit_order(
        order_data=TrailingStopOrderRequest(
            symbol=symbol,
            qty=float(qty),
            side=OrderSide.SELL,
            time_in_force=time_in_force,
            trail_percent=float(trail_percent),
        )
    )
    return PaperTradeResult(
        symbol,
        "TRAILING_STOP_SUBMITTED",
        f"trailing stop submitted with trail_percent={trail_percent:.2f}% tif={getattr(time_in_force, 'value', str(time_in_force))}",
        order_id=str(getattr(trailing_order, "id", "")),
        status=str(getattr(trailing_order, "status", "")),
    )


def submit_market_sell(client, symbol: str, qty: float, reason: str) -> PaperTradeResult:
    order = client.submit_order(
        order_data=MarketOrderRequest(
            symbol=symbol,
            qty=float(qty),
            side=OrderSide.SELL,
            time_in_force=TimeInForce.DAY,
        )
    )
    return PaperTradeResult(
        symbol,
        "PROFIT_TAKE_SUBMITTED",
        reason,
        order_id=str(getattr(order, "id", "")),
        status=str(getattr(order, "status", "")),
    )


def submit_flatten_sell(client, symbol: str, qty: float, reason: str) -> PaperTradeResult:
    order = client.submit_order(
        order_data=MarketOrderRequest(
            symbol=symbol,
            qty=float(qty),
            side=OrderSide.SELL,
            time_in_force=TimeInForce.DAY,
        )
    )
    return PaperTradeResult(
        symbol,
        "FLATTEN_SUBMITTED",
        reason,
        order_id=str(getattr(order, "id", "")),
        status=str(getattr(order, "status", "")),
    )


def open_trailing_stop_symbols(client) -> set:
    symbols = set()
    request = GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=500)
    for order in client.get_orders(filter=request) or []:
        if _order_side_value(order) != "sell":
            continue
        if _order_type_value(order) != "trailing_stop":
            continue
        symbol = _order_symbol(order)
        if symbol:
            symbols.add(symbol)
    return symbols


def open_trailing_stop_orders(client) -> dict:
    request = GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=500)
    orders_by_symbol = {}
    for order in client.get_orders(filter=request) or []:
        if _order_side_value(order) != "sell":
            continue
        if _order_type_value(order) != "trailing_stop":
            continue
        symbol = _order_symbol(order)
        if symbol:
            orders_by_symbol.setdefault(symbol, []).append(order)
    return orders_by_symbol


def cancel_trailing_stops(client, symbol: str, orders_by_symbol: Optional[dict] = None) -> None:
    orders = (orders_by_symbol or open_trailing_stop_orders(client)).get(symbol.upper(), [])
    for order in orders:
        order_id = getattr(order, "id", None)
        if order_id:
            client.cancel_order_by_id(str(order_id))


def load_profit_take_state(path: Path = DEFAULT_PROFIT_TAKE_STATE_FILE) -> dict:
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return {str(symbol).upper(): int(tier) for symbol, tier in raw.items()}


def save_profit_take_state(state: dict, path: Path = DEFAULT_PROFIT_TAKE_STATE_FILE) -> None:
    path.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")


def _position_profit_percent(position) -> float:
    plpc = getattr(position, "unrealized_plpc", None)
    if plpc not in (None, ""):
        return float(plpc) * 100

    avg_entry_price = float(getattr(position, "avg_entry_price", 0) or 0)
    current_price = float(getattr(position, "current_price", 0) or 0)
    if avg_entry_price <= 0:
        return 0.0
    return ((current_price - avg_entry_price) / avg_entry_price) * 100


def _position_qty_decimal(position) -> Decimal:
    raw_qty = getattr(position, "qty", 0) or 0
    try:
        return Decimal(str(raw_qty))
    except (InvalidOperation, ValueError, TypeError):
        return Decimal("0")


def _split_half_sell_qty(qty_dec: Decimal) -> Tuple[Decimal, Decimal]:
    if qty_dec <= 0:
        return Decimal("0"), Decimal("0")
    if qty_dec == qty_dec.to_integral_value():
        qty_int = int(qty_dec)
        if qty_int <= 1:
            return Decimal(qty_int), Decimal("0")
        sell_int = qty_int // 2
        remaining_int = qty_int - sell_int
        return Decimal(sell_int), Decimal(remaining_int)
    sell = qty_dec / Decimal("2")
    remaining = qty_dec - sell
    return sell, remaining


def _is_fractional(qty: float) -> bool:
    return abs(float(qty) - int(float(qty))) > 1e-8


def execute_profit_takes(
    client,
    state: Optional[dict] = None,
    state_path: Path = DEFAULT_PROFIT_TAKE_STATE_FILE,
    trail_percent: float = DEFAULT_TRAIL_PERCENT,
) -> List[PaperTradeResult]:
    profit_state = state if state is not None else load_profit_take_state(state_path)
    trailing_orders = open_trailing_stop_orders(client)
    results = []
    changed = False

    for position in client.get_all_positions() or []:
        symbol = _order_symbol(position)
        qty_dec = _position_qty_decimal(position)
        qty = float(qty_dec)
        if not symbol or qty_dec <= 0:
            continue

        profit_percent = _position_profit_percent(position)
        completed_tier = int(profit_state.get(symbol, 0) or 0)
        if profit_percent >= SECOND_PROFIT_TAKE_PERCENT and completed_tier < SECOND_PROFIT_TAKE_PERCENT:
            sell_qty = qty
            remaining_qty = 0.0
            tier = SECOND_PROFIT_TAKE_PERCENT
            reason = f"獲利達 {profit_percent:.2f}% >= {SECOND_PROFIT_TAKE_PERCENT}%，賣出剩餘持股"
        elif profit_percent >= FIRST_PROFIT_TAKE_PERCENT and completed_tier < FIRST_PROFIT_TAKE_PERCENT:
            sell_dec, remaining_dec = _split_half_sell_qty(qty_dec)
            sell_qty = float(sell_dec)
            remaining_qty = float(remaining_dec)
            tier = FIRST_PROFIT_TAKE_PERCENT
            reason = f"獲利達 {profit_percent:.2f}% >= {FIRST_PROFIT_TAKE_PERCENT}%，先賣出一半持股"
        else:
            continue

        cancel_trailing_stops(client, symbol, trailing_orders)
        if sell_qty > 0:
            results.append(submit_market_sell(client, symbol, sell_qty, reason))
        profit_state[symbol] = tier
        changed = True
        if state is None:
            save_profit_take_state(profit_state, state_path)

        if remaining_qty > 0:
            if _is_fractional(remaining_qty):
                results.append(
                    submit_market_sell(
                        client,
                        symbol,
                        remaining_qty,
                        f"{reason}（剩餘股數為 fractional，Alpaca 不支援 fractional trailing stop，改為市價賣出剩餘持股）",
                    )
                )
            else:
                results.append(submit_trailing_stop(client, symbol, remaining_qty, trail_percent))

    if state is None and changed:
        save_profit_take_state(profit_state, state_path)

    return results


def reconcile_missing_trailing_stops(
    client,
    trail_percent: float = DEFAULT_TRAIL_PERCENT,
    symbols: Optional[Iterable[str]] = None,
) -> List[PaperTradeResult]:
    target_symbols = {symbol.upper() for symbol in symbols} if symbols else None
    protected_symbols = open_trailing_stop_symbols(client)
    results = []

    for position in client.get_all_positions() or []:
        symbol = _order_symbol(position)
        if not symbol or (target_symbols is not None and symbol not in target_symbols):
            continue
        if symbol in protected_symbols:
            continue
        qty = float(getattr(position, "qty", 0) or 0)
        if qty <= 0:
            continue
        if _is_fractional(qty):
            results.append(
                submit_market_sell(
                    client,
                    symbol,
                    qty,
                    "position qty is fractional; Alpaca does not support fractional trailing stop -> market sell to flatten",
                )
            )
            continue
        results.append(submit_trailing_stop(client, symbol, qty, trail_percent))
        protected_symbols.add(symbol)

    return results


def execute_paper_trades(
    client,
    setups: Iterable[TradeSetup],
    qty: float = DEFAULT_QTY,
    max_new_trades: Optional[int] = DEFAULT_MAX_NEW_TRADES,
    daily_loss_limit: float = DEFAULT_DAILY_LOSS_LIMIT,
    limit_buffer: float = DEFAULT_LIMIT_BUFFER,
    trail_percent: float = DEFAULT_TRAIL_PERCENT,
    max_qty: int = DEFAULT_MAX_QTY,
    risk_per_trade: float = DEFAULT_RISK_PER_TRADE,
    max_position_value_pct: float = DEFAULT_MAX_POSITION_VALUE_PCT,
    max_portfolio_risk_pct: float = DEFAULT_MAX_PORTFOLIO_RISK_PCT,
    max_open_positions: int = DEFAULT_MAX_OPEN_POSITIONS,
    order_mode: str = "bracket",
    flatten_on_protection_error: bool = True,
    audit_log_path: Optional[Path] = None,
) -> List[PaperTradeResult]:
    setups = list(setups)
    loss = daily_loss(client)
    if loss >= daily_loss_limit:
        return [
            PaperTradeResult(
                setup.symbol,
                "HOLD",
                f"每日最大虧損限制已觸發：${loss:.2f} >= ${daily_loss_limit:.2f}",
            )
            for setup in setups
        ]

    existing_buy_count = daily_buy_count(client) if max_new_trades is not None else 0
    blocked_symbols = open_order_symbols(client)
    position_qty = position_qty_by_symbol(client)
    account = client.get_account()
    current_open_positions = len([symbol for symbol, current_qty in position_qty.items() if current_qty > 0])
    remaining_risk = remaining_portfolio_risk_budget(account, max_portfolio_risk_pct=max_portfolio_risk_pct)
    results = []
    submitted = 0

    for setup in setups:
        validation_error = validate_setup(setup)
        if validation_error:
            result = PaperTradeResult(setup.symbol, "HOLD", validation_error)
            results.append(result)
            write_audit_event(audit_log_path, "reject", result, setup)
            continue

        if max_new_trades is not None and existing_buy_count + submitted >= max_new_trades:
            result = PaperTradeResult(setup.symbol, "HOLD", f"每日最大交易檔數已達 {max_new_trades} 檔")
            results.append(result)
            write_audit_event(audit_log_path, "reject", result, setup)
            continue

        if setup.symbol in blocked_symbols:
            result = PaperTradeResult(setup.symbol, "HOLD", "已有未成交委託")
            results.append(result)
            write_audit_event(audit_log_path, "reject", result, setup)
            continue

        current_qty = float(position_qty.get(setup.symbol, 0) or 0)
        if _is_fractional(current_qty):
            result = PaperTradeResult(setup.symbol, "HOLD", "目前持倉為 fractional 股數，暫不加碼")
            results.append(result)
            write_audit_event(audit_log_path, "reject", result, setup)
            continue

        if current_qty <= 0 and max_open_positions is not None and current_open_positions + submitted >= max_open_positions:
            result = PaperTradeResult(setup.symbol, "HOLD", f"最大持倉檔數已達 {max_open_positions} 檔")
            results.append(result)
            write_audit_event(audit_log_path, "reject", result, setup)
            continue

        remaining_capacity = int(max_qty) - int(current_qty)
        if remaining_capacity <= 0:
            result = PaperTradeResult(setup.symbol, "HOLD", f"已達最大持倉 {max_qty} 股")
            results.append(result)
            write_audit_event(audit_log_path, "reject", result, setup)
            continue

        desired_qty = recommended_qty(setup, max_qty=max_qty)
        risk_qty = risk_based_qty(
            setup,
            account,
            risk_per_trade=risk_per_trade,
            max_qty=max_qty,
            max_position_value_pct=max_position_value_pct,
            remaining_portfolio_risk=remaining_risk,
        )
        order_qty = min(int(desired_qty), int(risk_qty), int(remaining_capacity))
        if order_qty <= 0:
            result = PaperTradeResult(setup.symbol, "HOLD", "風險額度不足，略過新倉")
            results.append(result)
            write_audit_event(audit_log_path, "reject", result, setup)
            continue

        if order_mode == "bracket":
            order = client.submit_order(
                order_data=build_bracket_stop_limit_order(setup, order_qty, limit_buffer=limit_buffer)
            )
            submitted += 1
            blocked_symbols.add(setup.symbol)
            remaining_risk -= (setup.entry - setup.stop_loss) * order_qty
            result = PaperTradeResult(
                setup.symbol,
                "BUY_BRACKET_SUBMITTED",
                f"Alpaca paper bracket stop-limit submitted; qty={order_qty} (pos {int(current_qty)} -> {int(current_qty) + int(order_qty)})",
                order_id=str(getattr(order, "id", "")),
                status=str(getattr(order, "status", "")),
            )
            results.append(result)
            write_audit_event(audit_log_path, "order_submitted", result, setup)
            continue

        if order_mode != "market":
            result = PaperTradeResult(setup.symbol, "HOLD", f"不支援的 order_mode: {order_mode}")
            results.append(result)
            write_audit_event(audit_log_path, "reject", result, setup)
            continue

        order = client.submit_order(
            order_data=MarketOrderRequest(
                symbol=setup.symbol,
                qty=float(order_qty),
                side=OrderSide.BUY,
                time_in_force=TimeInForce.DAY,
            )
        )
        submitted += 1
        blocked_symbols.add(setup.symbol)
        position_qty[setup.symbol] = current_qty + float(order_qty)
        remaining_risk -= (setup.entry - setup.stop_loss) * order_qty
        result = PaperTradeResult(
            setup.symbol,
            "BUY_MARKET_SUBMITTED",
            f"Alpaca paper market buy submitted; qty={order_qty} (pos {int(current_qty)} -> {int(current_qty) + int(order_qty)})",
            order_id=str(getattr(order, "id", "")),
            status=str(getattr(order, "status", "")),
        )
        results.append(result)
        write_audit_event(audit_log_path, "order_submitted", result, setup)
        try:
            filled_order = wait_for_fill(
                client,
                str(getattr(order, "id", "")),
                timeout_seconds=fill_timeout_seconds(),
            )
            filled_qty = float(getattr(filled_order, "filled_qty", None) or order_qty)
            protection_result = submit_trailing_stop(client, setup.symbol, filled_qty, trail_percent)
            results.append(protection_result)
            write_audit_event(audit_log_path, "protection_submitted", protection_result, setup)
        except Exception as exc:
            error_result = PaperTradeResult(setup.symbol, "PROTECTION_ERROR", str(exc))
            results.append(error_result)
            write_audit_event(audit_log_path, "protection_error", error_result, setup)
            if flatten_on_protection_error:
                flatten_result = submit_flatten_sell(
                    client,
                    setup.symbol,
                    order_qty,
                    f"保護單失敗，為避免裸多單自動平倉：{exc}",
                )
                results.append(flatten_result)
                write_audit_event(audit_log_path, "flatten_submitted", flatten_result, setup)

    return results


class ScanError(RuntimeError):
    pass


def run_scan(
    scan_script: str = DEFAULT_SCAN_SCRIPT,
    scan_args: Optional[Sequence[str]] = None,
    timeout_seconds: int = DEFAULT_SCAN_TIMEOUT_SECONDS,
) -> str:
    path = Path(scan_script)
    cmd = [sys.executable, str(path)]
    if scan_args:
        cmd.extend(scan_args)
    try:
        completed = subprocess.run(
            cmd,
            cwd=str(path.parent),
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or "").strip()
        stdout = (exc.stdout or "").strip()
        detail = stderr or stdout or f"exit_code={exc.returncode}"
        detail = " ".join(detail.split())
        raise ScanError(detail) from exc
    except subprocess.TimeoutExpired as exc:
        raise ScanError(f"timeout after {timeout_seconds}s") from exc
    if completed.stderr:
        print(completed.stderr, end="")
    return completed.stdout


def print_results(results: Iterable[PaperTradeResult]) -> None:
    print("| 股票代號 | 動作 | Alpaca Order ID | 狀態 | 原因 |")
    print("|---|---|---|---|---|")
    for result in results:
        print(
            f"| {result.symbol} | {result.action} | "
            f"{result.order_id or ''} | {result.status or ''} | {result.reason} |"
        )


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description="Run scan results through Alpaca paper trading.")
    parser.add_argument("--scan-script", default=os.environ.get("SCAN_SCRIPT", DEFAULT_SCAN_SCRIPT))
    parser.add_argument(
        "--scan-arg",
        action="append",
        default=[],
        help="Extra arg(s) forwarded to scan script (repeatable). Also supports env SCAN_ARGS (space-separated).",
    )
    parser.add_argument(
        "--scan-markdown-file",
        default=os.environ.get("SCAN_MARKDOWN_FILE", ""),
        help="Read scan markdown from file instead of executing scan script.",
    )
    parser.add_argument("--qty", type=float, default=float(os.environ.get("PAPER_TRADE_QTY", DEFAULT_QTY)))
    parser.add_argument("--max-qty", type=int, default=int(os.environ.get("PAPER_TRADE_MAX_QTY", DEFAULT_MAX_QTY)))
    parser.add_argument(
        "--max-new-trades",
        type=int,
        default=int(os.environ.get("PAPER_TRADE_MAX_NEW_TRADES", DEFAULT_MAX_NEW_TRADES)),
        help="Cap for new BUY orders per NY trade day.",
    )
    parser.add_argument("--max-open-positions", type=int, default=DEFAULT_MAX_OPEN_POSITIONS)
    parser.add_argument("--daily-loss-limit", type=float, default=DEFAULT_DAILY_LOSS_LIMIT)
    parser.add_argument("--risk-per-trade", type=float, default=DEFAULT_RISK_PER_TRADE)
    parser.add_argument("--max-position-value-pct", type=float, default=DEFAULT_MAX_POSITION_VALUE_PCT)
    parser.add_argument("--max-portfolio-risk-pct", type=float, default=DEFAULT_MAX_PORTFOLIO_RISK_PCT)
    parser.add_argument("--order-mode", choices=["bracket", "market"], default=os.environ.get("ORDER_MODE", "bracket"))
    parser.add_argument("--audit-log", default=os.environ.get("TRADE_AUDIT_LOG", ""))
    parser.add_argument("--limit-buffer", type=float, default=DEFAULT_LIMIT_BUFFER)
    parser.add_argument(
        "--trail-percent",
        type=float,
        default=float(os.environ.get("TRAIL_PERCENT", DEFAULT_TRAIL_PERCENT)),
    )
    parser.add_argument(
        "--reconcile-only",
        action="store_true",
        help="Only submit missing trailing stops for current positions, then exit.",
    )
    parser.add_argument(
        "--regular-hours-only",
        action="store_true",
        help="Skip scanning and new buys unless the US regular market is open.",
    )
    parser.add_argument(
        "--intraday-scan",
        action="store_true",
        help="Also scan Alpaca intraday minute bars for gap, momentum, and volume-spike setups.",
    )
    parser.add_argument(
        "--intraday-symbol",
        action="append",
        default=[],
        help="Symbol(s) for intraday scanning. Repeatable. Also supports env INTRADAY_SYMBOLS comma-separated.",
    )
    parser.add_argument(
        "--intraday-limit",
        type=int,
        default=int(os.environ.get("INTRADAY_LIMIT", intraday_scanner.DEFAULT_MAX_SETUPS)),
    )
    args = parser.parse_args(argv)

    if args.regular_hours_only and not is_regular_market_hours():
        client = alpaca_trading_agent.create_trading_client()
        results = execute_profit_takes(client, trail_percent=args.trail_percent)
        results.extend(reconcile_missing_trailing_stops(client, trail_percent=args.trail_percent))
        results.append(PaperTradeResult("-", "HOLD", "美股 regular hours 尚未開盤，不掃描或新增買單"))
        print_results(results)
        return

    if args.reconcile_only:
        client = alpaca_trading_agent.create_trading_client()
        results = execute_profit_takes(client, trail_percent=args.trail_percent)
        results.extend(reconcile_missing_trailing_stops(client, trail_percent=args.trail_percent))
        print_results(results)
        return

    scan_args = list(DEFAULT_SCAN_ARGS)
    scan_args.extend(args.scan_arg or [])
    env_scan_args = (os.environ.get("SCAN_ARGS") or "").strip()
    if env_scan_args:
        scan_args.extend(env_scan_args.split())

    scan_timeout_seconds = int(os.environ.get("SCAN_TIMEOUT_SECONDS", str(DEFAULT_SCAN_TIMEOUT_SECONDS)))
    scan_output = ""
    if args.scan_markdown_file:
        scan_output = Path(args.scan_markdown_file).read_text(encoding="utf-8")
    else:
        try:
            scan_output = run_scan(
                args.scan_script,
                scan_args=scan_args,
                timeout_seconds=scan_timeout_seconds,
            )
        except ScanError as exc:
            print_results(
                [
                    PaperTradeResult(
                        "-",
                        "HOLD",
                        f"掃描器執行失敗（可能為網路/DNS 限制）：{exc}. "
                        "可改用 --scan-markdown-file <path> 提供離線掃描結果。",
                    )
                ]
            )
            return

    client = alpaca_trading_agent.create_trading_client()
    setups = parse_scan_markdown(scan_output or "")
    intraday_errors = []
    if args.intraday_scan:
        intraday_symbols = list(args.intraday_symbol or [])
        env_intraday_symbols = (os.environ.get("INTRADAY_SYMBOLS") or "").strip()
        if env_intraday_symbols:
            intraday_symbols.extend(parse_symbols_csv(env_intraday_symbols))
        try:
            intraday_setups = intraday_scanner.scan_intraday_setups(
                symbols=intraday_symbols or None,
                limit=args.intraday_limit,
            )
            setups.extend(trade_setup_from_intraday(setup) for setup in intraday_setups)
        except Exception as exc:
            intraday_errors.append(PaperTradeResult("-", "HOLD", f"intraday scanner failed: {exc}"))

    results = execute_profit_takes(client, trail_percent=args.trail_percent)
    results.extend(reconcile_missing_trailing_stops(client, trail_percent=args.trail_percent))
    results.extend(intraday_errors)
    if not setups:
        if not results:
            results.append(PaperTradeResult("-", "HOLD", "掃描結果無符合條件的 US setups"))
        print_results(results)
        return
    results.extend(
        execute_paper_trades(
            client,
            setups,
            qty=args.qty,
            max_new_trades=args.max_new_trades,
            daily_loss_limit=args.daily_loss_limit,
            limit_buffer=args.limit_buffer,
            trail_percent=args.trail_percent,
            max_qty=args.max_qty,
            risk_per_trade=args.risk_per_trade,
            max_position_value_pct=args.max_position_value_pct,
            max_portfolio_risk_pct=args.max_portfolio_risk_pct,
            max_open_positions=args.max_open_positions,
            order_mode=args.order_mode,
            audit_log_path=Path(args.audit_log) if args.audit_log else None,
        )
    )
    print_results(results)


if __name__ == "__main__":
    main()
