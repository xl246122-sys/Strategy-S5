"""Decision-time expectation and the long-only intent. The gate is a strict inequality."""

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal

from bstocks_v2.constitution.guard import entry_window_open
from bstocks_v2.contracts.models import Direction, FillHorizon, TradeIntent
from bstocks_v2.feature_store.bars import SymbolSnapshot
from bstocks_v2.regime.classifier import FeatureSnapshot
from bstocks_v2.research_backtest.fills import floor_step
from bstocks_v2.research_backtest.models import MAX_POSITION_FRACTION
from bstocks_v2.strategies.s1.spec import SECTOR_PARENT
from bstocks_v2.strategies.s2.pattern import Pattern, PullbackBook, pattern_features
from bstocks_v2.strategies.s2.spec import (
    ACCOUNT_EQUITY,
    COMMISSION_RATE,
    GATE_MIN_NET_BPS,
    HALF_SPREAD,
    HOLD_SECONDS,
    POSITION_FRACTION,
    RISK_BUDGET,
    TTL_SECONDS,
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
    features: tuple[Decimal, Decimal, Decimal, Decimal, Decimal]
    rel_vol: Decimal | None
    sector: str
    limit_price: Decimal
    stop_price: Decimal
    take_profit_price: Decimal


def opening_qty(limit: Decimal, stop: Decimal) -> Decimal:
    """Whole shares from the opening account. One trade does not resize the next label."""

    if POSITION_FRACTION != MAX_POSITION_FRACTION:
        raise RuntimeError("position cap drifted")
    span = limit - stop
    if span <= 0 or limit <= 0:
        return Decimal(0)
    room = ACCOUNT_EQUITY * POSITION_FRACTION
    raw = min(RISK_BUDGET / span, room / limit)
    return floor_step(raw, Decimal(1))


def round_trip_cost_bps(limit: Decimal) -> Decimal:
    if limit <= 0:
        raise RuntimeError("limit")
    return (COMMISSION_RATE * 2 + (HALF_SPREAD * 2) / limit) * Decimal(10000)


def expected_net_bps(
    probabilities: tuple[Decimal, Decimal, Decimal],
    *,
    limit: Decimal,
    stop: Decimal,
    take_profit: Decimal,
    timeout_bps: Decimal,
) -> Decimal:
    """p_TP * R_TP - p_SL * R_SL + p_TO * E[R_TO] - round-trip cost, all in bps."""

    p_tp, p_sl, p_to = probabilities
    r_tp = (take_profit - limit) / limit * Decimal(10000)
    r_sl = (limit - stop) / limit * Decimal(10000)
    return p_tp * r_tp - p_sl * r_sl + p_to * timeout_bps - round_trip_cost_bps(limit)


def gate_open(net_bps: Decimal) -> bool:
    return net_bps > GATE_MIN_NET_BPS


def decide(
    *,
    symbol: str,
    pattern: Pattern | None,
    pattern_reason: str,
    snap: SymbolSnapshot,
    regime: FeatureSnapshot,
    research_eligible: bool,
    half_day: bool,
    book: PullbackBook,
) -> tuple[Candidate | None, str]:
    if (pattern is None) == (pattern_reason == "READY"):
        raise RuntimeError("pattern flag disagrees with the reason")
    if symbol not in SECTOR_PARENT:
        return None, "NOT_IN_S2_UNIVERSE"
    if not research_eligible:
        return None, "NOT_ELIGIBLE"
    if regime.data_asof_utc != snap.data_asof_utc:
        return None, "REGIME_ASOF"
    if not regime.entries_allowed or not regime.s2_enabled or not regime.vwap_intact:
        return None, "REGIME"
    if pattern is None:
        return None, pattern_reason
    if not entry_window_open(snap.data_asof_utc, half_day):
        return None, "ENTRY_WINDOW"
    book.commit(symbol)
    return _candidate(symbol, pattern, snap, regime), "SIGNAL"


def make_intent(
    candidate: Candidate,
    *,
    kind: str,
    probabilities: tuple[Decimal, Decimal, Decimal],
    timeout_bps: Decimal,
    model_version: str,
) -> TradeIntent:
    limit = candidate.limit_price
    stop = candidate.stop_price
    take_profit = candidate.take_profit_price
    p_tp, p_sl, p_to = probabilities
    if p_tp + p_sl + p_to != 1:
        raise RuntimeError("probabilities must sum to 1")
    gross = p_tp * _reward(limit, take_profit) - p_sl * _risk(limit, stop) + p_to * timeout_bps
    cost_bps = round_trip_cost_bps(limit)
    share_cost = COMMISSION_RATE * (limit + take_profit) + HALF_SPREAD * 2
    quantity = opening_qty(limit, stop)
    expected_cost = share_cost * quantity if quantity >= 1 else share_cost
    stamp = candidate.signal_end.strftime("%Y%m%dT%H%M%S")
    return TradeIntent(
        intent_id=f"s2{kind}{candidate.symbol}{stamp}",
        created_at_utc=candidate.signal_end,
        data_asof_utc=candidate.signal_end,
        symbol=candidate.symbol,
        strategy_id="S2",
        model_version=model_version,
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
        prediction_interval=(-_risk(limit, stop), _reward(limit, take_profit)),
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


def _candidate(
    symbol: str,
    pattern: Pattern,
    snap: SymbolSnapshot,
    regime: FeatureSnapshot,
) -> Candidate:
    return Candidate(
        symbol=symbol,
        signal_end=snap.data_asof_utc,
        ny_date=date.fromisoformat(snap.ny_date),
        features_hash=snap.features_hash,
        regime=regime.regime.value,
        features=pattern_features(pattern),
        rel_vol=pattern.rel_vol,
        sector=SECTOR_PARENT[symbol],
        limit_price=pattern.limit_price,
        stop_price=pattern.stop_price,
        take_profit_price=pattern.take_profit_price,
    )


def _reward(limit: Decimal, take_profit: Decimal) -> Decimal:
    return (take_profit - limit) / limit * Decimal(10000)


def _risk(limit: Decimal, stop: Decimal) -> Decimal:
    return (limit - stop) / limit * Decimal(10000)
