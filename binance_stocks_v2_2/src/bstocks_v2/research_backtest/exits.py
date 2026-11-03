"""Take-profit and stop resolution. A bar that hits both does not pick the winner."""

from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from bstocks_v2.research_backtest.fills import FillSlice, buy_limit, sell_limit, stop_price
from bstocks_v2.research_backtest.models import FillMode
from bstocks_v2.research_backtest.tape import BarTape, ClosedBar


@dataclass(frozen=True)
class ExitFill:
    price: Decimal
    qty: Decimal
    reason: str
    ambiguous: bool
    bar: ClosedBar


def minutes_cover(parent: ClosedBar, minutes: tuple[ClosedBar, ...]) -> bool:
    if parent.timeframe != "5m" or len(minutes) != 5:
        return False
    cursor = parent.start
    for minute in minutes:
        if minute.start != cursor or minute.end - minute.start != timedelta(minutes=1):
            return False
        cursor = minute.end
    return cursor == parent.end


def _stop(bar: ClosedBar, stop: Decimal, qty: Decimal, *, ambiguous: bool) -> ExitFill:
    return ExitFill(stop_price(bar, stop), qty, "STOP", ambiguous, bar)


def resolve_bracket(
    mode: FillMode,
    parent: ClosedBar,
    tape: BarTape,
    *,
    stop: Decimal,
    take_profit: Decimal,
    delta: Decimal,
    qty: Decimal,
    step: Decimal,
) -> ExitFill | None:
    """Exit a position that was already open before this 5-minute bar."""

    hit_stop = parent.low <= stop
    hit_tp = parent.high >= take_profit
    if mode is FillMode.LOOSE:
        if hit_stop and hit_tp:
            hit_stop = False
        if hit_tp:
            sold = sell_limit(mode, parent, take_profit, delta, qty, step)
            if sold.qty > 0:
                return ExitFill(sold.price, sold.qty, "TAKE_PROFIT", False, parent)
        if hit_stop:
            return _stop(parent, stop, qty, ambiguous=False)
        return None
    if hit_stop and hit_tp:
        return _resolve_conflict(parent, tape, stop, take_profit, delta, qty, step)
    if hit_stop:
        return _stop(parent, stop, qty, ambiguous=False)
    if hit_tp:
        sold = sell_limit(mode, parent, take_profit, delta, qty, step)
        if sold.qty > 0:
            return ExitFill(sold.price, sold.qty, "TAKE_PROFIT", False, parent)
    return None


def _resolve_conflict(
    parent: ClosedBar,
    tape: BarTape,
    stop: Decimal,
    take_profit: Decimal,
    delta: Decimal,
    qty: Decimal,
    step: Decimal,
) -> ExitFill:
    minutes = tape.minutes_inside(parent)
    if not minutes_cover(parent, minutes):
        return _stop(parent, stop, qty, ambiguous=True)
    for minute in minutes:
        hit_stop = minute.low <= stop
        hit_tp = minute.high >= take_profit
        if hit_stop and hit_tp:
            return _stop(minute, stop, qty, ambiguous=True)
        if hit_stop:
            return _stop(minute, stop, qty, ambiguous=False)
        if hit_tp:
            sold = sell_limit(FillMode.CONSERVATIVE, minute, take_profit, delta, qty, step)
            if sold.qty > 0:
                return ExitFill(sold.price, sold.qty, "TAKE_PROFIT", False, minute)
    return _stop(parent, stop, qty, ambiguous=True)


@dataclass(frozen=True)
class EntryPrint:
    fill: FillSlice
    immediate_stop: ExitFill | None


def conservative_entry(
    bar: ClosedBar,
    tape: BarTape,
    *,
    limit: Decimal,
    stop: Decimal,
    delta: Decimal,
    qty: Decimal,
    step: Decimal,
) -> EntryPrint:
    """A buy that shares its bar with the stop is a loss unless 1-minute bars prove otherwise."""

    touched = buy_limit(FillMode.CONSERVATIVE, bar, limit, delta, qty, step)
    if touched.qty == 0 or bar.low > stop:
        return EntryPrint(touched, None)
    minutes = tape.minutes_inside(bar)
    if not minutes_cover(bar, minutes):
        return EntryPrint(
            FillSlice(limit, touched.qty),
            _stop(bar, stop, touched.qty, ambiguous=True),
        )
    filled = Decimal(0)
    price = limit
    for minute in minutes:
        if filled == 0:
            bite = buy_limit(FillMode.CONSERVATIVE, minute, limit, delta, qty, step)
            if bite.qty == 0:
                continue
            filled = bite.qty
            price = bite.price
            if minute.low <= stop:
                return EntryPrint(
                    FillSlice(price, filled), _stop(minute, stop, filled, ambiguous=True)
                )
            continue
        if minute.low <= stop:
            return EntryPrint(
                FillSlice(price, filled), _stop(minute, stop, filled, ambiguous=False)
            )
    # The 5-minute low reached the stop, but the minutes did not show that print.
    qty_out = filled if filled > 0 else touched.qty
    return EntryPrint(FillSlice(price, qty_out), _stop(bar, stop, qty_out, ambiguous=True))
