"""Hard reject for a bounce that is still a falling knife. The reason code is fixed."""

from decimal import Decimal

from bstocks_v2.feature_store.bars import FeatureBar, SymbolSnapshot
from bstocks_v2.regime.classifier import FeatureSnapshot, Regime
from bstocks_v2.strategies.s4.spec import MARKET_BAR_ATR, MARKET_RET_NORM

KNIFE = "FALLING_KNIFE_GUARD_TRIGGERED"


def market_selloff(bar: FeatureBar | None, snap: SymbolSnapshot | None) -> bool:
    """SPY dropped at least 0.75 ATR on this bar, or its normalized return is at or below -1.5."""

    if bar is None or snap is None:
        return False
    if bar.symbol != "SPY" or snap.symbol != "SPY" or bar.end != snap.data_asof_utc:
        return False
    if snap.atr14 is not None and snap.atr14 > 0:
        if (bar.open - bar.close) / snap.atr14 >= MARKET_BAR_ATR:
            return True
    return snap.ret_norm is not None and snap.ret_norm <= MARKET_RET_NORM


def knife_details(
    *,
    regime: FeatureSnapshot | None,
    breakdown: bool,
    phi: Decimal | None,
    spy_bar: FeatureBar | None,
    spy_snap: SymbolSnapshot | None,
) -> tuple[str, ...]:
    """Every clause that fires. The caller still records one FALLING_KNIFE_GUARD_TRIGGERED."""

    found: list[str] = []
    if regime is not None and regime.regime == Regime.TREND_DOWN:
        found.append("REGIME_TREND_DOWN")
    if regime is not None and regime.regime == Regime.HIGH_VOL_PANIC:
        found.append("REGIME_HIGH_VOL_PANIC")
    if market_selloff(spy_bar, spy_snap):
        found.append("MARKET_SELLOFF")
    if breakdown:
        found.append("RVOL_BREAKDOWN")
    if phi is not None and phi >= 1:
        found.append("PHI_GE_ONE")
    return tuple(found)
