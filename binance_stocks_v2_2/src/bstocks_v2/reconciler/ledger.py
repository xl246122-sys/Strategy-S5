"""Three-way position check: local ledger, trade history, and the CARD funding snapshot."""

import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal

from bstocks_v2.contracts.decimals import parse_decimal


@dataclass(frozen=True)
class LedgerFill:
    symbol: str
    side: str
    quantity: Decimal


@dataclass(frozen=True)
class OpenOrderView:
    symbol: str
    side: str
    order_id: str
    status: str


@dataclass(frozen=True)
class FundingBalance:
    asset: str
    quantity: Decimal


@dataclass
class Reconciliation:
    matched: bool
    local_qty: dict[str, Decimal]
    trade_qty: dict[str, Decimal]
    wallet_qty: dict[str, Decimal]
    usdc: Decimal | None
    quantity_breaks: list[str] = field(default_factory=list)
    open_order_ids: list[str] = field(default_factory=list)
    halt_new_entries: bool = False


class PositionLedgerReconciler:
    def __init__(self, path: str) -> None:
        self._db = sqlite3.connect(path)
        self._db.execute("PRAGMA busy_timeout=5000")
        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS order_ledger (
                client_order_id TEXT PRIMARY KEY,
                order_id TEXT,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                status TEXT NOT NULL,
                filled_qty TEXT NOT NULL
            )
            """
        )
        self._db.commit()

    def close(self) -> None:
        self._db.close()

    def record_fill(
        self,
        client_order_id: str,
        order_id: str,
        fill: LedgerFill,
        status: str,
    ) -> None:
        self._db.execute(
            """
            INSERT INTO order_ledger (client_order_id, order_id, symbol, side, status, filled_qty)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(client_order_id) DO UPDATE SET
                order_id=excluded.order_id,
                status=excluded.status,
                filled_qty=excluded.filled_qty
            """,
            (client_order_id, order_id, fill.symbol, fill.side, status, str(fill.quantity)),
        )
        self._db.commit()

    def local_quantities(self) -> dict[str, Decimal]:
        rows = self._db.execute(
            "SELECT symbol, side, filled_qty, status FROM order_ledger"
        ).fetchall()
        return _net(
            [
                LedgerFill(str(symbol), str(side), parse_decimal(qty))
                for symbol, side, qty, status in rows
                if str(status) in {"FILLED", "PARTIALLY_FILLED"}
            ]
        )

    def reconcile(
        self,
        trades: list[LedgerFill],
        open_orders: list[OpenOrderView],
        funding: list[FundingBalance],
    ) -> Reconciliation:
        local = self.local_quantities()
        from_trades = _net(trades)
        wallet, usdc = _wallet(funding)
        symbols = set(local) | set(from_trades) | set(wallet)
        breaks: list[str] = []
        for symbol in sorted(symbols):
            local_qty = local.get(symbol, Decimal(0))
            trade_qty = from_trades.get(symbol, Decimal(0))
            wallet_qty = wallet.get(symbol, Decimal(0))
            views = (local_qty, trade_qty, wallet_qty)
            if len(set(views)) != 1:
                breaks.append(symbol)
        matched = not breaks
        return Reconciliation(
            matched=matched,
            local_qty=local,
            trade_qty=from_trades,
            wallet_qty=wallet,
            usdc=usdc,
            quantity_breaks=breaks,
            open_order_ids=[order.order_id for order in open_orders],
            halt_new_entries=not matched,
        )


def _net(fills: list[LedgerFill]) -> dict[str, Decimal]:
    totals: dict[str, Decimal] = defaultdict(lambda: Decimal(0))
    for fill in fills:
        sign = Decimal(1) if fill.side.upper() == "BUY" else Decimal(-1)
        totals[fill.symbol.removeprefix("EQ_")] += sign * fill.quantity
    return {symbol: qty for symbol, qty in totals.items() if qty != 0}


def _wallet(balances: list[FundingBalance]) -> tuple[dict[str, Decimal], Decimal | None]:
    positions: dict[str, Decimal] = {}
    usdc: Decimal | None = None
    for balance in balances:
        asset = balance.asset.upper()
        if asset == "USDC":
            usdc = balance.quantity
        elif asset.startswith("EQ_"):
            positions[asset.removeprefix("EQ_")] = balance.quantity
    return positions, usdc
