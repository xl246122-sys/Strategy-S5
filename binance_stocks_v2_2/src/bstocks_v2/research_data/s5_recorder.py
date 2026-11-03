"""Capacity-capped S5 shadow journal. It does not open a socket or place an order."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from bstocks_v2.market_ingestor.messages import ParsedEvent
from bstocks_v2.market_ingestor.shards import PoolLimitError
from bstocks_v2.research_data.manifest import assert_output_isolated
from bstocks_v2.sessions.calendar import SessionLabel, TradingCalendar

MAX_FOCUS_SYMBOLS = 20
_BAR_STEP_MS = 5 * 60 * 1000


@dataclass(frozen=True)
class RecordResult:
    stored: bool
    quality: str
    session: SessionLabel
    dedup_key: str


class ShadowRecorder:
    """Append-only journal outside the SIP store and outside the intraday WAL."""

    def __init__(self, output: Path, source: Path, calendar: TradingCalendar) -> None:
        assert_output_isolated(output, source)
        output.parent.mkdir(parents=True, exist_ok=True)
        self.path = output
        self._calendar = calendar
        self._symbols: set[str] = set()
        self._previous: dict[str, int] = {}
        self._db = sqlite3.connect(output)
        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS s5_shadow_event (
                dedup_key TEXT PRIMARY KEY,
                symbol TEXT NOT NULL,
                kind TEXT NOT NULL,
                session_label TEXT NOT NULL,
                exchange_ts TEXT NOT NULL,
                capture_ts TEXT NOT NULL,
                feed TEXT NOT NULL,
                quality TEXT NOT NULL,
                volume TEXT,
                bid TEXT,
                ask TEXT,
                stored INTEGER NOT NULL
            )
            """
        )
        self._db.commit()

    def close(self) -> None:
        self._db.close()

    def record(
        self,
        event: ParsedEvent,
        *,
        capture_ts: datetime,
        decision_ts: datetime,
        feed: str,
    ) -> RecordResult:
        if capture_ts.tzinfo is None or decision_ts.tzinfo is None:
            raise ValueError("recorder timestamps must be timezone-aware")
        symbol = event.symbol or ""
        exchange_ms = _exchange_ms(event)
        exchange_ts = datetime.fromtimestamp(exchange_ms / 1000, UTC)
        label = self._calendar.label(exchange_ts)
        key = f"{feed}|{symbol}|{event.kind}|{exchange_ms}|{label.value}"
        if self._exists(key):
            return RecordResult(False, "DUPLICATE", label, key)
        if symbol and symbol not in self._symbols and len(self._symbols) >= MAX_FOCUS_SYMBOLS:
            return RecordResult(False, "CAPACITY", label, key)
        quality = _quality(
            event,
            label,
            exchange_ts,
            capture_ts,
            decision_ts,
            self._previous.get(symbol),
        )
        self._insert(key, symbol, event, label, exchange_ts, capture_ts, feed, quality)
        if symbol and quality not in {"LEAKED_FUTURE", "CLOCK_REVERSED"}:
            self._symbols.add(symbol)
            if event.kind == "kline":
                self._previous[symbol] = exchange_ms
        return RecordResult(True, quality, label, key)

    def _exists(self, key: str) -> bool:
        row = self._db.execute(
            "SELECT 1 FROM s5_shadow_event WHERE dedup_key = ?",
            (key,),
        ).fetchone()
        return row is not None

    def _insert(
        self,
        key: str,
        symbol: str,
        event: ParsedEvent,
        label: SessionLabel,
        exchange_ts: datetime,
        capture_ts: datetime,
        feed: str,
        quality: str,
    ) -> None:
        self._db.execute(
            """
            INSERT INTO s5_shadow_event (
                dedup_key, symbol, kind, session_label, exchange_ts, capture_ts,
                feed, quality, volume, bid, ask, stored
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
            """,
            (
                key,
                symbol,
                event.kind,
                label.value,
                exchange_ts.isoformat(timespec="seconds"),
                capture_ts.isoformat(timespec="seconds"),
                feed,
                quality,
                _text(event.volume),
                _text(event.bid),
                _text(event.ask),
            ),
        )
        self._db.commit()


def focus_quote_streams(symbols: tuple[str, ...]) -> tuple[str, ...]:
    """Name the quote streams a focus pool would use. This function does not connect."""

    if len(symbols) > MAX_FOCUS_SYMBOLS:
        raise PoolLimitError("S5 focus quote pool is capped at 20 symbols")
    if len(set(symbols)) != len(symbols):
        raise PoolLimitError("duplicate S5 focus symbol")
    return tuple(f"{symbol}@quote" for symbol in symbols)


def _quality(
    event: ParsedEvent,
    label: SessionLabel,
    exchange_ts: datetime,
    capture_ts: datetime,
    decision_ts: datetime,
    previous_ms: int | None,
) -> str:
    if exchange_ts > decision_ts:
        return "LEAKED_FUTURE"
    if exchange_ts > capture_ts:
        return "CLOCK_REVERSED"
    if label is SessionLabel.NON_SESSION:
        return "CLOSED"
    if event.kind == "quote" and (event.bid is None or event.ask is None):
        return "NO_QUOTE"
    if event.kind == "kline" and (event.volume is None or event.volume == 0):
        return "EMPTY_PRINT"
    if (
        event.kind == "kline"
        and previous_ms is not None
        and _exchange_ms(event) - previous_ms > _BAR_STEP_MS + 5_000
    ):
        return "GAP"
    return "OK"


def _exchange_ms(event: ParsedEvent) -> int:
    if event.kind == "quote" and event.quote_time_ms is not None:
        return event.quote_time_ms
    if event.bar_open_ms is not None:
        return event.bar_open_ms
    if event.event_time_ms is not None:
        return event.event_time_ms
    raise ValueError("event has no exchange timestamp")


def _text(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return format(value, "f")
