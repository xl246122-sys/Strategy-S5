"""Fill probability from past drawdowns. The decision path does not receive future lows."""

from collections.abc import Mapping, Sequence
from datetime import datetime
from decimal import Decimal
from typing import Protocol

from bstocks_v2.contracts.decimals import parse_decimal
from bstocks_v2.limit_optimizer.spec import HORIZON_BARS, LAPLACE_ADD, PENETRATION_DELTA
from bstocks_v2.research_backtest.tape import ClosedBar


class FillModel(Protocol):
    """P(full fill within T | ask, limit). Implementations must not read the order's own future."""

    def probability(self, *, ask: Decimal, limit: Decimal, tier: str) -> Decimal: ...


class FillCalibrator:
    """Laplace rate of historical drawdowns under the stage-4 conservative full-fill rule.

    A training row is eligible only when its label window already ended at or before the cutoff.
    At a new decision the model sees the stored drawdowns, the live ask, and the candidate limit.
    """

    def __init__(
        self,
        drawdowns: Sequence[Decimal],
        *,
        delta: Decimal = PENETRATION_DELTA,
    ) -> None:
        parsed = tuple(parse_decimal(item) for item in drawdowns)
        if not parsed:
            raise ValueError("FILL_MODEL_UNTRAINED")
        width = parse_decimal(delta)
        if width <= 0:
            raise ValueError("delta")
        self._drawdowns = parsed
        self._delta = width

    def probability(self, *, ask: Decimal, limit: Decimal, tier: str) -> Decimal:
        del tier
        need = parse_decimal(ask) - parse_decimal(limit) + self._delta
        hits = sum(1 for item in self._drawdowns if item >= need)
        total = Decimal(len(self._drawdowns) + 2 * LAPLACE_ADD)
        return (Decimal(hits + LAPLACE_ADD)) / total


def drawdowns_before(
    bars: Sequence[ClosedBar],
    ask_at_end: Mapping[datetime, Decimal],
    *,
    symbol: str,
    horizon_bars: int,
    cutoff: datetime,
) -> tuple[Decimal, ...]:
    """Ask minus the lowest low over the next T bars, for windows that close by the cutoff.

    The decision bar's own low is not the label. A gap inside the window drops that row.
    Bars whose end is after the cutoff are not visible here.
    """

    if horizon_bars not in HORIZON_BARS:
        raise ValueError("horizon_bars must be 1, 2, or 3")
    if cutoff.tzinfo is None:
        raise ValueError("NAIVE_DATETIME")
    ordered = tuple(
        sorted(
            (
                bar
                for bar in bars
                if bar.symbol == symbol and bar.timeframe == "5m" and bar.end <= cutoff
            ),
            key=lambda bar: bar.end,
        )
    )
    drawdowns: list[Decimal] = []
    last = len(ordered) - horizon_bars
    for index in range(last):
        decision = ordered[index]
        window = ordered[index + 1 : index + 1 + horizon_bars]
        if not _contiguous(decision, window):
            continue
        if decision.end not in ask_at_end:
            raise ValueError("ASK_MISSING")
        ask = parse_decimal(ask_at_end[decision.end])
        min_low = min(bar.low for bar in window)
        drawdowns.append(ask - min_low)
    return tuple(drawdowns)


def conservative_full_fill(limit: Decimal, lows: Sequence[Decimal], delta: Decimal) -> bool:
    """Stage-4 conservative full fill: some later low prints at or through limit - delta."""

    line = parse_decimal(limit) - parse_decimal(delta)
    return any(parse_decimal(low) <= line for low in lows)


def _contiguous(decision: ClosedBar, window: Sequence[ClosedBar]) -> bool:
    previous = decision.end
    for bar in window:
        if bar.start != previous:
            return False
        previous = bar.end
    return True
