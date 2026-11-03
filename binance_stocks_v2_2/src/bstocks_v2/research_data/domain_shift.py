"""Compare Binance prints with Alpaca SIP bars on identical timestamps."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal


@dataclass(frozen=True)
class ComparedBar:
    symbol: str
    start: datetime
    close: Decimal
    volume: Decimal
    vwap: Decimal


@dataclass(frozen=True)
class ShiftSummary:
    overlap: int
    status: str
    close_median_bps: Decimal | None
    close_p95_bps: Decimal | None
    volume_median_ratio: Decimal | None
    vwap_median_bps: Decimal | None


def align(
    left: list[ComparedBar],
    right: list[ComparedBar],
) -> list[tuple[ComparedBar, ComparedBar]]:
    index = {(bar.symbol, bar.start): bar for bar in right}
    pairs: list[tuple[ComparedBar, ComparedBar]] = []
    for bar in left:
        other = index.get((bar.symbol, bar.start))
        if other is not None:
            pairs.append((bar, other))
    return pairs


def summarize(
    pairs: list[tuple[ComparedBar, ComparedBar]],
    minimum_overlap: int = 30,
) -> ShiftSummary:
    if len(pairs) < minimum_overlap:
        status = "insufficient_overlap"
    else:
        status = "ok"
    if not pairs:
        return ShiftSummary(0, status, None, None, None, None)
    close_bps = [_bps(alpaca.close, binance.close) for alpaca, binance in pairs]
    vwap_bps = [_bps(alpaca.vwap, binance.vwap) for alpaca, binance in pairs]
    ratios = [
        binance.volume / alpaca.volume
        for alpaca, binance in pairs
        if alpaca.volume > 0
    ]
    return ShiftSummary(
        overlap=len(pairs),
        status=status,
        close_median_bps=_median(close_bps),
        close_p95_bps=_p95(close_bps),
        volume_median_ratio=_median(ratios) if ratios else None,
        vwap_median_bps=_median(vwap_bps),
    )


def _bps(base: Decimal, other: Decimal) -> Decimal:
    if base == 0:
        return Decimal(0)
    return (other - base) / base * Decimal(10000)


def _median(values: list[Decimal]) -> Decimal:
    ordered = sorted(values)
    count = len(ordered)
    mid = count // 2
    if count % 2 == 1:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / Decimal(2)


def _p95(values: list[Decimal]) -> Decimal:
    ordered = sorted(abs(value) for value in values)
    if len(ordered) == 1:
        return ordered[0]
    rank = Decimal("0.95") * Decimal(len(ordered) - 1)
    low = int(rank)
    high = min(low + 1, len(ordered) - 1)
    weight = rank - Decimal(low)
    return ordered[low] * (Decimal(1) - weight) + ordered[high] * weight
