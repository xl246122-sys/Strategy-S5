"""In-process equity book. Faults are scripted. Nothing here opens a socket."""

import sqlite3
import time
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import cast

from bstocks_v2.contracts.decimals import parse_decimal
from bstocks_v2.guardian.spec import (
    CODE_ALREADY_FILLED,
    CODE_SHARES_FROZEN,
    CODE_TIMEOUT,
    CODE_UNEXPECTED,
    TERMINAL_ORDER,
)
from bstocks_v2.reconciler.ledger import FundingBalance, LedgerFill, OpenOrderView

_TIMEOUTS = frozenset({CODE_UNEXPECTED, CODE_TIMEOUT})


class ExchangeTimeout(Exception):
    """-1006 or -1007. The order may already exist under the same client id."""

    def __init__(self, code: int, message: str) -> None:
        if code not in _TIMEOUTS:
            raise ValueError("timeout code")
        super().__init__(f"{code} {message}")
        self.code = code
        self.message = message


@dataclass(frozen=True)
class PlaceRequest:
    client_order_id: str
    symbol: str
    side: str
    order_type: str
    quantity: Decimal
    price: Decimal | None
    trading_session: str | None
    wallet_type: str
    tokenize: bool
    quote_asset: str
    autofill: bool


@dataclass(frozen=True)
class PlaceResult:
    accepted: bool
    code: int | None
    order_id: str | None
    client_order_id: str


@dataclass(frozen=True)
class PlaceRecord:
    client_order_id: str
    symbol: str
    side: str
    order_type: str
    quantity: Decimal
    price: Decimal | None
    trading_session: str | None
    wallet_type: str
    tokenize: bool
    quote_asset: str
    code: int | None
    accepted: bool
    status_at_place: str | None
    duplicate: bool


@dataclass(frozen=True)
class CancelResult:
    code: int | None
    status: str | None
    immediate_terminal: bool


@dataclass(frozen=True)
class Detail:
    client_order_id: str
    order_id: str
    symbol: str
    side: str
    status: str
    quantity: Decimal
    filled_qty: Decimal
    price: Decimal | None
    fee: Decimal | None
    execution_event: str | None
    shares_frozen: bool


class BookBroker:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def seed_buy(
        self,
        *,
        client_order_id: str,
        symbol: str,
        quantity: Decimal,
        price: Decimal,
    ) -> str:
        order_id = self._next_id()
        self._insert_order(
            client_order_id=client_order_id,
            order_id=order_id,
            symbol=symbol,
            side="BUY",
            order_type="LIMIT",
            quantity=quantity,
            price=price,
            trading_session="RTH",
            wallet_type="CARD",
            tokenize=False,
            quote_asset="USDC",
            status="NEW",
            filled_qty=Decimal(0),
            autofill=False,
        )
        return order_id

    def set_fill(
        self,
        client_order_id: str,
        *,
        status: str,
        filled_qty: Decimal,
        fee: Decimal | None = None,
        execution_event: str | None = None,
    ) -> None:
        self.conn.execute(
            """
            UPDATE broker_order
            SET status = ?, filled_qty = ?, fee = ?, execution_event = ?,
                shares_frozen = 0, hold = 0, visible_ns = NULL,
                pending_status = NULL, pending_filled = NULL
            WHERE client_order_id = ?
            """,
            (
                status,
                format(filled_qty, "f"),
                None if fee is None else format(fee, "f"),
                execution_event,
                client_order_id,
            ),
        )
        self.conn.commit()

    def queue_place_fault(self, code: int, *, landed: bool, symbol: str | None = None) -> None:
        self.conn.execute(
            """
            INSERT INTO broker_place_fault (code, landed, consumed, symbol)
            VALUES (?, ?, 0, ?)
            """,
            (code, 1 if landed else 0, symbol),
        )
        self.conn.commit()

    def arm_cancel(
        self,
        client_order_id: str,
        *,
        code: int | None,
        accept_status: str | None,
        delay_ms: int,
        hold: bool,
        terminal_status: str,
        terminal_filled: Decimal,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO broker_cancel_script (
                client_order_id, code, accept_status, delay_ms, hold,
                terminal_status, terminal_filled
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(client_order_id) DO UPDATE SET
                code=excluded.code,
                accept_status=excluded.accept_status,
                delay_ms=excluded.delay_ms,
                hold=excluded.hold,
                terminal_status=excluded.terminal_status,
                terminal_filled=excluded.terminal_filled
            """,
            (
                client_order_id,
                code,
                accept_status,
                delay_ms,
                1 if hold else 0,
                terminal_status,
                format(terminal_filled, "f"),
            ),
        )
        self.conn.commit()

    def release(self, client_order_id: str) -> None:
        self.conn.execute(
            "UPDATE broker_order SET hold = 0 WHERE client_order_id = ?",
            (client_order_id,),
        )
        self.conn.commit()

    def place(self, req: PlaceRequest) -> PlaceResult:
        self._apply_due()
        if self._symbol_frozen(req.symbol):
            self._log_place(req, code=CODE_SHARES_FROZEN, accepted=False, duplicate=False)
            return PlaceResult(False, CODE_SHARES_FROZEN, None, req.client_order_id)
        existing = self._raw(req.client_order_id)
        if existing is not None:
            self._log_place(req, code=None, accepted=True, duplicate=True)
            return PlaceResult(True, None, str(existing["order_id"]), req.client_order_id)
        fault = self._pop_fault(req.symbol)
        if fault is not None:
            code, landed = fault
            order_id = self._next_id() if landed else None
            if landed and order_id is not None:
                self._insert_order(
                    client_order_id=req.client_order_id,
                    order_id=order_id,
                    symbol=req.symbol,
                    side=req.side,
                    order_type=req.order_type,
                    quantity=req.quantity,
                    price=req.price,
                    trading_session=req.trading_session,
                    wallet_type=req.wallet_type,
                    tokenize=req.tokenize,
                    quote_asset=req.quote_asset,
                    status="NEW",
                    filled_qty=Decimal(0),
                    autofill=False,
                )
            self._log_place(req, code=code, accepted=landed, duplicate=False)
            if code in _TIMEOUTS:
                raise ExchangeTimeout(code, "TIMEOUT")
            return PlaceResult(landed, code, order_id, req.client_order_id)
        order_id = self._next_id()
        self._insert_order(
            client_order_id=req.client_order_id,
            order_id=order_id,
            symbol=req.symbol,
            side=req.side,
            order_type=req.order_type,
            quantity=req.quantity,
            price=req.price,
            trading_session=req.trading_session,
            wallet_type=req.wallet_type,
            tokenize=req.tokenize,
            quote_asset=req.quote_asset,
            status="FILLED" if req.autofill else "NEW",
            filled_qty=req.quantity if req.autofill else Decimal(0),
            autofill=req.autofill,
        )
        self._log_place(req, code=None, accepted=True, duplicate=False)
        return PlaceResult(True, None, order_id, req.client_order_id)

    def cancel(self, client_order_id: str) -> CancelResult:
        self._apply_due()
        row = self._raw(client_order_id)
        if row is None or str(row["status"]) == "FILLED":
            self._log_cancel(client_order_id, CODE_ALREADY_FILLED, None)
            self.conn.commit()
            return CancelResult(CODE_ALREADY_FILLED, None, False)
        script = self.conn.execute(
            """
            SELECT code, accept_status, delay_ms, hold, terminal_status, terminal_filled
            FROM broker_cancel_script WHERE client_order_id = ?
            """,
            (client_order_id,),
        ).fetchone()
        if script is not None:
            self.conn.execute(
                "DELETE FROM broker_cancel_script WHERE client_order_id = ?",
                (client_order_id,),
            )
            code = None if script[0] is None else int(script[0])
            accept = None if script[1] is None else str(script[1])
            delay_ms = int(script[2])
            hold = int(script[3])
            terminal_status = str(script[4])
            terminal_filled = str(script[5])
            if code == CODE_ALREADY_FILLED:
                self._log_cancel(client_order_id, code, accept)
                self.conn.commit()
                return CancelResult(code, accept, False)
            visible_ns = time.time_ns() + delay_ms * 1_000_000
            self.conn.execute(
                """
                UPDATE broker_order
                SET shares_frozen = 1, hold = ?, visible_ns = ?,
                    pending_status = ?, pending_filled = ?
                WHERE client_order_id = ?
                """,
                (hold, visible_ns, terminal_status, terminal_filled, client_order_id),
            )
            self._log_cancel(client_order_id, code, accept)
            self.conn.commit()
            return CancelResult(code, accept, False)
        self.conn.execute(
            """
            UPDATE broker_order
            SET status = 'CANCELED', execution_event = 'ORDER_TERMINAL',
                shares_frozen = 0, hold = 0
            WHERE client_order_id = ?
            """,
            (client_order_id,),
        )
        self._log_cancel(client_order_id, None, None)
        self.conn.commit()
        return CancelResult(None, None, True)

    def detail(self, client_order_id: str) -> Detail | None:
        self._note_detail_read()
        self._apply_due(client_order_id)
        row = self._raw(client_order_id)
        if row is None:
            return None
        return _detail(row)

    def visible_ns(self, client_order_id: str) -> int | None:
        row = self._raw(client_order_id)
        if row is None or row["visible_ns"] is None:
            return None
        return int(row["visible_ns"])

    def place_log(self) -> list[PlaceRecord]:
        rows = self.conn.execute(
            """
            SELECT client_order_id, symbol, side, order_type, quantity, price, trading_session,
                   wallet_type, tokenize, quote_asset, code, accepted, status_at_place, duplicate
            FROM broker_place_log ORDER BY id
            """
        ).fetchall()
        return [_place_record(row) for row in rows]

    def cancel_log(self) -> list[tuple[str, int | None, str | None]]:
        rows = self.conn.execute(
            "SELECT client_order_id, code, status FROM broker_cancel_log ORDER BY id"
        ).fetchall()
        found: list[tuple[str, int | None, str | None]] = []
        for client_id, code, status in rows:
            found.append(
                (
                    str(client_id),
                    None if code is None else int(code),
                    None if status is None else str(status),
                )
            )
        return found

    def cancel_count(self) -> int:
        row = self.conn.execute("SELECT COUNT(*) FROM broker_cancel_log").fetchone()
        if row is None:
            return 0
        return int(row[0])

    def detail_reads(self) -> int:
        row = self.conn.execute(
            "SELECT value FROM broker_meta WHERE key = 'detail_reads'"
        ).fetchone()
        if row is None:
            return 0
        return int(row[0])

    def frozen_reject_count(self) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) FROM broker_place_log WHERE code = ?",
            (CODE_SHARES_FROZEN,),
        ).fetchone()
        if row is None:
            return 0
        return int(row[0])

    def duplicate_count(self) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) FROM broker_place_log WHERE duplicate = 1"
        ).fetchone()
        if row is None:
            return 0
        return int(row[0])

    def set_snapshot(self, trades: list[LedgerFill], funding: list[FundingBalance]) -> None:
        self.conn.execute("DELETE FROM broker_trade")
        self.conn.execute("DELETE FROM broker_funding")
        for trade in trades:
            self.conn.execute(
                "INSERT INTO broker_trade (symbol, side, quantity) VALUES (?, ?, ?)",
                (trade.symbol, trade.side, format(trade.quantity, "f")),
            )
        for balance in funding:
            self.conn.execute(
                "INSERT INTO broker_funding (asset, quantity) VALUES (?, ?)",
                (balance.asset, format(balance.quantity, "f")),
            )
        self.conn.execute(
            """
            INSERT INTO broker_meta (key, value) VALUES ('has_snapshot', '1')
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
            """
        )
        self.conn.commit()

    def has_snapshot(self) -> bool:
        row = self.conn.execute(
            "SELECT value FROM broker_meta WHERE key = 'has_snapshot'"
        ).fetchone()
        return row is not None and str(row[0]) == "1"

    def trades(self) -> list[LedgerFill]:
        rows = self.conn.execute(
            "SELECT symbol, side, quantity FROM broker_trade ORDER BY id"
        ).fetchall()
        return [
            LedgerFill(str(symbol), str(side), parse_decimal(str(qty)))
            for symbol, side, qty in rows
        ]

    def funding(self) -> list[FundingBalance]:
        rows = self.conn.execute(
            "SELECT asset, quantity FROM broker_funding ORDER BY asset"
        ).fetchall()
        return [FundingBalance(str(asset), parse_decimal(str(qty))) for asset, qty in rows]

    def open_orders(self) -> list[OpenOrderView]:
        self._apply_due()
        rows = self.conn.execute(
            "SELECT symbol, side, order_id, status FROM broker_order"
        ).fetchall()
        found: list[OpenOrderView] = []
        for symbol, side, order_id, status in rows:
            if str(status) in TERMINAL_ORDER:
                continue
            found.append(OpenOrderView(str(symbol), str(side), str(order_id), str(status)))
        return found

    def _apply_due(self, client_order_id: str | None = None) -> None:
        if client_order_id is None:
            rows = self.conn.execute(
                """
                SELECT client_order_id, hold, visible_ns, pending_status, pending_filled
                FROM broker_order WHERE pending_status IS NOT NULL
                """
            ).fetchall()
        else:
            rows = self.conn.execute(
                """
                SELECT client_order_id, hold, visible_ns, pending_status, pending_filled
                FROM broker_order WHERE client_order_id = ? AND pending_status IS NOT NULL
                """,
                (client_order_id,),
            ).fetchall()
        now = time.time_ns()
        changed = False
        for cid, hold, visible, pending_status, pending_filled in rows:
            if int(hold) == 1:
                continue
            if visible is not None and now < int(visible):
                continue
            self.conn.execute(
                """
                UPDATE broker_order
                SET status = ?, filled_qty = ?, execution_event = 'ORDER_TERMINAL',
                    shares_frozen = 0, hold = 0, pending_status = NULL, pending_filled = NULL
                WHERE client_order_id = ?
                """,
                (str(pending_status), str(pending_filled), str(cid)),
            )
            changed = True
        if changed:
            self.conn.commit()

    def _symbol_frozen(self, symbol: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM broker_order WHERE symbol = ? AND shares_frozen = 1",
            (symbol,),
        ).fetchone()
        return row is not None

    def _pop_fault(self, symbol: str) -> tuple[int, bool] | None:
        row = self.conn.execute(
            """
            SELECT id, code, landed, symbol FROM broker_place_fault
            WHERE consumed = 0 ORDER BY id LIMIT 1
            """
        ).fetchone()
        if row is None:
            return None
        owner = None if row[3] is None else str(row[3])
        if owner is not None and owner != symbol:
            return None
        self.conn.execute("UPDATE broker_place_fault SET consumed = 1 WHERE id = ?", (int(row[0]),))
        self.conn.commit()
        return int(row[1]), int(row[2]) == 1

    def _insert_order(
        self,
        *,
        client_order_id: str,
        order_id: str,
        symbol: str,
        side: str,
        order_type: str,
        quantity: Decimal,
        price: Decimal | None,
        trading_session: str | None,
        wallet_type: str,
        tokenize: bool,
        quote_asset: str,
        status: str,
        filled_qty: Decimal,
        autofill: bool,
    ) -> None:
        event = "ORDER_TERMINAL" if autofill else None
        self.conn.execute(
            """
            INSERT INTO broker_order (
                client_order_id, order_id, symbol, side, order_type, quantity, price,
                trading_session, wallet_type, tokenize, quote_asset, status, filled_qty, fee,
                execution_event, shares_frozen, hold, visible_ns, pending_status, pending_filled
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, 0, 0, NULL, NULL, NULL)
            """,
            (
                client_order_id,
                order_id,
                symbol,
                side,
                order_type,
                format(quantity, "f"),
                None if price is None else format(price, "f"),
                trading_session,
                wallet_type,
                0 if tokenize is False else 1,
                quote_asset,
                status,
                format(filled_qty, "f"),
                event,
            ),
        )
        self.conn.commit()

    def _log_place(
        self,
        req: PlaceRequest,
        *,
        code: int | None,
        accepted: bool,
        duplicate: bool,
    ) -> None:
        guard = self.conn.execute(
            "SELECT status FROM order_guard WHERE client_order_id = ?",
            (req.client_order_id,),
        ).fetchone()
        seen = None if guard is None else str(guard[0])
        self.conn.execute(
            """
            INSERT INTO broker_place_log (
                client_order_id, symbol, side, order_type, quantity, price, trading_session,
                wallet_type, tokenize, quote_asset, code, accepted, status_at_place, duplicate
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                req.client_order_id,
                req.symbol,
                req.side,
                req.order_type,
                format(req.quantity, "f"),
                None if req.price is None else format(req.price, "f"),
                req.trading_session,
                req.wallet_type,
                0 if req.tokenize is False else 1,
                req.quote_asset,
                code,
                1 if accepted else 0,
                seen,
                1 if duplicate else 0,
            ),
        )
        self.conn.commit()

    def _log_cancel(self, client_order_id: str, code: int | None, status: str | None) -> None:
        self.conn.execute(
            "INSERT INTO broker_cancel_log (client_order_id, code, status) VALUES (?, ?, ?)",
            (client_order_id, code, status),
        )

    def _note_detail_read(self) -> None:
        current = self.detail_reads()
        self.conn.execute(
            """
            INSERT INTO broker_meta (key, value) VALUES ('detail_reads', ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
            """,
            (str(current + 1),),
        )
        self.conn.commit()

    def _next_id(self) -> str:
        row = self.conn.execute(
            "SELECT value FROM broker_meta WHERE key = 'next_order'"
        ).fetchone()
        current = 1 if row is None else int(row[0]) + 1
        self.conn.execute(
            """
            INSERT INTO broker_meta (key, value) VALUES ('next_order', ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
            """,
            (str(current),),
        )
        self.conn.commit()
        return f"ord-{current}"

    def _raw(self, client_order_id: str) -> sqlite3.Row | None:
        self.conn.row_factory = sqlite3.Row
        try:
            found = self.conn.execute(
                "SELECT * FROM broker_order WHERE client_order_id = ?",
                (client_order_id,),
            ).fetchone()
        finally:
            self.conn.row_factory = None
        if found is None:
            return None
        return cast(sqlite3.Row, found)


def _detail(row: sqlite3.Row) -> Detail:
    price = row["price"]
    fee = row["fee"]
    event = row["execution_event"]
    return Detail(
        client_order_id=str(row["client_order_id"]),
        order_id=str(row["order_id"]),
        symbol=str(row["symbol"]),
        side=str(row["side"]),
        status=str(row["status"]),
        quantity=parse_decimal(str(row["quantity"])),
        filled_qty=parse_decimal(str(row["filled_qty"])),
        price=None if price is None else parse_decimal(str(price)),
        fee=None if fee is None else parse_decimal(str(fee)),
        execution_event=None if event is None else str(event),
        shares_frozen=int(row["shares_frozen"]) == 1,
    )


def _place_record(row: Sequence[object]) -> PlaceRecord:
    code = row[10]
    return PlaceRecord(
        client_order_id=str(row[0]),
        symbol=str(row[1]),
        side=str(row[2]),
        order_type=str(row[3]),
        quantity=parse_decimal(str(row[4])),
        price=None if row[5] is None else parse_decimal(str(row[5])),
        trading_session=None if row[6] is None else str(row[6]),
        wallet_type=str(row[7]),
        tokenize=int(str(row[8])) == 1,
        quote_asset=str(row[9]),
        code=None if code is None else int(str(code)),
        accepted=int(str(row[11])) == 1,
        status_at_place=None if row[12] is None else str(row[12]),
        duplicate=int(str(row[13])) == 1,
    )
