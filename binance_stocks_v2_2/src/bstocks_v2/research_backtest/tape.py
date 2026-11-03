"""Closed bars only. A decision at time t cannot see a bar whose end is still ahead."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from bstocks_v2.contracts.decimals import parse_decimal, require_price_scale
from bstocks_v2.research_backtest.errors import LookaheadError


@dataclass(frozen=True)
class ClosedBar:
    symbol: str
    start: datetime
    end: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    timeframe: str

    def __post_init__(self) -> None:
        if self.timeframe not in {"5m", "1m"}:
            raise ValueError("timeframe must be 5m or 1m")
        if self.start.tzinfo is None or self.end.tzinfo is None:
            raise ValueError("bar timestamps must be timezone-aware")
        width = timedelta(minutes=5 if self.timeframe == "5m" else 1)
        if self.end - self.start != width:
            raise ValueError("bar width does not match its timeframe")
        opened = require_price_scale(parse_decimal(self.open))
        high = require_price_scale(parse_decimal(self.high))
        low = require_price_scale(parse_decimal(self.low))
        closed = require_price_scale(parse_decimal(self.close))
        volume = parse_decimal(self.volume)
        if high < low or opened < low or opened > high or closed < low or closed > high:
            raise ValueError("OHLC is inconsistent")
        if volume < 0:
            raise ValueError("volume")
        object.__setattr__(self, "open", opened)
        object.__setattr__(self, "high", high)
        object.__setattr__(self, "low", low)
        object.__setattr__(self, "close", closed)
        object.__setattr__(self, "volume", volume)


class CausalView:
    """What a strategy is allowed to know when a 5-minute bar has just closed."""

    def __init__(self, tape: "BarTape", asof: datetime) -> None:
        self._tape = tape
        self.asof = asof

    def closed_bars(self, symbol: str, timeframe: str = "5m") -> tuple[ClosedBar, ...]:
        return tuple(
            bar
            for bar in self._tape.bars
            if bar.symbol == symbol and bar.timeframe == timeframe and bar.end <= self.asof
        )

    def demand(self, bar: ClosedBar) -> ClosedBar:
        if bar.end > self.asof:
            raise LookaheadError("unclosed bar")
        return bar

    def excursion(self) -> tuple[Decimal, Decimal]:
        raise LookaheadError("MFE is an after-the-fact audit and is not visible at the decision")


class BarTape:
    def __init__(self, bars: tuple[ClosedBar, ...] | list[ClosedBar]) -> None:
        ordered = tuple(
            sorted(bars, key=lambda bar: (bar.end, bar.start, bar.timeframe, bar.symbol))
        )
        seen: set[tuple[str, str, datetime]] = set()
        for bar in ordered:
            key = (bar.symbol, bar.timeframe, bar.start)
            if key in seen:
                raise ValueError(
                    f"duplicate bar {bar.symbol} {bar.timeframe} {bar.start.isoformat()}"
                )
            seen.add(key)
        self.bars = ordered

    def view(self, asof: datetime) -> CausalView:
        if asof.tzinfo is None:
            raise ValueError("asof must be timezone-aware")
        return CausalView(self, asof)

    def clock(self) -> tuple[ClosedBar, ...]:
        return tuple(bar for bar in self.bars if bar.timeframe == "5m")

    def minutes_inside(self, parent: ClosedBar) -> tuple[ClosedBar, ...]:
        return tuple(
            bar
            for bar in self.bars
            if bar.symbol == parent.symbol
            and bar.timeframe == "1m"
            and bar.start >= parent.start
            and bar.end <= parent.end
        )
