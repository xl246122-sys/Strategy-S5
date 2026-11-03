"""Causal RTH VWAP and time-of-day volume baselines."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from bstocks_v2.research_data.adjust import split_adjusted_volume
from bstocks_v2.research_data.calendar import NEW_YORK, XnysCalendar
from bstocks_v2.research_data.quality import PriceBar, bar_quality, trade_price

TOD_LOOKBACK = 20


@dataclass(frozen=True)
class FeatureRow:
    symbol: str
    start: datetime
    ny_date: str
    session: str
    bucket: str
    volume: Decimal
    cum_vwap: Decimal | None
    tod_mean_volume: Decimal | None
    tod_median_volume: Decimal | None
    baseline_days: int
    baseline_ready: bool
    vwap_intact: bool
    quality_flag: str
    feed: str


@dataclass(frozen=True)
class DayQuality:
    symbol: str
    ny_date: str
    vwap_intact: bool
    expected_buckets: int
    present_buckets: int
    internal_gaps: int
    rth_dollar_volume: Decimal
    non_sip: int


def build_rth_features(
    bars: list[PriceBar],
    calendar: XnysCalendar,
    price_factors: dict[str, Decimal],
    lookback: int = TOD_LOOKBACK,
) -> tuple[list[FeatureRow], list[DayQuality]]:
    ordered = sorted(bars, key=lambda bar: bar.start)
    seen: set[datetime] = set()
    grouped: dict[str, list[tuple[PriceBar, str, bool]]] = {}
    for bar in ordered:
        duplicate = bar.start in seen
        seen.add(bar.start)
        flag = bar_quality(bar, duplicate=duplicate)
        session = calendar.classify(bar.start, bar.end)
        if session != "RTH":
            continue
        day = bar.start.astimezone(NEW_YORK).date().isoformat()
        grouped.setdefault(day, []).append((bar, flag, duplicate))

    history: dict[str, list[Decimal]] = {}
    features: list[FeatureRow] = []
    days: list[DayQuality] = []
    for day in sorted(grouped):
        rows, quality, volumes = _one_day(
            grouped[day],
            calendar,
            price_factors.get(day, Decimal(1)),
            history,
            lookback,
        )
        features.extend(rows)
        days.append(quality)
        for bucket, volume in volumes.items():
            history.setdefault(bucket, []).append(volume)
    return features, days


def _one_day(
    rows: list[tuple[PriceBar, str, bool]],
    calendar: XnysCalendar,
    price_factor: Decimal,
    history: dict[str, list[Decimal]],
    lookback: int,
) -> tuple[list[FeatureRow], DayQuality, dict[str, Decimal]]:
    first = rows[0][0]
    day = first.start.astimezone(NEW_YORK).date()
    expected = calendar.rth_bucket_starts(day)
    present = {
        bar.start.astimezone(NEW_YORK).replace(second=0, microsecond=0) for bar, _, _ in rows
    }
    internal = _internal_gaps(expected, present)
    non_sip = sum(1 for _, flag, _ in rows if flag == "NON_SIP")
    intact = internal == 0 and non_sip == 0 and all(flag != "DUPLICATE_TS" for _, flag, _ in rows)
    intact = intact and all(flag != "OHLC_INVALID" for _, flag, _ in rows)
    num = Decimal(0)
    den = Decimal(0)
    dollar = Decimal(0)
    features: list[FeatureRow] = []
    volumes: dict[str, Decimal] = {}
    for bar, flag, _ in rows:
        local = bar.start.astimezone(NEW_YORK)
        bucket = local.strftime("%H:%M")
        if flag == "OK":
            price = trade_price(bar)
            num += price * bar.volume
            den += bar.volume
            dollar += price * bar.volume
        cum = num / den if den > 0 else None
        adjusted = split_adjusted_volume(bar.volume, price_factor)
        volumes[bucket] = adjusted
        window = history.get(bucket, [])[-lookback:]
        mean, median = _baseline(window)
        features.append(
            FeatureRow(
                symbol=bar.symbol,
                start=bar.start,
                ny_date=day.isoformat(),
                session="RTH",
                bucket=bucket,
                volume=bar.volume,
                cum_vwap=cum,
                tod_mean_volume=mean,
                tod_median_volume=median,
                baseline_days=len(window),
                baseline_ready=len(window) >= lookback,
                vwap_intact=intact,
                quality_flag=flag,
                feed=bar.feed,
            )
        )
    quality = DayQuality(
        symbol=first.symbol,
        ny_date=day.isoformat(),
        vwap_intact=intact,
        expected_buckets=len(expected),
        present_buckets=len(present),
        internal_gaps=internal,
        rth_dollar_volume=dollar,
        non_sip=non_sip,
    )
    return features, quality, volumes


def _internal_gaps(expected: list[datetime], present: set[datetime]) -> int:
    if not present:
        return 0
    first = min(present)
    last = max(present)
    return sum(1 for bucket in expected if first < bucket < last and bucket not in present)


def _baseline(window: list[Decimal]) -> tuple[Decimal | None, Decimal | None]:
    if not window:
        return None, None
    mean = sum(window, start=Decimal(0)) / Decimal(len(window))
    return mean, _median(window)


def _median(values: list[Decimal]) -> Decimal:
    ordered = sorted(values)
    count = len(ordered)
    mid = count // 2
    if count % 2 == 1:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / Decimal(2)
