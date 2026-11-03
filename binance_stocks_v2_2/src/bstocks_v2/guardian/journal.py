"""SQLite WAL for guardian positions, client ids, and operator commands."""

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from bstocks_v2.contracts.decimals import parse_decimal

_DDL = """
CREATE TABLE IF NOT EXISTS guardian_position (
    symbol TEXT PRIMARY KEY,
    state TEXT NOT NULL,
    buy_client_order_id TEXT NOT NULL,
    buy_order_id TEXT NOT NULL,
    buy_qty TEXT NOT NULL,
    filled_buy_qty TEXT NOT NULL,
    stop_price TEXT NOT NULL,
    take_profit_price TEXT NOT NULL,
    max_holding_seconds INTEGER NOT NULL,
    opened_at TEXT,
    tp_client_order_id TEXT,
    tp_filled_qty TEXT NOT NULL,
    sell_lock INTEGER NOT NULL,
    buy_cancel_pending INTEGER NOT NULL,
    exit_client_order_id TEXT,
    exit_role TEXT,
    remaining_qty TEXT NOT NULL,
    residual_qty TEXT NOT NULL,
    ws_total_cost TEXT,
    ws_filled_notional TEXT,
    realized_fee TEXT,
    fee_source TEXT,
    alert TEXT,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS guardian_flag (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS guardian_alert (
    id INTEGER PRIMARY KEY,
    level TEXT NOT NULL,
    code TEXT NOT NULL,
    symbol TEXT NOT NULL,
    detail TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS guardian_command (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    payload TEXT NOT NULL,
    done INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS guardian_market (
    symbol TEXT PRIMARY KEY,
    bid TEXT NOT NULL,
    trading_status TEXT NOT NULL,
    multiplier_down TEXT NOT NULL,
    multiplier_up TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS order_guard (
    client_order_id TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    role TEXT NOT NULL,
    status TEXT NOT NULL,
    place_attempts INTEGER NOT NULL,
    last_code INTEGER,
    UNIQUE(symbol, role)
);
CREATE TABLE IF NOT EXISTS broker_order (
    client_order_id TEXT PRIMARY KEY,
    order_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    order_type TEXT NOT NULL,
    quantity TEXT NOT NULL,
    price TEXT,
    trading_session TEXT,
    wallet_type TEXT NOT NULL,
    tokenize INTEGER NOT NULL,
    quote_asset TEXT NOT NULL,
    status TEXT NOT NULL,
    filled_qty TEXT NOT NULL,
    fee TEXT,
    execution_event TEXT,
    shares_frozen INTEGER NOT NULL,
    hold INTEGER NOT NULL,
    visible_ns INTEGER,
    pending_status TEXT,
    pending_filled TEXT
);
CREATE TABLE IF NOT EXISTS broker_place_fault (
    id INTEGER PRIMARY KEY,
    code INTEGER NOT NULL,
    landed INTEGER NOT NULL,
    consumed INTEGER NOT NULL,
    symbol TEXT
);
CREATE TABLE IF NOT EXISTS broker_cancel_script (
    client_order_id TEXT PRIMARY KEY,
    code INTEGER,
    accept_status TEXT,
    delay_ms INTEGER NOT NULL,
    hold INTEGER NOT NULL,
    terminal_status TEXT NOT NULL,
    terminal_filled TEXT
);
CREATE TABLE IF NOT EXISTS broker_place_log (
    id INTEGER PRIMARY KEY,
    client_order_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    order_type TEXT NOT NULL,
    quantity TEXT NOT NULL,
    price TEXT,
    trading_session TEXT,
    wallet_type TEXT NOT NULL,
    tokenize INTEGER NOT NULL,
    quote_asset TEXT NOT NULL,
    code INTEGER,
    accepted INTEGER NOT NULL,
    status_at_place TEXT,
    duplicate INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS broker_cancel_log (
    id INTEGER PRIMARY KEY,
    client_order_id TEXT NOT NULL,
    code INTEGER,
    status TEXT
);
CREATE TABLE IF NOT EXISTS broker_trade (
    id INTEGER PRIMARY KEY,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    quantity TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS broker_funding (
    asset TEXT PRIMARY KEY,
    quantity TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS broker_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


@dataclass
class Position:
    symbol: str
    state: str
    buy_client_order_id: str
    buy_order_id: str
    buy_qty: Decimal
    filled_buy_qty: Decimal
    stop_price: Decimal
    take_profit_price: Decimal
    max_holding_seconds: int
    opened_at: str | None
    tp_client_order_id: str | None
    tp_filled_qty: Decimal
    sell_lock: bool
    buy_cancel_pending: bool
    exit_client_order_id: str | None
    exit_role: str | None
    remaining_qty: Decimal
    residual_qty: Decimal
    ws_total_cost: Decimal | None
    ws_filled_notional: Decimal | None
    realized_fee: Decimal | None
    fee_source: str | None
    alert: str | None


@dataclass(frozen=True)
class MarketRow:
    bid: Decimal
    trading_status: str
    multiplier_down: Decimal
    multiplier_up: Decimal


@dataclass(frozen=True)
class GuardRow:
    client_order_id: str
    symbol: str
    role: str
    status: str
    place_attempts: int
    last_code: int | None


class Journal:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def save(self, pos: Position) -> None:
        self.conn.execute(
            """
            INSERT INTO guardian_position (
                symbol, state, buy_client_order_id, buy_order_id, buy_qty, filled_buy_qty,
                stop_price, take_profit_price, max_holding_seconds, opened_at,
                tp_client_order_id, tp_filled_qty, sell_lock, buy_cancel_pending,
                exit_client_order_id, exit_role, remaining_qty, residual_qty,
                ws_total_cost, ws_filled_notional, realized_fee, fee_source, alert, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(symbol) DO UPDATE SET
                state=excluded.state,
                buy_client_order_id=excluded.buy_client_order_id,
                buy_order_id=excluded.buy_order_id,
                buy_qty=excluded.buy_qty,
                filled_buy_qty=excluded.filled_buy_qty,
                stop_price=excluded.stop_price,
                take_profit_price=excluded.take_profit_price,
                max_holding_seconds=excluded.max_holding_seconds,
                opened_at=excluded.opened_at,
                tp_client_order_id=excluded.tp_client_order_id,
                tp_filled_qty=excluded.tp_filled_qty,
                sell_lock=excluded.sell_lock,
                buy_cancel_pending=excluded.buy_cancel_pending,
                exit_client_order_id=excluded.exit_client_order_id,
                exit_role=excluded.exit_role,
                remaining_qty=excluded.remaining_qty,
                residual_qty=excluded.residual_qty,
                ws_total_cost=excluded.ws_total_cost,
                ws_filled_notional=excluded.ws_filled_notional,
                realized_fee=excluded.realized_fee,
                fee_source=excluded.fee_source,
                alert=excluded.alert,
                updated_at=excluded.updated_at
            """,
            _position_values(pos),
        )
        self.conn.commit()

    def load(self, symbol: str) -> Position | None:
        row = self.conn.execute(
            """
            SELECT symbol, state, buy_client_order_id, buy_order_id, buy_qty, filled_buy_qty,
                   stop_price, take_profit_price, max_holding_seconds, opened_at,
                   tp_client_order_id, tp_filled_qty, sell_lock, buy_cancel_pending,
                   exit_client_order_id, exit_role, remaining_qty, residual_qty,
                   ws_total_cost, ws_filled_notional, realized_fee, fee_source, alert
            FROM guardian_position WHERE symbol = ?
            """,
            (symbol,),
        ).fetchone()
        if row is None:
            return None
        return _position(row)

    def list_positions(self) -> list[Position]:
        rows = self.conn.execute(
            """
            SELECT symbol, state, buy_client_order_id, buy_order_id, buy_qty, filled_buy_qty,
                   stop_price, take_profit_price, max_holding_seconds, opened_at,
                   tp_client_order_id, tp_filled_qty, sell_lock, buy_cancel_pending,
                   exit_client_order_id, exit_role, remaining_qty, residual_qty,
                   ws_total_cost, ws_filled_notional, realized_fee, fee_source, alert
            FROM guardian_position ORDER BY symbol
            """
        ).fetchall()
        return [_position(row) for row in rows]

    def set_flag(self, key: str, value: str) -> None:
        self.conn.execute(
            """
            INSERT INTO guardian_flag (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
            """,
            (key, value),
        )
        self.conn.commit()

    def get_flag(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM guardian_flag WHERE key = ?", (key,)).fetchone()
        if row is None:
            return None
        return str(row[0])

    def bump(self, key: str) -> int:
        current = self.get_flag(key)
        value = 1 if current is None else int(current) + 1
        self.set_flag(key, str(value))
        return value

    def alert(self, level: str, code: str, symbol: str, detail: str) -> None:
        self.conn.execute(
            """
            INSERT INTO guardian_alert (level, code, symbol, detail, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (level, code, symbol, detail, datetime.now(UTC).isoformat()),
        )
        self.conn.commit()

    def alerts(self) -> list[tuple[str, str, str]]:
        rows = self.conn.execute(
            "SELECT level, code, symbol FROM guardian_alert ORDER BY id"
        ).fetchall()
        return [(str(level), str(code), str(symbol)) for level, code, symbol in rows]

    def set_market(
        self,
        symbol: str,
        *,
        bid: Decimal,
        trading_status: str,
        multiplier_down: Decimal,
        multiplier_up: Decimal,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO guardian_market (
                symbol, bid, trading_status, multiplier_down, multiplier_up
            )
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(symbol) DO UPDATE SET
                bid=excluded.bid,
                trading_status=excluded.trading_status,
                multiplier_down=excluded.multiplier_down,
                multiplier_up=excluded.multiplier_up
            """,
            (
                symbol,
                format(bid, "f"),
                trading_status,
                format(multiplier_down, "f"),
                format(multiplier_up, "f"),
            ),
        )
        self.conn.commit()

    def market(self, symbol: str) -> MarketRow:
        row = self.conn.execute(
            """
            SELECT bid, trading_status, multiplier_down, multiplier_up
            FROM guardian_market WHERE symbol = ?
            """,
            (symbol,),
        ).fetchone()
        if row is None:
            raise KeyError(symbol)
        return MarketRow(
            parse_decimal(str(row[0])),
            str(row[1]),
            parse_decimal(str(row[2])),
            parse_decimal(str(row[3])),
        )

    def prepare_guard(self, client_order_id: str, symbol: str, role: str) -> GuardRow:
        existing = self.guard(client_order_id)
        if existing is not None:
            return existing
        self.conn.execute(
            """
            INSERT INTO order_guard (
                client_order_id, symbol, role, status, place_attempts, last_code
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (client_order_id, symbol, role, "WAL_PREPARED", 0, None),
        )
        self.conn.commit()
        row = self.guard(client_order_id)
        if row is None:
            raise KeyError(client_order_id)
        return row

    def mark_guard(
        self,
        client_order_id: str,
        status: str,
        *,
        attempts: int,
        code: int | None,
    ) -> None:
        self.conn.execute(
            """
            UPDATE order_guard
            SET status = ?, place_attempts = ?, last_code = ?
            WHERE client_order_id = ?
            """,
            (status, attempts, code, client_order_id),
        )
        self.conn.commit()

    def guard(self, client_order_id: str) -> GuardRow | None:
        row = self.conn.execute(
            """
            SELECT client_order_id, symbol, role, status, place_attempts, last_code
            FROM order_guard WHERE client_order_id = ?
            """,
            (client_order_id,),
        ).fetchone()
        if row is None:
            return None
        last = row[5]
        return GuardRow(
            str(row[0]),
            str(row[1]),
            str(row[2]),
            str(row[3]),
            int(row[4]),
            None if last is None else int(last),
        )

    def guards(self) -> list[GuardRow]:
        rows = self.conn.execute(
            """
            SELECT client_order_id, symbol, role, status, place_attempts, last_code
            FROM order_guard ORDER BY client_order_id
            """
        ).fetchall()
        found: list[GuardRow] = []
        for row in rows:
            last = row[5]
            found.append(
                GuardRow(
                    str(row[0]),
                    str(row[1]),
                    str(row[2]),
                    str(row[3]),
                    int(row[4]),
                    None if last is None else int(last),
                )
            )
        return found

    def pending_commands(self) -> list[tuple[int, str, str]]:
        rows = self.conn.execute(
            """
            SELECT id, name, payload FROM guardian_command
            WHERE done = 0 ORDER BY id
            """
        ).fetchall()
        return [(int(cmd_id), str(name), str(payload)) for cmd_id, name, payload in rows]

    def finish_command(self, cmd_id: int) -> None:
        self.conn.execute("UPDATE guardian_command SET done = 1 WHERE id = ?", (cmd_id,))
        self.conn.commit()


def init_db(conn: sqlite3.Connection) -> None:
    mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()
    if mode is None or str(mode[0]).lower() != "wal":
        raise RuntimeError("guardian journal requires WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.executescript(_DDL)
    conn.commit()


def _position_values(pos: Position) -> tuple[object, ...]:
    return (
        pos.symbol,
        pos.state,
        pos.buy_client_order_id,
        pos.buy_order_id,
        format(pos.buy_qty, "f"),
        format(pos.filled_buy_qty, "f"),
        format(pos.stop_price, "f"),
        format(pos.take_profit_price, "f"),
        pos.max_holding_seconds,
        pos.opened_at,
        pos.tp_client_order_id,
        format(pos.tp_filled_qty, "f"),
        1 if pos.sell_lock else 0,
        1 if pos.buy_cancel_pending else 0,
        pos.exit_client_order_id,
        pos.exit_role,
        format(pos.remaining_qty, "f"),
        format(pos.residual_qty, "f"),
        None if pos.ws_total_cost is None else format(pos.ws_total_cost, "f"),
        None if pos.ws_filled_notional is None else format(pos.ws_filled_notional, "f"),
        None if pos.realized_fee is None else format(pos.realized_fee, "f"),
        pos.fee_source,
        pos.alert,
        datetime.now(UTC).isoformat(),
    )


def _position(row: tuple[object, ...]) -> Position:
    return Position(
        symbol=str(row[0]),
        state=str(row[1]),
        buy_client_order_id=str(row[2]),
        buy_order_id=str(row[3]),
        buy_qty=parse_decimal(str(row[4])),
        filled_buy_qty=parse_decimal(str(row[5])),
        stop_price=parse_decimal(str(row[6])),
        take_profit_price=parse_decimal(str(row[7])),
        max_holding_seconds=int(str(row[8])),
        opened_at=None if row[9] is None else str(row[9]),
        tp_client_order_id=None if row[10] is None else str(row[10]),
        tp_filled_qty=parse_decimal(str(row[11])),
        sell_lock=bool(row[12]),
        buy_cancel_pending=bool(row[13]),
        exit_client_order_id=None if row[14] is None else str(row[14]),
        exit_role=None if row[15] is None else str(row[15]),
        remaining_qty=parse_decimal(str(row[16])),
        residual_qty=parse_decimal(str(row[17])),
        ws_total_cost=None if row[18] is None else parse_decimal(str(row[18])),
        ws_filled_notional=None if row[19] is None else parse_decimal(str(row[19])),
        realized_fee=None if row[20] is None else parse_decimal(str(row[20])),
        fee_source=None if row[21] is None else str(row[21]),
        alert=None if row[22] is None else str(row[22]),
    )
