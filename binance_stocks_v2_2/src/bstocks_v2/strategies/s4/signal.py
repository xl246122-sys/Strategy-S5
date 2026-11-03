"""Baseline bounce versus the OU-and-knife arm. The probability triple does not decide entry."""

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal

from bstocks_v2.constitution.guard import entry_window_open
from bstocks_v2.contracts.models import Direction, FillHorizon, TradeIntent
from bstocks_v2.feature_store.bars import FeatureBar, SymbolSnapshot
from bstocks_v2.regime.classifier import FeatureSnapshot, Regime
from bstocks_v2.research_backtest.fills import floor_step
from bstocks_v2.research_backtest.models import MAX_POSITION_FRACTION
from bstocks_v2.strategies.s1.spec import SECTOR_PARENT
from bstocks_v2.strategies.s4.bounce import Pattern
from bstocks_v2.strategies.s4.guard import KNIFE, knife_details
from bstocks_v2.strategies.s4.ou import OuFit, fit_ar1, qualifies
from bstocks_v2.strategies.s4.spec import (
    ACCOUNT_EQUITY,
    COMMISSION_RATE,
    HALF_SPREAD,
    HOLD_SECONDS,
    POSITION_FRACTION,
    RISK_BUDGET,
    TTL_SECONDS,
    WINDOW,
)

_P_TP = Decimal("0.333333334")
_P_SL = Decimal("0.333333333")
_P_TO = Decimal("0.333333333")
UNCALIBRATED = (_P_TP, _P_SL, _P_TO)


@dataclass(frozen=True)
class Candidate:
    symbol: str
    signal_end: datetime
    ny_date: date
    features_hash: str
    regime: str
    rsi: Decimal
    z: Decimal
    limit_price: Decimal
    stop_price: Decimal
    take_profit_price: Decimal
    phi: Decimal | None
    half_life_minutes: Decimal | None


@dataclass(frozen=True)
class Decision:
    candidate: Candidate | None
    reason: str
    knife: tuple[str, ...] = ()


def opening_qty(limit: Decimal, stop: Decimal) -> Decimal:
    """Whole shares from the opening account. One trade does not resize the next."""

    if POSITION_FRACTION != MAX_POSITION_FRACTION:
        raise RuntimeError("position cap drifted")
    span = limit - stop
    if span <= 0 or limit <= 0:
        return Decimal(0)
    room = ACCOUNT_EQUITY * POSITION_FRACTION
    raw = min(RISK_BUDGET / span, room / limit)
    return floor_step(raw, Decimal(1))


def decide(
    *,
    arm: str,
    symbol: str,
    pattern: Pattern | None,
    pattern_reason: str,
    snap: SymbolSnapshot,
    regime: FeatureSnapshot | None,
    research_eligible: bool,
    half_day: bool,
    residuals: tuple[Decimal, ...],
    breakdown: bool,
    spy_bar: FeatureBar | None,
    spy_snap: SymbolSnapshot | None,
) -> Decision:
    if arm not in {"baseline", "guarded"}:
        raise ValueError("arm")
    if (pattern is None) == (pattern_reason == "READY"):
        raise RuntimeError("pattern flag disagrees with the reason")
    early = _screen(
        symbol=symbol,
        pattern=pattern,
        pattern_reason=pattern_reason,
        snap=snap,
        regime=regime,
        research_eligible=research_eligible,
        half_day=half_day,
    )
    if early is not None or pattern is None:
        return Decision(None, early or pattern_reason)
    if arm == "baseline":
        return Decision(_candidate(symbol, pattern, snap, regime, None), "SIGNAL")
    fit, fit_reason = _fit(residuals)
    details = knife_details(
        regime=regime,
        breakdown=breakdown,
        phi=None if fit is None else fit.phi,
        spy_bar=spy_bar,
        spy_snap=spy_snap,
    )
    if details:
        return Decision(None, KNIFE, details)
    if regime is None:
        return Decision(None, "NO_REGIME")
    if regime.regime == Regime.UNKNOWN:
        return Decision(None, "REGIME_UNKNOWN")
    if not regime.s4_research_enabled:
        return Decision(None, "S4_RESEARCH_OFF")
    if fit is None:
        return Decision(None, fit_reason or "OU_WARMUP")
    verdict = qualifies(fit)
    if verdict != "OK":
        return Decision(None, verdict)
    return Decision(_candidate(symbol, pattern, snap, regime, fit), "SIGNAL")


def make_intent(candidate: Candidate, *, arm: str) -> TradeIntent:
    """`arm` is `baseline` or `guarded`. Equal probabilities do not decide entry."""

    if arm not in {"baseline", "guarded"}:
        raise ValueError("arm")
    if sum(UNCALIBRATED) != 1:
        raise RuntimeError("placeholder probabilities drifted")
    limit = candidate.limit_price
    stop = candidate.stop_price
    take_profit = candidate.take_profit_price
    p_tp, p_sl, p_to = UNCALIBRATED
    reward = (take_profit - limit) / limit * Decimal(10000)
    risk = (limit - stop) / limit * Decimal(10000)
    cost_bps = (COMMISSION_RATE * 2 + (HALF_SPREAD * 2) / limit) * Decimal(10000)
    gross = p_tp * reward - p_sl * risk
    share_cost = COMMISSION_RATE * (limit + take_profit) + HALF_SPREAD * 2
    quantity = opening_qty(limit, stop)
    expected_cost = share_cost * quantity if quantity >= 1 else share_cost
    stamp = candidate.signal_end.strftime("%Y%m%dT%H%M%SZ")
    tag = "b" if arm == "baseline" else "g"
    return TradeIntent(
        intent_id=f"s4{tag}{candidate.symbol}{stamp}",
        created_at_utc=candidate.signal_end,
        data_asof_utc=candidate.signal_end,
        symbol=candidate.symbol,
        strategy_id="S4",
        model_version=f"s4-v1-vwap-rsi-{arm}",
        regime=candidate.regime,
        features_hash=candidate.features_hash,
        vwap_intact=True,
        signal_valid_until=candidate.signal_end + timedelta(seconds=TTL_SECONDS),
        direction=Direction.LONG,
        p_win_calibrated=p_tp,
        p_sl_calibrated=p_sl,
        p_timeout_calibrated=p_to,
        expected_return_bps=gross,
        expected_net_bps=gross - cost_bps,
        prediction_interval=(-risk, reward),
        candidate_limit_prices=[limit],
        p_fill_by_price_horizon=[
            FillHorizon(price=limit, horizon_seconds=TTL_SECONDS, p_fill=Decimal(0))
        ],
        entry_price_ceiling=limit,
        stop_price=stop,
        take_profit_price=take_profit,
        max_holding_seconds=HOLD_SECONDS,
        risk_budget_usdc=RISK_BUDGET,
        min_viable_notional_usdc=Decimal(120),
        expected_cost_usdc=expected_cost,
        requires_fractional=False,
        whole_share_fallback_qty=Decimal(0),
        rejection_reasons=[],
        data_feed="sip",
        data_freshness_ms=0,
    )


def _screen(
    *,
    symbol: str,
    pattern: Pattern | None,
    pattern_reason: str,
    snap: SymbolSnapshot,
    regime: FeatureSnapshot | None,
    research_eligible: bool,
    half_day: bool,
) -> str | None:
    if symbol not in SECTOR_PARENT:
        return "NOT_IN_S4_UNIVERSE"
    if not research_eligible:
        return "NOT_ELIGIBLE"
    if regime is not None and regime.data_asof_utc != snap.data_asof_utc:
        return "REGIME_ASOF"
    if pattern is None:
        return pattern_reason
    if not entry_window_open(snap.data_asof_utc, half_day):
        return "ENTRY_WINDOW"
    return None


def _fit(residuals: tuple[Decimal, ...]) -> tuple[OuFit | None, str | None]:
    if len(residuals) < WINDOW:
        return None, "OU_WARMUP"
    fit = fit_ar1(residuals[-WINDOW:])
    if fit is None:
        return None, "OU_SINGULAR"
    return fit, None


def _candidate(
    symbol: str,
    pattern: Pattern,
    snap: SymbolSnapshot,
    regime: FeatureSnapshot | None,
    fit: OuFit | None,
) -> Candidate:
    return Candidate(
        symbol=symbol,
        signal_end=snap.data_asof_utc,
        ny_date=date.fromisoformat(snap.ny_date),
        features_hash=snap.features_hash,
        regime="NONE" if regime is None else regime.regime.value,
        rsi=pattern.rsi,
        z=pattern.z,
        limit_price=pattern.limit_price,
        stop_price=pattern.stop_price,
        take_profit_price=pattern.take_profit_price,
        phi=None if fit is None else fit.phi,
        half_life_minutes=None if fit is None else fit.half_life_minutes,
    )
