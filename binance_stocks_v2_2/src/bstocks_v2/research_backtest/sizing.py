"""Share count for one accepted intent. Fractional limit orders fall back to whole shares."""

from dataclasses import dataclass
from decimal import Decimal

from pydantic import ValidationError

from bstocks_v2.contracts.errors import OrderRejected
from bstocks_v2.contracts.models import (
    MIN_VIABLE_NOTIONAL,
    OrderDraft,
    OrderType,
    Side,
    TradingSession,
)
from bstocks_v2.research_backtest.fills import floor_step
from bstocks_v2.research_backtest.models import SymbolFilter


@dataclass(frozen=True)
class SizedOrder:
    quantity: Decimal
    adjustments: tuple[str, ...]
    gross_take_profit: Decimal
    round_trip_friction: Decimal
    draft: OrderDraft


@dataclass(frozen=True)
class SizingRejection:
    reasons: tuple[str, ...]


def plan_buy(
    *,
    symbol: str,
    client_order_id: str,
    limit: Decimal,
    stop: Decimal,
    take_profit: Decimal,
    risk_budget: Decimal,
    equity: Decimal,
    max_position_fraction: Decimal,
    whole_share_fallback: Decimal,
    filters: SymbolFilter,
    commission_rate: Decimal,
    half_spread: Decimal,
) -> SizedOrder | SizingRejection:
    if not stop < limit < take_profit:
        return SizingRejection(("GEOMETRY",))
    distance = limit - stop
    room = equity * max_position_fraction
    raw = min(risk_budget / distance, room / limit)
    adjustments: list[str] = []
    if filters.fractional_limit_allowed:
        qty = floor_step(raw, filters.step_size)
    else:
        qty = floor_step(raw, Decimal(1))
        if qty != raw:
            adjustments.append("486442")
        if whole_share_fallback > 0:
            fallback = floor_step(whole_share_fallback, Decimal(1))
            if Decimal(0) < fallback < qty:
                qty = fallback
        if qty < 1:
            return SizingRejection(("WHOLE_SHARE_EXCEEDS_RISK_OR_CAP",))
    qty = floor_step(qty, filters.step_size)
    if not filters.fractional_limit_allowed and qty < 1:
        return SizingRejection(("WHOLE_SHARE_EXCEEDS_RISK_OR_CAP",))
    if qty < filters.min_qty:
        return SizingRejection(("MIN_QTY",))
    notional = qty * limit
    if notional < max(filters.min_notional, MIN_VIABLE_NOTIONAL):
        return SizingRejection(("NOTIONAL_TOO_SMALL",))
    gross = (take_profit - limit) * qty
    friction = _friction(limit, take_profit, qty, commission_rate, half_spread)
    try:
        draft = OrderDraft(
            symbol=symbol,
            side=Side.BUY,
            order_type=OrderType.LIMIT,
            client_order_id=client_order_id,
            quantity=qty,
            price=limit,
            trading_session=TradingSession.RTH,
            quote_asset="USDC",
            wallet_type="CARD",
            tokenize=False,
            is_opening=True,
            confirmed_long_qty=Decimal(0),
            gross_take_profit_usdc=gross,
            round_trip_friction_usdc=friction,
        )
    except OrderRejected as exc:
        return SizingRejection((exc.reason,))
    except ValidationError:
        return SizingRejection(("PRICE_PRECISION",))
    return SizedOrder(qty, tuple(adjustments), gross, friction, draft)


def _friction(
    limit: Decimal,
    take_profit: Decimal,
    qty: Decimal,
    commission_rate: Decimal,
    half_spread: Decimal,
) -> Decimal:
    entry_fee = commission_rate * limit * qty
    exit_fee = commission_rate * take_profit * qty
    return entry_fee + exit_fee + half_spread * qty * 2
