"""Shared S1–S4 arbitrator. It ranks intents and never sends an order."""

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from pydantic import BaseModel, ConfigDict, model_validator

from bstocks_v2.constitution.guard import MAX_FRICTION_RATIO, MAX_OPEN_POSITIONS, NEW_YORK
from bstocks_v2.contracts.decimals import Money, Price
from bstocks_v2.contracts.models import TradeIntent
from bstocks_v2.market_ingestor.staleness import KLINE_STALE_MS
from bstocks_v2.regime.classifier import SECTORS
from bstocks_v2.signal_arbitrator.spec import (
    ADMITTED,
    ASSUMED_HALF_SPREAD,
    COMMISSION_RATE,
    COOLDOWN,
    MAX_PER_SECTOR,
    SCORE_QUANTUM,
)
from bstocks_v2.strategies.s1.spec import SECTOR_PARENT
from bstocks_v2.strategies.s4.lock import LIVE_ENABLED

_HALTED = frozenset({"TRADING_HALTED", "TRADING_PAUSED"})
_SortKey = tuple[Decimal, Decimal, Decimal, Decimal, str, str]


class Holding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    symbol: str
    sector: str


class SlotBook(BaseModel):
    """Open longs and pending buys. Each pending buy already occupies one slot."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    open_positions: tuple[Holding, ...] = ()
    pending_buys: tuple[Holding, ...] = ()

    @model_validator(mode="after")
    def _slots_match_the_map(self) -> "SlotBook":
        seen: set[str] = set()
        holdings = (*self.open_positions, *self.pending_buys)
        if len(holdings) > MAX_OPEN_POSITIONS:
            raise ValueError("book exceeds 5 slots")
        for item in holdings:
            if item.symbol in seen:
                raise ValueError("a symbol cannot occupy two slots")
            seen.add(item.symbol)
            parent = SECTOR_PARENT.get(item.symbol)
            if parent is None or parent not in SECTORS or item.sector != parent:
                raise ValueError("holding sector must match the mapped parent")
        return self

    @property
    def available_slots(self) -> int:
        return MAX_OPEN_POSITIONS - len(self.open_positions) - len(self.pending_buys)


class Candidate(BaseModel):
    """One strategy intent plus the quote context the intent does not carry."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    intent: TradeIntent
    bar_close_time: datetime
    sector: str
    tradability: str = "BUY_SELL"
    trading_status: str = "TRADING"
    half_spread: Price = Decimal("0.01")
    notional_usdc: Money = Decimal("320")

    @model_validator(mode="after")
    def _bar_matches_the_intent(self) -> "Candidate":
        if self.bar_close_time.tzinfo is None:
            raise ValueError("bar_close_time must be timezone-aware")
        if self.bar_close_time != self.intent.data_asof_utc:
            raise ValueError("bar_close_time must equal data_asof_utc")
        if self.notional_usdc <= 0:
            raise ValueError("notional must be positive")
        if self.half_spread < 0:
            raise ValueError("half_spread cannot be negative")
        return self


@dataclass(frozen=True)
class AuditRecord:
    event: str
    reason_code: str
    symbol: str
    strategy_id: str
    intent_id: str
    bar_close_time: str
    detail: str

    def as_dict(self) -> dict[str, str]:
        return {
            "event": self.event,
            "reason_code": self.reason_code,
            "symbol": self.symbol,
            "strategy_id": self.strategy_id,
            "intent_id": self.intent_id,
            "bar_close_time": self.bar_close_time,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class ArbitrationResult:
    allocated: tuple[TradeIntent, ...]
    records: tuple[AuditRecord, ...]
    available_slots: int
    anchors: tuple[tuple[str, datetime], ...]


@dataclass(frozen=True)
class _Scored:
    candidate: Candidate
    risk_bps: Decimal
    risk_adjusted: Decimal
    interval_lower: Decimal
    sort_key: _SortKey


class SignalArbitrator:
    """One cross-section in, at most one intent per symbol out. No order route."""

    def __init__(self, *, log_path: Path | None = None, cooldown: timedelta = COOLDOWN) -> None:
        if cooldown < timedelta(0):
            raise ValueError("cooldown cannot be negative")
        self.cooldown = cooldown
        self.log_path = log_path
        self._anchors: dict[str, datetime] = {}
        if log_path is not None:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            if not log_path.exists():
                log_path.touch()
            self._load()

    def arbitrate(
        self,
        candidates: tuple[Candidate, ...] | list[Candidate],
        book: SlotBook,
        *,
        is_half_day: bool,
        production_mode: bool,
    ) -> ArbitrationResult:
        _unique_ids(candidates)
        gated: list[AuditRecord] = []
        eligible: list[_Scored] = []
        for candidate in candidates:
            reason = _gate(
                candidate, book, is_half_day=is_half_day, production_mode=production_mode
            )
            if reason is None:
                eligible.append(_score(candidate))
            else:
                code, detail = reason
                gated.append(_record("REJECT", code, candidate, detail))
        winners, same_bar = _collapse(eligible)
        kept, cooled = _one_per_symbol(winners, self._anchors, self.cooldown)
        allocated, crowded = _fill(kept, book)
        for item in allocated:
            self._anchors[item.candidate.intent.symbol] = item.candidate.bar_close_time
        chosen = tuple(_allocate_record(item, rank) for rank, item in enumerate(allocated, start=1))
        ordered_gates = tuple(sorted(gated, key=_audit_key))
        records = (*ordered_gates, *same_bar, *cooled, *crowded, *chosen)
        self._write(records)
        anchors = tuple(sorted(self._anchors.items()))
        return ArbitrationResult(
            allocated=tuple(item.candidate.intent for item in allocated),
            records=records,
            available_slots=book.available_slots,
            anchors=anchors,
        )

    def _load(self) -> None:
        if self.log_path is None:
            return
        latest: dict[str, datetime] = {}
        for line in self.log_path.read_text(encoding="utf-8").splitlines():
            if not line:
                continue
            payload = json.loads(line)
            if not isinstance(payload, dict):
                continue
            event = payload.get("event")
            symbol = payload.get("symbol")
            stamp = payload.get("bar_close_time")
            if event != "ALLOCATE" or not isinstance(symbol, str) or not isinstance(stamp, str):
                continue
            latest[symbol] = datetime.fromisoformat(stamp)
        self._anchors = latest

    def _write(self, records: tuple[AuditRecord, ...]) -> None:
        if self.log_path is None or not records:
            return
        with self.log_path.open("a", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record.as_dict(), ensure_ascii=False) + "\n")


def net_edge_blocked(net_bps: Decimal, ratio: Decimal) -> bool:
    """Non-positive net, or round-trip friction strictly above 20% of target gross."""

    return net_bps <= 0 or ratio > MAX_FRICTION_RATIO


def round_trip_friction_ratio(limit: Decimal, take_profit: Decimal) -> Decimal:
    """Commission both ways plus the frozen half-spread, divided by target gross."""

    gross = take_profit - limit
    if gross <= 0:
        raise ValueError("take profit must sit above the limit")
    friction = COMMISSION_RATE * (limit + take_profit) + ASSUMED_HALF_SPREAD * 2
    return friction / gross


def _unique_ids(candidates: tuple[Candidate, ...] | list[Candidate]) -> None:
    seen: set[str] = set()
    for candidate in candidates:
        intent_id = candidate.intent.intent_id
        if intent_id in seen:
            raise ValueError("duplicate intent_id")
        seen.add(intent_id)


def _gate(
    candidate: Candidate,
    book: SlotBook,
    *,
    is_half_day: bool,
    production_mode: bool,
) -> tuple[str, str] | None:
    intent = candidate.intent
    fresh = intent.data_freshness_ms
    if fresh > KLINE_STALE_MS:
        return "DATA_STALE", f"data_freshness_ms={fresh};limit={KLINE_STALE_MS}"
    if not intent.vwap_intact:
        return "VWAP_GAP_UNRECOVERED", "vwap_intact=false"
    tradable = candidate.tradability == "BUY_SELL" and candidate.trading_status not in _HALTED
    if not tradable:
        return (
            "SYMBOL_NOT_TRADABLE",
            f"tradability={candidate.tradability};trading_status={candidate.trading_status}",
        )
    if intent.regime in {"UNKNOWN", "HIGH_VOL_PANIC"}:
        return "MARKET_REGIME_BLOCKED", f"regime={intent.regime}"
    session = _session_block(candidate.bar_close_time, is_half_day)
    if session is not None:
        return session, session
    if intent.strategy_id not in ADMITTED:
        return "STRATEGY_NOT_ADMITTED", intent.strategy_id
    if intent.strategy_id == "S4" and (production_mode or LIVE_ENABLED is not False):
        return "STRATEGY_LIVE_DISABLED", "S4"
    parent = SECTOR_PARENT.get(intent.symbol)
    if parent is None or parent not in SECTORS:
        return "SECTOR_UNKNOWN", intent.symbol
    if candidate.sector != parent:
        return "SECTOR_MISSTATED", f"stated={candidate.sector};parent={parent}"
    if any(item.symbol == intent.symbol for item in (*book.open_positions, *book.pending_buys)):
        return "SYMBOL_ALREADY_RESERVED", "open_or_pending"
    ratio = round_trip_friction_ratio(intent.entry_price_ceiling, intent.take_profit_price)
    if net_edge_blocked(intent.expected_net_bps, ratio):
        clauses: list[str] = []
        if intent.expected_net_bps <= 0:
            clauses.append("net_bps<=0")
        if ratio > MAX_FRICTION_RATIO:
            clauses.append("friction_ratio>0.20")
        detail = f"{','.join(clauses)};ratio={_fmt(ratio)};net_bps={_fmt(intent.expected_net_bps)}"
        return "NEGATIVE_OR_INSUFFICIENT_NET_EDGE", detail
    if intent.rejection_reasons:
        return intent.rejection_reasons[0], "intent_rejection"
    return None


def _session_block(moment: datetime, is_half_day: bool) -> str | None:
    local = moment.astimezone(NEW_YORK)
    if local.weekday() >= 5:
        return "NON_RTH"
    hour, minute = (12, 45) if is_half_day else (15, 45)
    cutoff = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    opened = local.replace(hour=9, minute=30, second=0, microsecond=0)
    if local >= cutoff:
        return "PAST_ENTRY_CUTOFF_TIME"
    if local < opened:
        return "NON_RTH"
    return None


def _score(candidate: Candidate) -> _Scored:
    intent = candidate.intent
    limit = intent.entry_price_ceiling
    risk_bps = (limit - intent.stop_price) / limit * Decimal(10000)
    adjusted = intent.expected_net_bps / risk_bps
    lower = intent.prediction_interval[0]
    key: _SortKey = (
        -adjusted,
        -lower,
        candidate.half_spread,
        candidate.notional_usdc,
        intent.strategy_id,
        intent.intent_id,
    )
    return _Scored(candidate, risk_bps, adjusted, lower, key)


def _collapse(eligible: list[_Scored]) -> tuple[list[_Scored], tuple[AuditRecord, ...]]:
    groups: dict[tuple[str, datetime], list[_Scored]] = {}
    for item in eligible:
        stamp = item.candidate.bar_close_time
        groups.setdefault((item.candidate.intent.symbol, stamp), []).append(item)
    winners: list[_Scored] = []
    eliminated: list[AuditRecord] = []
    order = sorted(groups, key=lambda item: (_utc(item[1]), item[0]))
    for key in order:
        members = groups[key]
        winner = min(members, key=lambda item: item.sort_key)
        winners.append(winner)
        ranked = sorted(members, key=lambda item: item.sort_key)
        for loser in ranked:
            if loser.candidate.intent.intent_id == winner.candidate.intent.intent_id:
                continue
            eliminated.append(_same_bar(loser, winner))
    return winners, tuple(eliminated)


def _one_per_symbol(
    winners: list[_Scored],
    anchors: dict[str, datetime],
    cooldown: timedelta,
) -> tuple[list[_Scored], tuple[AuditRecord, ...]]:
    by_symbol: dict[str, list[_Scored]] = {}
    for item in winners:
        by_symbol.setdefault(item.candidate.intent.symbol, []).append(item)
    kept: list[_Scored] = []
    records: list[AuditRecord] = []
    for symbol in sorted(by_symbol):
        series = sorted(by_symbol[symbol], key=lambda item: _utc(item.candidate.bar_close_time))
        anchor = anchors.get(symbol)
        chosen: _Scored | None = None
        for item in series:
            moment = item.candidate.bar_close_time
            if anchor is not None and _utc(moment) - _utc(anchor) <= cooldown:
                records.append(_cooled(item, anchor, cooldown))
                continue
            if chosen is None:
                chosen = item
                anchor = moment
                continue
            gap = _utc(moment) - _utc(chosen.candidate.bar_close_time)
            if gap <= cooldown:
                records.append(_cooled(item, chosen.candidate.bar_close_time, cooldown))
            else:
                detail = f"kept={chosen.candidate.intent.intent_id}"
                records.append(
                    _record("ELIMINATE", "SAME_SYMBOL_SUPERSEDED", item.candidate, detail)
                )
        if chosen is not None:
            kept.append(chosen)
    return kept, tuple(records)


def _fill(kept: list[_Scored], book: SlotBook) -> tuple[list[_Scored], tuple[AuditRecord, ...]]:
    available = book.available_slots
    counts: dict[str, int] = {}
    for holding in (*book.open_positions, *book.pending_buys):
        counts[holding.sector] = counts.get(holding.sector, 0) + 1
    records: list[AuditRecord] = []
    allocated: list[_Scored] = []
    for item in sorted(kept, key=lambda row: row.sort_key):
        if len(allocated) >= available:
            code = "NO_AVAILABLE_SLOT" if available == 0 else "OUTRANKED_FOR_SLOT"
            detail = f"filled={len(allocated)};available={available}"
            records.append(_record("ELIMINATE", code, item.candidate, detail))
            continue
        sector = item.candidate.sector
        if counts.get(sector, 0) >= MAX_PER_SECTOR:
            detail = f"sector={sector};cap={MAX_PER_SECTOR}"
            records.append(_record("ELIMINATE", "SECTOR_CONCENTRATION", item.candidate, detail))
            continue
        allocated.append(item)
        counts[sector] = counts.get(sector, 0) + 1
    return allocated, tuple(records)


def _same_bar(loser: _Scored, winner: _Scored) -> AuditRecord:
    detail = ";".join(
        (
            f"winner={winner.candidate.intent.intent_id}",
            f"edge={_fmt(loser.risk_adjusted)}",
            f"winner_edge={_fmt(winner.risk_adjusted)}",
            f"lower={_fmt(loser.interval_lower)}",
            f"winner_lower={_fmt(winner.interval_lower)}",
        )
    )
    return _record("ELIMINATE", "SAME_BAR_OUTRANKED", loser.candidate, detail)


def _cooled(item: _Scored, anchor: datetime, cooldown: timedelta) -> AuditRecord:
    seconds = int(cooldown.total_seconds())
    detail = f"anchor={_stamp(anchor)};cooldown_seconds={seconds}"
    return _record("ELIMINATE", "COOLDOWN_DUPLICATE", item.candidate, detail)


def _allocate_record(item: _Scored, rank: int) -> AuditRecord:
    detail = ";".join(
        (
            f"edge={_fmt(item.risk_adjusted)}",
            f"risk_bps={_fmt(item.risk_bps)}",
            f"lower={_fmt(item.interval_lower)}",
            f"sector={item.candidate.sector}",
            f"half_spread={_fmt(item.candidate.half_spread)}",
            f"notional={_fmt(item.candidate.notional_usdc)}",
            f"rank={rank}",
        )
    )
    return _record("ALLOCATE", "ALLOCATED", item.candidate, detail)


def _record(event: str, reason: str, candidate: Candidate, detail: str) -> AuditRecord:
    intent = candidate.intent
    return AuditRecord(
        event,
        reason,
        intent.symbol,
        intent.strategy_id,
        intent.intent_id,
        _stamp(candidate.bar_close_time),
        detail,
    )


def _audit_key(record: AuditRecord) -> tuple[str, str, str, str]:
    return (record.bar_close_time, record.symbol, record.strategy_id, record.intent_id)


def _fmt(value: Decimal) -> str:
    return format(value.quantize(SCORE_QUANTUM, rounding=ROUND_HALF_UP), "f")


def _stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat()


def _utc(moment: datetime) -> datetime:
    return moment.astimezone(UTC)
