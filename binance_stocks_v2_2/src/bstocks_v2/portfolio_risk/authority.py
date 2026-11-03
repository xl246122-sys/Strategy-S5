"""Account-level risk authority. Live placement stays closed."""

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import TypeVar

from bstocks_v2.constitution.guard import (
    CLIENT_ORDER_ID,
    NEW_YORK,
    SESSION_POLICY_GLOBAL,
    SESSION_POLICY_S5_RESEARCH,
    ConstitutionGuard,
    live_trade_enabled,
)
from bstocks_v2.contracts.decimals import parse_decimal
from bstocks_v2.contracts.errors import FeeTrapError, LiveTradingDisabledError, OrderRejected
from bstocks_v2.contracts.models import OrderDraft, OrderType, Side, TradingSession
from bstocks_v2.portfolio_risk.equity import (
    OrderMark,
    PositionMark,
    assert_identity,
    liquidation_value,
    locked_principal,
)
from bstocks_v2.portfolio_risk.spec import (
    CODE_ASSET,
    CODE_PDT,
    COMMISSION_RATE,
    HALF_SPREAD,
    HWM_RETAINED,
    MAX_OPEN_POSITIONS,
    MAX_POSITION_FRACTION,
    MIN_VIABLE_NOTIONAL,
    OPEN_BUY_STATUSES,
    PDT_MAX_DAY_TRADES,
    PDT_WINDOW_SESSIONS,
    PLANNED_EQUITY,
    RISK_FRACTION,
    UNLOCK_PHRASE,
)
from bstocks_v2.portfolio_risk.store import (
    OrderRow,
    PositionRow,
    RiskStore,
    SignedText,
    sign_text,
    sign_unlock,
    signatures_match,
)
from bstocks_v2.reconciler.ledger import Reconciliation


@dataclass(frozen=True)
class EntryRequest:
    symbol: str
    client_order_id: str
    limit_price: Decimal
    quantity: Decimal
    stop_price: Decimal
    take_profit_price: Decimal
    now_et: datetime
    is_half_day: bool = False
    trading_session: TradingSession = TradingSession.RTH


@dataclass(frozen=True)
class EntryDecision:
    accepted: bool
    reason: str
    client_order_id: str
    equity: Decimal
    slots: int


@dataclass(frozen=True)
class OrderView:
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
class PositionView:
    symbol: str
    quantity: Decimal
    cost_notional: Decimal
    bid: Decimal
    opened_session: str


@dataclass(frozen=True)
class EquityView:
    equity: Decimal
    available: Decimal
    locked: Decimal
    liquidation: Decimal
    basis: Decimal
    opening: Decimal
    deposits: Decimal
    hwm: Decimal
    halt_new_entries: bool
    halt_reason: str
    halt_transition_ms: int | None
    slots: int
    generation: int
    buy_ban_code: str
    orders: tuple[OrderView, ...]
    positions: tuple[PositionView, ...]


_T = TypeVar("_T")


class RecordingGateway:
    """Mock cancel port. It has no HTTP client."""

    def __init__(self) -> None:
        self.cancels: list[tuple[str, str]] = []

    def cancel_buy(self, client_order_id: str, symbol: str) -> None:
        self.cancels.append((client_order_id, symbol))


class PortfolioRisk:
    def __init__(
        self,
        store: RiskStore,
        gateway: RecordingGateway,
        guard: ConstitutionGuard,
    ) -> None:
        self._store = store
        self._gateway = gateway
        self._guard = guard
        self._calendar_days: list[date] | None = None

    @classmethod
    def open(
        cls,
        root: Path,
        *,
        verified_equity: Decimal | None = None,
        code_version: str = "stage13",
        pdt_enabled: bool = True,
        pdt_max_day_trades: int = PDT_MAX_DAY_TRADES,
        gateway: RecordingGateway | None = None,
    ) -> "PortfolioRisk":
        if live_trade_enabled():
            raise LiveTradingDisabledError("portfolio risk")
        store = RiskStore(root)
        port = gateway if gateway is not None else RecordingGateway()
        book = cls(store, port, ConstitutionGuard())
        try:
            book._boot(
                verified_equity=PLANNED_EQUITY if verified_equity is None else verified_equity,
                code_version=code_version,
                pdt_enabled=pdt_enabled,
                pdt_max_day_trades=pdt_max_day_trades,
            )
        except Exception:
            store.close()
            raise
        return book

    def close(self) -> None:
        self._store.close()

    def live_place(self) -> None:
        self._guard.check_endpoint("POST", "https://api.binance.com/sapi/v1/equity/order/place")

    def live_cancel(self) -> None:
        self._guard.check_endpoint("POST", "https://api.binance.com/sapi/v1/equity/order/cancel")

    def note_pid(self, pid: int) -> None:
        self._run(lambda: self._store.set_meta("pid", str(pid)))

    def command(self) -> str:
        found = self._store.meta("command")
        return "" if found is None else found

    def snapshot(self) -> EquityView:
        return self._run(self._view)

    def alerts(self) -> list[tuple[str, str, str]]:
        return self._run(self._store.alerts)

    def cancellations(self) -> list[tuple[str, str]]:
        return self._run(self._store.cancels)

    def day_trade_count(self, day: date) -> int:
        def read() -> int:
            return self._count_day_trades(day)

        return self._run(read)

    def approve(
        self,
        request: EntryRequest,
        *,
        session_policy: str = SESSION_POLICY_GLOBAL,
    ) -> EntryDecision:
        cancels: list[tuple[str, str]] = []

        def decide() -> EntryDecision:
            cancels.extend(self._maybe_halt(request.now_et))
            view = self._view()
            reason = self._reject_reason(request, view)
            if reason is not None:
                if reason == "PDT_LIMIT" and "PDT_LIMIT" not in self._store.alert_codes():
                    self._store.add_alert(
                        "CRITICAL",
                        "PDT_LIMIT",
                        request.symbol,
                        _stamp(request.now_et),
                    )
                return EntryDecision(
                    False, reason, request.client_order_id, view.equity, view.slots
                )
            try:
                self._guard.check_order(
                    _draft(
                        request,
                        research_only=session_policy == SESSION_POLICY_S5_RESEARCH,
                    ),
                    now_et=request.now_et,
                    is_half_day=request.is_half_day,
                    open_positions=view.slots,
                    pending_buys=0,
                    equity=view.equity,
                    hwm=view.hwm,
                    entries_halted=view.halt_new_entries,
                    session_policy=session_policy,
                )
            except OrderRejected as exc:
                return EntryDecision(
                    False, exc.reason, request.client_order_id, view.equity, view.slots
                )
            self._store.replace_order(
                OrderRow(
                    request.client_order_id,
                    request.symbol,
                    "NEW",
                    request.limit_price,
                    request.quantity,
                    Decimal(0),
                    Decimal(0),
                    None,
                    None,
                )
            )
            fresh = self._view()
            return EntryDecision(
                True, "ACCEPTED", request.client_order_id, fresh.equity, fresh.slots
            )

        decision = self._run(decide)
        self._dispatch(cancels)
        return decision

    def cancel_unfilled(self, client_order_id: str) -> None:
        """Drop a research buy that never traded. A partial or a fill stays on the book."""

        def apply() -> None:
            found = _order_map(self._store.orders()).get(client_order_id)
            if found is None:
                raise OrderRejected("UNKNOWN_ORDER")
            if found.status == "CANCELED":
                return
            if found.status == "FILLED" or found.filled_qty > 0:
                raise OrderRejected("PARTIAL_OPEN")
            self._store.replace_order(
                OrderRow(
                    found.client_order_id,
                    found.symbol,
                    "CANCELED",
                    found.limit_price,
                    found.quantity,
                    found.filled_qty,
                    found.filled_notional,
                    found.total_cost_raw,
                    found.rest_fee,
                )
            )

        self._run(apply)

    def on_buy_report(
        self,
        client_order_id: str,
        *,
        status: str,
        filled_qty: Decimal,
        filled_notional: Decimal,
        total_cost_raw: Decimal | None,
        rest_fee: Decimal | None,
        bid: Decimal,
        now_et: datetime,
    ) -> EquityView:
        cancels: list[tuple[str, str]] = []

        def apply() -> EquityView:
            self._apply_fill(
                client_order_id,
                status=status,
                filled_qty=filled_qty,
                filled_notional=filled_notional,
                total_cost_raw=total_cost_raw,
                rest_fee=rest_fee,
                bid=bid,
                now_et=now_et,
            )
            cancels.extend(self._maybe_halt(now_et))
            return self._view()

        view = self._run(apply)
        self._dispatch(cancels)
        return view

    def mark(self, bids: dict[str, Decimal], now_et: datetime) -> EquityView:
        cancels: list[tuple[str, str]] = []

        def apply() -> EquityView:
            for position in self._store.positions():
                if position.symbol not in bids:
                    continue
                self._guard.check_price(bids[position.symbol])
                self._store.replace_position(
                    PositionRow(
                        position.symbol,
                        position.quantity,
                        position.cost_notional,
                        bids[position.symbol],
                        position.opened_session,
                    )
                )
            cancels.extend(self._maybe_halt(now_et))
            return self._view()

        view = self._run(apply)
        self._dispatch(cancels)
        return view

    def reduce_position(
        self,
        symbol: str,
        quantity: Decimal,
        price: Decimal,
        now_et: datetime,
    ) -> EquityView:
        """Guardian flatten. The halt flag does not block a long reduction."""

        cancels: list[tuple[str, str]] = []

        def apply() -> EquityView:
            self._guard.check_price(price)
            if quantity <= 0:
                raise OrderRejected("FILL_QUANTITY")
            position = _position_map(self._store.positions()).get(symbol)
            if position is None or quantity > position.quantity:
                raise OrderRejected("SHORT")
            proceeds = quantity * price
            fee = COMMISSION_RATE * proceeds
            basis = _required_decimal(self._store, "basis") + proceeds - fee
            self._store.set_meta("basis", format(basis, "f"))
            sells = int(_required(self._store, "sells")) + 1
            self._store.set_meta("sells", str(sells))
            remaining = position.quantity - quantity
            session = _session_day(now_et).isoformat()
            if position.opened_session == session:
                self._store.add_day_trade(symbol, session, _stamp(now_et))
            if remaining == 0:
                self._store.delete_position(symbol)
            else:
                cost_cut = position.cost_notional * quantity / position.quantity
                self._store.replace_position(
                    PositionRow(
                        symbol,
                        remaining,
                        position.cost_notional - cost_cut,
                        price,
                        position.opened_session,
                    )
                )
            cancels.extend(self._maybe_halt(now_et))
            return self._view()

        view = self._run(apply)
        self._dispatch(cancels)
        return view

    def credit_verified_deposit(self, amount: Decimal, now_et: datetime) -> EquityView:
        if amount <= 0:
            raise OrderRejected("DEPOSIT")

        cancels: list[tuple[str, str]] = []

        def apply() -> EquityView:
            basis = _required_decimal(self._store, "basis") + amount
            deposits = _required_decimal(self._store, "deposits") + amount
            self._store.set_meta("basis", format(basis, "f"))
            self._store.set_meta("deposits", format(deposits, "f"))
            cancels.extend(self._maybe_halt(now_et))
            return self._view()

        view = self._run(apply)
        self._dispatch(cancels)
        return view

    def on_broker_code(self, code: int, now_et: datetime) -> EquityView:
        if code not in {CODE_PDT, CODE_ASSET}:
            return self.snapshot()

        def apply() -> EquityView:
            day = _session_day(now_et).isoformat()
            self._store.set_meta("buy_ban_session", day)
            self._store.set_meta("buy_ban_code", str(code))
            self._store.add_alert("CRITICAL", str(code), day, _stamp(now_et))
            return self._view()

        return self._run(apply)

    def absorb_audit(self, reconciliation: Reconciliation, now_et: datetime) -> EquityView:
        if reconciliation.matched and not reconciliation.halt_new_entries:
            return self.snapshot()
        cancels: list[tuple[str, str]] = []

        def apply() -> EquityView:
            detail = ",".join(reconciliation.quantity_breaks) or "UNMATCHED"
            cancels.extend(self._force_halt("RECONCILE_BREAK", detail, now_et))
            return self._view()

        view = self._run(apply)
        self._dispatch(cancels)
        return view

    def unlock(self, confirmation: str, signature: str, now_et: datetime) -> EquityView:
        def apply() -> EquityView:
            view = self._view()
            if confirmation != UNLOCK_PHRASE:
                raise OrderRejected("UNLOCK_CONFIRMATION")
            expected = sign_unlock(
                self._store.unlock_key(),
                UNLOCK_PHRASE,
                format(view.hwm, "f"),
                view.generation,
            )
            if not signatures_match(expected, signature):
                raise OrderRejected("UNLOCK_SIGNATURE")
            if not view.halt_new_entries:
                return view
            if view.equity <= view.hwm * HWM_RETAINED:
                raise OrderRejected("HWM_STILL_BREACHED")
            self._store.save_signed(view.hwm, False, view.generation + 1)
            self._store.set_meta("halt_reason", "")
            self._store.add_alert("CRITICAL", "HWM_UNLOCK", format(view.hwm, "f"), _stamp(now_et))
            return self._view()

        return self._run(apply)

    def _boot(
        self,
        *,
        verified_equity: Decimal,
        code_version: str,
        pdt_enabled: bool,
        pdt_max_day_trades: int,
    ) -> None:
        opening = parse_decimal(verified_equity)
        if opening <= 0:
            raise OrderRejected("EQUITY")
        if self._store.meta("hmac_key") is None:
            self._create(opening, code_version, pdt_enabled, pdt_max_day_trades)
            return
        cancels: list[tuple[str, str]] = []

        def repair() -> None:
            self._repair_signed()
            self._store.set_meta("code_version", code_version)
            if self._store.meta("halt") == "1":
                cancels.extend(self._cancel_open_buys(datetime.now(tz=UTC)))

        self._run(repair)
        self._dispatch(cancels)

    def _create(
        self,
        opening: Decimal,
        code_version: str,
        pdt_enabled: bool,
        pdt_max_day_trades: int,
    ) -> None:
        if pdt_max_day_trades < 1:
            raise OrderRejected("PDT_MAX")

        def build() -> None:
            self._store.install_keys()
            self._store.set_meta("basis", format(opening, "f"))
            self._store.set_meta("opening_cash", format(opening, "f"))
            self._store.set_meta("deposits", "0")
            self._store.set_meta("sells", "0")
            self._store.set_meta("pdt_enabled", "1" if pdt_enabled else "0")
            self._store.set_meta("pdt_max", str(pdt_max_day_trades))
            self._store.set_meta("buy_ban_session", "")
            self._store.set_meta("buy_ban_code", "")
            self._store.set_meta("code_version", code_version)
            self._store.set_meta("halt_reason", "")
            self._store.set_meta("halt_ms", "")
            self._store.set_meta("pid", "")
            self._store.set_meta("command", "")
            hwm = max(PLANNED_EQUITY, opening)
            halt = opening <= hwm * HWM_RETAINED
            self._store.save_signed(hwm, halt, 1)
            if halt:
                self._store.set_meta("halt_reason", "HWM_LOCK")
                self._store.set_meta("halt_ms", "0")
                self._store.add_alert(
                    "CRITICAL",
                    "HWM_LOCK",
                    f"equity={format(opening, 'f')} hwm={format(hwm, 'f')}",
                    _stamp(datetime.now(tz=UTC)),
                )

        self._run(build)

    def _repair_signed(self) -> None:
        key = self._store.hmac_key()
        db = self._store.read_signed_row()
        file_state = self._store.file_signed()
        db_ok = False
        if db is not None and not db.corrupt:
            db_ok = signatures_match(sign_text_row(key, db), db.sig)
        file_ok = False
        if file_state is not None and not file_state.corrupt:
            file_ok = signatures_match(sign_text_row(key, file_state), file_state.sig)
        hwms: list[Decimal] = []
        halt = False
        generation = 1
        tamper = False
        if db_ok and db is not None:
            hwms.append(parse_decimal(db.hwm))
            halt = halt or db.halt
            generation = max(generation, db.generation)
        else:
            tamper = True
        if file_ok and file_state is not None:
            hwms.append(parse_decimal(file_state.hwm))
            halt = halt or file_state.halt
            generation = max(generation, file_state.generation)
        elif file_state is None or file_state.corrupt or not file_ok:
            tamper = True
        if not hwms:
            hwms.append(PLANNED_EQUITY)
            halt = True
        if tamper:
            halt = True
        hwm = max(hwms)
        current_ok = (
            db_ok
            and file_ok
            and db is not None
            and file_state is not None
            and db.hwm == file_state.hwm
            and db.halt == file_state.halt
            and db.generation == file_state.generation
        )
        if current_ok:
            return
        self._store.save_signed(hwm, halt, generation + 1)
        reason = "STATE_TAMPER" if tamper else _required(self._store, "halt_reason")
        self._store.set_meta("halt_reason", reason)
        if tamper and "STATE_TAMPER" not in self._store.alert_codes():
            self._store.add_alert(
                "CRITICAL",
                "STATE_TAMPER",
                format(hwm, "f"),
                _stamp(datetime.now(tz=UTC)),
            )

    def _reject_reason(self, request: EntryRequest, view: EquityView) -> str | None:
        if request.symbol.startswith("EQ_") or not request.symbol:
            return "SYMBOL"
        if CLIENT_ORDER_ID.fullmatch(request.client_order_id) is None:
            return "CLIENT_ORDER_ID"
        if any(order.client_order_id == request.client_order_id for order in view.orders):
            return "DUPLICATE_CLIENT_ID"
        try:
            self._guard.check_price(request.limit_price)
            self._guard.check_price(request.stop_price)
            self._guard.check_price(request.take_profit_price)
        except OrderRejected as exc:
            return exc.reason
        if not request.stop_price < request.limit_price < request.take_profit_price:
            return "GEOMETRY"
        if request.quantity <= 0:
            return "FILL_QUANTITY"
        if view.halt_new_entries:
            return "HALT_NEW_ENTRIES"
        ban_day = self._store.meta("buy_ban_session") or ""
        if ban_day and ban_day == _session_day(request.now_et).isoformat():
            code = self._store.meta("buy_ban_code") or "486440"
            return code
        if _required(self._store, "pdt_enabled") == "1":
            window_count = self._count_day_trades(_session_day(request.now_et))
            if window_count >= int(_required(self._store, "pdt_max")):
                return "PDT_LIMIT"
        open_symbols = {order.symbol for order in view.orders if order.status in OPEN_BUY_STATUSES}
        if request.symbol in open_symbols:
            return "ALREADY_IN_POSITION"
        if any(position.symbol == request.symbol for position in view.positions):
            return "ALREADY_IN_POSITION"
        if view.slots >= MAX_OPEN_POSITIONS:
            return "MAX_POSITIONS"
        notional = request.limit_price * request.quantity
        if notional > view.available:
            return "INSUFFICIENT_CASH"
        if notional > view.equity * MAX_POSITION_FRACTION:
            return "CONCENTRATION"
        risk = (request.limit_price - request.stop_price) * request.quantity
        if risk > view.equity * RISK_FRACTION:
            return "RISK_BUDGET"
        if notional < MIN_VIABLE_NOTIONAL:
            return "NOTIONAL_TOO_SMALL"
        return None

    def _apply_fill(
        self,
        client_order_id: str,
        *,
        status: str,
        filled_qty: Decimal,
        filled_notional: Decimal,
        total_cost_raw: Decimal | None,
        rest_fee: Decimal | None,
        bid: Decimal,
        now_et: datetime,
    ) -> None:
        if status not in {"NEW", "ACCEPTED", "PARTIALLY_FILLED", "FILLED", "CANCELED"}:
            raise OrderRejected("ORDER_STATUS")
        self._guard.check_price(bid)
        order = _order_map(self._store.orders()).get(client_order_id)
        if order is None:
            raise OrderRejected("UNKNOWN_ORDER")
        if order.status in {"FILLED", "CANCELED"}:
            raise OrderRejected("ORDER_TERMINAL")
        if filled_qty < order.filled_qty or filled_notional < order.filled_notional:
            raise OrderRejected("FILL_REGRESSION")
        if filled_qty > order.quantity:
            raise OrderRejected("FILL_QUANTITY")
        fully_filled = status == "FILLED"
        if fully_filled and filled_qty != order.quantity:
            raise OrderRejected("FILL_QUANTITY")
        if not fully_filled and rest_fee is not None:
            raise FeeTrapError("refusing tc-FN as realized commission before the order is filled")
        delta_qty = filled_qty - order.filled_qty
        delta_notional = filled_notional - order.filled_notional
        released = order.limit_price * delta_qty
        if delta_notional > released:
            raise OrderRejected("FILL_ABOVE_LIMIT")
        if delta_notional != 0:
            basis = _required_decimal(self._store, "basis") - delta_notional
            self._store.set_meta("basis", format(basis, "f"))
        fee = order.rest_fee
        if fully_filled and rest_fee is not None:
            basis = _required_decimal(self._store, "basis") - rest_fee
            self._store.set_meta("basis", format(basis, "f"))
            fee = rest_fee
        self._store.replace_order(
            OrderRow(
                order.client_order_id,
                order.symbol,
                status,
                order.limit_price,
                order.quantity,
                filled_qty,
                filled_notional,
                total_cost_raw if total_cost_raw is not None else order.total_cost_raw,
                fee,
            )
        )
        if delta_qty > 0:
            positions = _position_map(self._store.positions())
            current = positions.get(order.symbol)
            session = _session_day(now_et).isoformat()
            if current is None:
                self._store.replace_position(
                    PositionRow(order.symbol, delta_qty, delta_notional, bid, session)
                )
            else:
                self._store.replace_position(
                    PositionRow(
                        order.symbol,
                        current.quantity + delta_qty,
                        current.cost_notional + delta_notional,
                        bid,
                        current.opened_session,
                    )
                )
        elif delta_qty == 0:
            positions = _position_map(self._store.positions())
            current = positions.get(order.symbol)
            if current is not None:
                self._store.replace_position(
                    PositionRow(
                        current.symbol,
                        current.quantity,
                        current.cost_notional,
                        bid,
                        current.opened_session,
                    )
                )

    def _maybe_halt(self, now_et: datetime) -> list[tuple[str, str]]:
        view = self._view()
        hwm = view.hwm
        if view.equity > hwm:
            hwm = view.equity
            self._store.save_signed(hwm, view.halt_new_entries, view.generation + 1)
            view = self._view()
        if view.halt_new_entries or view.equity > hwm * HWM_RETAINED:
            return []
        return self._force_halt(
            "HWM_LOCK",
            f"equity={format(view.equity, 'f')} hwm={format(hwm, 'f')}",
            now_et,
        )

    def _force_halt(self, reason: str, detail: str, now_et: datetime) -> list[tuple[str, str]]:
        started = time.perf_counter_ns()
        view = self._view()
        if not view.halt_new_entries:
            self._store.save_signed(view.hwm, True, view.generation + 1)
        self._store.set_meta("halt_reason", reason)
        if reason not in self._store.alert_codes():
            self._store.add_alert("CRITICAL", reason, detail, _stamp(now_et))
        cancels = self._cancel_open_buys(now_et)
        elapsed_ms = (time.perf_counter_ns() - started) // 1_000_000
        if self._store.meta("halt_ms") in {None, ""}:
            self._store.set_meta("halt_ms", str(elapsed_ms))
        return cancels

    def _cancel_open_buys(self, now_et: datetime) -> list[tuple[str, str]]:
        cancels: list[tuple[str, str]] = []
        for order in self._store.orders():
            if order.status not in OPEN_BUY_STATUSES:
                continue
            self._store.replace_order(
                OrderRow(
                    order.client_order_id,
                    order.symbol,
                    "CANCELED",
                    order.limit_price,
                    order.quantity,
                    order.filled_qty,
                    order.filled_notional,
                    order.total_cost_raw,
                    order.rest_fee,
                )
            )
            self._store.add_cancel(order.client_order_id, order.symbol, _stamp(now_et))
            cancels.append((order.client_order_id, order.symbol))
        return cancels

    def _count_day_trades(self, day: date) -> int:
        window = {session.isoformat() for session in self._sessions_ending(day)}
        return sum(1 for _symbol, session in self._store.day_trade_sessions() if session in window)

    def _sessions_ending(self, day: date) -> list[date]:
        if self._calendar_days is None:
            from bstocks_v2.research_data.calendar import load_xnys_calendar

            self._calendar_days = sorted(load_xnys_calendar().closes())
        ended = [session for session in self._calendar_days if session <= day]
        return ended[-PDT_WINDOW_SESSIONS:]

    def _view(self) -> EquityView:
        orders = self._store.orders()
        positions = self._store.positions()
        marks = [_order_mark(order) for order in orders]
        position_marks = [_position_mark(position) for position in positions]
        basis = _required_decimal(self._store, "basis")
        opening = _required_decimal(self._store, "opening_cash")
        deposits = _required_decimal(self._store, "deposits")
        sells = int(_required(self._store, "sells"))
        equity = assert_identity(
            opening=opening,
            deposits=deposits,
            basis=basis,
            sells=sells,
            orders=marks,
            marks=position_marks,
        )
        locked = locked_principal(marks)
        liquidation = liquidation_value(position_marks)
        hwm = parse_decimal(_required(self._store, "hwm"))
        halt = _required(self._store, "halt") == "1"
        halt_ms_text = self._store.meta("halt_ms") or ""
        slots = len(
            {order.symbol for order in orders if order.status in OPEN_BUY_STATUSES}
            | {position.symbol for position in positions}
        )
        return EquityView(
            equity=equity,
            available=basis - locked,
            locked=locked,
            liquidation=liquidation,
            basis=basis,
            opening=opening,
            deposits=deposits,
            hwm=hwm,
            halt_new_entries=halt,
            halt_reason=self._store.meta("halt_reason") or "",
            halt_transition_ms=None if halt_ms_text == "" else int(halt_ms_text),
            slots=slots,
            generation=int(_required(self._store, "generation")),
            buy_ban_code=self._store.meta("buy_ban_code") or "",
            orders=tuple(_order_view(order) for order in orders),
            positions=tuple(_position_view(position) for position in positions),
        )

    def _run(self, fn: Callable[[], _T]) -> _T:
        self._store.begin()
        try:
            result = fn()
        except Exception:
            self._store.rollback()
            raise
        self._store.commit()
        return result

    def _dispatch(self, cancels: list[tuple[str, str]]) -> None:
        for client_order_id, symbol in cancels:
            self._gateway.cancel_buy(client_order_id, symbol)


def _draft(request: EntryRequest, *, research_only: bool) -> OrderDraft:
    gross = (request.take_profit_price - request.limit_price) * request.quantity
    friction = (
        COMMISSION_RATE * (request.limit_price + request.take_profit_price) * request.quantity
        + HALF_SPREAD * request.quantity * 2
    )
    return OrderDraft(
        symbol=request.symbol,
        side=Side.BUY,
        order_type=OrderType.LIMIT,
        client_order_id=request.client_order_id,
        quantity=request.quantity,
        price=request.limit_price,
        trading_session=request.trading_session,
        quote_asset="USDC",
        wallet_type="CARD",
        tokenize=False,
        is_opening=True,
        confirmed_long_qty=Decimal(0),
        gross_take_profit_usdc=gross,
        round_trip_friction_usdc=friction,
        research_only=research_only,
    )


def _order_mark(order: OrderRow) -> OrderMark:
    return OrderMark(
        order.status,
        order.limit_price,
        order.quantity,
        order.filled_qty,
        order.filled_notional,
        order.total_cost_raw,
        order.rest_fee,
    )


def _position_mark(position: PositionRow) -> PositionMark:
    return PositionMark(position.quantity, position.cost_notional, position.bid)


def _order_view(order: OrderRow) -> OrderView:
    return OrderView(
        order.client_order_id,
        order.symbol,
        order.status,
        order.limit_price,
        order.quantity,
        order.filled_qty,
        order.filled_notional,
        order.total_cost_raw,
        order.rest_fee,
    )


def _position_view(position: PositionRow) -> PositionView:
    return PositionView(
        position.symbol,
        position.quantity,
        position.cost_notional,
        position.bid,
        position.opened_session,
    )


def _order_map(orders: list[OrderRow]) -> dict[str, OrderRow]:
    return {order.client_order_id: order for order in orders}


def _position_map(positions: list[PositionRow]) -> dict[str, PositionRow]:
    return {position.symbol: position for position in positions}


def _required(store: RiskStore, key: str) -> str:
    value = store.meta(key)
    if value is None:
        raise RuntimeError(f"missing {key}")
    return value


def _required_decimal(store: RiskStore, key: str) -> Decimal:
    return parse_decimal(_required(store, key))


def _session_day(now_et: datetime) -> date:
    if now_et.tzinfo is None:
        raise OrderRejected("NAIVE_DATETIME")
    return now_et.astimezone(NEW_YORK).date()


def _stamp(now_et: datetime) -> str:
    return now_et.astimezone(UTC).isoformat()


def sign_text_row(key: bytes, signed: SignedText) -> str:
    return sign_text(key, signed.hwm, signed.halt, signed.generation)
