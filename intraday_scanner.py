from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, Iterable, List, Optional, Sequence
from zoneinfo import ZoneInfo

from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.data.enums import DataFeed
from alpaca.data.requests import StockBarsRequest, StockSnapshotRequest
from alpaca.data.timeframe import TimeFrame

import alpaca_trading_agent


NEW_YORK_TZ = ZoneInfo("America/New_York")
DEFAULT_INTRADAY_SYMBOLS = [
    "AAPL",
    "MSFT",
    "NVDA",
    "AMD",
    "TSLA",
    "META",
    "AMZN",
    "GOOGL",
    "AVGO",
    "NFLX",
    "PLTR",
    "COIN",
    "MSTR",
    "SMCI",
    "ARM",
    "MU",
    "CRWD",
    "SNOW",
    "SHOP",
    "UBER",
    "PANW",
    "HOOD",
    "SOFI",
    "RIVN",
    "NIO",
    "LCID",
    "BABA",
    "TSM",
    "QQQ",
    "SPY",
]
DEFAULT_LOOKBACK_MINUTES = 90
DEFAULT_MAX_SETUPS = 10
DEFAULT_GAP_PERCENT = 2.0
DEFAULT_MOMENTUM_PERCENT = 1.2
DEFAULT_VOLUME_SPIKE = 2.0
DEFAULT_MIN_PRICE = 5.0
DEFAULT_MIN_AVG_DOLLAR_VOLUME = 1_000_000.0
DEFAULT_MIN_ATR_PERCENT = 0.2
DEFAULT_ATR_STOP_MULTIPLIER = 1.5
DEFAULT_DATA_FEED = DataFeed.IEX


@dataclass
class IntradayBar:
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class IntradaySetup:
    symbol: str
    entry: float
    stop_loss: float
    take_profit: float
    reward_risk: float
    gap_percent: float
    momentum_percent: float
    volume_spike: float
    atr_percent: float = 0.0
    avg_dollar_volume: float = 0.0
    score: float = 0.0
    rationale: str = ""


def create_stock_data_client() -> StockHistoricalDataClient:
    api_key, secret_key = alpaca_trading_agent.load_credentials()
    return StockHistoricalDataClient(api_key, secret_key)


def _safe_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _bar_attr(bar, *names, default=None):
    for name in names:
        if hasattr(bar, name):
            return getattr(bar, name)
        if isinstance(bar, dict) and name in bar:
            return bar[name]
    return default


def normalize_bar(bar) -> IntradayBar:
    return IntradayBar(
        timestamp=_bar_attr(bar, "timestamp", "t", default=datetime.now(NEW_YORK_TZ)),
        open=_safe_float(_bar_attr(bar, "open", "o")),
        high=_safe_float(_bar_attr(bar, "high", "h")),
        low=_safe_float(_bar_attr(bar, "low", "l")),
        close=_safe_float(_bar_attr(bar, "close", "c")),
        volume=_safe_float(_bar_attr(bar, "volume", "v")),
    )


def _average(values: Sequence[float]) -> float:
    values = [value for value in values if value > 0]
    if not values:
        return 0.0
    return sum(values) / len(values)


def _percent_change(new_value: float, old_value: float) -> float:
    if old_value <= 0:
        return 0.0
    return ((new_value - old_value) / old_value) * 100


def _average_true_range(bars: Sequence[IntradayBar]) -> float:
    if len(bars) < 2:
        return 0.0
    true_ranges = []
    previous_close = bars[0].close
    for bar in bars[1:]:
        true_ranges.append(
            max(
                bar.high - bar.low,
                abs(bar.high - previous_close),
                abs(bar.low - previous_close),
            )
        )
        previous_close = bar.close
    return _average(true_ranges)


def detect_intraday_setup(
    symbol: str,
    bars: Iterable[IntradayBar],
    previous_close: float,
    min_gap_percent: float = DEFAULT_GAP_PERCENT,
    min_momentum_percent: float = DEFAULT_MOMENTUM_PERCENT,
    min_volume_spike: float = DEFAULT_VOLUME_SPIKE,
    min_price: float = DEFAULT_MIN_PRICE,
    min_avg_dollar_volume: float = DEFAULT_MIN_AVG_DOLLAR_VOLUME,
    min_atr_percent: float = DEFAULT_MIN_ATR_PERCENT,
    atr_stop_multiplier: float = DEFAULT_ATR_STOP_MULTIPLIER,
) -> Optional[IntradaySetup]:
    ordered_bars = sorted([bar for bar in bars if bar.close > 0], key=lambda bar: bar.timestamp)
    if len(ordered_bars) < 6:
        return None

    first = ordered_bars[0]
    last = ordered_bars[-1]
    if last.close < min_price:
        return None

    baseline_count = max(3, len(ordered_bars) // 2)
    recent_count = min(3, len(ordered_bars) // 2)
    baseline_volume = _average([bar.volume for bar in ordered_bars[:baseline_count]])
    recent_volume = _average([bar.volume for bar in ordered_bars[-recent_count:]])
    volume_spike = recent_volume / baseline_volume if baseline_volume > 0 else 0.0
    avg_dollar_volume = _average([bar.close * bar.volume for bar in ordered_bars])
    if avg_dollar_volume < min_avg_dollar_volume:
        return None

    gap_percent = _percent_change(first.open, previous_close)
    momentum_percent = _percent_change(last.close, first.open)
    atr = _average_true_range(ordered_bars)
    atr_percent = _percent_change(last.close + atr, last.close)
    if atr_percent < min_atr_percent:
        return None

    gap_hit = gap_percent >= min_gap_percent
    momentum_hit = momentum_percent >= min_momentum_percent
    volume_hit = volume_spike >= min_volume_spike
    if not (gap_hit or (momentum_hit and volume_hit)):
        return None

    entry = round(last.close, 2)
    fixed_stop = entry * 0.95
    atr_stop = entry - (atr * atr_stop_multiplier) if atr > 0 else fixed_stop
    stop_loss = round(min(fixed_stop, atr_stop), 2)
    if stop_loss >= entry:
        return None
    risk = entry - stop_loss
    take_profit = round(entry + 2 * risk, 2)
    reward_risk = round((take_profit - entry) / risk, 2)
    score = round(max(0.0, gap_percent) + max(0.0, momentum_percent) + volume_spike + atr_percent, 2)

    reasons = []
    if gap_hit:
        reasons.append(f"gap {gap_percent:.2f}%")
    if momentum_hit:
        reasons.append(f"intraday momentum {momentum_percent:.2f}%")
    if volume_hit:
        reasons.append(f"volume spike {volume_spike:.2f}x")

    return IntradaySetup(
        symbol=symbol.upper(),
        entry=entry,
        stop_loss=stop_loss,
        take_profit=take_profit,
        reward_risk=reward_risk,
        gap_percent=round(gap_percent, 2),
        momentum_percent=round(momentum_percent, 2),
        volume_spike=round(volume_spike, 2),
        atr_percent=round(atr_percent, 2),
        avg_dollar_volume=round(avg_dollar_volume, 2),
        score=score,
        rationale=", ".join(reasons),
    )


def rank_intraday_setups(setups: Iterable[Optional[IntradaySetup]], limit: int = DEFAULT_MAX_SETUPS) -> List[IntradaySetup]:
    candidates = [setup for setup in setups if setup is not None]
    return sorted(candidates, key=lambda setup: setup.score, reverse=True)[:limit]


def _extract_bar_mapping(barset) -> Dict[str, List[IntradayBar]]:
    raw = getattr(barset, "data", barset)
    if raw is None:
        return {}

    mapping = {}
    for symbol, bars in raw.items():
        mapping[str(symbol).upper()] = [normalize_bar(bar) for bar in (bars or [])]
    return mapping


def _previous_close_from_snapshot(snapshot) -> float:
    previous_daily_bar = getattr(snapshot, "previous_daily_bar", None)
    if previous_daily_bar is None and isinstance(snapshot, dict):
        previous_daily_bar = snapshot.get("previous_daily_bar") or snapshot.get("prevDailyBar")
    return _safe_float(_bar_attr(previous_daily_bar, "close", "c"))


def fetch_intraday_bars(
    data_client,
    symbols: Sequence[str],
    now: Optional[datetime] = None,
    lookback_minutes: int = DEFAULT_LOOKBACK_MINUTES,
    feed: DataFeed = DEFAULT_DATA_FEED,
) -> Dict[str, List[IntradayBar]]:
    current = now or datetime.now(NEW_YORK_TZ)
    start = current - timedelta(minutes=lookback_minutes)
    barset = data_client.get_stock_bars(
        StockBarsRequest(
            symbol_or_symbols=list(symbols),
            timeframe=TimeFrame.Minute,
            start=start,
            end=current,
            feed=feed,
        )
    )
    return _extract_bar_mapping(barset)


def fetch_previous_closes(
    data_client,
    symbols: Sequence[str],
    feed: DataFeed = DEFAULT_DATA_FEED,
) -> Dict[str, float]:
    snapshots = data_client.get_stock_snapshot(StockSnapshotRequest(symbol_or_symbols=list(symbols), feed=feed)) or {}
    return {
        str(symbol).upper(): _previous_close_from_snapshot(snapshot)
        for symbol, snapshot in snapshots.items()
    }


def scan_intraday_setups(
    data_client=None,
    symbols: Optional[Sequence[str]] = None,
    now: Optional[datetime] = None,
    limit: int = DEFAULT_MAX_SETUPS,
    lookback_minutes: int = DEFAULT_LOOKBACK_MINUTES,
    feed: DataFeed = DEFAULT_DATA_FEED,
) -> List[IntradaySetup]:
    target_symbols = [symbol.upper() for symbol in (symbols or DEFAULT_INTRADAY_SYMBOLS)]
    if not target_symbols:
        return []

    client = data_client or create_stock_data_client()
    bars_by_symbol = fetch_intraday_bars(client, target_symbols, now=now, lookback_minutes=lookback_minutes, feed=feed)
    previous_closes = fetch_previous_closes(client, target_symbols, feed=feed)

    return rank_intraday_setups(
        [
            detect_intraday_setup(symbol, bars, previous_closes.get(symbol, 0.0))
            for symbol, bars in bars_by_symbol.items()
        ],
        limit=limit,
    )
