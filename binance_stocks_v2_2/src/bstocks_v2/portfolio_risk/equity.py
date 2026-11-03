"""Account equity. Open-order `tc - FN` is not a fee and is not a loss."""

from dataclasses import dataclass
from decimal import Decimal

from bstocks_v2.portfolio_risk.spec import COMMISSION_RATE, OPEN_BUY_STATUSES


class EquityInvariantError(Exception):
    """The cash identity moved by something other than fills, fees, and deposits."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class OrderMark:
    status: str
    limit_price: Decimal
    quantity: Decimal
    filled_qty: Decimal
    filled_notional: Decimal
    total_cost_raw: Decimal | None
    rest_fee: Decimal | None


@dataclass(frozen=True)
class PositionMark:
    quantity: Decimal
    cost_notional: Decimal
    bid: Decimal


def sell_fee(quantity: Decimal, bid: Decimal, rate: Decimal = COMMISSION_RATE) -> Decimal:
    return rate * quantity * bid


def liquidation_value(marks: list[PositionMark], rate: Decimal = COMMISSION_RATE) -> Decimal:
    total = Decimal(0)
    for mark in marks:
        total += mark.quantity * mark.bid - sell_fee(mark.quantity, mark.bid, rate)
    return total


def locked_principal(orders: list[OrderMark]) -> Decimal:
    total = Decimal(0)
    for order in orders:
        if order.status not in OPEN_BUY_STATUSES:
            continue
        total += order.limit_price * (order.quantity - order.filled_qty)
    return total


def open_order_trap(order: OrderMark) -> Decimal:
    """Principal still sitting inside `tc - FN` on an order that is not fully filled."""

    if order.total_cost_raw is None or order.filled_qty >= order.quantity:
        return Decimal(0)
    return order.total_cost_raw - order.filled_notional


def account_equity(
    basis: Decimal,
    marks: list[PositionMark],
    rate: Decimal = COMMISSION_RATE,
) -> Decimal:
    """`basis` already contains available cash and locked buy principal."""

    return basis + liquidation_value(marks, rate)


def assert_identity(
    *,
    opening: Decimal,
    deposits: Decimal,
    basis: Decimal,
    sells: int,
    orders: list[OrderMark],
    marks: list[PositionMark],
    rate: Decimal = COMMISSION_RATE,
) -> Decimal:
    liquidation = liquidation_value(marks, rate)
    equity = account_equity(basis, marks, rate)
    if equity != basis + liquidation:
        raise EquityInvariantError("equity identity")
    if locked_principal(orders) < 0:
        raise EquityInvariantError("locked principal")
    if sells == 0:
        cost = sum((mark.cost_notional for mark in marks), Decimal(0))
        fees = sum((order.rest_fee or Decimal(0) for order in orders), Decimal(0))
        expected = opening + deposits - cost - fees
        if basis != expected:
            raise EquityInvariantError("tc-FN is not part of equity")
        trap = sum((open_order_trap(order) for order in orders), Decimal(0))
        if trap != 0 and (basis == expected - trap or equity == expected + liquidation - trap):
            raise EquityInvariantError("tc-FN is not part of equity")
    return equity


def explained_open_delta(
    *,
    deposits: Decimal,
    cost: Decimal,
    bid_gross: Decimal,
    buy_fees: Decimal,
    rate: Decimal = COMMISSION_RATE,
) -> Decimal:
    """Change from opening cash when nothing has been sold.

    The haircut is the estimated sell fee on the filled shares. Locked principal
    is still inside equity, so it is not part of this change.
    """

    return deposits + (bid_gross - cost) - sell_fee_on_gross(bid_gross, rate) - buy_fees


def sell_fee_on_gross(gross: Decimal, rate: Decimal = COMMISSION_RATE) -> Decimal:
    return rate * gross
