"""Shared read-only tape fan-out. Each desk keeps its own account."""

import os
import threading
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from bstocks_v2.console.archive import SessionArchive
from bstocks_v2.console.calendar import LiveCalendar
from bstocks_v2.console.desks import TITLES, Desk
from bstocks_v2.console.pool import SYMBOLS as POOL
from bstocks_v2.feature_store.bars import FeatureBar
from bstocks_v2.market_ingestor.messages import ParsedEvent

STRATEGIES = ("S1", "S2", "S3", "S4", "S5")


class Hub:
    def __init__(self, root: Path | None = None, archive: SessionArchive | None = None) -> None:
        self.calendar = LiveCalendar()
        self.archive = archive
        self.desks = [Desk(strategy, root, self.calendar, archive) for strategy in STRATEGIES]
        self.alerts: list[dict[str, str]] = []
        self.quotes: dict[str, dict[str, str]] = {}
        self.feed_status = "未连接"
        self.scan_status = ""
        self.alpaca_status = "未启动"
        self.tradable_known = 0
        self.started_at = datetime.now(UTC)
        self._boot = time.monotonic()
        self.bind_host = "127.0.0.1"
        self.bind_port = 8765
        self.worker = "stopped"
        self._seq = 0
        self._lock = threading.Lock()

    def close(self) -> None:
        for desk in self.desks:
            desk.close()

    def set_feed(self, status: str) -> None:
        with self._lock:
            self.feed_status = status

    def set_scan(self, status: str) -> None:
        with self._lock:
            self.scan_status = status

    def apply_quotes(self, rows: tuple[tuple[str, Decimal, Decimal], ...]) -> int:
        when = datetime.now(UTC)
        applied = 0
        with self._lock:
            for symbol, bid, ask in rows:
                if bid <= 0 or ask <= 0:
                    continue
                self._archive_trade(symbol, (bid + ask) / 2, when)
                for desk in self.desks:
                    desk.on_quote(symbol, bid, ask, when)
                self.quotes[symbol] = {"bid": format(bid, "f"), "ask": format(ask, "f")}
                applied += 1
            for desk in self.desks:
                desk.review_quoted()
        return applied

    def set_alpaca(self, status: str) -> None:
        with self._lock:
            self.alpaca_status = status

    def set_bind(self, host: str, port: int) -> None:
        with self._lock:
            self.bind_host = host
            self.bind_port = port

    def set_worker(self, status: str) -> None:
        with self._lock:
            self.worker = status

    def set_tradable(self, rows: dict[str, str]) -> None:
        with self._lock:
            self.tradable_known = len(rows)
            for desk in self.desks:
                for symbol, state in rows.items():
                    desk.on_tradable(symbol, state)

    def accept(self, event: ParsedEvent) -> None:
        with self._lock:
            alerts: list[dict[str, str]] = []
            if event.kind == "quote":
                self._quote(event)
            elif event.kind == "tradability" and event.symbol and event.raw_state:
                symbol = event.symbol.upper()
                for desk in self.desks:
                    desk.on_tradable(symbol, event.raw_state)
            elif event.kind == "price":
                for symbol, price, time_ms in event.marks:
                    when = _stamp(time_ms)
                    self._archive_trade(symbol, price, when)
                    for desk in self.desks:
                        desk.on_print(symbol, price, when)
            elif event.kind == "kline" and event.symbol and event.close is not None:
                symbol = event.symbol.upper()
                when = _stamp(event.event_time_ms)
                self._archive_kline(event)
                for desk in self.desks:
                    desk.on_print(symbol, event.close, when)
                if event.closed is True:
                    for desk in self.desks:
                        alerts.extend(desk.on_kline(event))
            self._take(alerts)

    def _quote(self, event: ParsedEvent) -> None:
        symbol = event.symbol
        bid = event.bid
        ask = event.ask
        if symbol is None or bid is None or ask is None:
            return
        name = symbol.upper()
        when = _stamp(event.event_time_ms)
        for desk in self.desks:
            desk.on_quote(name, bid, ask, when)
        self._archive_trade(name, (bid + ask) / 2, when)
        self.quotes[name] = {"bid": format(bid, "f"), "ask": format(ask, "f")}

    def replay(self, bars: list[FeatureBar]) -> None:
        grouped: dict[datetime, list[FeatureBar]] = {}
        for bar in bars:
            grouped.setdefault(bar.start, []).append(bar)
        for start in sorted(grouped):
            with self._lock:
                alerts: list[dict[str, str]] = []
                for desk in self.desks:
                    alerts.extend(desk.replay_group(grouped[start]))
                self._take(alerts)

    def state(self) -> dict[str, object]:
        with self._lock:
            return {
                "mode": "PAPER",
                "live_orders": False,
                "server": {
                    "pid": os.getpid(),
                    "started_at": self.started_at.isoformat(),
                    "uptime_seconds": int(time.monotonic() - self._boot),
                    "bind": f"{self.bind_host}:{self.bind_port}",
                    "worker": self.worker,
                    "live_orders": False,
                },
                "feed": _feed_line(self.feed_status, self.scan_status),
                "alpaca": self.alpaca_status,
                "calendar": self.calendar.source,
                "tradable_known": self.tradable_known,
                "universe": list(POOL),
                "quotes": self.quotes,
                "alerts": list(self.alerts),
                "desks": [desk.view() for desk in self.desks],
                "titles": TITLES,
            }

    def _archive_trade(self, symbol: str, price: Decimal, when: datetime) -> None:
        if self.archive is not None:
            self.archive.add_trade(symbol, price, when)

    def _archive_kline(self, event: ParsedEvent) -> None:
        if (
            self.archive is None
            or event.closed is not True
            or event.symbol is None
            or event.bar_open_ms is None
            or event.open is None
            or event.high is None
            or event.low is None
            or event.close is None
            or event.volume is None
        ):
            return
        start = _stamp(event.bar_open_ms)
        self.archive.add_kline(
            event.symbol.upper(),
            start,
            start + timedelta(minutes=5),
            event.open,
            event.high,
            event.low,
            event.close,
            event.volume,
        )

    def _take(self, alerts: list[dict[str, str]]) -> None:
        for alert in alerts:
            self._seq += 1
            alert["id"] = str(self._seq)
            self.alerts.append(alert)
        if len(self.alerts) > 100:
            self.alerts = self.alerts[-100:]


def _feed_line(feed: str, scan: str) -> str:
    if scan:
        return f"{feed}；{scan}"
    return feed


def _stamp(milliseconds: int | None) -> datetime:
    if milliseconds is None:
        return datetime.now(UTC)
    return datetime(1970, 1, 1, tzinfo=UTC) + timedelta(milliseconds=milliseconds)
