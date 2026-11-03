"""Opening-range lock. ORH and ORL stay unpublished until the window's last bar has closed."""

from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from decimal import Decimal

from bstocks_v2.feature_store.bars import FeatureBar
from bstocks_v2.feature_store.errors import LookaheadError
from bstocks_v2.research_data.calendar import NEW_YORK
from bstocks_v2.strategies.s3.spec import BARS_IN_RANGE, HORIZONS

_OPEN = time(9, 30)


@dataclass(frozen=True)
class RangeState:
    horizon_minutes: int
    locked: bool
    orh: Decimal | None
    orl: Decimal | None
    first_break: bool
    reason: str


@dataclass
class _Sym:
    day: object | None = None
    last_start: datetime | None = None
    count: int = 0
    highs: list[Decimal] = field(default_factory=list)
    lows: list[Decimal] = field(default_factory=list)
    locked: dict[int, tuple[Decimal, Decimal]] = field(default_factory=dict)
    broke: dict[int, bool] = field(default_factory=dict)
    invalid: bool = False
    last_view: dict[int, RangeState] | None = None


class OpeningRangeBook:
    """One symbol's RTH bars in time order. A later bar cannot rewrite a locked range."""

    def __init__(self) -> None:
        self._states: dict[str, _Sym] = {}

    def observe(self, bar: FeatureBar) -> dict[int, RangeState]:
        if not bar.closed:
            raise LookaheadError("unclosed bar")
        if bar.end - bar.start != timedelta(minutes=5):
            raise ValueError("bar width")
        if bar.start.tzinfo is None or bar.end.tzinfo is None:
            raise ValueError("bar timestamps must be timezone-aware")
        state = self._states.setdefault(bar.symbol, _Sym())
        day = bar.start.astimezone(NEW_YORK).date()
        if state.day == day and state.last_start == bar.start:
            if state.last_view is None:
                raise LookaheadError("duplicate bar has no stored range")
            return state.last_view
        if state.last_start is not None and bar.start < state.last_start:
            raise LookaheadError("bar is earlier than the last closed bar")
        if state.day != day:
            state = _Sym(day=day)
            self._states[bar.symbol] = state
        local = bar.start.astimezone(NEW_YORK).timetz().replace(tzinfo=None)
        if state.last_start is None:
            if local != _OPEN:
                state.invalid = True
        elif bar.start != state.last_start + timedelta(minutes=5):
            state.invalid = True
        if bar.high < bar.low or bar.open < bar.low or bar.open > bar.high:
            state.invalid = True
        if bar.close < bar.low or bar.close > bar.high:
            state.invalid = True
        if not state.invalid:
            state.highs.append(bar.high)
            state.lows.append(bar.low)
            state.count += 1
        view = _views(state, bar.close if not state.invalid else None)
        state.last_start = bar.start
        state.last_view = view
        return view


def _views(state: _Sym, close: Decimal | None) -> dict[int, RangeState]:
    view: dict[int, RangeState] = {}
    for horizon in HORIZONS:
        needed = BARS_IN_RANGE[horizon]
        if state.invalid or close is None:
            view[horizon] = RangeState(horizon, False, None, None, False, "DAY_INVALID")
            continue
        if state.count < needed:
            view[horizon] = RangeState(horizon, False, None, None, False, "WINDOW_OPEN")
            continue
        if horizon not in state.locked:
            state.locked[horizon] = (max(state.highs[:needed]), min(state.lows[:needed]))
            state.broke[horizon] = False
        orh, orl = state.locked[horizon]
        if state.count == needed:
            view[horizon] = RangeState(horizon, True, orh, orl, False, "LOCK_BAR")
            continue
        if state.broke.get(horizon, False):
            view[horizon] = RangeState(horizon, True, orh, orl, False, "ALREADY_BROKE")
            continue
        if close > orh:
            state.broke[horizon] = True
            view[horizon] = RangeState(horizon, True, orh, orl, True, "FIRST_BREAK")
            continue
        view[horizon] = RangeState(horizon, True, orh, orl, False, "INSIDE_RANGE")
    return view
