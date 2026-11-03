"""Excursions are computed after the replay stops. A strategy cannot read them."""

from datetime import datetime, timedelta
from decimal import Decimal

from bstocks_v2.research_backtest.tape import ClosedBar


def per_share(
    reference: Decimal, high: Decimal | None, low: Decimal | None
) -> tuple[Decimal, Decimal]:
    if high is None or low is None:
        return Decimal(0), Decimal(0)
    favorable = high - reference
    adverse = reference - low
    if favorable < 0:
        favorable = Decimal(0)
    if adverse < 0:
        adverse = Decimal(0)
    return favorable, adverse


def opportunity_cost(
    *,
    unfilled_qty: Decimal,
    limit_price: Decimal,
    best_high: Decimal | None,
    commission_rate: Decimal,
    half_spread: Decimal,
) -> Decimal:
    if unfilled_qty <= 0 or best_high is None:
        return Decimal(0)
    favorable, _adverse = per_share(limit_price, best_high, limit_price)
    gross = favorable * unfilled_qty
    entry_fee = commission_rate * limit_price * unfilled_qty
    exit_fee = commission_rate * (limit_price + favorable) * unfilled_qty
    spread = half_spread * unfilled_qty * 2
    net = gross - entry_fee - exit_fee - spread
    if net < 0:
        return Decimal(0)
    return net


def window_extremes(
    bars: tuple[ClosedBar, ...],
    *,
    symbol: str,
    start_inclusive: datetime,
    end_exclusive: datetime,
) -> tuple[Decimal | None, Decimal | None]:
    chosen = [
        bar
        for bar in bars
        if bar.symbol == symbol
        and bar.timeframe == "5m"
        and start_inclusive <= bar.start < end_exclusive
    ]
    if not chosen:
        return None, None
    return max(bar.high for bar in chosen), min(bar.low for bar in chosen)


def holding_deadline(
    signal_end: datetime, max_holding_seconds: int, session_flatten: datetime
) -> datetime:
    return min(signal_end + timedelta(seconds=max_holding_seconds), session_flatten)
