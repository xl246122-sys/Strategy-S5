"""WAL the client id before a mock place. A timeout reads detail and keeps that same id.

The real POST /order/place door stays shut. This gateway only talks to an injected broker.
"""

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Protocol

from bstocks_v2.constitution.guard import CLIENT_ORDER_ID, ConstitutionGuard
from bstocks_v2.contracts.decimals import parse_decimal
from bstocks_v2.contracts.models import OrderType, Side, TradingSession
from bstocks_v2.limit_optimizer.stage11 import PreparedOrder

_ACTIVE = frozenset(
    {
        "WAL_PREPARED",
        "SUBMITTING",
        "UNKNOWN_TIMEOUT",
        "DETAIL_ABSENT",
        "DETAIL_CONFIRMED",
        "ACKNOWLEDGED",
    }
)
_PLACE_PATH = "/sapi/v1/equity/order/place"
_DETAIL_PATH = "/sapi/v1/equity/order/detail"
_TIMEOUTS = frozenset({-1006, -1007})


class ExchangeTimeout(Exception):
    """-1006 or -1007. The place may have landed. The client id must not change."""

    def __init__(self, code: int, message: str) -> None:
        if code not in _TIMEOUTS:
            raise ValueError("timeout code")
        super().__init__(f"{code} {message}")
        self.code = code
        self.message = message


class ResendForbidden(Exception):
    def __init__(self, reason: str, client_order_id: str) -> None:
        super().__init__(reason)
        self.reason = reason
        self.client_order_id = client_order_id


@dataclass(frozen=True)
class BrokerOrder:
    client_order_id: str
    symbol: str
    price: Decimal
    quantity: Decimal


class Broker(Protocol):
    def place(self, order: BrokerOrder) -> str: ...

    def detail(self, client_order_id: str) -> BrokerOrder | None: ...


@dataclass(frozen=True)
class PlaceOutcome:
    client_order_id: str
    status: str
    detail_queried: bool
    place_attempts: int
    code: int | None


@dataclass(frozen=True)
class _Row:
    client_order_id: str
    intent_id: str
    symbol: str
    price: Decimal
    quantity: Decimal
    status: str
    place_attempts: int
    last_code: int | None


class IdempotentMockGateway:
    def __init__(self, path: str) -> None:
        self.path = path
        self._guard = ConstitutionGuard()
        self._db = sqlite3.connect(path)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS order_prepare_wal (
                client_order_id TEXT PRIMARY KEY,
                intent_id TEXT NOT NULL UNIQUE,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                order_type TEXT NOT NULL,
                price TEXT NOT NULL,
                quantity TEXT NOT NULL,
                trading_session TEXT NOT NULL,
                quote_asset TEXT NOT NULL,
                wallet_type TEXT NOT NULL,
                tokenize INTEGER NOT NULL,
                ttl_seconds INTEGER NOT NULL,
                tier TEXT NOT NULL,
                status TEXT NOT NULL,
                place_attempts INTEGER NOT NULL,
                last_code INTEGER,
                created_at TEXT NOT NULL
            )
            """
        )
        self._db.commit()

    def close(self) -> None:
        self._db.close()

    def live_place(self) -> None:
        """The research tests call this to prove the exchange write is still refused."""

        self._guard.check_endpoint("POST", _PLACE_PATH)

    def status_of(self, client_order_id: str) -> str | None:
        row = self._row(client_order_id)
        if row is None:
            return None
        return row.status

    def attempts_of(self, client_order_id: str) -> int:
        row = self._row(client_order_id)
        if row is None:
            raise KeyError(client_order_id)
        return row.place_attempts

    def prepare(self, order: PreparedOrder) -> str:
        client_id = order.client_order_id
        if CLIENT_ORDER_ID.fullmatch(client_id) is None:
            raise ResendForbidden("CLIENT_ORDER_ID", client_id)
        draft = order.draft
        if draft.client_order_id != client_id:
            raise ResendForbidden("CLIENT_ORDER_ID", client_id)
        if (
            draft.side is not Side.BUY
            or draft.order_type is not OrderType.LIMIT
            or draft.trading_session is not TradingSession.RTH
            or draft.quote_asset != "USDC"
            or draft.wallet_type != "CARD"
            or draft.tokenize is not False
            or draft.price is None
            or draft.quantity is None
        ):
            raise ResendForbidden("FIELD_MATRIX", client_id)
        existing = self._intent_id(order.intent_id)
        if existing is not None:
            if existing != client_id:
                raise ResendForbidden("RESEND_NEW_ID_FORBIDDEN", existing)
            return existing
        if self._row(client_id) is not None:
            raise ResendForbidden("DUPLICATE_CLIENT_ID", client_id)
        self._db.execute(
            """
            INSERT INTO order_prepare_wal (
                client_order_id, intent_id, symbol, side, order_type, price, quantity,
                trading_session, quote_asset, wallet_type, tokenize, ttl_seconds, tier,
                status, place_attempts, last_code, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                client_id,
                order.intent_id,
                draft.symbol,
                draft.side.value,
                draft.order_type.value,
                format(draft.price, "f"),
                format(draft.quantity, "f"),
                draft.trading_session.value,
                draft.quote_asset,
                draft.wallet_type,
                0,
                order.ttl_seconds,
                order.tier,
                "WAL_PREPARED",
                0,
                None,
                datetime.now(UTC).isoformat(),
            ),
        )
        self._db.commit()
        return client_id

    def submit_mock(self, client_order_id: str, broker: Broker) -> PlaceOutcome:
        row = self._require(client_order_id)
        if row.status in {"ACKNOWLEDGED", "DETAIL_CONFIRMED"}:
            raise ResendForbidden("ALREADY_CONFIRMED", client_order_id)
        if row.status in {"SUBMITTING", "UNKNOWN_TIMEOUT"}:
            return self._from_detail(row, broker)
        if row.status not in {"WAL_PREPARED", "DETAIL_ABSENT"}:
            raise ResendForbidden(row.status, client_order_id)
        self._write(
            client_order_id,
            status="SUBMITTING",
            attempts=row.place_attempts,
            code=row.last_code,
        )
        self._db.commit()
        try:
            broker.place(_broker_order(row))
        except ExchangeTimeout as exc:
            self._write(
                client_order_id,
                status="UNKNOWN_TIMEOUT",
                attempts=row.place_attempts + 1,
                code=exc.code,
            )
            self._db.commit()
            current = self._require(client_order_id)
            return self._from_detail(current, broker)
        self._write(
            client_order_id,
            status="ACKNOWLEDGED",
            attempts=row.place_attempts + 1,
            code=None,
        )
        self._db.commit()
        return PlaceOutcome(client_order_id, "ACKNOWLEDGED", False, row.place_attempts + 1, None)

    def _from_detail(self, row: _Row, broker: Broker) -> PlaceOutcome:
        found = broker.detail(row.client_order_id)
        if found is None:
            self._write(
                row.client_order_id,
                status="DETAIL_ABSENT",
                attempts=row.place_attempts,
                code=row.last_code,
            )
            self._db.commit()
            return PlaceOutcome(
                row.client_order_id,
                "DETAIL_ABSENT",
                True,
                row.place_attempts,
                row.last_code,
            )
        if found.client_order_id != row.client_order_id:
            raise ResendForbidden("DETAIL_ID_MISMATCH", row.client_order_id)
        self._write(
            row.client_order_id,
            status="DETAIL_CONFIRMED",
            attempts=row.place_attempts,
            code=row.last_code,
        )
        self._db.commit()
        return PlaceOutcome(
            row.client_order_id,
            "DETAIL_CONFIRMED",
            True,
            row.place_attempts,
            row.last_code,
        )

    def _intent_id(self, intent_id: str) -> str | None:
        found = self._db.execute(
            "SELECT client_order_id, status FROM order_prepare_wal WHERE intent_id = ?",
            (intent_id,),
        ).fetchone()
        if found is None:
            return None
        status = str(found[1])
        if status not in _ACTIVE:
            return None
        return str(found[0])

    def _require(self, client_order_id: str) -> _Row:
        row = self._row(client_order_id)
        if row is None:
            raise KeyError(client_order_id)
        return row

    def _row(self, client_order_id: str) -> _Row | None:
        found = self._db.execute(
            """
            SELECT client_order_id, intent_id, symbol, price, quantity,
                   status, place_attempts, last_code
            FROM order_prepare_wal WHERE client_order_id = ?
            """,
            (client_order_id,),
        ).fetchone()
        if found is None:
            return None
        code = None if found[7] is None else int(found[7])
        return _Row(
            str(found[0]),
            str(found[1]),
            str(found[2]),
            parse_decimal(found[3]),
            parse_decimal(found[4]),
            str(found[5]),
            int(found[6]),
            code,
        )

    def _write(self, client_order_id: str, *, status: str, attempts: int, code: int | None) -> None:
        self._db.execute(
            """
            UPDATE order_prepare_wal
            SET status = ?, place_attempts = ?, last_code = ?
            WHERE client_order_id = ?
            """,
            (status, attempts, code, client_order_id),
        )


def read_wal_status(path: str, client_order_id: str) -> str | None:
    """A second connection, so a test can see the committed row before the broker returns."""

    db = sqlite3.connect(path)
    try:
        found = db.execute(
            "SELECT status FROM order_prepare_wal WHERE client_order_id = ?",
            (client_order_id,),
        ).fetchone()
    finally:
        db.close()
    if found is None:
        return None
    return str(found[0])


class ScriptedEquityBroker:
    """In-memory equity broker. Actions: ack, timeout_accept, timeout_drop, unexpected_accept."""

    def __init__(self, actions: list[str], wal_path: str | None = None) -> None:
        self._actions = list(actions)
        self.wal_path = wal_path
        self.orders: dict[str, BrokerOrder] = {}
        self.place_ids: list[str] = []
        self.detail_ids: list[str] = []
        self.seen_status: list[str | None] = []
        self.paths: list[tuple[str, str]] = []

    def place(self, order: BrokerOrder) -> str:
        self.paths.append(("POST", _PLACE_PATH))
        self.place_ids.append(order.client_order_id)
        if self.wal_path is not None:
            self.seen_status.append(read_wal_status(self.wal_path, order.client_order_id))
        action = self._actions.pop(0)
        if action == "ack":
            self.orders[order.client_order_id] = order
            return "NEW"
        if action == "timeout_accept":
            self.orders[order.client_order_id] = order
            raise ExchangeTimeout(-1007, "TIMEOUT")
        if action == "timeout_drop":
            raise ExchangeTimeout(-1007, "TIMEOUT")
        if action == "unexpected_accept":
            self.orders[order.client_order_id] = order
            raise ExchangeTimeout(-1006, "UNEXPECTED_RESP")
        raise ValueError(action)

    def detail(self, client_order_id: str) -> BrokerOrder | None:
        self.paths.append(("GET", _DETAIL_PATH))
        self.detail_ids.append(client_order_id)
        return self.orders.get(client_order_id)


def _broker_order(row: _Row) -> BrokerOrder:
    return BrokerOrder(row.client_order_id, row.symbol, row.price, row.quantity)

