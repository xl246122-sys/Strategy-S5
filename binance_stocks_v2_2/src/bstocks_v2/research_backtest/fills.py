"""Fill quantities. Loose mode copies Backtrader's touch rule. Conservative mode does not."""

from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_DOWN, Decimal

from bstocks_v2.research_backtest.models import EngineConfig, FillMode
from bstocks_v2.research_backtest.tape import ClosedBar


def floor_step(qty: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        raise ValueError("step_size must be positive")
    units = (qty / step).to_integral_value(ROUND_DOWN)
    return units * step


def penetration_delta(
    config: EngineConfig,
    *,
    symbol: str,
    signal_end: datetime,
    atr: dict[tuple[str, datetime], Decimal],
) -> Decimal | None:
    if config.penetration == "fixed":
        return config.fixed_delta
    if config.penetration == "spread":
        return config.spread_multiple * (config.half_spread * 2)
    value = atr.get((symbol, signal_end))
    if value is None or value <= 0:
        return None
    return config.atr_fraction * value


def _queue_qty(depth: Decimal, delta: Decimal, remaining: Decimal, step: Decimal) -> Decimal:
    if depth <= 0 or remaining <= 0:
        return Decimal(0)
    if depth >= delta:
        return remaining
    return floor_step(remaining * depth / delta, step)


@dataclass(frozen=True)
class FillSlice:
    price: Decimal
    qty: Decimal


def buy_limit(
    mode: FillMode,
    bar: ClosedBar,
    limit: Decimal,
    delta: Decimal,
    remaining: Decimal,
    step: Decimal,
) -> FillSlice:
    """Buy limit. Loose may pay the open when the open is already through the limit."""

    if mode is FillMode.LOOSE:
        if limit >= bar.open:
            return FillSlice(bar.open, remaining)
        if limit >= bar.low:
            return FillSlice(limit, remaining)
        return FillSlice(limit, Decimal(0))
    qty = _queue_qty(limit - bar.low, delta, remaining, step)
    return FillSlice(limit, qty)


def sell_limit(
    mode: FillMode,
    bar: ClosedBar,
    limit: Decimal,
    delta: Decimal,
    remaining: Decimal,
    step: Decimal,
) -> FillSlice:
    """Sell limit. Loose may sell the open when the open is already through the limit."""

    if mode is FillMode.LOOSE:
        if limit <= bar.open:
            return FillSlice(bar.open, remaining)
        if limit <= bar.high:
            return FillSlice(limit, remaining)
        return FillSlice(limit, Decimal(0))
    qty = _queue_qty(bar.high - limit, delta, remaining, step)
    return FillSlice(limit, qty)


def stop_price(bar: ClosedBar, stop: Decimal) -> Decimal:
    """Sell stop. A gap through the stop prints at the open; otherwise it prints at the stop."""

    if bar.open <= stop:
        return bar.open
    return stop
