"""Decision-time ORB candidate. The probability triple is a placeholder and does not gate."""

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

from bstocks_v2.constitution.guard import entry_window_open
from bstocks_v2.contracts.models import Direction, FillHorizon, TradeIntent
from bstocks_v2.feature_store.bars import SymbolSnapshot
from bstocks_v2.regime.classifier import FeatureSnapshot
from bstocks_v2.research_backtest.models import MAX_POSITION_FRACTION
from bstocks_v2.strategies.s1.spec import SECTOR_PARENT
from bstocks_v2.strategies.s3.orb import RangeState
from bstocks_v2.strategies.s3.spec import (
    ACCOUNT_EQUITY,
    CEILING_ATR,
    COMMISSION_RATE,
    HALF_SPREAD,
    HOLD_SECONDS,
    POSITION_FRACTION,
    RISK_BUDGET,
    RVOL_BASELINE_DAYS,
    RVOL_MIN,
    STOP_ATR,
    TARGET_MULTIPLE,
    TTL_SECONDS,
)

_CENT = Decimal("0.01")
_P_TP = Decimal("0.333333334")
_P_SL = Decimal("0.333333333")
_P_TO = Decimal("0.333333333")
UNCALIBRATED = (_P_TP, _P_SL, _P_TO)


@dataclass(frozen=True)
class Candidate:
    symbol: str
    horizon: int
    signal_end: datetime
    bar_start: datetime
    ny_date: date
    features_hash: str
    regime: str
    orh: Decimal
    orl: Decimal
    atr: Decimal
    rvol_median: Decimal
    limit_price: Decimal
    ceiling: Decimal
    stop_price: Decimal
    take_profit_price: Decimal


def decide(
    *,
    symbol: str,
    snap: SymbolSnapshot,
    regime: FeatureSnapshot,
    research_eligible: bool,
    half_day: bool,
    opening: RangeState,
) -> tuple[Candidate | None, str]:
    if symbol not in SECTOR_PARENT:
        return None, "NOT_IN_S3_UNIVERSE"
    if not research_eligible:
        return None, "NOT_ELIGIBLE"
    if regime.data_asof_utc != snap.data_asof_utc:
        return None, "REGIME_ASOF"
    if not regime.s3_enabled or not regime.entries_allowed:
        return None, "S3_DISABLED"
    if not snap.vwap_intact or snap.vwap is None:
        return None, "VWAP_GAP"
    if not opening.first_break or opening.orh is None or opening.orl is None:
        return None, opening.reason
    if not entry_window_open(snap.data_asof_utc, half_day):
        return None, "ENTRY_WINDOW"
    if snap.atr14 is None or snap.atr14 <= 0:
        return None, "NO_ATR"
    if snap.baseline_days < RVOL_BASELINE_DAYS or snap.rvol_median is None:
        return None, "NO_RVOL"
    if snap.rvol_median < RVOL_MIN:
        return None, "RVOL_LOW"
    if snap.close <= snap.vwap:
        return None, "BELOW_VWAP"
    built = _prices(opening.orh, opening.orl, snap.atr14)
    if built is None:
        return None, "GEOMETRY"
    limit, ceiling, stop, take_profit = built
    return (
        Candidate(
            symbol=symbol,
            horizon=opening.horizon_minutes,
            signal_end=snap.data_asof_utc,
            bar_start=snap.bar_start,
            ny_date=date.fromisoformat(snap.ny_date),
            features_hash=snap.features_hash,
            regime=regime.regime.value,
            orh=opening.orh,
            orl=opening.orl,
            atr=snap.atr14,
            rvol_median=snap.rvol_median,
            limit_price=limit,
            ceiling=ceiling,
            stop_price=stop,
            take_profit_price=take_profit,
        ),
        "SIGNAL",
    )


def make_intent(candidate: Candidate, *, arm: str) -> TradeIntent:
    """`arm` is `baseline` or `sequential`. Equal probabilities do not decide entry."""

    if arm not in {"baseline", "sequential"}:
        raise ValueError("arm")
    if sum(UNCALIBRATED) != 1:
        raise RuntimeError("placeholder probabilities drifted")
    limit = candidate.limit_price
    stop = candidate.stop_price
    take_profit = candidate.take_profit_price
    p_tp, p_sl, p_to = UNCALIBRATED
    reward = (take_profit - limit) / limit * Decimal(10000)
    risk = (limit - stop) / limit * Decimal(10000)
    cost = (COMMISSION_RATE * 2 + (HALF_SPREAD * 2) / limit) * Decimal(10000)
    gross = p_tp * reward - p_sl * risk
    stamp = candidate.signal_end.strftime("%Y%m%dT%H%M%SZ")
    tag = "b" if arm == "baseline" else "q"
    return TradeIntent(
        intent_id=f"s3{candidate.horizon}{tag}{candidate.symbol}{stamp}",
        created_at_utc=candidate.signal_end,
        data_asof_utc=candidate.signal_end,
        symbol=candidate.symbol,
        strategy_id="S3",
        model_version=f"s3-v1-orb-rvol-{arm}",
        regime=candidate.regime,
        features_hash=candidate.features_hash,
        vwap_intact=True,
        signal_valid_until=candidate.signal_end + timedelta(seconds=TTL_SECONDS),
        direction=Direction.LONG,
        p_win_calibrated=p_tp,
        p_sl_calibrated=p_sl,
        p_timeout_calibrated=p_to,
        expected_return_bps=gross,
        expected_net_bps=gross - cost,
        prediction_interval=(-risk, reward),
        candidate_limit_prices=[limit],
        p_fill_by_price_horizon=[
            FillHorizon(price=limit, horizon_seconds=TTL_SECONDS, p_fill=Decimal(0))
        ],
        entry_price_ceiling=candidate.ceiling,
        stop_price=stop,
        take_profit_price=take_profit,
        max_holding_seconds=HOLD_SECONDS,
        risk_budget_usdc=RISK_BUDGET,
        min_viable_notional_usdc=Decimal(120),
        expected_cost_usdc=cost,
        requires_fractional=False,
        whole_share_fallback_qty=Decimal(0),
        rejection_reasons=[],
        data_feed="sip",
        data_freshness_ms=0,
    )


def _prices(
    orh: Decimal,
    orl: Decimal,
    atr: Decimal,
) -> tuple[Decimal, Decimal, Decimal, Decimal] | None:
    if POSITION_FRACTION != MAX_POSITION_FRACTION:
        raise RuntimeError("position cap drifted")
    limit = _cents(orh)
    ceiling = _cents(orh + CEILING_ATR * atr)
    stop = _cents(min(orl, limit - STOP_ATR * atr))
    if not stop < limit:
        return None
    risk = limit - stop
    take_profit = _cents(limit + TARGET_MULTIPLE * risk)
    if take_profit <= limit:
        return None
    if ceiling >= take_profit:
        ceiling = take_profit - _CENT
    if ceiling < limit or not stop < ceiling < take_profit:
        return None
    if ACCOUNT_EQUITY <= 0:
        return None
    return limit, ceiling, stop, take_profit


def _cents(value: Decimal) -> Decimal:
    return value.quantize(_CENT, rounding=ROUND_HALF_UP)
