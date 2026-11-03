"""Reference formulas. The incremental engine does not call these for RSI or ATR."""

from decimal import Decimal

from bstocks_v2.market_ingestor.wal import wilder_rsi

ATR_PERIOD = 14
RSI_PERIOD = 6
TOD_LOOKBACK = 20
TREND_BARS = 6
SLOPE_LAG = 3


def atr_of(bars: list[tuple[Decimal, Decimal, Decimal]]) -> Decimal | None:
    """Wilder ATR(14) from (high, low, close), recomputed from the first bar."""

    if len(bars) < ATR_PERIOD:
        return None
    true_ranges: list[Decimal] = []
    previous: Decimal | None = None
    for high, low, close in bars:
        if previous is None:
            true_range = high - low
        else:
            true_range = max(high - low, abs(high - previous), abs(low - previous))
        true_ranges.append(true_range)
        previous = close
    span = Decimal(ATR_PERIOD)
    value = sum(true_ranges[:ATR_PERIOD], start=Decimal(0)) / span
    for true_range in true_ranges[ATR_PERIOD:]:
        value = ((value * (span - 1)) + true_range) / span
    return value


def rsi_of(closes: list[Decimal]) -> Decimal | None:
    return wilder_rsi(closes, RSI_PERIOD)


def mean(values: list[Decimal]) -> Decimal:
    return sum(values, start=Decimal(0)) / Decimal(len(values))


def median(values: list[Decimal]) -> Decimal:
    ordered = sorted(values)
    count = len(ordered)
    mid = count // 2
    if count % 2 == 1:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / Decimal(2)


def sample_stdev(values: list[Decimal]) -> Decimal | None:
    count = len(values)
    if count < 2:
        return None
    center = mean(values)
    variance = sum((item - center) ** 2 for item in values) / Decimal(count - 1)
    if variance == 0:
        return Decimal(0)
    return variance.sqrt()
