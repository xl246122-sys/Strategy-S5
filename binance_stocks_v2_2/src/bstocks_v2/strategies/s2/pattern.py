"""Causal VWAP / RSI(6) pullback. A later bar cannot rewrite an earlier pattern."""

from dataclasses import dataclass, field
from datetime import datetime
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal

from bstocks_v2.feature_store.bars import FeatureBar, SymbolSnapshot
from bstocks_v2.strategies.s2.spec import (
    BASE_BARS,
    COOLDOWN_BARS,
    LOOKBACK,
    RSI_DIP,
    RSI_RECLAIM,
    STOP_ATR,
    TARGET_MULTIPLE,
    VWAP_EXTENSION_ATR,
)

_CENT = Decimal("0.01")
_READY = "READY"


@dataclass(frozen=True)
class Pattern:
    """One fresh reclaim. Prices are already the bracket the replay will use."""

    rsi: Decimal
    dip_rsi: Decimal
    extension: Decimal
    slope_atr: Decimal
    volume_ratio: Decimal
    rel_vol: Decimal | None
    limit_price: Decimal
    stop_price: Decimal
    take_profit_price: Decimal


@dataclass
class _Print:
    low: Decimal
    close: Decimal
    volume: Decimal
    vwap: Decimal
    rsi: Decimal
    atr: Decimal
    slope: Decimal


@dataclass
class _State:
    day: str | None = None
    last_start: datetime | None = None
    committed: int | None = None
    prints: list[_Print] = field(default_factory=list)


class PullbackBook:
    """Per-symbol session buffer. VWAP slope and RSI come from the causal feature book."""

    def __init__(self) -> None:
        self._states: dict[str, _State] = {}

    def observe(self, bar: FeatureBar, snap: SymbolSnapshot) -> tuple[Pattern | None, str]:
        if bar.symbol != snap.symbol or bar.end != snap.data_asof_utc:
            raise RuntimeError("snapshot is not this bar")
        if snap.close != bar.close:
            raise RuntimeError("snapshot close disagrees with the bar")
        state = self._states.setdefault(bar.symbol, _State())
        if state.last_start is not None and bar.start <= state.last_start:
            raise RuntimeError("bar is not after the previous print")
        if state.day != snap.ny_date:
            state.prints.clear()
            state.committed = None
            state.day = snap.ny_date
        state.last_start = bar.start
        if not snap.vwap_intact or snap.vwap is None:
            state.prints.clear()
            state.committed = None
            return None, "NOT_INTACT"
        if snap.rsi6 is None or snap.atr14 is None or snap.atr14 <= 0 or snap.vwap_slope is None:
            return None, "WARMUP"
        state.prints.append(
            _Print(
                low=bar.low,
                close=bar.close,
                volume=bar.volume,
                vwap=snap.vwap,
                rsi=snap.rsi6,
                atr=snap.atr14,
                slope=snap.vwap_slope,
            )
        )
        return _judge(state, snap.rel_vol)

    def commit(self, symbol: str) -> None:
        state = self._states[symbol]
        if not state.prints:
            raise RuntimeError("nothing to commit")
        state.committed = len(state.prints) - 1


def pattern_features(pattern: Pattern) -> tuple[Decimal, Decimal, Decimal, Decimal, Decimal]:
    """Five causal inputs. The order is part of the frozen design matrix."""

    return (
        (pattern.rsi - RSI_RECLAIM) / RSI_RECLAIM,
        (RSI_DIP - pattern.dip_rsi) / RSI_DIP,
        pattern.extension,
        pattern.slope_atr,
        pattern.volume_ratio,
    )


def _judge(state: _State, rel_vol: Decimal | None) -> tuple[Pattern | None, str]:
    index = len(state.prints) - 1
    if index < LOOKBACK + BASE_BARS:
        return None, "WARMUP"
    current = state.prints[index]
    previous = state.prints[index - 1]
    if current.slope <= 0:
        return None, "SLOPE"
    if current.close <= current.vwap:
        return None, "BELOW_VWAP"
    extension = (current.close - current.vwap) / current.atr
    if extension > VWAP_EXTENSION_ATR:
        return None, "EXTENDED"
    if previous.rsi > RSI_RECLAIM or current.rsi <= RSI_RECLAIM:
        return None, "NO_CROSS"
    window_start = index - LOOKBACK
    dip = _latest_dip(state.prints, window_start, index)
    if dip is None:
        return None, "NO_DIP"
    if dip < BASE_BARS:
        return None, "WARMUP"
    base = state.prints[dip - BASE_BARS : dip]
    pull = state.prints[dip:index]
    span = state.prints[dip : index + 1]
    if not _contracted(base, pull):
        return None, "VOLUME"
    if not _held_structure(base, span):
        return None, "STRUCTURE"
    if state.committed is not None and index - state.committed < COOLDOWN_BARS:
        return None, "COOLDOWN"
    bracket = _bracket(current.close, current.atr)
    if bracket is None:
        return None, "GEOMETRY"
    limit, stop, take_profit = bracket
    base_volume = _mean_volume(base)
    return (
        Pattern(
            rsi=current.rsi,
            dip_rsi=min(item.rsi for item in state.prints[window_start:index]),
            extension=extension,
            slope_atr=current.slope / current.atr,
            volume_ratio=_mean_volume(pull) / base_volume,
            rel_vol=rel_vol,
            limit_price=limit,
            stop_price=stop,
            take_profit_price=take_profit,
        ),
        _READY,
    )


def _latest_dip(prints: list[_Print], start: int, index: int) -> int | None:
    for cursor in range(index - 1, start - 1, -1):
        if prints[cursor].rsi < RSI_DIP:
            return cursor
    return None


def _contracted(base: list[_Print], pull: list[_Print]) -> bool:
    return _mean_volume(pull) < _mean_volume(base)


def _held_structure(base: list[_Print], span: list[_Print]) -> bool:
    if any(item.close <= item.vwap for item in span):
        return False
    return min(item.low for item in span) >= min(item.low for item in base)


def _mean_volume(prints: list[_Print]) -> Decimal:
    return sum((item.volume for item in prints), start=Decimal(0)) / Decimal(len(prints))


def _bracket(close: Decimal, atr: Decimal) -> tuple[Decimal, Decimal, Decimal] | None:
    limit = close.quantize(_CENT, rounding=ROUND_HALF_UP)
    distance = atr * STOP_ATR
    stop = (limit - distance).quantize(_CENT, rounding=ROUND_DOWN)
    take_profit = (limit + distance * TARGET_MULTIPLE).quantize(_CENT, rounding=ROUND_DOWN)
    if stop <= 0 or not stop < limit < take_profit:
        return None
    return limit, stop, take_profit
