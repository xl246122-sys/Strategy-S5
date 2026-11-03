"""S3 research chain. Every handoff appends an audit row and, when asked, a JSON line."""

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from bstocks_v2.constitution.guard import ConstitutionGuard
from bstocks_v2.contracts.models import TradeIntent
from bstocks_v2.execution_gateway.mock import MockExecutionGateway, MockOrder
from bstocks_v2.guardian.stage8 import mark_exit
from bstocks_v2.limit_optimizer.stage8 import LimitPlan, bind_limit, replace_limit
from bstocks_v2.portfolio_risk.stage8 import (
    RiskBook,
    note_fill,
    note_flat,
    release_pending,
    reserve,
)
from bstocks_v2.signal_arbitrator.stage8 import arbitrate
from bstocks_v2.strategies.s3.spec import ACCOUNT_EQUITY


@dataclass(frozen=True)
class AuditRow:
    stage: str
    state: str
    symbol: str
    detail: str


@dataclass(frozen=True)
class ChainResult:
    state: str
    reason: str
    client_order_id: str | None
    limit_price: Decimal | None
    quantity: Decimal | None
    open_positions: int
    pending_buys: int
    audit: tuple[AuditRow, ...]


class Stage8Session:
    """Signal, arbitrator, limit, risk, mock gateway, and guardian. No network."""

    def __init__(self, log_path: Path | None = None) -> None:
        self.log_path = log_path
        self.guard = ConstitutionGuard()
        self.book = RiskBook(equity=ACCOUNT_EQUITY, hwm=ACCOUNT_EQUITY)
        self.gateway = MockExecutionGateway()
        self.audit: list[AuditRow] = []
        self._plans: dict[str, LimitPlan] = {}
        self._states: dict[str, str] = {}
        if log_path is not None:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text("", encoding="utf-8")

    def submit(
        self,
        intent: TradeIntent,
        *,
        now_et: datetime,
        is_half_day: bool,
    ) -> ChainResult:
        start = len(self.audit)
        decision = arbitrate(intent)
        self._row(
            "arbitrator", "PASS" if decision.accepted else "REJECT", intent.symbol, decision.reason
        )
        if not decision.accepted:
            return self._done("REJECTED", decision.reason, None, None, None, start)
        plan = bind_limit(intent)
        chased = replace_limit(plan, plan.ceiling + Decimal("0.01"))
        if not chased.refused_chase or chased.limit != plan.limit:
            raise RuntimeError("the ceiling probe was allowed to raise the bid")
        self._plans[intent.intent_id] = plan
        self._row("limit_optimizer", "LIMIT_SET", intent.symbol, format(plan.limit, "f"))
        client_id = _client_order_id(intent.intent_id)
        reserved = reserve(
            self.book,
            intent,
            client_order_id=client_id,
            limit=plan.limit,
            now_et=now_et,
            is_half_day=is_half_day,
            guard=self.guard,
        )
        self._row(
            "portfolio_risk",
            "SLOT_RESERVED" if reserved.accepted else "REJECT",
            intent.symbol,
            reserved.reason,
        )
        if not reserved.accepted or reserved.sized is None:
            return self._done("REJECTED", reserved.reason, None, plan.limit, None, start)
        order = self.gateway.accept(reserved.sized)
        self._states[order.client_order_id] = "ENTRY_WORKING"
        self._row("gateway", "ORDER_ACCEPTED", order.symbol, order.client_order_id)
        self._row("guardian", "ENTRY_WORKING", order.symbol, order.client_order_id)
        return self._done(
            "ENTRY_WORKING",
            "ORDER_ACCEPTED",
            order.client_order_id,
            order.price,
            order.quantity,
            start,
        )

    def fill(self, client_order_id: str) -> str:
        order = self._order(client_order_id)
        if self._states[client_order_id] != "ENTRY_WORKING":
            raise RuntimeError("fill requires a working entry")
        order.status = "FILLED"
        note_fill(self.book, order.symbol)
        self._states[client_order_id] = "OPEN"
        self._row("gateway", "FILLED", order.symbol, client_order_id)
        self._row("guardian", "OPEN", order.symbol, client_order_id)
        return "OPEN"

    def expire(self, client_order_id: str) -> str:
        order = self._order(client_order_id)
        if self._states[client_order_id] != "ENTRY_WORKING":
            raise RuntimeError("expire requires a working entry")
        order.status = "EXPIRED"
        release_pending(self.book, order.symbol)
        self._states[client_order_id] = "FLAT"
        self._row("gateway", "EXPIRED", order.symbol, client_order_id)
        self._row("guardian", "FLAT", order.symbol, "TTL")
        return "FLAT"

    def exit(
        self,
        client_order_id: str,
        reason: str,
        *,
        now_et: datetime,
        is_half_day: bool,
    ) -> str:
        order = self._order(client_order_id)
        state = mark_exit(
            self._states[client_order_id], reason, now_et=now_et, is_half_day=is_half_day
        )
        order.status = state
        note_flat(self.book, order.symbol)
        self._states[client_order_id] = state
        self._row("guardian", state, order.symbol, reason)
        return state

    def _order(self, client_order_id: str) -> MockOrder:
        order = self.gateway.orders.get(client_order_id)
        if order is None or client_order_id not in self._states:
            raise KeyError(client_order_id)
        return order

    def _done(
        self,
        state: str,
        reason: str,
        client_order_id: str | None,
        limit_price: Decimal | None,
        quantity: Decimal | None,
        start: int,
    ) -> ChainResult:
        return ChainResult(
            state=state,
            reason=reason,
            client_order_id=client_order_id,
            limit_price=limit_price,
            quantity=quantity,
            open_positions=self.book.open_positions,
            pending_buys=self.book.pending_buys,
            audit=tuple(self.audit[start:]),
        )

    def _row(self, stage: str, state: str, symbol: str, detail: str) -> None:
        row = AuditRow(stage, state, symbol, detail)
        self.audit.append(row)
        if self.log_path is None:
            return
        line = json.dumps(
            {"stage": stage, "state": state, "symbol": symbol, "detail": detail},
            ensure_ascii=False,
        )
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")


def _client_order_id(intent_id: str) -> str:
    digest = hashlib.sha256(intent_id.encode("utf-8")).hexdigest()
    return f"v22{digest[:32]}"
