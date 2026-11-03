"""Low-latency break confirmation. Evidence stops after three one-minute closes."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from bstocks_v2.research_backtest.tape import ClosedBar
from bstocks_v2.strategies.s3.spec import (
    CUSUM_H,
    CUSUM_K,
    LOCATION_MIN,
    MAX_STEPS,
    SPRT_DELTA,
    SPRT_LOWER,
    SPRT_UPPER,
)


@dataclass(frozen=True)
class SequentialResult:
    accept: bool
    steps: int
    cusum: Decimal
    llr: Decimal
    terminal: str
    break_atr: Decimal | None
    location: Decimal | None
    spy_sync: bool | None


def observation_innovations(
    minutes: tuple[ClosedBar, ...],
    *,
    orh: Decimal,
    atr: Decimal,
) -> tuple[tuple[ClosedBar, ...], tuple[Decimal, ...]]:
    """Minutes after the first close above ORH, truncated to MAX_STEPS.

    Bars beyond that truncation are not read by the caller that uses this pair.
    """

    if atr <= 0:
        raise ValueError("atr")
    ordered = tuple(sorted(minutes, key=lambda bar: bar.start))
    _require_minutes(ordered)
    start: int | None = None
    for index, bar in enumerate(ordered):
        if bar.close > orh:
            start = index
            break
    if start is None:
        return (), ()
    chosen = ordered[start : start + MAX_STEPS]
    sigma = atr / Decimal(5).sqrt()
    if sigma <= 0:
        raise ValueError("sigma")
    previous = ordered[start - 1].close if start > 0 else chosen[0].open
    innovations: list[Decimal] = []
    for bar in chosen:
        innovations.append((bar.close - previous) / sigma)
        previous = bar.close
    return chosen, tuple(innovations)


def judge(innovations: tuple[Decimal, ...]) -> tuple[bool, int, Decimal, Decimal, str]:
    """CUSUM and SPRT. More than MAX_STEPS is refused so a fourth bar cannot sneak in."""

    if len(innovations) > MAX_STEPS:
        raise ValueError("too many steps")
    cusum = Decimal(0)
    llr = Decimal(0)
    if not innovations:
        return False, 0, cusum, llr, "NO_INNOVATION"
    half_delta = (SPRT_DELTA * SPRT_DELTA) / Decimal(2)
    for step, value in enumerate(innovations, start=1):
        cusum = cusum + value - CUSUM_K
        if cusum < 0:
            cusum = Decimal(0)
        llr = llr + SPRT_DELTA * value - half_delta
        if cusum > CUSUM_H or llr > SPRT_UPPER:
            if cusum > CUSUM_H and llr > SPRT_UPPER:
                terminal = "BOTH_ACCEPT"
            elif cusum > CUSUM_H:
                terminal = "CUSUM_ACCEPT"
            else:
                terminal = "SPRT_ACCEPT"
            return True, step, cusum, llr, terminal
        if llr < SPRT_LOWER:
            return False, step, cusum, llr, "SPRT_REJECT"
    return False, len(innovations), cusum, llr, "UNDECIDED_REJECT"


def confirm(
    minutes: tuple[ClosedBar, ...],
    *,
    orh: Decimal,
    atr: Decimal,
    spy: tuple[ClosedBar, ...] = (),
) -> SequentialResult:
    """Price path first. Location and SPY sync are applied only on the accepting minute."""

    chosen, innovations = observation_innovations(minutes, orh=orh, atr=atr)
    accepted, steps, cusum, llr, terminal = judge(innovations)
    if not accepted:
        return SequentialResult(False, steps, cusum, llr, terminal, None, None, None)
    minute = chosen[steps - 1]
    location = _location(minute)
    break_atr = (minute.close - orh) / atr
    if location < LOCATION_MIN:
        return SequentialResult(False, steps, cusum, llr, "LOCATION", break_atr, location, None)
    sync = _spy_sync(chosen, steps, spy)
    if sync is None:
        return SequentialResult(
            False, steps, cusum, llr, "SPY_MINUTE_MISSING", break_atr, location, None
        )
    if not sync:
        return SequentialResult(False, steps, cusum, llr, "SPY_SYNC", break_atr, location, False)
    return SequentialResult(True, steps, cusum, llr, terminal, break_atr, location, True)


def _location(bar: ClosedBar) -> Decimal:
    span = bar.high - bar.low
    if span <= 0:
        return Decimal("0.5")
    return (bar.close - bar.low) / span


def _spy_sync(
    chosen: tuple[ClosedBar, ...],
    steps: int,
    spy: tuple[ClosedBar, ...],
) -> bool | None:
    """True only when SPY's accepting minute did not fall. None if that minute is missing."""

    minute = chosen[steps - 1]
    spy_bar = _match(spy, minute.start)
    if spy_bar is None:
        return None
    spy_prev = _previous_spy(spy, spy_bar)
    if spy_prev is None:
        return None
    return spy_bar.close - spy_prev >= 0


def _match(spy: tuple[ClosedBar, ...], start: datetime) -> ClosedBar | None:
    for bar in spy:
        if bar.start == start:
            return bar
    return None


def _previous_spy(spy: tuple[ClosedBar, ...], bar: ClosedBar) -> Decimal | None:
    ordered = tuple(sorted(spy, key=lambda item: item.start))
    for index, item in enumerate(ordered):
        if item.start == bar.start:
            if index == 0:
                return bar.open
            return ordered[index - 1].close
    return None


def _require_minutes(ordered: tuple[ClosedBar, ...]) -> None:
    previous: datetime | None = None
    for bar in ordered:
        if bar.timeframe != "1m":
            raise ValueError("minute bar")
        if previous is not None and bar.start <= previous:
            raise ValueError("minute order")
        previous = bar.start
