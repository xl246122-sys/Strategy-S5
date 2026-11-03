"""Stage 12 three-way audit. A break is returned to the guardian; this module places nothing."""

from decimal import Decimal

from bstocks_v2.reconciler.ledger import (
    FundingBalance,
    LedgerFill,
    OpenOrderView,
    PositionLedgerReconciler,
    Reconciliation,
)


class LiveReconciler:
    def __init__(self, path: str) -> None:
        self.ledger = PositionLedgerReconciler(path)

    def close(self) -> None:
        self.ledger.close()

    def record_buy(
        self,
        client_order_id: str,
        order_id: str,
        symbol: str,
        quantity: Decimal,
        status: str,
    ) -> None:
        self.ledger.record_fill(
            client_order_id,
            order_id,
            LedgerFill(symbol, "BUY", quantity),
            status,
        )

    def record_sell(
        self,
        client_order_id: str,
        order_id: str,
        symbol: str,
        quantity: Decimal,
        *,
        complete: bool,
    ) -> None:
        if quantity <= 0:
            return
        status = "FILLED" if complete else "PARTIALLY_FILLED"
        self.ledger.record_fill(
            client_order_id,
            order_id,
            LedgerFill(symbol, "SELL", quantity),
            status,
        )

    def audit(
        self,
        trades: list[LedgerFill],
        open_orders: list[OpenOrderView],
        funding: list[FundingBalance],
    ) -> Reconciliation:
        return self.ledger.reconcile(trades, open_orders, funding)
