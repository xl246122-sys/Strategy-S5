"""SQLite WAL plus an HMAC-signed sidecar. A one-sided edit fails closed into the lock."""

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from bstocks_v2.contracts.decimals import parse_decimal
from bstocks_v2.portfolio_risk.spec import DB_NAME, FILE_NAME

_DDL = """
CREATE TABLE IF NOT EXISTS risk_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS risk_order (
    client_order_id TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    status TEXT NOT NULL,
    limit_price TEXT NOT NULL,
    quantity TEXT NOT NULL,
    filled_qty TEXT NOT NULL,
    filled_notional TEXT NOT NULL,
    total_cost_raw TEXT,
    rest_fee TEXT
);
CREATE TABLE IF NOT EXISTS risk_position (
    symbol TEXT PRIMARY KEY,
    quantity TEXT NOT NULL,
    cost_notional TEXT NOT NULL,
    bid TEXT NOT NULL,
    opened_session TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS risk_alert (
    id INTEGER PRIMARY KEY,
    level TEXT NOT NULL,
    code TEXT NOT NULL,
    detail TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS risk_cancel (
    id INTEGER PRIMARY KEY,
    client_order_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS risk_day_trade (
    id INTEGER PRIMARY KEY,
    symbol TEXT NOT NULL,
    session TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


@dataclass(frozen=True)
class SignedText:
    hwm: str
    halt: bool
    generation: int
    sig: str
    corrupt: bool = False


@dataclass(frozen=True)
class OrderRow:
    client_order_id: str
    symbol: str
    status: str
    limit_price: Decimal
    quantity: Decimal
    filled_qty: Decimal
    filled_notional: Decimal
    total_cost_raw: Decimal | None
    rest_fee: Decimal | None


@dataclass(frozen=True)
class PositionRow:
    symbol: str
    quantity: Decimal
    cost_notional: Decimal
    bid: Decimal
    opened_session: str


def sign_text(key: bytes, hwm: str, halt: bool, generation: int) -> str:
    flag = "1" if halt else "0"
    message = f"{hwm}|{flag}|{generation}".encode()
    return hmac.new(key, message, hashlib.sha256).hexdigest()


def sign_unlock(key: bytes, phrase: str, hwm: str, generation: int) -> str:
    message = f"{phrase}|{hwm}|{generation}".encode()
    return hmac.new(key, message, hashlib.sha256).hexdigest()


def signatures_match(expected: str, actual: str) -> bool:
    if len(expected) != len(actual):
        return False
    return hmac.compare_digest(expected, actual)


class RiskStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.db_path = root / DB_NAME
        self.file_path = root / FILE_NAME
        self._conn = sqlite3.connect(self.db_path, timeout=5, isolation_level=None)
        self._conn.execute("PRAGMA busy_timeout=5000")
        mode = self._conn.execute("PRAGMA journal_mode=WAL").fetchone()
        if mode is None or str(mode[0]).lower() != "wal":
            raise RuntimeError("portfolio risk store requires WAL")
        self._conn.executescript(_DDL)
        self._tx = False
        self._pending: SignedText | None = None
        self._hmac_key: bytes | None = None
        self._unlock_key: bytes | None = None

    def close(self) -> None:
        if self._tx:
            self.rollback()
        self._conn.close()

    def begin(self) -> None:
        if self._tx:
            raise RuntimeError("nested transaction")
        self._conn.execute("BEGIN IMMEDIATE")
        self._tx = True

    def commit(self) -> None:
        if not self._tx:
            return
        pending = self._pending
        self._pending = None
        self._conn.execute("COMMIT")
        self._tx = False
        if pending is not None:
            _write_file(self.file_path, pending)

    def rollback(self) -> None:
        self._pending = None
        if not self._tx:
            return
        self._conn.execute("ROLLBACK")
        self._tx = False

    def meta(self, key: str) -> str | None:
        row = self._conn.execute("SELECT value FROM risk_meta WHERE key = ?", (key,)).fetchone()
        if row is None:
            return None
        return str(row[0])

    def set_meta(self, key: str, value: str) -> None:
        self._conn.execute(
            """
            INSERT INTO risk_meta (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )

    def hmac_key(self) -> bytes:
        if self._hmac_key is None:
            raw = self.meta("hmac_key")
            if raw is None:
                raise RuntimeError("hmac key is missing")
            self._hmac_key = bytes.fromhex(raw)
        return self._hmac_key

    def unlock_key(self) -> bytes:
        if self._unlock_key is None:
            raw = self.meta("unlock_key")
            if raw is None:
                raise RuntimeError("unlock key is missing")
            self._unlock_key = bytes.fromhex(raw)
        return self._unlock_key

    def install_keys(self) -> None:
        hmac_key = secrets.token_bytes(32)
        unlock_key = secrets.token_bytes(32)
        self._hmac_key = hmac_key
        self._unlock_key = unlock_key
        self.set_meta("hmac_key", hmac_key.hex())
        self.set_meta("unlock_key", unlock_key.hex())

    def save_signed(self, hwm: Decimal, halt: bool, generation: int) -> SignedText:
        text = format(hwm, "f")
        sig = sign_text(self.hmac_key(), text, halt, generation)
        self.set_meta("hwm", text)
        self.set_meta("halt", "1" if halt else "0")
        self.set_meta("generation", str(generation))
        self.set_meta("sig", sig)
        pending = SignedText(text, halt, generation, sig)
        self._pending = pending
        return pending

    def read_signed_row(self) -> SignedText | None:
        hwm = self.meta("hwm")
        halt = self.meta("halt")
        generation = self.meta("generation")
        sig = self.meta("sig")
        if hwm is None or halt is None or generation is None or sig is None:
            return None
        if halt not in {"0", "1"}:
            return SignedText(hwm, True, 0, sig, corrupt=True)
        try:
            gen = int(generation)
        except ValueError:
            return SignedText(hwm, True, 0, sig, corrupt=True)
        return SignedText(hwm, halt == "1", gen, sig)

    def file_signed(self) -> SignedText | None:
        return _read_file(self.file_path)

    def orders(self) -> list[OrderRow]:
        rows = self._conn.execute(
            """
            SELECT client_order_id, symbol, status, limit_price, quantity,
                   filled_qty, filled_notional, total_cost_raw, rest_fee
            FROM risk_order
            ORDER BY symbol, client_order_id
            """
        ).fetchall()
        found: list[OrderRow] = []
        for row in rows:
            total = row[7]
            fee = row[8]
            found.append(
                OrderRow(
                    str(row[0]),
                    str(row[1]),
                    str(row[2]),
                    parse_decimal(row[3]),
                    parse_decimal(row[4]),
                    parse_decimal(row[5]),
                    parse_decimal(row[6]),
                    None if total is None else parse_decimal(total),
                    None if fee is None else parse_decimal(fee),
                )
            )
        return found

    def replace_order(self, order: OrderRow) -> None:
        self._conn.execute(
            """
            INSERT INTO risk_order (
                client_order_id, symbol, status, limit_price, quantity,
                filled_qty, filled_notional, total_cost_raw, rest_fee
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(client_order_id) DO UPDATE SET
                symbol = excluded.symbol,
                status = excluded.status,
                limit_price = excluded.limit_price,
                quantity = excluded.quantity,
                filled_qty = excluded.filled_qty,
                filled_notional = excluded.filled_notional,
                total_cost_raw = excluded.total_cost_raw,
                rest_fee = excluded.rest_fee
            """,
            (
                order.client_order_id,
                order.symbol,
                order.status,
                format(order.limit_price, "f"),
                format(order.quantity, "f"),
                format(order.filled_qty, "f"),
                format(order.filled_notional, "f"),
                None if order.total_cost_raw is None else format(order.total_cost_raw, "f"),
                None if order.rest_fee is None else format(order.rest_fee, "f"),
            ),
        )

    def positions(self) -> list[PositionRow]:
        rows = self._conn.execute(
            """
            SELECT symbol, quantity, cost_notional, bid, opened_session
            FROM risk_position
            ORDER BY symbol
            """
        ).fetchall()
        return [
            PositionRow(
                str(row[0]),
                parse_decimal(row[1]),
                parse_decimal(row[2]),
                parse_decimal(row[3]),
                str(row[4]),
            )
            for row in rows
        ]

    def replace_position(self, position: PositionRow) -> None:
        self._conn.execute(
            """
            INSERT INTO risk_position (symbol, quantity, cost_notional, bid, opened_session)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(symbol) DO UPDATE SET
                quantity = excluded.quantity,
                cost_notional = excluded.cost_notional,
                bid = excluded.bid,
                opened_session = excluded.opened_session
            """,
            (
                position.symbol,
                format(position.quantity, "f"),
                format(position.cost_notional, "f"),
                format(position.bid, "f"),
                position.opened_session,
            ),
        )

    def delete_position(self, symbol: str) -> None:
        self._conn.execute("DELETE FROM risk_position WHERE symbol = ?", (symbol,))

    def add_alert(self, level: str, code: str, detail: str, created_at: str) -> None:
        self._conn.execute(
            """
            INSERT INTO risk_alert (level, code, detail, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (level, code, detail, created_at),
        )

    def alerts(self) -> list[tuple[str, str, str]]:
        rows = self._conn.execute(
            "SELECT level, code, detail FROM risk_alert ORDER BY id"
        ).fetchall()
        return [(str(level), str(code), str(detail)) for level, code, detail in rows]

    def alert_codes(self) -> set[str]:
        rows = self._conn.execute("SELECT code FROM risk_alert").fetchall()
        return {str(row[0]) for row in rows}

    def add_cancel(self, client_order_id: str, symbol: str, created_at: str) -> None:
        self._conn.execute(
            """
            INSERT INTO risk_cancel (client_order_id, symbol, created_at)
            VALUES (?, ?, ?)
            """,
            (client_order_id, symbol, created_at),
        )

    def cancels(self) -> list[tuple[str, str]]:
        rows = self._conn.execute(
            "SELECT client_order_id, symbol FROM risk_cancel ORDER BY id"
        ).fetchall()
        return [(str(client), str(symbol)) for client, symbol in rows]

    def add_day_trade(self, symbol: str, session: str, created_at: str) -> None:
        self._conn.execute(
            "INSERT INTO risk_day_trade (symbol, session, created_at) VALUES (?, ?, ?)",
            (symbol, session, created_at),
        )

    def day_trade_sessions(self) -> list[tuple[str, str]]:
        rows = self._conn.execute(
            "SELECT symbol, session FROM risk_day_trade ORDER BY id"
        ).fetchall()
        return [(str(symbol), str(session)) for symbol, session in rows]


def _write_file(path: Path, signed: SignedText) -> None:
    payload = {
        "generation": signed.generation,
        "halt": signed.halt,
        "hwm": signed.hwm,
        "sig": signed.sig,
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(raw, encoding="utf-8")
    os.replace(temporary, path)


def _read_file(path: Path) -> SignedText | None:
    if not path.exists():
        return None
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return SignedText("", True, 0, "", corrupt=True)
    if not isinstance(loaded, dict):
        return SignedText("", True, 0, "", corrupt=True)
    hwm = loaded.get("hwm")
    halt = loaded.get("halt")
    generation = loaded.get("generation")
    sig = loaded.get("sig")
    if (
        not isinstance(hwm, str)
        or type(halt) is not bool
        or type(generation) is not int
        or not isinstance(sig, str)
    ):
        return SignedText("", True, 0, "", corrupt=True)
    return SignedText(hwm, halt, generation, sig)
