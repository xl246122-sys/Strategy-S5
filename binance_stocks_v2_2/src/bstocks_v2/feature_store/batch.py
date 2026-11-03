"""One-pass offline features. RSI, ATR, and VWAP are recomputed from the raw bars."""

from datetime import datetime, time, timedelta
from decimal import Decimal

from bstocks_v2.feature_store.bars import FeatureBar, SymbolSnapshot, make_symbol_snapshot
from bstocks_v2.feature_store.errors import LookaheadError
from bstocks_v2.feature_store.spec import (
    SLOPE_LAG,
    TOD_LOOKBACK,
    TREND_BARS,
    atr_of,
    mean,
    median,
    rsi_of,
    sample_stdev,
)
from bstocks_v2.research_data.adjust import split_adjusted_volume
from bstocks_v2.research_data.calendar import NEW_YORK, XnysCalendar

_OPEN = time(9, 30)


def batch_snapshots(bars: list[FeatureBar], calendar: XnysCalendar) -> list[SymbolSnapshot]:
    """Walk the series once and rebuild each indicator from the bars seen so far."""

    accepted: list[FeatureBar] = []
    session: list[FeatureBar] = []
    hist_cum: dict[str, list[Decimal]] = {}
    hist_ret: dict[str, list[Decimal]] = {}
    hist_atr: dict[str, list[Decimal]] = {}
    pending_cum: dict[str, Decimal] = {}
    pending_ret: dict[str, Decimal] = {}
    pending_atr: dict[str, Decimal] = {}
    day_key: object | None = None
    last_start: datetime | None = None
    intact = True
    opened = False
    session_cum = Decimal(0)
    snapshots: list[SymbolSnapshot] = []

    for bar in bars:
        if not bar.closed:
            raise LookaheadError("unclosed bar")
        if bar.start.tzinfo is None or bar.end.tzinfo is None or bar.end <= bar.start:
            raise ValueError("bar interval is not usable")
        if calendar.classify(bar.start, bar.end) != "RTH":
            continue
        if last_start is not None and bar.start < last_start:
            raise LookaheadError("bar is earlier than the last closed bar")
        if last_start is not None and bar.start == last_start:
            if not snapshots:
                raise LookaheadError("duplicate bar has no stored snapshot")
            snapshots.append(snapshots[-1])
            continue
        day = bar.start.astimezone(NEW_YORK).date()
        if day_key != day:
            if day_key is not None and intact and opened:
                for bucket, cum in pending_cum.items():
                    hist_cum.setdefault(bucket, []).append(cum)
                    if bucket in pending_ret:
                        hist_ret.setdefault(bucket, []).append(pending_ret[bucket])
                    if bucket in pending_atr:
                        hist_atr.setdefault(bucket, []).append(pending_atr[bucket])
            pending_cum = {}
            pending_ret = {}
            pending_atr = {}
            session = []
            session_cum = Decimal(0)
            intact = True
            opened = False
            last_start = None
            day_key = day
        bucket = bar.start.astimezone(NEW_YORK).strftime("%H:%M")
        if last_start is None:
            opened = _clock(bar.start) == _OPEN
            intact = opened
        elif bar.start != last_start + timedelta(minutes=5):
            intact = False
        if not _valid(bar):
            intact = False
        accepted_bar = intact and _valid(bar)
        if accepted_bar:
            if bar.volume > 0:
                session_cum += split_adjusted_volume(bar.volume, bar.price_factor)
            session.append(bar)
            accepted.append(bar)
            pending_cum[bucket] = session_cum
            ret_now = _session_return(session)
            if ret_now is not None:
                pending_ret[bucket] = ret_now
            atr_now = atr_of([(item.high, item.low, item.close) for item in accepted])
            if atr_now is not None:
                pending_atr[bucket] = atr_now
        last_start = bar.start
        snapshots.append(
            _emit(
                bar,
                accepted,
                session,
                session_cum,
                intact,
                accepted_bar,
                bucket,
                hist_cum,
                hist_ret,
                hist_atr,
            )
        )
    return snapshots


def _emit(
    bar: FeatureBar,
    accepted: list[FeatureBar],
    session: list[FeatureBar],
    session_cum: Decimal,
    intact: bool,
    accepted_bar: bool,
    bucket: str,
    hist_cum: dict[str, list[Decimal]],
    hist_ret: dict[str, list[Decimal]],
    hist_atr: dict[str, list[Decimal]],
) -> SymbolSnapshot:
    ret_window = hist_ret.get(bucket, [])[-TOD_LOOKBACK:]
    cum_window = hist_cum.get(bucket, [])[-TOD_LOOKBACK:]
    atr_window = hist_atr.get(bucket, [])[-TOD_LOOKBACK:]
    baseline_days = len(cum_window)
    sigma = sample_stdev(ret_window) if len(ret_window) >= 2 else None
    atr = atr_of([(item.high, item.low, item.close) for item in accepted])
    rsi = rsi_of([item.close for item in accepted])
    ret = _session_return(session) if accepted_bar else None
    vwap = _session_vwap(session) if intact else None
    ret_norm = _normalize(ret, sigma, baseline_days, len(ret_window))
    rvol_mean, rvol_median = _rvol(session_cum if accepted_bar else None, cum_window)
    rel_vol = None
    if atr is not None and atr_window:
        middle = median(atr_window)
        if middle > 0:
            rel_vol = atr / middle
    trend_z, rms6, slope = _session_path(
        session, hist_ret, hist_cum, intact and accepted_bar
    )
    above = None if vwap is None else bar.close > vwap
    warmed = (
        intact
        and vwap is not None
        and atr is not None
        and trend_z is not None
        and baseline_days >= TOD_LOOKBACK
    )
    return make_symbol_snapshot(
        symbol=bar.symbol,
        data_asof_utc=bar.end,
        bar_start=bar.start,
        ny_date=bar.start.astimezone(NEW_YORK).date().isoformat(),
        bucket=bucket,
        vwap_intact=intact,
        close=bar.close,
        vwap=vwap,
        rsi6=rsi,
        atr14=atr,
        rvol_mean=rvol_mean,
        rvol_median=rvol_median,
        ret=ret,
        sigma_tod=sigma,
        ret_norm=ret_norm,
        vwap_slope=slope,
        trend_z=trend_z,
        rms6=rms6,
        rel_vol=rel_vol,
        baseline_days=baseline_days,
        above_vwap=above,
        warmed=warmed,
    )


def _session_path(
    session: list[FeatureBar],
    hist_ret: dict[str, list[Decimal]],
    hist_cum: dict[str, list[Decimal]],
    publish: bool,
) -> tuple[Decimal | None, Decimal | None, Decimal | None]:
    if not publish:
        return None, None, None
    norms: list[Decimal | None] = []
    vwaps: list[Decimal | None] = []
    num = Decimal(0)
    den = Decimal(0)
    for index, bar in enumerate(session):
        if bar.volume > 0:
            num += _trade_price(bar) * bar.volume
            den += bar.volume
        vwaps.append(num / den if den > 0 else None)
        bucket = bar.start.astimezone(NEW_YORK).strftime("%H:%M")
        ret_window = hist_ret.get(bucket, [])[-TOD_LOOKBACK:]
        baseline = len(hist_cum.get(bucket, [])[-TOD_LOOKBACK:])
        sigma = sample_stdev(ret_window) if len(ret_window) >= 2 else None
        norms.append(_normalize(_return_at(session, index), sigma, baseline, len(ret_window)))
    trend_z, rms6 = _trend(norms)
    return trend_z, rms6, _slope(vwaps)


def _return_at(session: list[FeatureBar], index: int) -> Decimal | None:
    bar = session[index]
    if index == 0:
        return (bar.close - bar.open) / bar.open
    previous = session[index - 1].close
    if previous <= 0:
        return None
    return (bar.close - previous) / previous


def _session_return(session: list[FeatureBar]) -> Decimal | None:
    if not session:
        return None
    return _return_at(session, len(session) - 1)


def _session_vwap(session: list[FeatureBar]) -> Decimal | None:
    num = Decimal(0)
    den = Decimal(0)
    for bar in session:
        if bar.volume <= 0:
            continue
        num += _trade_price(bar) * bar.volume
        den += bar.volume
    if den <= 0:
        return None
    return num / den


def _rvol(cum: Decimal | None, window: list[Decimal]) -> tuple[Decimal | None, Decimal | None]:
    if cum is None or not window:
        return None, None
    average = mean(window)
    middle = median(window)
    mean_ratio = cum / average if average > 0 else None
    median_ratio = cum / middle if middle > 0 else None
    return mean_ratio, median_ratio


def _normalize(
    ret: Decimal | None,
    sigma: Decimal | None,
    baseline_days: int,
    ret_days: int,
) -> Decimal | None:
    if ret is None or sigma is None or sigma <= 0:
        return None
    if baseline_days < TOD_LOOKBACK or ret_days < TOD_LOOKBACK:
        return None
    return ret / sigma


def _trend(norms: list[Decimal | None]) -> tuple[Decimal | None, Decimal | None]:
    if len(norms) < TREND_BARS:
        return None, None
    window = norms[-TREND_BARS:]
    if any(item is None for item in window):
        return None, None
    values = [item for item in window if item is not None]
    total = sum(values, start=Decimal(0))
    square = sum((item * item for item in values), start=Decimal(0)) / Decimal(TREND_BARS)
    return total, square.sqrt()


def _slope(vwaps: list[Decimal | None]) -> Decimal | None:
    if len(vwaps) < SLOPE_LAG + 1:
        return None
    window = vwaps[-(SLOPE_LAG + 1) :]
    if any(item is None for item in window):
        return None
    first = window[0]
    last = window[-1]
    if first is None or last is None:
        return None
    return last - first


def _trade_price(bar: FeatureBar) -> Decimal:
    if bar.bar_vwap is not None:
        return bar.bar_vwap
    return (bar.high + bar.low + bar.close) / Decimal(3)


def _valid(bar: FeatureBar) -> bool:
    if bar.feed.casefold() != "sip" or bar.price_factor <= 0 or bar.volume < 0:
        return False
    if min(bar.open, bar.high, bar.low, bar.close) <= 0 or bar.high < bar.low:
        return False
    if bar.open < bar.low or bar.open > bar.high or bar.close < bar.low or bar.close > bar.high:
        return False
    return not (bar.bar_vwap is not None and bar.bar_vwap <= 0)


def _clock(value: datetime) -> time:
    return value.astimezone(NEW_YORK).timetz().replace(tzinfo=None)
