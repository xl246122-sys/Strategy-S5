"""Independent stop, target, and flatten service. It never opens a live order route."""

import json
import os
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from bstocks_v2.constitution.guard import NEW_YORK, ConstitutionGuard, flatten_deadline
from bstocks_v2.contracts.errors import OrderRejected
from bstocks_v2.contracts.models import OrderDraft, OrderType, Side, TradingSession
from bstocks_v2.guardian.broker import (
    BookBroker,
    Detail,
    ExchangeTimeout,
    PlaceRequest,
    PlaceResult,
)
from bstocks_v2.guardian.ids import client_order_id
from bstocks_v2.guardian.journal import GuardRow, Journal, Position, init_db
from bstocks_v2.guardian.ladder import marketable_price, session_phase, split_shares
from bstocks_v2.guardian.spec import (
    BUY_PENDING,
    CANCEL_CONFIRM_TIMEOUT_NS,
    CANCEL_PENDING_FOR_SL,
    CENT,
    CODE_ALREADY_FILLED,
    CODE_EXTENDED_MARKET,
    CODE_MARKET_RESTRICTED,
    CODE_NO_DEPTH,
    CODE_SHARES_FROZEN,
    EXIT_EXECUTION_LADDER,
    EXIT_REASONS,
    EXIT_REQUIRED,
    FLAT_RECONCILED,
    FROZEN_ESCALATED,
    GUARD_DONE,
    GUARD_UNKNOWN,
    PARTIAL_POSITION,
    PROTECTED_POSITION,
    ROLE_EXIT_EXTENDED,
    ROLE_EXIT_LIMIT,
    ROLE_EXIT_MARKET,
    ROLE_TP,
    TERMINAL_ORDER,
    TP_PENDING,
    UNCERTAIN,
)
from bstocks_v2.reconciler.audit import LiveReconciler
from bstocks_v2.reconciler.ledger import Reconciliation


@dataclass(frozen=True)
class BuyPlan:
    symbol: str
    quantity: Decimal
    limit_price: Decimal
    stop_price: Decimal
    take_profit_price: Decimal
    max_holding_seconds: int = 86400


@dataclass(frozen=True)
class Recovered:
    milliseconds: int
    generation: int
    halted: bool


class Guardian:
    def __init__(self, conn: sqlite3.Connection, path: str) -> None:
        self.path = path
        self.journal = Journal(conn)
        self.broker = BookBroker(conn)
        self.audit = LiveReconciler(path)
        self.rules = ConstitutionGuard()
        self.last_audit: Reconciliation | None = None
        self._conn = conn
        self._now: datetime | None = None
        self._half = False

    @classmethod
    def open(cls, path: str | Path) -> "Guardian":
        conn = sqlite3.connect(str(path))
        init_db(conn)
        return cls(conn, str(path))

    def close(self) -> None:
        self.audit.close()
        self._conn.close()

    def live_place(self) -> None:
        self.rules.check_endpoint("POST", "/sapi/v1/equity/order/place")

    def live_cancel(self) -> None:
        self.rules.check_endpoint("POST", "/sapi/v1/equity/order/cancel")

    @property
    def halted(self) -> bool:
        return self.journal.get_flag("halt_new_entries") == "1"

    def alerts(self) -> list[tuple[str, str, str]]:
        return self.journal.alerts()

    def get(self, symbol: str) -> Position:
        return self._must(symbol)

    def set_clock(self, now_et: datetime, *, is_half_day: bool) -> None:
        if now_et.tzinfo is None:
            raise OrderRejected("NAIVE_DATETIME")
        self._now = now_et.astimezone(NEW_YORK)
        self._half = is_half_day

    def set_market(
        self,
        symbol: str,
        *,
        bid: Decimal,
        trading_status: str,
        multiplier_down: Decimal,
        multiplier_up: Decimal,
    ) -> None:
        self.rules.check_price(bid)
        self.rules.check_price(multiplier_down)
        self.rules.check_price(multiplier_up)
        self.journal.set_market(
            symbol,
            bid=bid,
            trading_status=trading_status,
            multiplier_down=multiplier_down,
            multiplier_up=multiplier_up,
        )

    def adopt_buy(self, plan: BuyPlan) -> str:
        if self.halted:
            raise OrderRejected("HALT_NEW_ENTRIES")
        if not plan.symbol or plan.symbol.startswith("EQ_"):
            raise OrderRejected("SYMBOL")
        if self.journal.load(plan.symbol) is not None:
            raise OrderRejected("DUPLICATE_SYMBOL")
        if plan.quantity <= 0 or plan.max_holding_seconds <= 0:
            raise OrderRejected("QUANTITY")
        if not plan.stop_price < plan.limit_price < plan.take_profit_price:
            raise OrderRejected("GEOMETRY")
        self.rules.check_price(plan.limit_price)
        self.rules.check_price(plan.stop_price)
        self.rules.check_price(plan.take_profit_price)
        client_id = client_order_id(plan.symbol, "buy")
        order_id = self.broker.seed_buy(
            client_order_id=client_id,
            symbol=plan.symbol,
            quantity=plan.quantity,
            price=plan.limit_price,
        )
        self.journal.save(
            Position(
                symbol=plan.symbol,
                state=BUY_PENDING,
                buy_client_order_id=client_id,
                buy_order_id=order_id,
                buy_qty=plan.quantity,
                filled_buy_qty=Decimal(0),
                stop_price=plan.stop_price,
                take_profit_price=plan.take_profit_price,
                max_holding_seconds=plan.max_holding_seconds,
                opened_at=None,
                tp_client_order_id=None,
                tp_filled_qty=Decimal(0),
                sell_lock=False,
                buy_cancel_pending=False,
                exit_client_order_id=None,
                exit_role=None,
                remaining_qty=Decimal(0),
                residual_qty=Decimal(0),
                ws_total_cost=None,
                ws_filled_notional=None,
                realized_fee=None,
                fee_source=None,
                alert=None,
            )
        )
        cap = (plan.limit_price * Decimal(10)).quantize(CENT)
        self.set_market(
            plan.symbol,
            bid=plan.limit_price,
            trading_status="TRADING",
            multiplier_down=CENT,
            multiplier_up=cap,
        )
        return BUY_PENDING

    def on_buy_report(
        self,
        symbol: str,
        *,
        status: str,
        filled_qty: Decimal,
        total_cost: Decimal | None = None,
        filled_notional: Decimal | None = None,
    ) -> str:
        pos = self._must(symbol)
        if pos.state not in {BUY_PENDING, PARTIAL_POSITION}:
            raise OrderRejected("BUY_REPORT_STATE")
        if filled_qty <= 0 or filled_qty > pos.buy_qty:
            raise OrderRejected("QUANTITY")
        if status == "FILLED" and filled_qty != pos.buy_qty:
            raise OrderRejected("BUY_STATUS")
        if status not in {"FILLED", "PARTIALLY_FILLED"}:
            raise OrderRejected("BUY_STATUS")
        pos.filled_buy_qty = filled_qty
        pos.opened_at = pos.opened_at or self._stamp()
        pos.ws_total_cost = total_cost
        pos.ws_filled_notional = filled_notional
        pos.realized_fee = None
        pos.fee_source = None
        pos.state = PROTECTED_POSITION if status == "FILLED" else PARTIAL_POSITION
        self._apply_detail_fee(pos, status, filled_qty)
        self.broker.set_fill(pos.buy_client_order_id, status=status, filled_qty=filled_qty)
        self.audit.record_buy(
            pos.buy_client_order_id,
            pos.buy_order_id,
            pos.symbol,
            filled_qty,
            status,
        )
        self.journal.save(pos)
        return pos.state

    def expire_buy(self, symbol: str) -> str:
        pos = self._must(symbol)
        if pos.state not in {BUY_PENDING, PARTIAL_POSITION}:
            raise OrderRejected("BUY_EXPIRE_STATE")
        self._cancel_buy(pos)
        pos = self._must(symbol)
        if pos.state != PROTECTED_POSITION:
            return pos.state
        return self.arm_take_profit(symbol)

    def arm_take_profit(self, symbol: str) -> str:
        pos = self._must(symbol)
        if pos.state != PROTECTED_POSITION:
            raise OrderRejected("TP_STATE")
        if pos.filled_buy_qty <= 0:
            raise OrderRejected("QUANTITY")
        if pos.tp_client_order_id is not None:
            return pos.state
        result = self._submit_sell(
            pos,
            ROLE_TP,
            pos.filled_buy_qty,
            price=pos.take_profit_price,
            session="RTH",
            autofill=False,
            allowed=pos.filled_buy_qty,
        )
        if not result.accepted:
            return self._freeze(pos, "TP_PLACE_FAILED")
        pos.tp_client_order_id = result.client_order_id
        pos.state = TP_PENDING
        self.journal.save(pos)
        return pos.state

    def on_clock(
        self,
        now_et: datetime,
        *,
        is_half_day: bool,
        bid: Decimal,
        trading_status: str,
        multiplier_down: Decimal,
        multiplier_up: Decimal,
    ) -> None:
        self.set_clock(now_et, is_half_day=is_half_day)
        symbols = [pos.symbol for pos in self.journal.list_positions()]
        for symbol in symbols:
            self.set_market(
                symbol,
                bid=bid,
                trading_status=trading_status,
                multiplier_down=multiplier_down,
                multiplier_up=multiplier_up,
            )
        if self._now is not None and self._now >= flatten_deadline(self._now, is_half_day):
            self._flatten_all()
            return
        for pos in self.journal.list_positions():
            self._maybe_risk_exit(pos)

    def request_exit(self, symbol: str, reason: str) -> str:
        if reason not in EXIT_REASONS:
            raise OrderRejected("EXIT_REASON")
        if reason == "FLATTEN" and (
            self._now is None or self._now < flatten_deadline(self._now, self._half)
        ):
            raise OrderRejected("FLATTEN_EARLY")
        pos = self._must(symbol)
        if pos.state in {FLAT_RECONCILED, UNCERTAIN, FROZEN_ESCALATED}:
            return pos.state
        if pos.sell_lock or pos.state == CANCEL_PENDING_FOR_SL:
            return self._on_tp_terminal(pos)
        if pos.state in {EXIT_REQUIRED, EXIT_EXECUTION_LADDER}:
            return self.poll(symbol)
        market = self.journal.market(symbol)
        if market.trading_status == "TRADING_HALTED":
            pos.state = EXIT_REQUIRED
            pos.alert = "TRADING_HALTED"
            pos.remaining_qty = pos.filled_buy_qty - pos.tp_filled_qty
            self.journal.save(pos)
            self.journal.alert("CRITICAL", "TRADING_HALTED", symbol, "halted")
            return pos.state
        if pos.state in {BUY_PENDING, PARTIAL_POSITION}:
            return self._exit_from_buy(symbol)
        if pos.state == PROTECTED_POSITION:
            qty = pos.filled_buy_qty - pos.tp_filled_qty
            if qty <= 0:
                return self._flat(pos, "ALREADY_FLAT")
            return self._ladder(pos, qty)
        if pos.state == TP_PENDING:
            return self._begin_cancel_tp(pos)
        return self._freeze(pos, "EXIT_STATE")

    def poll(self, symbol: str) -> str:
        pos = self._must(symbol)
        if pos.buy_cancel_pending:
            self._after_buy_terminal(pos)
            pos = self._must(symbol)
        if pos.state == CANCEL_PENDING_FOR_SL:
            return self._on_tp_terminal(pos)
        if pos.state == EXIT_REQUIRED:
            return self._poll_required(pos)
        if pos.state == EXIT_EXECUTION_LADDER and pos.exit_client_order_id is not None:
            detail = self.broker.detail(pos.exit_client_order_id)
            if detail is not None and detail.status == "FILLED":
                return self._complete(
                    pos,
                    detail.filled_qty,
                    pos.residual_qty,
                    pos.exit_client_order_id,
                    complete=True,
                )
        return pos.state

    def reconcile_now(self) -> Reconciliation:
        if not self.broker.has_snapshot():
            raise RuntimeError("remote snapshot is not loaded")
        result = self.audit.audit(
            self.broker.trades(),
            self.broker.open_orders(),
            self.broker.funding(),
        )
        self.last_audit = result
        if not result.matched:
            self._lock(result.quantity_breaks)
        return result

    def recover(self) -> Recovered:
        started = time.perf_counter_ns()
        self._resume_guards()
        for pos in self.journal.list_positions():
            self._sync_position(pos)
        if self.broker.has_snapshot():
            self.reconcile_now()
        elapsed = (time.perf_counter_ns() - started) // 1_000_000
        generation = self.journal.bump("recover_generation")
        self.journal.set_flag("recover_ms", str(elapsed))
        return Recovered(elapsed, generation, self.halted)

    def mark_pid(self) -> None:
        self.journal.set_flag("pid", str(os.getpid()))

    def touch_heartbeat(self) -> None:
        self.journal.set_flag("heartbeat_ns", str(time.time_ns()))

    def drain_commands(self) -> bool:
        stop = False
        for cmd_id, name, payload in self.journal.pending_commands():
            if name == "SHUTDOWN":
                stop = True
            elif name == "EXIT":
                body = json.loads(payload)
                if not isinstance(body, dict):
                    raise OrderRejected("COMMAND")
                self.request_exit(str(body["symbol"]), str(body["reason"]))
            else:
                self.journal.alert("CRITICAL", "UNKNOWN_COMMAND", "", name)
            self.journal.finish_command(cmd_id)
        return stop

    def _exit_from_buy(self, symbol: str) -> str:
        self._cancel_buy(self._must(symbol))
        pos = self._must(symbol)
        if pos.buy_cancel_pending:
            return pos.state
        if pos.state == FLAT_RECONCILED or pos.filled_buy_qty <= 0:
            if pos.state != FLAT_RECONCILED:
                return self._flat(pos, "BUY_CANCELED")
            return pos.state
        return self._ladder(pos, pos.filled_buy_qty)

    def _cancel_buy(self, pos: Position) -> str:
        result = self.broker.cancel(pos.buy_client_order_id)
        if not result.immediate_terminal:
            pos.buy_cancel_pending = True
            self.journal.set_flag(f"cancel_ns:{pos.symbol}", str(time.time_ns()))
            self.journal.save(pos)
            return pos.state
        return self._after_buy_terminal(pos)

    def _after_buy_terminal(self, pos: Position) -> str:
        if self._cancel_timed_out(pos.symbol) and pos.buy_cancel_pending:
            return self._freeze(pos, "CANCEL_TIMEOUT")
        detail = self.broker.detail(pos.buy_client_order_id)
        if detail is None or detail.shares_frozen or detail.status not in TERMINAL_ORDER:
            pos.buy_cancel_pending = True
            self.journal.save(pos)
            return pos.state
        pos.buy_cancel_pending = False
        pos.filled_buy_qty = detail.filled_qty
        if detail.filled_qty <= 0:
            return self._flat(pos, "BUY_CANCELED")
        status = "FILLED" if detail.filled_qty >= pos.buy_qty else "PARTIALLY_FILLED"
        self.audit.record_buy(
            pos.buy_client_order_id,
            detail.order_id,
            pos.symbol,
            detail.filled_qty,
            status,
        )
        pos.state = PROTECTED_POSITION
        pos.opened_at = pos.opened_at or self._stamp()
        self.journal.save(pos)
        return pos.state

    def _begin_cancel_tp(self, pos: Position) -> str:
        if pos.tp_client_order_id is None:
            qty = pos.filled_buy_qty - pos.tp_filled_qty
            return self._ladder(pos, qty)
        result = self.broker.cancel(pos.tp_client_order_id)
        if result.code == CODE_ALREADY_FILLED:
            return self._handle_already_filled(pos)
        pos.state = CANCEL_PENDING_FOR_SL
        pos.sell_lock = True
        self.journal.save(pos)
        if result.immediate_terminal:
            return self._on_tp_terminal(pos)
        self.journal.set_flag(f"cancel_ns:{pos.symbol}", str(time.time_ns()))
        return pos.state

    def _handle_already_filled(self, pos: Position) -> str:
        if pos.tp_client_order_id is None:
            return self._freeze(pos, "CANCEL_486444_UNCONFIRMED")
        detail = self.broker.detail(pos.tp_client_order_id)
        if detail is not None and detail.status == "FILLED":
            self._book_detail_sell(pos, detail)
            return self._flat(pos, "TP_ALREADY_FILLED")
        return self._freeze(pos, "CANCEL_486444_UNCONFIRMED")

    def _on_tp_terminal(self, pos: Position) -> str:
        if pos.tp_client_order_id is None:
            return pos.state
        if self._cancel_timed_out(pos.symbol):
            return self._freeze(pos, "CANCEL_TIMEOUT")
        detail = self.broker.detail(pos.tp_client_order_id)
        if detail is None or detail.shares_frozen or detail.status not in TERMINAL_ORDER:
            pos.state = CANCEL_PENDING_FOR_SL
            pos.sell_lock = True
            self.journal.save(pos)
            return pos.state
        pos.tp_filled_qty = detail.filled_qty
        pos.sell_lock = False
        self.journal.save(pos)
        if detail.status == "FILLED" or detail.filled_qty >= pos.filled_buy_qty:
            self._book_detail_sell(pos, detail)
            return self._flat(pos, "TP_FILLED")
        if detail.filled_qty > 0:
            self._book_detail_sell(pos, detail)
        remaining = pos.filled_buy_qty - detail.filled_qty
        if remaining <= 0:
            return self._flat(pos, "TP_FILLED")
        return self._ladder(pos, remaining)

    def _ladder(self, pos: Position, qty: Decimal) -> str:
        if qty <= 0:
            return self._flat(pos, "ALREADY_FLAT")
        market = self.journal.market(pos.symbol)
        if market.trading_status == "TRADING_HALTED":
            pos.state = EXIT_REQUIRED
            pos.alert = "TRADING_HALTED"
            pos.remaining_qty = qty
            self.journal.save(pos)
            self.journal.alert("CRITICAL", "TRADING_HALTED", pos.symbol, "halted")
            return pos.state
        pos.state = EXIT_EXECUTION_LADDER
        pos.remaining_qty = qty
        pos.sell_lock = False
        self.journal.save(pos)
        allowed = pos.filled_buy_qty - pos.tp_filled_qty
        result = self._submit_sell(
            pos,
            ROLE_EXIT_MARKET,
            qty,
            price=None,
            session=None,
            autofill=True,
            allowed=allowed,
        )
        if result.code == CODE_SHARES_FROZEN:
            pos.state = CANCEL_PENDING_FOR_SL
            pos.sell_lock = True
            self.journal.save(pos)
            return pos.state
        if result.accepted and self._filled(result.client_order_id):
            pos.exit_role = ROLE_EXIT_MARKET
            return self._complete(pos, qty, Decimal(0), result.client_order_id, complete=True)
        if result.accepted:
            pos.exit_client_order_id = result.client_order_id
            pos.exit_role = ROLE_EXIT_MARKET
            self.journal.save(pos)
            return pos.state
        if result.code in {CODE_NO_DEPTH, CODE_MARKET_RESTRICTED}:
            return self._marketable(pos, qty)
        if result.code == CODE_EXTENDED_MARKET:
            return self._extended(pos, qty)
        return self._freeze(pos, "EXIT_UNHANDLED")

    def _marketable(self, pos: Position, qty: Decimal) -> str:
        market = self.journal.market(pos.symbol)
        price = marketable_price(market.bid, market.multiplier_down, market.multiplier_up)
        allowed = pos.filled_buy_qty - pos.tp_filled_qty
        result = self._submit_sell(
            pos,
            ROLE_EXIT_LIMIT,
            qty,
            price=price,
            session="RTH",
            autofill=True,
            allowed=allowed,
        )
        if result.accepted and self._filled(result.client_order_id):
            pos.exit_role = ROLE_EXIT_LIMIT
            return self._complete(pos, qty, Decimal(0), result.client_order_id, complete=True)
        if not result.accepted:
            return self._freeze(pos, "LIMIT_EXIT_FAILED")
        pos.exit_client_order_id = result.client_order_id
        pos.exit_role = ROLE_EXIT_LIMIT
        self.journal.save(pos)
        return pos.state

    def _extended(self, pos: Position, qty: Decimal) -> str:
        whole, residual = split_shares(qty)
        pos.residual_qty = residual
        if whole <= 0:
            pos.state = EXIT_REQUIRED
            pos.alert = "FRACTIONAL_RESIDUAL"
            pos.remaining_qty = residual
            self.journal.save(pos)
            self.journal.alert("CRITICAL", "EXIT_REQUIRED", pos.symbol, format(residual, "f"))
            return pos.state
        market = self.journal.market(pos.symbol)
        price = marketable_price(market.bid, market.multiplier_down, market.multiplier_up)
        allowed = pos.filled_buy_qty - pos.tp_filled_qty
        result = self._submit_sell(
            pos,
            ROLE_EXIT_EXTENDED,
            whole,
            price=price,
            session="EXTENDED",
            autofill=True,
            allowed=allowed,
        )
        if not result.accepted or not self._filled(result.client_order_id):
            return self._freeze(pos, "EXTENDED_EXIT_FAILED")
        pos.exit_role = ROLE_EXIT_EXTENDED
        return self._complete(pos, whole, residual, result.client_order_id, complete=True)

    def _poll_required(self, pos: Position) -> str:
        if pos.residual_qty > 0:
            phase = "RTH"
            if self._now is not None:
                phase = session_phase(self._now, self._half)
            if phase != "RTH":
                return pos.state
            return self._ladder(pos, pos.residual_qty)
        if self.journal.market(pos.symbol).trading_status == "TRADING_HALTED":
            return pos.state
        if pos.tp_client_order_id is not None:
            detail = self.broker.detail(pos.tp_client_order_id)
            if detail is not None and detail.status not in TERMINAL_ORDER:
                return self._begin_cancel_tp(pos)
        qty = pos.filled_buy_qty - pos.tp_filled_qty
        if qty <= 0:
            return self._flat(pos, "ALREADY_FLAT")
        return self._ladder(pos, qty)

    def _submit_sell(
        self,
        pos: Position,
        role: str,
        quantity: Decimal,
        *,
        price: Decimal | None,
        session: str | None,
        autofill: bool,
        allowed: Decimal,
    ) -> PlaceResult:
        client_id = client_order_id(pos.symbol, role)
        row = self.journal.prepare_guard(client_id, pos.symbol, role)
        if row.status in GUARD_DONE:
            detail = self.broker.detail(client_id)
            order_id = None if detail is None else detail.order_id
            return PlaceResult(True, row.last_code, order_id, client_id)
        if row.status in GUARD_UNKNOWN:
            return self._finish_unknown(row)
        request = PlaceRequest(
            client_order_id=client_id,
            symbol=pos.symbol,
            side="SELL",
            order_type="LIMIT" if price is not None else "MARKET",
            quantity=quantity,
            price=price,
            trading_session=session,
            wallet_type="CARD",
            tokenize=False,
            quote_asset="USDC",
            autofill=autofill,
        )
        self._check_sell(request, allowed)
        attempts = row.place_attempts + 1
        self.journal.mark_guard(client_id, "SUBMITTING", attempts=attempts, code=None)
        try:
            result = self.broker.place(request)
        except ExchangeTimeout as exc:
            self.journal.mark_guard(
                client_id, "UNKNOWN_TIMEOUT", attempts=attempts, code=exc.code
            )
            detail = self.broker.detail(client_id)
            if detail is None:
                self.journal.mark_guard(
                    client_id, "DETAIL_ABSENT", attempts=attempts, code=exc.code
                )
                result = self.broker.place(request)
                status = "ACKNOWLEDGED" if result.accepted else "REJECTED"
                self.journal.mark_guard(client_id, status, attempts=attempts + 1, code=exc.code)
                return result
            self.journal.mark_guard(
                client_id, "DETAIL_CONFIRMED", attempts=attempts, code=exc.code
            )
            return PlaceResult(True, exc.code, detail.order_id, client_id)
        status = "ACKNOWLEDGED" if result.accepted else "REJECTED"
        self.journal.mark_guard(client_id, status, attempts=attempts, code=result.code)
        return result

    def _check_sell(self, request: PlaceRequest, allowed: Decimal) -> None:
        if request.quantity <= 0 or request.quantity > allowed:
            raise OrderRejected("SHORT")
        trading_session = None
        if request.trading_session is not None:
            trading_session = TradingSession(request.trading_session)
        draft = OrderDraft(
            symbol=request.symbol,
            side=Side.SELL,
            order_type=OrderType(request.order_type),
            client_order_id=request.client_order_id,
            quantity=request.quantity,
            price=request.price,
            notional=None,
            trading_session=trading_session,
            quote_asset=request.quote_asset,
            wallet_type=request.wallet_type,
            tokenize=request.tokenize,
            is_opening=False,
            confirmed_long_qty=allowed,
        )
        now = self._now if self._now is not None else datetime.now(NEW_YORK)
        self.rules.check_order(
            draft,
            now_et=now,
            is_half_day=self._half,
            open_positions=1,
            pending_buys=0,
            equity=Decimal("1600"),
            hwm=Decimal("1600"),
            entries_halted=self.halted,
        )

    def _complete(
        self,
        pos: Position,
        sold: Decimal,
        residual: Decimal,
        client_id: str,
        *,
        complete: bool,
    ) -> str:
        detail = self.broker.detail(client_id)
        order_id = client_id if detail is None else detail.order_id
        self.audit.record_sell(
            client_id,
            order_id,
            pos.symbol,
            sold,
            complete=complete,
        )
        pos.exit_client_order_id = client_id
        pos.residual_qty = residual
        pos.remaining_qty = residual
        pos.sell_lock = False
        if residual > 0:
            pos.state = EXIT_REQUIRED
            pos.alert = "FRACTIONAL_RESIDUAL"
            self.journal.save(pos)
            self.journal.alert("CRITICAL", "EXIT_REQUIRED", pos.symbol, format(residual, "f"))
            return pos.state
        return self._flat(pos, "EXIT_FILLED")

    def _book_detail_sell(self, pos: Position, detail: Detail) -> None:
        complete = detail.filled_qty >= detail.quantity and detail.status == "FILLED"
        self.audit.record_sell(
            detail.client_order_id,
            detail.order_id,
            pos.symbol,
            detail.filled_qty,
            complete=complete,
        )

    def _filled(self, client_id: str) -> bool:
        detail = self.broker.detail(client_id)
        return detail is not None and detail.status == "FILLED"

    def _flat(self, pos: Position, reason: str) -> str:
        pos.state = FLAT_RECONCILED
        pos.sell_lock = False
        pos.buy_cancel_pending = False
        pos.remaining_qty = Decimal(0)
        pos.alert = reason
        self.journal.save(pos)
        return pos.state

    def _freeze(self, pos: Position, code: str) -> str:
        pos.state = FROZEN_ESCALATED
        pos.sell_lock = True
        pos.alert = code
        self.journal.save(pos)
        self.journal.set_flag("halt_new_entries", "1")
        self.journal.alert("CRITICAL", code, pos.symbol, code)
        return pos.state

    def _lock(self, symbols: list[str]) -> None:
        self.journal.set_flag("halt_new_entries", "1")
        for pos in self.journal.list_positions():
            if pos.state == FLAT_RECONCILED:
                continue
            pos.state = UNCERTAIN
            pos.sell_lock = True
            pos.alert = "RECONCILE_BREAK"
            self.journal.save(pos)
        self.journal.alert("CRITICAL", "RECONCILE_BREAK", ",".join(symbols), "quantity break")

    def _flatten_all(self) -> None:
        symbols = [pos.symbol for pos in self.journal.list_positions()]
        for symbol in symbols:
            pos = self._must(symbol)
            if pos.state in {FLAT_RECONCILED, UNCERTAIN, FROZEN_ESCALATED}:
                continue
            if pos.state == EXIT_REQUIRED and pos.residual_qty > 0:
                continue
            if pos.state == CANCEL_PENDING_FOR_SL:
                self._on_tp_terminal(pos)
                continue
            self.request_exit(symbol, "FLATTEN")

    def _maybe_risk_exit(self, pos: Position) -> None:
        if pos.state not in {PARTIAL_POSITION, PROTECTED_POSITION, TP_PENDING}:
            return
        market = self.journal.market(pos.symbol)
        if market.bid <= pos.stop_price:
            self.request_exit(pos.symbol, "STOP")
            return
        if pos.opened_at is None or self._now is None:
            return
        opened = datetime.fromisoformat(pos.opened_at)
        if self._now - opened >= timedelta(seconds=pos.max_holding_seconds):
            self.request_exit(pos.symbol, "MAX_HOLD")

    def _apply_detail_fee(self, pos: Position, status: str, filled_qty: Decimal) -> None:
        if status != "FILLED" or filled_qty != pos.buy_qty:
            return
        detail = self.broker.detail(pos.buy_client_order_id)
        if detail is None or detail.fee is None:
            return
        pos.realized_fee = detail.fee
        pos.fee_source = "rest_detail"

    def _resume_guards(self) -> None:
        for row in self.journal.guards():
            if row.status in GUARD_UNKNOWN:
                self._finish_unknown(row)

    def _finish_unknown(self, row: GuardRow) -> PlaceResult:
        detail = self.broker.detail(row.client_order_id)
        if detail is None:
            self.journal.mark_guard(
                row.client_order_id,
                "DETAIL_ABSENT",
                attempts=row.place_attempts,
                code=row.last_code,
            )
            return PlaceResult(False, row.last_code, None, row.client_order_id)
        self.journal.mark_guard(
            row.client_order_id,
            "DETAIL_CONFIRMED",
            attempts=row.place_attempts,
            code=row.last_code,
        )
        return PlaceResult(True, row.last_code, detail.order_id, row.client_order_id)

    def _sync_position(self, pos: Position) -> None:
        if pos.state in {FLAT_RECONCILED, UNCERTAIN, FROZEN_ESCALATED}:
            return
        if pos.tp_client_order_id is not None:
            detail = self.broker.detail(pos.tp_client_order_id)
            if (
                detail is not None
                and detail.status == "FILLED"
                and not detail.shares_frozen
                and detail.filled_qty >= pos.filled_buy_qty
            ):
                self._book_detail_sell(pos, detail)
                self._flat(pos, "TP_FILLED")
                return
        if pos.state == CANCEL_PENDING_FOR_SL:
            self._on_tp_terminal(pos)

    def _cancel_timed_out(self, symbol: str) -> bool:
        raw = self.journal.get_flag(f"cancel_ns:{symbol}")
        if raw is None:
            return False
        return time.time_ns() - int(raw) >= CANCEL_CONFIRM_TIMEOUT_NS

    def _stamp(self) -> str:
        moment = self._now if self._now is not None else datetime.now(NEW_YORK)
        return moment.isoformat()

    def _must(self, symbol: str) -> Position:
        pos = self.journal.load(symbol)
        if pos is None:
            raise KeyError(symbol)
        return pos
