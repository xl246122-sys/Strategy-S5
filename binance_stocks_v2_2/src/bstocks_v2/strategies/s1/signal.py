"""Long-only S1 intent. A failed gate returns no intent and does not reach an order gateway."""

from datetime import timedelta
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal

from bstocks_v2.contracts.models import Direction, FillHorizon, TradeIntent
from bstocks_v2.feature_store.bars import SymbolSnapshot
from bstocks_v2.regime.classifier import FeatureSnapshot
from bstocks_v2.research_backtest.fills import floor_step
from bstocks_v2.strategies.s1.filters import Fit
from bstocks_v2.strategies.s1.spec import (
    COMMISSION_RATE,
    HALF_SPREAD,
    HOLD_SECONDS,
    MAX_POSITION,
    MIN_UPDATES,
    RISK_BUDGET,
    SECTOR_PARENT,
    STOP_ATR,
    TARGET_MULTIPLE,
    TTL_SECONDS,
    VWAP_EXTENSION_ATR,
    Z_MIN,
)

_CENT = Decimal("0.01")
_P_WIN = Decimal("0.333333334")
_P_SL = Decimal("0.333333333")
_P_TIMEOUT = Decimal("0.333333333")


def decide(
    *,
    model: str,
    symbol: str,
    fit: Fit,
    sector_strength: Decimal | None,
    stock: SymbolSnapshot,
    regime: FeatureSnapshot,
    research_eligible: bool,
    min_updates: int = MIN_UPDATES,
) -> tuple[TradeIntent | None, str]:
    if symbol not in SECTOR_PARENT:
        return None, "NOT_IN_S1_UNIVERSE"
    if not research_eligible:
        return None, "NOT_ELIGIBLE"
    if regime.data_asof_utc != stock.data_asof_utc:
        return None, "REGIME_ASOF"
    if not regime.entries_allowed or not regime.s1_enabled or not regime.vwap_intact:
        return None, "REGIME"
    if not stock.vwap_intact or stock.vwap is None:
        return None, "VWAP"
    atr = stock.atr14
    if atr is None or atr <= 0:
        return None, "ATR"
    if stock.close <= stock.vwap:
        return None, "BELOW_VWAP"
    if stock.close - stock.vwap > atr * VWAP_EXTENSION_ATR:
        return None, "EXTENDED"
    if fit.updates < min_updates:
        return None, "WARMUP"
    if not fit.solved or fit.variance_alpha is None or fit.variance_alpha <= 0:
        return None, "VARIANCE"
    if fit.alpha <= 0:
        return None, "ALPHA"
    z_alpha = fit.z_after(min_updates)
    if z_alpha is None or z_alpha <= Z_MIN:
        return None, "Z"
    if sector_strength is None:
        return None, "STRENGTH_WARMUP"
    if sector_strength <= 0:
        return None, "SECTOR"
    intent = _intent(model, symbol, fit, stock, regime, atr)
    if intent is None:
        return None, "GEOMETRY"
    return intent, "SIGNAL"


def _intent(
    model: str,
    symbol: str,
    fit: Fit,
    stock: SymbolSnapshot,
    regime: FeatureSnapshot,
    atr: Decimal,
) -> TradeIntent | None:
    limit = stock.close.quantize(_CENT, rounding=ROUND_HALF_UP)
    distance = atr * STOP_ATR
    stop = (limit - distance).quantize(_CENT, rounding=ROUND_DOWN)
    take_profit = (limit + distance * TARGET_MULTIPLE).quantize(_CENT, rounding=ROUND_DOWN)
    if not stop < limit < take_profit:
        return None
    span = limit - stop
    raw_qty = min(RISK_BUDGET / span, MAX_POSITION / limit)
    quantity = floor_step(raw_qty, Decimal(1))
    tp_bps = (take_profit - limit) / limit * Decimal(10000)
    sl_bps = (limit - stop) / limit * Decimal(10000)
    cost_per_share = COMMISSION_RATE * (limit + take_profit) + HALF_SPREAD * 2
    cost_bps = cost_per_share / limit * Decimal(10000)
    expected_return = _P_WIN * tp_bps - _P_SL * sl_bps
    expected_net = expected_return - cost_bps
    if quantity >= 1:
        expected_cost = cost_per_share * quantity
    else:
        expected_cost = cost_per_share
    stamp = stock.data_asof_utc.strftime("%Y%m%dT%H%M%S")
    return TradeIntent(
        intent_id=f"s1{model}{symbol}{stamp}",
        created_at_utc=stock.data_asof_utc,
        data_asof_utc=stock.data_asof_utc,
        symbol=symbol,
        strategy_id="S1",
        model_version=f"s1-v1-{model}-uncalibrated",
        regime=regime.regime.value,
        features_hash=stock.features_hash,
        vwap_intact=stock.vwap_intact,
        signal_valid_until=stock.data_asof_utc + timedelta(seconds=TTL_SECONDS),
        direction=Direction.LONG,
        p_win_calibrated=_P_WIN,
        p_sl_calibrated=_P_SL,
        p_timeout_calibrated=_P_TIMEOUT,
        expected_return_bps=expected_return,
        expected_net_bps=expected_net,
        prediction_interval=(-sl_bps, tp_bps),
        candidate_limit_prices=[limit],
        p_fill_by_price_horizon=[
            FillHorizon(price=limit, horizon_seconds=TTL_SECONDS, p_fill=Decimal(0))
        ],
        entry_price_ceiling=limit,
        stop_price=stop,
        take_profit_price=take_profit,
        max_holding_seconds=HOLD_SECONDS,
        risk_budget_usdc=RISK_BUDGET,
        min_viable_notional_usdc=Decimal("120"),
        expected_cost_usdc=expected_cost,
        requires_fractional=False,
        whole_share_fallback_qty=Decimal(0),
        rejection_reasons=[],
        data_feed="sip",
        data_freshness_ms=0,
    )

