"""Map Binance Stocks `{listenKey}@orderReport` into CanonicalOrder.

Partial fills must never book `tc - FN` as realized commission. That difference
includes the principal still locked on the unfilled quantity.
"""

from decimal import Decimal
from typing import TypeVar

from bstocks_v2.contracts.decimals import parse_decimal
from bstocks_v2.contracts.errors import FeeTrapError, NormalizerError
from bstocks_v2.contracts.models import (
    CanonicalOrder,
    ExecutionEvent,
    FeeSource,
    OrderStatus,
    OrderType,
    Side,
    TradingSession,
)
from bstocks_v2.normalizer.binder import OrderIdBinder

_STATUS = {
    "new": OrderStatus.NEW,
    "accepted": OrderStatus.ACCEPTED,
    "partially_filled": OrderStatus.PARTIALLY_FILLED,
    "filled": OrderStatus.FILLED,
    "canceled": OrderStatus.CANCELED,
    "cancelled": OrderStatus.CANCELED,
    "expired": OrderStatus.EXPIRED,
    "rejected": OrderStatus.REJECTED,
}
_SIDE = {"buy": Side.BUY, "sell": Side.SELL}
_TYPE = {"limit": OrderType.LIMIT, "market": OrderType.MARKET}
_SESSION_FROM_WIRE = {
    "regular": TradingSession.RTH,
    "extended": TradingSession.EXTENDED,
    "extended hours": TradingSession.EXTENDED,
    "pre-market": TradingSession.EXTENDED,
    "after hours": TradingSession.EXTENDED,
    "post-market": TradingSession.EXTENDED,
    "24 hours trading": TradingSession.H24,
    "24h": TradingSession.H24,
}
_SESSION_TO_WIRE = {
    TradingSession.RTH: "Regular",
    TradingSession.EXTENDED: "Extended Hours",
    TradingSession.H24: "24 Hours Trading",
}


def session_from_wire(label: str) -> TradingSession:
    key = label.strip().lower()
    try:
        return _SESSION_FROM_WIRE[key]
    except KeyError as exc:
        raise NormalizerError(f"unknown session label: {label}") from exc


def session_to_wire(session: TradingSession) -> str:
    return _SESSION_TO_WIRE[session]


class EquityPayloadNormalizer:
    def normalize_order_report(
        self,
        payload: dict[str, object],
        binder: OrderIdBinder | None = None,
        estimated_fee_rate: Decimal | None = None,
        rest_fee: Decimal | None = None,
    ) -> CanonicalOrder:
        if "clientOrderId" in payload:
            raise NormalizerError("orderReport does not carry clientOrderId; bind it locally")
        order_id = _required_str(payload, "i")
        symbol = _required_str(payload, "b").removeprefix("EQ_")
        if not symbol or symbol.startswith("EQ_"):
            raise NormalizerError("symbol missing after EQ_ removal")
        side = _map(_SIDE, _required_str(payload, "S"), "side")
        order_type = _map(_TYPE, _required_str(payload, "o"), "order type")
        status = _map(_STATUS, _required_str(payload, "s"), "status")
        event = _event(payload.get("x"))
        session = session_from_wire(_required_str(payload, "n")) if "n" in payload else None
        quantity = _decimal_field(payload, "Q")
        filled_qty = _decimal_field(payload, "fq")
        filled_notional = _decimal_field(payload, "FN")
        limit_price = _optional_decimal(payload, "p")
        total_cost = _optional_decimal(payload, "tc")
        if quantity <= 0 or filled_qty < 0 or filled_qty > quantity:
            raise NormalizerError("filled quantity is outside the order quantity")
        client_order_id = binder.client_id_for(order_id) if binder is not None else None
        realized, reserved, source, final = _commission(
            status=status,
            quantity=quantity,
            filled_qty=filled_qty,
            limit_price=limit_price,
            filled_notional=filled_notional,
            total_cost=total_cost,
            estimated_fee_rate=estimated_fee_rate,
            rest_fee=rest_fee,
        )
        fully_filled = status is OrderStatus.FILLED and filled_qty == quantity
        if not fully_filled and total_cost is not None and realized == total_cost - filled_notional:
            raise FeeTrapError("refusing tc-FN as realized commission before the order is filled")
        return CanonicalOrder(
            order_id=order_id,
            client_order_id=client_order_id,
            symbol=symbol,
            wire_quote=_required_str(payload, "q"),
            side=side,
            order_type=order_type,
            status=status,
            execution_event=event,
            is_terminal=event is ExecutionEvent.ORDER_TERMINAL,
            session=session,
            quantity=quantity,
            limit_price=limit_price,
            filled_qty=filled_qty,
            filled_notional=filled_notional,
            total_cost_raw=total_cost,
            realized_commission=realized,
            provisional_reserved_fee=reserved,
            fee_is_final=final,
            fee_source=source,
        )


def equity_after_buy_report(
    cash_usdc: Decimal,
    hwm_usdc: Decimal,
    realized_commission: Decimal,
) -> tuple[Decimal, bool]:
    """Subtract only realized commission. Locked principal inside `tc` stays in equity."""

    equity = cash_usdc - realized_commission
    halt = equity <= hwm_usdc * Decimal("0.92")
    return equity, halt


def _commission(
    *,
    status: OrderStatus,
    quantity: Decimal,
    filled_qty: Decimal,
    limit_price: Decimal | None,
    filled_notional: Decimal,
    total_cost: Decimal | None,
    estimated_fee_rate: Decimal | None,
    rest_fee: Decimal | None,
) -> tuple[Decimal, Decimal | None, FeeSource, bool]:
    fully_filled = status is OrderStatus.FILLED and filled_qty == quantity
    trap = (total_cost - filled_notional) if total_cost is not None else None
    reserved = None
    if total_cost is not None and limit_price is not None:
        reserved = total_cost - (quantity * limit_price)
    if rest_fee is not None:
        realized, source, final = rest_fee, FeeSource.REST_DETAIL, True
    elif fully_filled and trap is not None:
        realized, source, final = trap, FeeSource.WS_TERMINAL_INITIAL, False
    elif estimated_fee_rate is not None:
        realized = estimated_fee_rate * filled_notional
        source, final = FeeSource.PROVISIONAL_RATE, False
    else:
        realized, source, final = Decimal("0"), FeeSource.PROVISIONAL_WITHHELD, False
    if not fully_filled and trap is not None and realized == trap:
        raise FeeTrapError("refusing tc-FN as realized commission before the order is filled")
    return realized, reserved, source, final


def _event(raw: object) -> ExecutionEvent | None:
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise NormalizerError("execution event must be a string")
    try:
        return ExecutionEvent(raw)
    except ValueError as exc:
        raise NormalizerError(f"unknown execution event: {raw}") from exc


def _required_str(payload: dict[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise NormalizerError(f"missing string field {key}")
    return value


_T = TypeVar("_T")


def _decimal_field(payload: dict[str, object], key: str) -> Decimal:
    if key not in payload:
        raise NormalizerError(f"missing numeric field {key}")
    return _parse_number(payload[key], key)


def _optional_decimal(payload: dict[str, object], key: str) -> Decimal | None:
    if key not in payload or payload[key] is None:
        return None
    return _parse_number(payload[key], key)


def _parse_number(value: object, key: str) -> Decimal:
    try:
        return parse_decimal(value)
    except (TypeError, ValueError, ArithmeticError) as exc:
        raise NormalizerError(f"bad numeric field {key}") from exc


def _map(table: dict[str, _T], raw: str, label: str) -> _T:
    try:
        return table[raw.strip().lower()]
    except KeyError as exc:
        raise NormalizerError(f"unknown {label}: {raw}") from exc
