"""One thirty-minute opening bar. Later closes can break it once."""

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from decimal import Decimal

from bstocks_v2.feature_store.bars import FeatureBar
from bstocks_v2.research_data.calendar import NEW_YORK
from bstocks_v2.strategies.s3.orb import RangeState
from bstocks_v2.tf30.spec import BAR_MINUTES

_OPEN = time(9, 30)
_WIDTH = timedelta(minutes=BAR_MINUTES)


@dataclass
class _Day:
    day: object | None = None
    orh: Decimal | None = None
    orl: Decimal | None = None
    broke: bool = False
    invalid: bool = False
    last_start: datetime | None = None


class ThirtyRange:
    """Lock ORH and ORL on the 09:30 bar. A later close above ORH is the only break."""

    def __init__(self) -> None:
        self._days: dict[str, _Day] = {}

    def observe(self, bar: FeatureBar) -> RangeState:
        state = self._days.setdefault(bar.symbol, _Day())
        day = bar.start.astimezone(NEW_YORK).date()
        if state.day != day:
            state.day = day
            state.orh = None
            state.orl = None
            state.broke = False
            state.invalid = False
            state.last_start = None
        if state.last_start is not None and bar.start <= state.last_start:
            state.invalid = True
        state.last_start = bar.start
        if state.invalid or bar.end - bar.start != _WIDTH:
            state.invalid = True
            return RangeState(BAR_MINUTES, False, None, None, False, "DAY_INVALID")
        if state.orh is None or state.orl is None:
            if bar.start.astimezone(NEW_YORK).timetz().replace(tzinfo=None) != _OPEN:
                state.invalid = True
                return RangeState(BAR_MINUTES, False, None, None, False, "DAY_INVALID")
            state.orh = bar.high
            state.orl = bar.low
            return RangeState(BAR_MINUTES, True, state.orh, state.orl, False, "LOCK_BAR")
        if state.broke:
            return RangeState(BAR_MINUTES, True, state.orh, state.orl, False, "ALREADY_BROKE")
        if bar.close > state.orh:
            state.broke = True
            return RangeState(BAR_MINUTES, True, state.orh, state.orl, True, "FIRST_BREAK")
        return RangeState(BAR_MINUTES, True, state.orh, state.orl, False, "INSIDE_RANGE")
