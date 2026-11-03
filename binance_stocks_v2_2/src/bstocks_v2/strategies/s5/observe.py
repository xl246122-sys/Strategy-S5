"""One shadow row. A penetrated bar is an estimate, not an exchange fill."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from bstocks_v2.contracts.models import Side
from bstocks_v2.research_backtest.tape import ClosedBar
from bstocks_v2.research_data.calendar import NEW_YORK
from bstocks_v2.sessions.calendar import SessionLabel, TradingCalendar
from bstocks_v2.strategies.s5.execution import ResearchQuote, match_limit, quote_block, size_limit
from bstocks_v2.strategies.s5.spec import (
    ARMS,
    B0_MAX_SIMPLE_RETURN,
    B1_MAX_Z,
    MAX_SPREAD_BPS,
    MIN_DEPTH,
    MIN_NOTIONAL,
    MIN_RESOLVED,
    MODEL_VERSION,
    PENETRATION,
    PLANNED_STOP,
    PRIMARY_NET_TARGET,
    RISK_BUDGET,
    TTL,
)

SOURCES = frozenset({"fixture", "live_readonly"})
_ENTRY = {
    "A_RTH_TO_POST": SessionLabel.RTH,
    "B_POST_TO_POST": SessionLabel.POST,
    "C_POST_TO_OVERNIGHT": SessionLabel.POST,
}
_STALE_MS = 5_000
_Z95 = Decimal("1.96")
_CI_QUANTUM = Decimal("0.000001")
LIVE_SESSION_FLOOR = 20


def freeze_payload() -> dict[str, object]:
    """The numbers P3 registered. P6 does not refit them."""

    return {
        "arms": list(ARMS),
        "b0": format(B0_MAX_SIMPLE_RETURN, "f"),
        "b1": format(B1_MAX_Z, "f"),
        "min_depth": format(MIN_DEPTH, "f"),
        "min_notional": format(MIN_NOTIONAL, "f"),
        "model": MODEL_VERSION,
        "penetration": format(PENETRATION, "f"),
        "risk": format(RISK_BUDGET, "f"),
        "spread_bps": format(MAX_SPREAD_BPS, "f"),
        "stop": format(PLANNED_STOP, "f"),
        "target": format(PRIMARY_NET_TARGET, "f"),
        "ttl_seconds": int(TTL.total_seconds()),
    }


def parameter_freeze() -> str:
    body = json.dumps(freeze_payload(), separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(body.encode()).hexdigest()


@dataclass(frozen=True)
class ShadowTape:
    """A quote and the bar that closed after the decision. Prices stay Decimal."""

    symbol: str
    arm: str
    source: str
    decision_ts: datetime
    exchange_ts: datetime
    capture_ts: datetime
    scored_ts: datetime
    bid: Decimal | None
    ask: Decimal | None
    bid_size: Decimal | None
    ask_size: Decimal | None
    queue_ahead: Decimal | None
    entry_bar: ClosedBar | None
    exit_bid: Decimal | None
    exit_ts: datetime | None
    multiplier_down: Decimal
    multiplier_up: Decimal
    equity: Decimal

    def __post_init__(self) -> None:
        if self.arm not in ARMS or self.source not in SOURCES or not self.symbol:
            raise ValueError("tape")
        if self.symbol.startswith("EQ_"):
            raise ValueError("tape")
        moments = (self.decision_ts, self.exchange_ts, self.capture_ts, self.scored_ts)
        if any(moment.tzinfo is None for moment in moments):
            raise ValueError("tape")
        if self.exit_ts is not None and self.exit_ts.tzinfo is None:
            raise ValueError("tape")
        if self.scored_ts < self.decision_ts:
            raise ValueError("tape")
        _non_negative(self.bid, "bid")
        _non_negative(self.ask, "ask")
        _non_negative(self.bid_size, "bid_size")
        _non_negative(self.ask_size, "ask_size")
        _non_negative(self.queue_ahead, "queue")
        _non_negative(self.exit_bid, "exit")
        if isinstance(self.equity, float) or self.equity <= 0:
            raise ValueError("tape")
        if isinstance(self.multiplier_down, float) or isinstance(self.multiplier_up, float):
            raise ValueError("tape")


@dataclass(frozen=True)
class Observation:
    dedup_key: str
    symbol: str
    arm: str
    decision_ts: str
    session_date: str
    session_label: str
    source: str
    quality: str
    bid: str | None
    ask: str | None
    bid_size: str | None
    ask_size: str | None
    queue_ahead: str | None
    limit_price: str | None
    quantity: str | None
    estimated_qty: str
    remaining_qty: str | None
    ttl_seconds: int
    quote_lag_ms: int | None
    status: str
    fill_rate_kind: str
    path: str
    path_bar_close: str | None
    exit_bid: str | None
    reason: str
    books_profit: bool
    exchange_fill_id: None
    freeze: str

    def as_dict(self) -> dict[str, object]:
        return {
            "arm": self.arm,
            "ask": self.ask,
            "ask_size": self.ask_size,
            "bid": self.bid,
            "bid_size": self.bid_size,
            "books_profit": self.books_profit,
            "decision_ts": self.decision_ts,
            "dedup_key": self.dedup_key,
            "estimated_qty": self.estimated_qty,
            "exchange_fill_id": self.exchange_fill_id,
            "exit_bid": self.exit_bid,
            "fill_rate_kind": self.fill_rate_kind,
            "freeze": self.freeze,
            "limit_price": self.limit_price,
            "path": self.path,
            "path_bar_close": self.path_bar_close,
            "quality": self.quality,
            "quantity": self.quantity,
            "queue_ahead": self.queue_ahead,
            "quote_lag_ms": self.quote_lag_ms,
            "reason": self.reason,
            "remaining_qty": self.remaining_qty,
            "session_date": self.session_date,
            "session_label": self.session_label,
            "source": self.source,
            "status": self.status,
            "symbol": self.symbol,
            "ttl_seconds": self.ttl_seconds,
        }


def observe(tape: ShadowTape, calendar: TradingCalendar, *, freeze: str) -> Observation:
    """Score one tape. Profit is never booked from this function."""

    if freeze != parameter_freeze():
        raise ValueError("freeze")
    label = calendar.label(tape.decision_ts)
    lag = _lag_ms(tape)
    base = _blank(tape, label, freeze, lag)
    if lag is None:
        return _held(base, "CLOCK", "CLOCK")
    if tape.capture_ts > tape.decision_ts:
        return _held(base, "NOT_YET_VISIBLE", "HELD")
    if label is SessionLabel.NON_SESSION:
        return _held(base, "CLOSED", "HELD")
    if label is not _ENTRY[tape.arm]:
        return _held(base, "ARM_SESSION", "OK")
    if lag > _STALE_MS:
        return _held(base, "STALE", "STALE")
    if tape.entry_bar is not None and _bar_hidden(tape):
        return _finish(
            base, "UNFILLED", "unobserved", "UNFILLABLE", "LOOKAHEAD", None, None, None, None
        )
    blocked = quote_block(
        ResearchQuote(
            tape.bid,
            tape.ask,
            tape.bid_size,
            tape.ask_size,
            tape.multiplier_down,
            tape.multiplier_up,
        )
    )
    if blocked is not None:
        return _finish(
            base, "UNFILLED", "unobserved", "UNFILLABLE", blocked, None, None, None, None
        )
    if tape.queue_ahead is None:
        return _finish(
            base, "UNFILLED", "unobserved", "UNFILLABLE", "QUEUE_UNKNOWN", None, None, None, None
        )
    assert tape.bid is not None
    sized = size_limit(tape.bid, tape.equity, tape.multiplier_down, tape.multiplier_up)
    if isinstance(sized, str):
        return _finish(base, "UNFILLED", "unobserved", "UNFILLABLE", sized, None, None, None, None)
    if tape.entry_bar is None:
        return _finish(
            base, "UNFILLED", "unobserved", "UNFILLABLE", "NO_BAR",
            format(sized.price, "f"), format(sized.quantity, "f"), None, None,
        )
    if tape.entry_bar.end > tape.decision_ts + TTL:
        return _finish(
            base, "UNFILLED", "unobserved", "UNFILLABLE", "TTL_EXPIRED",
            format(sized.price, "f"), format(sized.quantity, "f"), None, None,
        )
    capacity = tape.entry_bar.volume - tape.queue_ahead
    if capacity <= 0:
        return _finish(
            base, "UNFILLED", "unobserved", "UNFILLABLE", "QUEUE",
            format(sized.price, "f"), format(sized.quantity, "f"), "0", format(sized.quantity, "f"),
        )
    displayed = None if tape.queue_ahead == 0 else capacity
    view = match_limit(
        calendar,
        side=Side.BUY,
        limit=sized.price,
        bar=tape.entry_bar,
        placed_label=label.value,
        now_et=tape.scored_ts,
        displayed=displayed,
        remaining=sized.quantity,
    )
    if view.status == "FILLED":
        status, kind, reason = "FILLED_ESTIMATE", "estimate", "PENETRATED_ESTIMATE"
    elif view.status == "PARTIALLY_FILLED":
        status, kind, reason = "PARTIAL_ESTIMATE", "estimate", "PARTIAL_ESTIMATE"
    else:
        status, kind, reason = "UNFILLED", "unobserved", view.reason
    remaining = sized.quantity - view.qty
    path, exit_text, close_text = _path(tape, view.qty)
    return _finish(
        base,
        status,
        kind,
        path,
        reason,
        format(sized.price, "f"),
        format(sized.quantity, "f"),
        format(view.qty, "f"),
        format(remaining, "f"),
        exit_text,
        close_text,
    )


def estimate_interval(successes: int, trials: int) -> dict[str, str] | None:
    """Wilson interval on an estimate. Below the registered floor it is withheld."""

    if trials < MIN_RESOLVED or successes < 0 or successes > trials:
        return None
    count = Decimal(trials)
    rate = Decimal(successes) / count
    z2 = _Z95 * _Z95
    denom = Decimal(1) + z2 / count
    center = (rate + z2 / (Decimal(2) * count)) / denom
    margin = _Z95 * _sqrt(rate * (Decimal(1) - rate) / count + z2 / (Decimal(4) * count * count))
    margin = margin / denom
    low = max(Decimal(0), center - margin)
    high = min(Decimal(1), center + margin)
    return {
        "kind": "estimate",
        "low": format(low.quantize(_CI_QUANTUM), "f"),
        "high": format(high.quantize(_CI_QUANTUM), "f"),
    }


def live_session_dates(rows: Sequence[Observation]) -> tuple[str, ...]:
    """Fixture rows do not count toward the 20-session engineering floor."""

    found = {
        row.session_date
        for row in rows
        if row.source == "live_readonly" and row.quality == "OK"
    }
    return tuple(sorted(found))


def _blank(tape: ShadowTape, label: SessionLabel, freeze: str, lag: int | None) -> Observation:
    local = tape.decision_ts.astimezone(NEW_YORK)
    decision = tape.decision_ts.isoformat(timespec="seconds")
    exchange = tape.exchange_ts.isoformat(timespec="seconds")
    return Observation(
        dedup_key=f"{tape.source}|{tape.symbol}|{tape.arm}|{decision}|{exchange}",
        symbol=tape.symbol,
        arm=tape.arm,
        decision_ts=decision,
        session_date=local.date().isoformat(),
        session_label=label.value,
        source=tape.source,
        quality="OK" if lag is not None and lag <= _STALE_MS else "HELD",
        bid=_text(tape.bid),
        ask=_text(tape.ask),
        bid_size=_text(tape.bid_size),
        ask_size=_text(tape.ask_size),
        queue_ahead=_text(tape.queue_ahead),
        limit_price=None,
        quantity=None,
        estimated_qty="0",
        remaining_qty=None,
        ttl_seconds=int(TTL.total_seconds()),
        quote_lag_ms=lag,
        status="UNFILLED",
        fill_rate_kind="unobserved",
        path="UNFILLABLE",
        path_bar_close=None,
        exit_bid=None,
        reason="UNSCORED",
        books_profit=False,
        exchange_fill_id=None,
        freeze=freeze,
    )


def _held(base: Observation, reason: str, quality: str) -> Observation:
    row = _finish(base, "UNFILLED", "unobserved", "UNFILLABLE", reason, None, None, None, None)
    return _copy_quality(row, quality)


def _copy_quality(row: Observation, quality: str) -> Observation:
    return Observation(
        dedup_key=row.dedup_key,
        symbol=row.symbol,
        arm=row.arm,
        decision_ts=row.decision_ts,
        session_date=row.session_date,
        session_label=row.session_label,
        source=row.source,
        quality=quality,
        bid=row.bid,
        ask=row.ask,
        bid_size=row.bid_size,
        ask_size=row.ask_size,
        queue_ahead=row.queue_ahead,
        limit_price=row.limit_price,
        quantity=row.quantity,
        estimated_qty=row.estimated_qty,
        remaining_qty=row.remaining_qty,
        ttl_seconds=row.ttl_seconds,
        quote_lag_ms=row.quote_lag_ms,
        status=row.status,
        fill_rate_kind=row.fill_rate_kind,
        path=row.path,
        path_bar_close=row.path_bar_close,
        exit_bid=row.exit_bid,
        reason=row.reason,
        books_profit=False,
        exchange_fill_id=None,
        freeze=row.freeze,
    )


def _finish(
    base: Observation,
    status: str,
    kind: str,
    path: str,
    reason: str,
    limit: str | None,
    quantity: str | None,
    estimated: str | None,
    remaining: str | None,
    exit_bid: str | None = None,
    path_close: str | None = None,
) -> Observation:
    if kind == "estimate" and status not in {"FILLED_ESTIMATE", "PARTIAL_ESTIMATE"}:
        raise RuntimeError("estimate drifted")
    if status == "UNFILLED" and kind != "unobserved":
        raise RuntimeError("estimate drifted")
    return Observation(
        dedup_key=base.dedup_key,
        symbol=base.symbol,
        arm=base.arm,
        decision_ts=base.decision_ts,
        session_date=base.session_date,
        session_label=base.session_label,
        source=base.source,
        quality=base.quality,
        bid=base.bid,
        ask=base.ask,
        bid_size=base.bid_size,
        ask_size=base.ask_size,
        queue_ahead=base.queue_ahead,
        limit_price=limit,
        quantity=quantity,
        estimated_qty="0" if estimated is None else estimated,
        remaining_qty=remaining,
        ttl_seconds=base.ttl_seconds,
        quote_lag_ms=base.quote_lag_ms,
        status=status,
        fill_rate_kind=kind,
        path=path,
        path_bar_close=path_close,
        exit_bid=exit_bid,
        reason=reason,
        books_profit=False,
        exchange_fill_id=None,
        freeze=base.freeze,
    )


def _path(tape: ShadowTape, qty: Decimal) -> tuple[str, str | None, str | None]:
    close_text = None if tape.entry_bar is None else format(tape.entry_bar.close, "f")
    if qty <= 0:
        return "UNFILLABLE", None, close_text
    if (
        tape.exit_ts is None
        or tape.exit_bid is None
        or tape.exit_bid <= 0
        or tape.exit_ts < tape.decision_ts
        or tape.exit_ts > tape.scored_ts
    ):
        return "EXIT_FAILED", None, close_text
    return "PATH_RECORDED", format(tape.exit_bid, "f"), close_text


def _bar_hidden(tape: ShadowTape) -> bool:
    bar = tape.entry_bar
    if bar is None:
        return False
    if bar.symbol != tape.symbol:
        return True
    if bar.start < tape.decision_ts or bar.end > tape.scored_ts:
        return True
    return bar.end <= tape.decision_ts


def _lag_ms(tape: ShadowTape) -> int | None:
    if tape.exchange_ts > tape.capture_ts:
        return None
    return int((tape.capture_ts - tape.exchange_ts).total_seconds() * 1000)


def _text(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return format(value, "f")


def _non_negative(value: Decimal | None, name: str) -> None:
    if value is None:
        return
    if isinstance(value, float) or value < 0:
        raise ValueError(name)


def _sqrt(value: Decimal) -> Decimal:
    if value < 0:
        raise ValueError("sqrt")
    if value == 0:
        return Decimal(0)
    guess = value if value >= 1 else Decimal(1)
    quantum = Decimal("0.000000000001")
    for _ in range(60):
        nxt = (guess + value / guess) / 2
        if abs(nxt - guess) <= quantum:
            return nxt
        guess = nxt
    return guess
