"""Path labels for one filled bracket. This module does not read 1-minute bars."""

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from bstocks_v2.constitution.guard import flatten_deadline
from bstocks_v2.research_backtest.fills import buy_limit, sell_limit, stop_price
from bstocks_v2.research_backtest.models import FillMode
from bstocks_v2.research_backtest.tape import ClosedBar
from bstocks_v2.research_data.calendar import NEW_YORK, XnysCalendar
from bstocks_v2.strategies.s2.signal import opening_qty
from bstocks_v2.strategies.s2.spec import HOLD_SECONDS, PENETRATION, TRAIN_END, TTL_SECONDS

_STEP = Decimal(1)
_OUTCOMES = frozenset({"TP", "SL", "TO"})


@dataclass(frozen=True)
class PathLabel:
    outcome: str
    gross_bps: Decimal | None
    exit_price: Decimal | None
    ambiguous: bool


def train_timeout_bps(rows: list[tuple[date, str, Decimal | None]]) -> Decimal:
    """Mean gross timeout return on the training split only. Later rows are ignored."""

    chosen = [
        bps
        for day, outcome, bps in rows
        if day <= TRAIN_END and outcome == "TO" and bps is not None
    ]
    if not chosen:
        return Decimal(0)
    return sum(chosen, start=Decimal(0)) / Decimal(len(chosen))


def label_path(
    bars: list[ClosedBar],
    signal_index: int,
    *,
    limit: Decimal,
    stop: Decimal,
    take_profit: Decimal,
    calendar: XnysCalendar,
) -> PathLabel:
    """Walk the same deadline and conservative-fill order as the replay engine."""

    if signal_index < 0 or signal_index >= len(bars):
        raise RuntimeError("signal index")
    signal = bars[signal_index]
    qty = opening_qty(limit, stop)
    if qty < 1:
        return PathLabel("REJECTED", None, None, False)
    valid_until = signal.end + timedelta(seconds=TTL_SECONDS)
    filled_at = None
    entry = limit
    previous = signal.end
    for bar in bars[signal_index + 1 :]:
        if bar.symbol != signal.symbol or bar.start < previous:
            raise RuntimeError("tape is out of order")
        previous = bar.start
        half = _half_day(calendar, bar)
        if filled_at is None:
            if bar.start >= valid_until or bar.start >= flatten_deadline(bar.start, half):
                return PathLabel("UNFILLED", None, None, False)
            if bar.start < signal.end:
                continue
            touched = buy_limit(FillMode.CONSERVATIVE, bar, limit, PENETRATION, qty, _STEP)
            if touched.qty <= 0:
                continue
            entry = touched.price
            if bar.low <= stop:
                return _closed("SL", entry, stop_price(bar, stop), True)
            filled_at = bar.end
            continue
        deadline = flatten_deadline(bar.start, half)
        if bar.start >= deadline or bar.start >= filled_at + timedelta(seconds=HOLD_SECONDS):
            return _closed("TO", entry, bar.open, False)
        if bar.low <= stop:
            sold = sell_limit(FillMode.CONSERVATIVE, bar, take_profit, PENETRATION, qty, _STEP)
            return _closed("SL", entry, stop_price(bar, stop), sold.qty > 0)
        sold = sell_limit(FillMode.CONSERVATIVE, bar, take_profit, PENETRATION, qty, _STEP)
        if sold.qty > 0:
            return _closed("TP", entry, sold.price, False)
    if filled_at is None:
        return PathLabel("UNFILLED", None, None, False)
    return PathLabel("OPEN", None, None, False)


def _closed(outcome: str, entry: Decimal, price: Decimal, ambiguous: bool) -> PathLabel:
    if outcome not in _OUTCOMES or entry <= 0:
        raise RuntimeError("label")
    return PathLabel(outcome, (price - entry) / entry * Decimal(10000), price, ambiguous)


def _half_day(calendar: XnysCalendar, bar: ClosedBar) -> bool:
    day = bar.start.astimezone(NEW_YORK).date()
    if not calendar.is_session(day):
        raise RuntimeError("bar is not on a session")
    return calendar.close_time(day).hour == 13
