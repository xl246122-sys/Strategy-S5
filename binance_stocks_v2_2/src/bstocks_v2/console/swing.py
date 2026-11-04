"""Daily swing plans. A signal enters on the next session and shares the desk account."""

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal

from bstocks_v2.console.calendar import LiveCalendar
from bstocks_v2.feature_store.bars import FeatureBar
from bstocks_v2.feature_store.spec import median, sample_stdev
from bstocks_v2.research_data.calendar import NEW_YORK
from bstocks_v2.strategies.s4.spec import RSI_OVERSOLD, Z_STRETCH
from bstocks_v2.strategies.s5.spec import B0_MAX_SIMPLE_RETURN

_CENT = Decimal("0.01")
_KEEP = 60
_RSI = 6
_ATR = 14
_VWAP = 20
_Z = 20
_STRENGTH = 6
_ORB = 5
_RVOL = 20


@dataclass(frozen=True)
class DailyBar:
    symbol: str
    day: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    complete: bool


@dataclass(frozen=True)
class SwingOrder:
    strategy: str
    symbol: str
    signal_day: date
    limit: Decimal
    stop: Decimal
    take_profit: Decimal


@dataclass
class _Fold:
    day: date | None = None
    bars: list[FeatureBar] = field(default_factory=list)


class SwingBook:
    """Fold regular-session five-minute bars. Plans are valid for one following session."""

    def __init__(self, calendar: LiveCalendar) -> None:
        self.calendar = calendar
        self._open: dict[str, _Fold] = {}
        self._history: dict[str, list[DailyBar]] = {}
        self._plans: dict[tuple[str, str], SwingOrder] = {}

    def observe(self, bar: FeatureBar, *, arm: bool) -> DailyBar | None:
        if bar.end - bar.start != timedelta(minutes=5):
            return None
        if self.calendar.classify(bar.start, bar.end) != "RTH":
            return None
        day = bar.start.astimezone(NEW_YORK).date()
        held = self._open.get(bar.symbol)
        if held is None or held.day != day:
            held = _Fold(day)
            self._open[bar.symbol] = held
        if held.bars and bar.start != held.bars[-1].end:
            held.bars.clear()
            return None
        held.bars.append(bar)
        expected = _expected_bars(self.calendar, day)
        if expected is None or len(held.bars) != expected:
            return None
        finished = held.bars[-1].end.astimezone(NEW_YORK).timetz().replace(tzinfo=None)
        if finished != _close(self.calendar, day):
            held.bars.clear()
            return None
        daily = _fold_day(held.bars, day)
        self._open.pop(bar.symbol, None)
        self._push(daily, arm=arm)
        return daily

    def seed(self, bars: list[DailyBar]) -> None:
        ordered = sorted(bars, key=lambda item: (item.day, item.symbol))
        for bar in ordered:
            if bar.complete:
                self._push(bar, arm=False)

    def due(self, symbol: str, day: date) -> list[SwingOrder]:
        orders: list[SwingOrder] = []
        for strategy in ("S1", "S2", "S3", "S4", "S5"):
            order = self._plans.get((strategy, symbol))
            if order is None:
                continue
            nxt = _next_session(self.calendar, order.signal_day)
            if nxt == day:
                orders.append(order)
                self._plans.pop((strategy, symbol), None)
            elif nxt is not None and day > nxt:
                self._plans.pop((strategy, symbol), None)
        return orders

    def _push(self, daily: DailyBar, *, arm: bool) -> None:
        rows = self._history.setdefault(daily.symbol, [])
        rows.append(daily)
        del rows[:-_KEEP]
        if not arm:
            return
        spy = self._history.get("SPY", [])
        for strategy, order in _signals(daily.symbol, rows, spy).items():
            self._plans[(strategy, daily.symbol)] = order


def _signals(symbol: str, rows: list[DailyBar], spy: list[DailyBar]) -> dict[str, SwingOrder]:
    if len(rows) < _RVOL or rows[-1].symbol != symbol:
        return {}
    current = rows[-1]
    atr = _atr(rows)
    rsi_now = _rsi(rows)
    rsi_prev = _rsi(rows[:-1])
    vwap = _vwap(rows[-_VWAP:])
    if atr is None or atr <= 0 or rsi_now is None or vwap is None:
        return {}
    found: dict[str, SwingOrder] = {}
    z_value = _residual_z(rows, spy)
    if (
        z_value is not None
        and z_value > Decimal(2)
        and current.close > vwap
        and current.close - vwap <= atr
    ):
        bracket = _bracket(current.close, atr)
        if bracket is not None:
            found["S1"] = _order("S1", current, bracket)
    if (
        rsi_prev is not None
        and rsi_prev <= Decimal(50)
        and rsi_now > Decimal(50)
        and current.close > vwap
        and current.close - vwap <= atr
        and _contracted(rows)
    ):
        bracket = _bracket(current.close, atr)
        if bracket is not None:
            found["S2"] = _order("S2", current, bracket)
    prior_high = max(item.high for item in rows[-(_ORB + 1) : -1])
    base = [item.volume for item in rows[-(_RVOL + 1) : -1]]
    middle = median(base)
    if current.close > prior_high and middle > 0 and current.volume >= middle:
        bracket = _bracket(current.close, atr)
        if bracket is not None:
            found["S3"] = _order("S3", current, bracket)
    if (
        rsi_prev is not None
        and rsi_prev < RSI_OVERSOLD
        and rsi_now >= RSI_OVERSOLD
        and current.close < vwap - (atr * Z_STRETCH)
        and current.close > current.open
    ):
        bracket = _bracket(current.close, atr)
        if bracket is not None:
            found["S4"] = _order("S4", current, bracket)
    previous = rows[-2].close
    if previous > 0 and (current.close - previous) / previous <= B0_MAX_SIMPLE_RETURN:
        bracket = _bracket(current.close, atr)
        if bracket is not None:
            found["S5"] = _order("S5", current, bracket)
    return found


def _order(strategy: str, bar: DailyBar, prices: tuple[Decimal, Decimal, Decimal]) -> SwingOrder:
    limit, stop, take_profit = prices
    return SwingOrder(strategy, bar.symbol, bar.day, limit, stop, take_profit)


def _bracket(close: Decimal, atr: Decimal) -> tuple[Decimal, Decimal, Decimal] | None:
    limit = close.quantize(_CENT, rounding=ROUND_HALF_UP)
    stop = (limit - atr).quantize(_CENT, rounding=ROUND_DOWN)
    take_profit = (limit + atr * Decimal("1.5")).quantize(_CENT, rounding=ROUND_DOWN)
    if stop <= 0 or not stop < limit < take_profit:
        return None
    return limit, stop, take_profit


def _atr(rows: list[DailyBar]) -> Decimal | None:
    if len(rows) < _ATR:
        return None
    previous: Decimal | None = None
    ranges: list[Decimal] = []
    for item in rows:
        span = item.high - item.low
        if previous is not None:
            span = max(span, abs(item.high - previous), abs(item.low - previous))
        ranges.append(span)
        previous = item.close
    window = ranges[-_ATR:]
    value = sum(window, start=Decimal(0)) / Decimal(_ATR)
    return value


def _rsi(rows: list[DailyBar]) -> Decimal | None:
    if len(rows) <= _RSI:
        return None
    gains: list[Decimal] = []
    losses: list[Decimal] = []
    for older, newer in zip(rows, rows[1:], strict=False):
        delta = newer.close - older.close
        gains.append(delta if delta > 0 else Decimal(0))
        losses.append(-delta if delta < 0 else Decimal(0))
    if len(gains) < _RSI:
        return None
    avg_gain = sum(gains[:_RSI], start=Decimal(0)) / Decimal(_RSI)
    avg_loss = sum(losses[:_RSI], start=Decimal(0)) / Decimal(_RSI)
    for gain, loss in zip(gains[_RSI:], losses[_RSI:], strict=False):
        avg_gain = ((avg_gain * Decimal(_RSI - 1)) + gain) / Decimal(_RSI)
        avg_loss = ((avg_loss * Decimal(_RSI - 1)) + loss) / Decimal(_RSI)
    if avg_loss == 0:
        return Decimal(100) if avg_gain > 0 else Decimal(50)
    rs = avg_gain / avg_loss
    return Decimal(100) - (Decimal(100) / (Decimal(1) + rs))


def _vwap(rows: list[DailyBar]) -> Decimal | None:
    volume = sum((item.volume for item in rows), start=Decimal(0))
    if volume <= 0:
        return None
    weighted = Decimal(0)
    for item in rows:
        typical = (item.high + item.low + item.close) / Decimal(3)
        weighted += typical * item.volume
    return weighted / volume


def _residual_z(rows: list[DailyBar], spy: list[DailyBar]) -> Decimal | None:
    spy_by_day = {item.day: item for item in spy}
    residuals: list[Decimal] = []
    previous: DailyBar | None = None
    for item in rows:
        market = spy_by_day.get(item.day)
        if previous is not None and previous.close > 0 and market is not None:
            prior_market = spy_by_day.get(previous.day)
            if prior_market is not None and prior_market.close > 0:
                stock_ret = (item.close - previous.close) / previous.close
                market_ret = (market.close - prior_market.close) / prior_market.close
                residuals.append(stock_ret - market_ret)
        previous = item
    window = residuals[-_Z:]
    if len(window) < _Z:
        return None
    sigma = sample_stdev(window)
    if sigma is None or sigma <= 0:
        return None
    center = sum(window, start=Decimal(0)) / Decimal(len(window))
    return (window[-1] - center) / sigma


def _contracted(rows: list[DailyBar]) -> bool:
    if len(rows) < _STRENGTH + 1:
        return False
    base = rows[-(_STRENGTH + 1) : -1]
    current = rows[-1].volume
    average = sum((item.volume for item in base), start=Decimal(0)) / Decimal(len(base))
    return average > 0 and current < average


def _fold_day(bars: list[FeatureBar], day: date) -> DailyBar:
    volume = sum((bar.volume for bar in bars), start=Decimal(0))
    return DailyBar(
        symbol=bars[0].symbol,
        day=day,
        open=bars[0].open,
        high=max(bar.high for bar in bars),
        low=min(bar.low for bar in bars),
        close=bars[-1].close,
        volume=volume,
        complete=True,
    )


def _expected_bars(calendar: LiveCalendar, day: date) -> int | None:
    close = calendar.close_on(day)
    if close is None:
        return None
    start = datetime.combine(day, datetime.min.time(), tzinfo=NEW_YORK).replace(hour=9, minute=30)
    stop = datetime.combine(day, close, tzinfo=NEW_YORK)
    return int((stop - start).total_seconds() // (5 * 60))


def _close(calendar: LiveCalendar, day: date) -> time:
    close = calendar.close_on(day)
    if close is None:
        raise ValueError("missing close")
    return close


def _next_session(calendar: LiveCalendar, day: date) -> date | None:
    cursor = day + timedelta(days=1)
    for _ in range(12):
        if calendar.close_on(cursor) is not None:
            return cursor
        cursor += timedelta(days=1)
    return None
