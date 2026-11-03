"""One-bar causal feature state. Past snapshots are not rewritten."""

from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from decimal import Decimal

from bstocks_v2.feature_store.bars import FeatureBar, SymbolSnapshot, make_symbol_snapshot
from bstocks_v2.feature_store.errors import LookaheadError
from bstocks_v2.feature_store.spec import (
    ATR_PERIOD,
    RSI_PERIOD,
    SLOPE_LAG,
    TOD_LOOKBACK,
    TREND_BARS,
    mean,
    median,
    sample_stdev,
)
from bstocks_v2.research_data.adjust import split_adjusted_volume
from bstocks_v2.research_data.calendar import NEW_YORK, XnysCalendar

_OPEN = time(9, 30)


@dataclass
class _Pending:
    cum: Decimal
    ret: Decimal | None
    atr: Decimal | None


@dataclass
class _Sym:
    day: object | None = None
    last_start: datetime | None = None
    last_key: tuple[str, ...] | None = None
    last_snapshot: SymbolSnapshot | None = None
    intact: bool = True
    opened_on_time: bool = False
    vwap_num: Decimal = Decimal(0)
    vwap_den: Decimal = Decimal(0)
    cum_volume: Decimal = Decimal(0)
    last_accepted_close: Decimal | None = None
    session_accepted: int = 0
    rsi_gains: list[Decimal] = field(default_factory=list)
    rsi_losses: list[Decimal] = field(default_factory=list)
    rsi_avg_gain: Decimal | None = None
    rsi_avg_loss: Decimal | None = None
    rsi_seeded: bool = False
    current_rsi: Decimal | None = None
    atr_seed: list[Decimal] = field(default_factory=list)
    atr_seeded: bool = False
    current_atr: Decimal | None = None
    pending: dict[str, _Pending] = field(default_factory=dict)
    hist_cum: dict[str, list[Decimal]] = field(default_factory=dict)
    hist_ret: dict[str, list[Decimal]] = field(default_factory=dict)
    hist_atr: dict[str, list[Decimal]] = field(default_factory=dict)
    session_norms: list[Decimal | None] = field(default_factory=list)
    session_vwaps: list[Decimal | None] = field(default_factory=list)


class CausalFeatureBook:
    """Append-only book. Each symbol is updated only with its own closed bars."""

    def __init__(self, calendar: XnysCalendar) -> None:
        self.calendar = calendar
        self._states: dict[str, _Sym] = {}
        self.latest: dict[str, SymbolSnapshot] = {}

    def update(self, bar: FeatureBar) -> SymbolSnapshot | None:
        if not bar.closed:
            raise LookaheadError("unclosed bar")
        _require_interval(bar)
        if self.calendar.classify(bar.start, bar.end) != "RTH":
            return None
        state = self._states.setdefault(bar.symbol, _Sym())
        snapshot = _advance(state, bar)
        if snapshot is not None:
            self.latest[bar.symbol] = snapshot
        return snapshot


def _advance(state: _Sym, bar: FeatureBar) -> SymbolSnapshot:
    key = _print_key(bar)
    if state.last_start is not None and bar.start < state.last_start:
        raise LookaheadError("bar is earlier than the last closed bar")
    if state.last_start is not None and bar.start == state.last_start:
        if state.last_key != key:
            raise ValueError("duplicate bar disagrees with the stored print")
        if state.last_snapshot is None:
            raise LookaheadError("duplicate bar has no stored snapshot")
        return state.last_snapshot

    day = bar.start.astimezone(NEW_YORK).date()
    if state.day != day:
        _commit(state)
        _reset_session(state)
        state.day = day

    bucket = bar.start.astimezone(NEW_YORK).strftime("%H:%M")
    if state.last_start is None:
        if _clock(bar.start) == _OPEN:
            state.opened_on_time = True
            state.intact = True
        else:
            state.opened_on_time = False
            state.intact = False
    else:
        expected = state.last_start + timedelta(minutes=5)
        if bar.start != expected:
            state.intact = False
    if not _valid(bar):
        state.intact = False

    accepted = state.intact and _valid(bar)
    ret: Decimal | None = None
    if accepted:
        _push_rsi(state, bar.close)
        _push_atr(state, bar.high, bar.low)
        if bar.volume > 0:
            price = _trade_price(bar)
            state.vwap_num += price * bar.volume
            state.vwap_den += bar.volume
            state.cum_volume += split_adjusted_volume(bar.volume, bar.price_factor)
        if state.session_accepted == 0:
            ret = (bar.close - bar.open) / bar.open
        else:
            previous = state.last_accepted_close
            if previous is None or previous <= 0:
                ret = None
            else:
                ret = (bar.close - previous) / previous
        state.session_accepted += 1
        state.last_accepted_close = bar.close

    vwap = _published_vwap(state)
    ret_window = state.hist_ret.get(bucket, [])[-TOD_LOOKBACK:]
    cum_window = state.hist_cum.get(bucket, [])[-TOD_LOOKBACK:]
    atr_window = state.hist_atr.get(bucket, [])[-TOD_LOOKBACK:]
    baseline_days = len(cum_window)
    sigma = sample_stdev(ret_window) if len(ret_window) >= 2 else None
    ret_norm: Decimal | None = None
    if (
        ret is not None
        and sigma is not None
        and sigma > 0
        and baseline_days >= TOD_LOOKBACK
        and len(ret_window) >= TOD_LOOKBACK
    ):
        ret_norm = ret / sigma
    rvol_mean: Decimal | None = None
    rvol_median: Decimal | None = None
    if accepted and cum_window:
        average = mean(cum_window)
        middle = median(cum_window)
        if average > 0:
            rvol_mean = state.cum_volume / average
        if middle > 0:
            rvol_median = state.cum_volume / middle
    rel_vol: Decimal | None = None
    if state.current_atr is not None and atr_window:
        middle_atr = median(atr_window)
        if middle_atr > 0:
            rel_vol = state.current_atr / middle_atr

    if accepted:
        state.pending[bucket] = _Pending(state.cum_volume, ret, state.current_atr)
        state.session_norms.append(ret_norm)
        state.session_vwaps.append(vwap)
    else:
        state.session_norms.append(None)
        state.session_vwaps.append(None)

    trend_z, rms6 = _trend(state.session_norms)
    slope = _slope(state.session_vwaps)
    above = None if vwap is None else bar.close > vwap
    warmed = (
        state.intact
        and vwap is not None
        and state.current_atr is not None
        and trend_z is not None
        and baseline_days >= TOD_LOOKBACK
    )
    snapshot = make_symbol_snapshot(
        symbol=bar.symbol,
        data_asof_utc=bar.end,
        bar_start=bar.start,
        ny_date=day.isoformat(),
        bucket=bucket,
        vwap_intact=state.intact,
        close=bar.close,
        vwap=vwap,
        rsi6=state.current_rsi,
        atr14=state.current_atr,
        rvol_mean=rvol_mean,
        rvol_median=rvol_median,
        ret=ret,
        sigma_tod=sigma if len(ret_window) >= 2 else None,
        ret_norm=ret_norm,
        vwap_slope=slope,
        trend_z=trend_z,
        rms6=rms6,
        rel_vol=rel_vol,
        baseline_days=baseline_days,
        above_vwap=above,
        warmed=warmed,
    )
    state.last_start = bar.start
    state.last_key = key
    state.last_snapshot = snapshot
    return snapshot


def _commit(state: _Sym) -> None:
    if state.day is None or not state.intact or not state.opened_on_time:
        return
    for bucket, item in state.pending.items():
        state.hist_cum.setdefault(bucket, []).append(item.cum)
        if item.ret is not None:
            state.hist_ret.setdefault(bucket, []).append(item.ret)
        if item.atr is not None:
            state.hist_atr.setdefault(bucket, []).append(item.atr)


def _reset_session(state: _Sym) -> None:
    state.intact = True
    state.opened_on_time = False
    state.vwap_num = Decimal(0)
    state.vwap_den = Decimal(0)
    state.cum_volume = Decimal(0)
    state.session_accepted = 0
    state.pending.clear()
    state.session_norms.clear()
    state.session_vwaps.clear()
    state.last_start = None


def _push_rsi(state: _Sym, close: Decimal) -> None:
    previous = state.last_accepted_close
    if previous is None:
        state.current_rsi = None
        return
    delta = close - previous
    gain = delta if delta > 0 else Decimal(0)
    loss = -delta if delta < 0 else Decimal(0)
    span = Decimal(RSI_PERIOD)
    if not state.rsi_seeded:
        state.rsi_gains.append(gain)
        state.rsi_losses.append(loss)
        if len(state.rsi_gains) < RSI_PERIOD:
            state.current_rsi = None
            return
        state.rsi_avg_gain = sum(state.rsi_gains, start=Decimal(0)) / span
        state.rsi_avg_loss = sum(state.rsi_losses, start=Decimal(0)) / span
        state.rsi_seeded = True
    else:
        gain_avg = state.rsi_avg_gain
        loss_avg = state.rsi_avg_loss
        if gain_avg is None or loss_avg is None:
            raise RuntimeError("RSI seed was marked ready without averages")
        state.rsi_avg_gain = ((gain_avg * (span - 1)) + gain) / span
        state.rsi_avg_loss = ((loss_avg * (span - 1)) + loss) / span
    gain_ready = state.rsi_avg_gain
    loss_ready = state.rsi_avg_loss
    if gain_ready is None or loss_ready is None:
        raise RuntimeError("RSI averages missing after an update")
    state.current_rsi = _rsi(gain_ready, loss_ready)


def _push_atr(state: _Sym, high: Decimal, low: Decimal) -> None:
    previous = state.last_accepted_close
    if previous is None:
        true_range = high - low
    else:
        true_range = max(high - low, abs(high - previous), abs(low - previous))
    span = Decimal(ATR_PERIOD)
    if not state.atr_seeded:
        state.atr_seed.append(true_range)
        if len(state.atr_seed) < ATR_PERIOD:
            state.current_atr = None
            return
        state.current_atr = sum(state.atr_seed, start=Decimal(0)) / span
        state.atr_seeded = True
        return
    current = state.current_atr
    if current is None:
        raise RuntimeError("ATR seed was marked ready without a value")
    state.current_atr = ((current * (span - 1)) + true_range) / span


def _published_vwap(state: _Sym) -> Decimal | None:
    if not state.intact or state.vwap_den <= 0:
        return None
    return state.vwap_num / state.vwap_den


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


def _rsi(avg_gain: Decimal, avg_loss: Decimal) -> Decimal:
    if avg_loss == 0:
        return Decimal(100) if avg_gain > 0 else Decimal(50)
    rs = avg_gain / avg_loss
    return Decimal(100) - (Decimal(100) / (Decimal(1) + rs))


def _trade_price(bar: FeatureBar) -> Decimal:
    if bar.bar_vwap is not None:
        return bar.bar_vwap
    return (bar.high + bar.low + bar.close) / Decimal(3)


def _valid(bar: FeatureBar) -> bool:
    if bar.feed.casefold() != "sip":
        return False
    if bar.price_factor <= 0 or bar.volume < 0:
        return False
    if min(bar.open, bar.high, bar.low, bar.close) <= 0:
        return False
    if bar.high < bar.low:
        return False
    if bar.open < bar.low or bar.open > bar.high or bar.close < bar.low or bar.close > bar.high:
        return False
    return not (bar.bar_vwap is not None and bar.bar_vwap <= 0)


def _clock(value: datetime) -> time:
    return value.astimezone(NEW_YORK).timetz().replace(tzinfo=None)


def _print_key(bar: FeatureBar) -> tuple[str, ...]:
    return (
        bar.symbol,
        bar.start.isoformat(),
        format(bar.open, "f"),
        format(bar.high, "f"),
        format(bar.low, "f"),
        format(bar.close, "f"),
        format(bar.volume, "f"),
        "" if bar.bar_vwap is None else format(bar.bar_vwap, "f"),
        format(bar.price_factor, "f"),
        bar.feed,
    )


def _require_interval(bar: FeatureBar) -> None:
    if bar.start.tzinfo is None or bar.end.tzinfo is None:
        raise ValueError("bar timestamps must be timezone-aware")
    if bar.end <= bar.start:
        raise ValueError("bar end must be after bar start")
