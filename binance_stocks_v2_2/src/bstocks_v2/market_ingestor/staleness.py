"""Periodic streams age out. State-change streams do not, even after a full session of silence."""

from dataclasses import dataclass
from enum import StrEnum

FIVE_MINUTES_MS = 5 * 60 * 1000
KLINE_STALE_MS = 6 * 60 * 1000
PRICE_STALE_MS = 10_000
QUOTE_STALE_MS = 2_000


class StreamClass(StrEnum):
    PERIODIC = "periodic"
    STATE = "state"


@dataclass(frozen=True)
class StreamHealth:
    stream: str
    stream_class: StreamClass
    stale: bool
    reason: str


def classify(stream: str) -> StreamClass:
    if stream == "calendar" or stream.endswith("@tradability") or stream.endswith("@tradingStatus"):
        return StreamClass.STATE
    if stream == "price" or stream.endswith("@kline_5m") or stream.endswith("@quote"):
        return StreamClass.PERIODIC
    raise ValueError(f"unknown stream {stream}")


def assess(
    stream: str,
    *,
    last_event_ms: int | None,
    now_ms: int,
    connection_alive: bool,
    has_initial_snapshot: bool,
) -> StreamHealth:
    kind = classify(stream)
    if kind is StreamClass.STATE:
        if connection_alive and has_initial_snapshot:
            return StreamHealth(stream, kind, False, "state_change_stream")
        return StreamHealth(stream, kind, False, "state_change_stream_socket_pending")
    if not connection_alive or last_event_ms is None:
        return StreamHealth(stream, kind, True, "periodic_stream_silent")
    age = now_ms - last_event_ms
    limit = _limit(stream)
    if age > limit:
        return StreamHealth(stream, kind, True, "periodic_stream_stale")
    return StreamHealth(stream, kind, False, "fresh")


def _limit(stream: str) -> int:
    if stream == "price":
        return PRICE_STALE_MS
    if stream.endswith("@quote"):
        return QUOTE_STALE_MS
    return KLINE_STALE_MS


class ConnectionWatch:
    """State streams stay fresh while the socket answers PING. Silence is not staleness."""

    def __init__(self, pong_timeout_ms: int = 20_000) -> None:
        self.pong_timeout_ms = pong_timeout_ms
        self.last_pong_ms: int | None = None

    def note_pong(self, now_ms: int) -> None:
        self.last_pong_ms = now_ms

    def alive(self, now_ms: int) -> bool:
        if self.last_pong_ms is None:
            return False
        return now_ms - self.last_pong_ms <= self.pong_timeout_ms
