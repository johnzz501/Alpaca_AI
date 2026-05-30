#!/usr/bin/env python3
import argparse
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import pandas as pd


@dataclass(frozen=True)
class BreakoutConfig:
    fast_window: int = 10
    slow_window: int = 30
    breakout_window: int = 20
    volume_window: int = 20
    volume_multiplier: float = 1.5
    stop_loss_pct: float = 0.05
    take_profit_pct: float = 0.12


@dataclass
class BacktestResult:
    trades: pd.DataFrame
    equity_curve: pd.DataFrame
    final_equity: float
    total_return_pct: float
    max_drawdown_pct: float
    win_rate_pct: float


def _validate_ohlcv(data: pd.DataFrame) -> None:
    required = {"close", "high", "low", "volume"}
    missing = required.difference(data.columns)
    if missing:
        raise ValueError(f"missing required OHLCV column(s): {', '.join(sorted(missing))}")
    if data.empty:
        raise ValueError("price data must not be empty")


def generate_breakout_signals(data: pd.DataFrame, config: Optional[BreakoutConfig] = None) -> pd.DataFrame:
    """Generate long-only moving-average breakout signals from OHLCV data."""
    _validate_ohlcv(data)
    cfg = config or BreakoutConfig()
    signals = data.copy()
    close = signals["close"].astype(float)
    high = signals["high"].astype(float)
    volume = signals["volume"].astype(float)

    signals["fast_ma"] = close.rolling(cfg.fast_window, min_periods=cfg.fast_window).mean()
    signals["slow_ma"] = close.rolling(cfg.slow_window, min_periods=cfg.slow_window).mean()
    signals["prior_breakout_high"] = high.shift(1).rolling(
        cfg.breakout_window,
        min_periods=cfg.breakout_window,
    ).max()
    signals["avg_volume"] = volume.shift(1).rolling(
        cfg.volume_window,
        min_periods=cfg.volume_window,
    ).mean()

    trend_ok = signals["fast_ma"] > signals["slow_ma"]
    breakout_ok = close > signals["prior_breakout_high"]
    volume_ok = volume >= signals["avg_volume"] * cfg.volume_multiplier
    signals["signal"] = (trend_ok & breakout_ok & volume_ok).astype(int)
    signals["entry_price"] = close.where(signals["signal"].eq(1))
    signals["stop_loss"] = (signals["entry_price"] * (1 - cfg.stop_loss_pct)).round(2)
    signals["take_profit"] = (signals["entry_price"] * (1 + cfg.take_profit_pct)).round(2)
    return signals


def backtest_signals(
    signals: pd.DataFrame,
    initial_cash: float = 100_000.0,
    risk_per_trade: float = 0.01,
    slippage_pct: float = 0.0,
    commission_per_share: float = 0.0,
) -> BacktestResult:
    """Run a simple single-position long-only backtest over generated signals."""
    if initial_cash <= 0:
        raise ValueError("initial_cash must be greater than 0")
    if not (0 < risk_per_trade <= 1):
        raise ValueError("risk_per_trade must be between 0 and 1")

    cash = float(initial_cash)
    position = None
    trades = []
    equity_rows = []

    for timestamp, row in signals.iterrows():
        close = float(row["close"])
        high = float(row.get("high", close))
        low = float(row.get("low", close))
        if position:
            exit_price = None
            exit_reason = ""
            if low <= position["stop_loss"]:
                exit_price = position["stop_loss"] * (1 - slippage_pct)
                exit_reason = "stop_loss"
            elif high >= position["take_profit"]:
                exit_price = position["take_profit"] * (1 - slippage_pct)
                exit_reason = "take_profit"

            if exit_price is not None:
                exit_commission = position["qty"] * commission_per_share
                pnl = round((exit_price - position["entry_price"]) * position["qty"] - position["entry_commission"] - exit_commission, 2)
                cash += position["qty"] * exit_price - exit_commission
                trades.append(
                    {
                        **position,
                        "exit_time": timestamp,
                        "exit_price": round(exit_price, 4),
                        "exit_reason": exit_reason,
                        "pnl": pnl,
                    }
                )
                position = None

        if position is None and int(row.get("signal", 0)) == 1:
            entry_price = float(row["entry_price"]) * (1 + slippage_pct)
            stop_loss = float(row["stop_loss"])
            take_profit = float(row["take_profit"])
            per_share_risk = max(0.01, entry_price - stop_loss)
            qty = int((cash * risk_per_trade) // per_share_risk)
            affordable_qty = int(cash // entry_price)
            qty = max(0, min(qty, affordable_qty))
            if qty > 0:
                entry_commission = qty * commission_per_share
                cash -= qty * entry_price + entry_commission
                position = {
                    "entry_time": timestamp,
                    "entry_price": round(entry_price, 4),
                    "stop_loss": stop_loss,
                    "take_profit": take_profit,
                    "qty": qty,
                    "entry_commission": round(entry_commission, 2),
                }

        position_value = position["qty"] * close if position else 0.0
        equity_rows.append({"time": timestamp, "equity": round(cash + position_value, 2)})

    equity_curve = pd.DataFrame(equity_rows).set_index("time")
    trades_frame = pd.DataFrame(trades)
    final_equity = float(equity_curve["equity"].iloc[-1])
    rolling_high = equity_curve["equity"].cummax()
    drawdown = (equity_curve["equity"] - rolling_high) / rolling_high
    wins = 0 if trades_frame.empty else int((trades_frame["pnl"] > 0).sum())
    return BacktestResult(
        trades=trades_frame,
        equity_curve=equity_curve,
        final_equity=final_equity,
        total_return_pct=round((final_equity / initial_cash - 1) * 100, 2),
        max_drawdown_pct=round(float(drawdown.min()) * 100, 2),
        win_rate_pct=0.0 if trades_frame.empty else round(wins / len(trades_frame) * 100, 2),
    )


def backtest_multi_symbol(
    signals_by_symbol: dict,
    initial_cash: float = 100_000.0,
    risk_per_trade: float = 0.01,
    slippage_pct: float = 0.0,
    commission_per_share: float = 0.0,
) -> BacktestResult:
    if not signals_by_symbol:
        raise ValueError("signals_by_symbol must not be empty")

    symbols = list(signals_by_symbol)
    cash_per_symbol = initial_cash / len(symbols)
    trade_frames = []
    equity_frames = []

    for symbol, signals in signals_by_symbol.items():
        result = backtest_signals(
            signals,
            initial_cash=cash_per_symbol,
            risk_per_trade=risk_per_trade,
            slippage_pct=slippage_pct,
            commission_per_share=commission_per_share,
        )
        trades = result.trades.copy()
        if not trades.empty:
            trades.insert(0, "symbol", symbol)
            trade_frames.append(trades)
        equity_frames.append(result.equity_curve.rename(columns={"equity": symbol}))

    combined_equity = pd.concat(equity_frames, axis=1).ffill().fillna(cash_per_symbol)
    equity_curve = pd.DataFrame({"equity": combined_equity.sum(axis=1).round(2)})
    trades_frame = pd.concat(trade_frames, ignore_index=True) if trade_frames else pd.DataFrame()
    final_equity = float(equity_curve["equity"].iloc[-1])
    rolling_high = equity_curve["equity"].cummax()
    drawdown = (equity_curve["equity"] - rolling_high) / rolling_high
    wins = 0 if trades_frame.empty else int((trades_frame["pnl"] > 0).sum())
    return BacktestResult(
        trades=trades_frame,
        equity_curve=equity_curve,
        final_equity=final_equity,
        total_return_pct=round((final_equity / initial_cash - 1) * 100, 2),
        max_drawdown_pct=round(float(drawdown.min()) * 100, 2),
        win_rate_pct=0.0 if trades_frame.empty else round(wins / len(trades_frame) * 100, 2),
    )


def run_csv_backtest(csv_path: str, config: Optional[BreakoutConfig] = None) -> BacktestResult:
    data = pd.read_csv(csv_path, parse_dates=True, index_col=0)
    signals = generate_breakout_signals(data, config=config)
    return backtest_signals(signals)


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description="Run a Pandas breakout strategy backtest from an OHLCV CSV.")
    parser.add_argument("csv", help="CSV with date index and close/high/low/volume columns")
    parser.add_argument("--trades-out", default="", help="Optional path to write closed trades CSV")
    args = parser.parse_args(argv)

    result = run_csv_backtest(args.csv)
    print(f"Final equity: {result.final_equity:.2f}")
    print(f"Total return: {result.total_return_pct:.2f}%")
    print(f"Max drawdown: {result.max_drawdown_pct:.2f}%")
    print(f"Win rate: {result.win_rate_pct:.2f}%")
    if args.trades_out:
        Path(args.trades_out).parent.mkdir(parents=True, exist_ok=True)
        result.trades.to_csv(args.trades_out, index=False)


if __name__ == "__main__":
    main()
