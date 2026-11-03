"""Causal VWAP / RSI(6) oversold bounce. A later bar cannot rewrite an earlier residual."""

from dataclasses import dataclass, field
from datetime import datetime
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal

from bstocks_v2.feature_store.bars import FeatureBar, SymbolSnapshot
from bstocks_v2.strategies.s4.ou import dimensionless
from bstocks_v2.strategies.s4.spec import (
    BREAK_BARS,
    BREAK_RECENT,
    LONG_BODY_ATR,
    RSI_OVERSOLD,
    RVOL_KNIFE,
    STOP_ATR,
    Z_STRETCH,
)

_CENT = Decimal("0.01")
_READY = "READY"


@dataclass(frozen=True)
class Pattern:
    """One fresh cross back through RSI 30. The bracket is the one the replay will use."""

    rsi: Decimal
    z: Decimal
    rvol: Decimal | None
    limit_price: Decimal
    stop_price: Decimal
    take_profit_price: Decimal


@dataclass
class _Print:
    open: Decimal
    low: Decimal
    close: Decimal
    atr: Decimal
    rsi: Decimal
    z: Decimal
    rvol: Decimal | None


@dataclass
class _State:
    day: str | None = None
    last_start: datetime | None = None
    prints: list[_Print] = field(default_factory=list)


class BounceBook:
    """Per-symbol session buffer. z uses only the closed bar's own VWAP and ATR."""

    def __init__(self) -> None:
        self._states: dict[str, _State] = {}

    def observe(self, bar: FeatureBar, snap: SymbolSnapshot) -> tuple[Pattern | None, str]:
        if not bar.closed:
            raise RuntimeError("bar is not closed")
        if bar.symbol != snap.symbol or bar.end != snap.data_asof_utc:
            raise RuntimeError("snapshot is not this bar")
        if snap.close != bar.close:
            raise RuntimeError("snapshot close disagrees with the bar")
        state = self._states.setdefault(bar.symbol, _State())
        if state.last_start is not None and bar.start <= state.last_start:
            raise RuntimeError("bar is not after the previous print")
        state.last_start = bar.start
        if state.day != snap.ny_date:
            state.prints.clear()
            state.day = snap.ny_date
        if not snap.vwap_intact or snap.vwap is None:
            state.prints.clear()
            return None, "NOT_INTACT"
        if snap.rsi6 is None or snap.atr14 is None or snap.atr14 <= 0:
            state.prints.clear()
            return None, "WARMUP"
        state.prints.append(
            _Print(
                open=bar.open,
                low=bar.low,
                close=bar.close,
                atr=snap.atr14,
                rsi=snap.rsi6,
                z=dimensionless(snap.close, snap.vwap, snap.atr14),
                rvol=snap.rvol_median,
            )
        )
        return _judge(state, snap.vwap)

    def residuals(self, symbol: str) -> tuple[Decimal, ...]:
        state = self._states.get(symbol)
        if state is None:
            return ()
        return tuple(item.z for item in state.prints)

    def rvol_breakdown(self, symbol: str) -> bool:
        """High-RVOL long red break of the prior six lows, on one of the last three prints."""

        state = self._states.get(symbol)
        if state is None or len(state.prints) <= BREAK_BARS:
            return False
        start = max(BREAK_BARS, len(state.prints) - BREAK_RECENT)
        for index in range(start, len(state.prints)):
            item = state.prints[index]
            if item.rvol is None or item.rvol <= RVOL_KNIFE or item.atr <= 0:
                continue
            if item.open - item.close < LONG_BODY_ATR * item.atr:
                continue
            prior = state.prints[index - BREAK_BARS : index]
            if item.close < min(print_.low for print_ in prior):
                return True
        return False


def _judge(state: _State, vwap: Decimal) -> tuple[Pattern | None, str]:
    if len(state.prints) < 2:
        return None, "WARMUP"
    current = state.prints[-1]
    previous = state.prints[-2]
    if current.close >= vwap:
        return None, "ABOVE_VWAP"
    if current.z > -Z_STRETCH:
        return None, "NO_STRETCH"
    if previous.rsi >= RSI_OVERSOLD or current.rsi < RSI_OVERSOLD:
        return None, "NO_CROSS"
    if current.close <= current.open:
        return None, "NO_BOUNCE"
    bracket = _bracket(current.close, current.atr, vwap)
    if bracket is None:
        return None, "GEOMETRY"
    limit, stop, take_profit = bracket
    return (
        Pattern(
            rsi=current.rsi,
            z=current.z,
            rvol=current.rvol,
            limit_price=limit,
            stop_price=stop,
            take_profit_price=take_profit,
        ),
        _READY,
    )


def _bracket(
    close: Decimal, atr: Decimal, vwap: Decimal
) -> tuple[Decimal, Decimal, Decimal] | None:
    limit = close.quantize(_CENT, rounding=ROUND_HALF_UP)
    stop = (limit - atr * STOP_ATR).quantize(_CENT, rounding=ROUND_DOWN)
    take_profit = vwap.quantize(_CENT, rounding=ROUND_DOWN)
    if stop <= 0 or not stop < limit < take_profit:
        return None
    return limit, stop, take_profit
