"""Fold six closed regular-session five-minute bars into one thirty-minute bar."""

from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal

from bstocks_v2.feature_store.bars import FeatureBar
from bstocks_v2.research_data.calendar import NEW_YORK, XnysCalendar
from bstocks_v2.tf30.spec import BAR_MINUTES, BARS_PER_BAR, SOURCE_MINUTES

_WIDTH = timedelta(minutes=SOURCE_MINUTES)
_SLOT = timedelta(minutes=BAR_MINUTES)


@dataclass
class _Bucket:
    start: object
    bars: list[FeatureBar] = field(default_factory=list)


class Aggregator:
    """Emit a bar only when six contiguous five-minute prints fill the slot."""

    def __init__(self, calendar: XnysCalendar) -> None:
        self.calendar = calendar
        self._open: dict[str, _Bucket] = {}

    def push(self, bar: FeatureBar) -> FeatureBar | None:
        if bar.end - bar.start != _WIDTH:
            return None
        if self.calendar.classify(bar.start, bar.end) != "RTH":
            return None
        local = bar.start.astimezone(NEW_YORK).replace(second=0, microsecond=0)
        floored = local.replace(minute=(local.minute // BAR_MINUTES) * BAR_MINUTES)
        slot = floored.astimezone(bar.start.tzinfo)
        held = self._open.get(bar.symbol)
        if held is None or held.start != slot:
            held = _Bucket(slot)
            self._open[bar.symbol] = held
        if held.bars:
            if bar.start != held.bars[-1].end:
                held.bars.clear()
                return None
        elif bar.start != slot:
            return None
        held.bars.append(bar)
        if len(held.bars) < BARS_PER_BAR:
            return None
        if held.bars[-1].end != slot + _SLOT:
            held.bars.clear()
            return None
        folded = _fold(held.bars)
        self._open.pop(bar.symbol, None)
        return folded

    def push_group(self, bars: list[FeatureBar]) -> list[FeatureBar]:
        folded: list[FeatureBar] = []
        for bar in bars:
            done = self.push(bar)
            if done is not None:
                folded.append(done)
        return folded


def _fold(bars: list[FeatureBar]) -> FeatureBar:
    first = bars[0]
    last = bars[-1]
    volume = sum((bar.volume for bar in bars), start=Decimal(0))
    weighted = Decimal(0)
    for bar in bars:
        price = bar.bar_vwap
        if price is None:
            price = (bar.high + bar.low + bar.close) / Decimal(3)
        weighted += price * bar.volume
    vwap = weighted / volume if volume > 0 else None
    feed = first.feed if all(bar.feed == first.feed for bar in bars) else "mixed"
    return FeatureBar(
        symbol=first.symbol,
        start=first.start,
        end=last.end,
        open=first.open,
        high=max(bar.high for bar in bars),
        low=min(bar.low for bar in bars),
        close=last.close,
        volume=volume,
        bar_vwap=vwap,
        closed=True,
        feed=feed,
        price_factor=first.price_factor,
    )
