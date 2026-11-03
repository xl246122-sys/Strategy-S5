"""In-memory gateway. It has no HTTP client and refuses a live place path."""

from dataclasses import dataclass
from decimal import Decimal

from bstocks_v2.constitution.guard import ConstitutionGuard, live_trade_enabled
from bstocks_v2.contracts.errors import LiveTradingDisabledError, OrderRejected
from bstocks_v2.contracts.models import OrderType, Side
from bstocks_v2.research_backtest.sizing import SizedOrder


@dataclass
class MockOrder:
    client_order_id: str
    symbol: str
    price: Decimal
    quantity: Decimal
    status: str


class MockExecutionGateway:
    def __init__(self) -> None:
        if live_trade_enabled():
            raise LiveTradingDisabledError("mock gateway")
        self._guard = ConstitutionGuard()
        self.orders: dict[str, MockOrder] = {}

    def accept(self, sized: SizedOrder) -> MockOrder:
        draft = sized.draft
        if draft.side is not Side.BUY or draft.order_type is not OrderType.LIMIT:
            raise OrderRejected("FIELD_MATRIX")
        if draft.wallet_type != "CARD" or draft.tokenize is not False:
            raise OrderRejected("WALLET_TYPE")
        if draft.price is None or draft.quantity is None or draft.client_order_id is None:
            raise OrderRejected("FIELD_MATRIX")
        if draft.client_order_id in self.orders:
            raise OrderRejected("DUPLICATE_CLIENT_ID")
        order = MockOrder(
            client_order_id=draft.client_order_id,
            symbol=draft.symbol,
            price=draft.price,
            quantity=draft.quantity,
            status="NEW",
        )
        self.orders[order.client_order_id] = order
        return order

    def live_place(self, path: str) -> None:
        """The research chain calls this only to prove the live route is closed."""

        self._guard.check_endpoint("POST", path)
