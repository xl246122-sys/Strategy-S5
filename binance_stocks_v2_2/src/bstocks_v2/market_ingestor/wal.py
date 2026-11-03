"""Intraday 5-minute bars in SQLite WAL, and the restart recovery that reads them."""

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from bstocks_v2.market_ingestor.messages import ParsedEvent

NEW_YORK = ZoneInfo("America/New_York")
BAR_MS = 5 * 60 * 1000
RSI_PERIOD = 6


@dataclass(frozen=True)
class StoredBar:
    symbol: str
    session_date: str
    open_time_ms: int
    close_time_ms: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    vwap: Decimal
    vwap_source: str


@dataclass(frozen=True)
class SessionRecovery:
    symbol: str
    session_date: str
    bars: tuple[StoredBar, ...]
    orb_high_5m: Decimal | None
    orb_low_5m: Decimal | None
    orb_high_15m: Decimal | None
    orb_low_15m: Decimal | None
    orb_high_30m: Decimal | None
    orb_low_30m: Decimal | None
    rth_vwap: Decimal | None
    rsi6: Decimal | None
    vwap_intact: bool
    missing_open_times_ms: tuple[int, ...]


class IntradayBarStore:
    def __init__(self, path: str) -> None:
        self.path = path
        self._db = sqlite3.connect(path)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS intraday_bars_wal (
                symbol TEXT NOT NULL,
                session_date TEXT NOT NULL,
                open_time_ms INTEGER NOT NULL,
                close_time_ms INTEGER NOT NULL,
                open TEXT NOT NULL,
                high TEXT NOT NULL,
                low TEXT NOT NULL,
                close TEXT NOT NULL,
                volume TEXT NOT NULL,
                vwap TEXT NOT NULL,
                vwap_source TEXT NOT NULL,
                PRIMARY KEY (symbol, open_time_ms)
            )
            """
        )
        self._db.commit()

    def close(self) -> None:
        self._db.close()

    def checkpoint(self) -> None:
        self._db.execute("PRAGMA wal_checkpoint(FULL)")
        self._db.commit()

    def upsert_event(self, event: ParsedEvent) -> None:
        if event.kind != "kline" or not event.closed:
            return
        if event.symbol is None or event.bar_open_ms is None or event.bar_close_ms is None:
            return
        if None in (event.open, event.high, event.low, event.close, event.volume, event.vwap):
            return
        assert event.open is not None
        assert event.high is not None
        assert event.low is not None
        assert event.close is not None
        assert event.volume is not None
        assert event.vwap is not None
        opened = datetime.fromtimestamp(event.bar_open_ms / 1000, tz=NEW_YORK)
        session_date = opened.date().isoformat()
        self._write(
            StoredBar(
                symbol=event.symbol,
                session_date=session_date,
                open_time_ms=event.bar_open_ms,
                close_time_ms=event.bar_close_ms,
                open=event.open,
                high=event.high,
                low=event.low,
                close=event.close,
                volume=event.volume,
                vwap=event.vwap,
                vwap_source=event.vwap_source or "wire",
            )
        )

    def upsert_bar(self, bar: StoredBar) -> None:
        self._write(bar)

    def _write(self, bar: StoredBar) -> None:
        self._db.execute(
            """
            INSERT INTO intraday_bars_wal (
                symbol, session_date, open_time_ms, close_time_ms,
                open, high, low, close, volume, vwap, vwap_source
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(symbol, open_time_ms) DO UPDATE SET
                close_time_ms=excluded.close_time_ms,
                open=excluded.open, high=excluded.high, low=excluded.low, close=excluded.close,
                volume=excluded.volume, vwap=excluded.vwap, vwap_source=excluded.vwap_source
            """,
            (
                bar.symbol,
                bar.session_date,
                bar.open_time_ms,
                bar.close_time_ms,
                str(bar.open),
                str(bar.high),
                str(bar.low),
                str(bar.close),
                str(bar.volume),
                str(bar.vwap),
                bar.vwap_source,
            ),
        )
        self._db.commit()

    def load_session(self, symbol: str, session_date: str) -> list[StoredBar]:
        rows = self._db.execute(
            """
            SELECT symbol, session_date, open_time_ms, close_time_ms,
                   open, high, low, close, volume, vwap, vwap_source
            FROM intraday_bars_wal
            WHERE symbol = ? AND session_date = ?
            ORDER BY open_time_ms
            """,
            (symbol, session_date),
        ).fetchall()
        return [_row(row) for row in rows]

    def load_symbol(self, symbol: str) -> list[StoredBar]:
        rows = self._db.execute(
            """
            SELECT symbol, session_date, open_time_ms, close_time_ms,
                   open, high, low, close, volume, vwap, vwap_source
            FROM intraday_bars_wal
            WHERE symbol = ?
            ORDER BY open_time_ms
            """,
            (symbol,),
        ).fetchall()
        return [_row(row) for row in rows]

    def symbols(self) -> list[str]:
        rows = self._db.execute(
            "SELECT DISTINCT symbol FROM intraday_bars_wal ORDER BY symbol"
        ).fetchall()
        return [str(row[0]) for row in rows]


def recover_session(
    store: IntradayBarStore,
    symbol: str,
    session_open: datetime,
    asof: datetime,
    backfill: list[StoredBar] | None = None,
) -> SessionRecovery:
    """Rebuild one RTH session. ``session_open`` is 09:30 America/New_York."""

    session_date = session_open.date().isoformat()
    bars = {bar.open_time_ms: bar for bar in store.load_session(symbol, session_date)}
    for extra in backfill or []:
        if extra.symbol == symbol and extra.open_time_ms not in bars:
            bars[extra.open_time_ms] = extra
    open_ms = int(session_open.timestamp() * 1000)
    asof_ms = int(asof.timestamp() * 1000)
    expected: list[int] = []
    cursor = open_ms
    while cursor + BAR_MS <= asof_ms:
        expected.append(cursor)
        cursor += BAR_MS
    missing = tuple(item for item in expected if item not in bars)
    ordered = tuple(bars[item] for item in expected if item in bars)
    intact = not missing and all(
        ordered[index].open_time_ms - ordered[index - 1].open_time_ms == BAR_MS
        for index in range(1, len(ordered))
    )
    if ordered and ordered[0].open_time_ms != open_ms:
        intact = False
    return SessionRecovery(
        symbol=symbol,
        session_date=session_date,
        bars=ordered,
        orb_high_5m=_orb_high(ordered, 1),
        orb_low_5m=_orb_low(ordered, 1),
        orb_high_15m=_orb_high(ordered, 3),
        orb_low_15m=_orb_low(ordered, 3),
        orb_high_30m=_orb_high(ordered, 6),
        orb_low_30m=_orb_low(ordered, 6),
        rth_vwap=_cumulative_vwap(ordered) if intact else None,
        rsi6=wilder_rsi([bar.close for bar in ordered], RSI_PERIOD) if intact else None,
        vwap_intact=intact and bool(ordered),
        missing_open_times_ms=missing,
    )


def wilder_rsi(closes: list[Decimal], period: int = RSI_PERIOD) -> Decimal | None:
    if len(closes) < period + 1:
        return None
    gains: list[Decimal] = []
    losses: list[Decimal] = []
    for previous, current in zip(closes, closes[1:], strict=False):
        delta = current - previous
        gains.append(delta if delta > 0 else Decimal(0))
        losses.append(-delta if delta < 0 else Decimal(0))
    span = Decimal(period)
    avg_gain = sum(gains[:period], start=Decimal(0)) / span
    avg_loss = sum(losses[:period], start=Decimal(0)) / span
    for gain, loss in zip(gains[period:], losses[period:], strict=False):
        avg_gain = ((avg_gain * (span - 1)) + gain) / span
        avg_loss = ((avg_loss * (span - 1)) + loss) / span
    if avg_loss == 0:
        return Decimal(100) if avg_gain > 0 else Decimal(50)
    rs = avg_gain / avg_loss
    return Decimal(100) - (Decimal(100) / (Decimal(1) + rs))


def _cumulative_vwap(bars: tuple[StoredBar, ...] | list[StoredBar]) -> Decimal | None:
    numerator = Decimal(0)
    denominator = Decimal(0)
    for bar in bars:
        if bar.volume <= 0:
            continue
        numerator += bar.vwap * bar.volume
        denominator += bar.volume
    if denominator == 0:
        return None
    return numerator / denominator


def _orb_high(bars: tuple[StoredBar, ...], count: int) -> Decimal | None:
    if len(bars) < count:
        return None
    return max(bar.high for bar in bars[:count])


def _orb_low(bars: tuple[StoredBar, ...], count: int) -> Decimal | None:
    if len(bars) < count:
        return None
    return min(bar.low for bar in bars[:count])


def _row(row: tuple[object, ...]) -> StoredBar:
    return StoredBar(
        symbol=str(row[0]),
        session_date=str(row[1]),
        open_time_ms=int(str(row[2])),
        close_time_ms=int(str(row[3])),
        open=Decimal(str(row[4])),
        high=Decimal(str(row[5])),
        low=Decimal(str(row[6])),
        close=Decimal(str(row[7])),
        volume=Decimal(str(row[8])),
        vwap=Decimal(str(row[9])),
        vwap_source=str(row[10]),
    )
