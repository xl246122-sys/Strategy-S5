"""Mock S5 book. It is not installed on the global Guardian, and it never places a live order."""

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from bstocks_v2.constitution.guard import (
    NEW_YORK,
    SESSION_POLICY_S5_RESEARCH,
    ConstitutionGuard,
)
from bstocks_v2.contracts.decimals import parse_decimal
from bstocks_v2.contracts.errors import FeeTrapError, OrderRejected, ShortSellingForbiddenError
from bstocks_v2.contracts.models import OrderDraft, OrderType, Side, TradingSession
from bstocks_v2.portfolio_risk.authority import EntryRequest, PortfolioRisk
from bstocks_v2.portfolio_risk.spec import COMMISSION_RATE
from bstocks_v2.research_backtest.tape import ClosedBar
from bstocks_v2.sessions.calendar import SessionLabel, TradingCalendar
from bstocks_v2.sessions.eligibility import SessionEligibility
from bstocks_v2.strategies.s5.execution import (
    TERMINAL_STATUS,
    FillView,
    ResearchQuote,
    checked_price,
    eligibility_reason,
    match_limit,
    quote_block,
    required_session,
    research_client_id,
    size_limit,
    tif_cross,
)
from bstocks_v2.strategies.s5.features import arm_window, clock_in_window
from bstocks_v2.strategies.s5.lock import refuse_live_route, s5_live_enabled
from bstocks_v2.strategies.s5.spec import ARM_A, ARM_B, ARM_C, OVERNIGHT_EXIT, POST_EXIT
from bstocks_v2.strategies.s5.universe import in_universe

_ENTRY_LABEL = {ARM_A: SessionLabel.RTH, ARM_B: SessionLabel.POST, ARM_C: SessionLabel.POST}
_SCHEMA = """
CREATE TABLE IF NOT EXISTS s5_flag (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS s5_alert (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    level TEXT NOT NULL,
    code TEXT NOT NULL,
    symbol TEXT NOT NULL,
    detail TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS s5_refusal (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    reason TEXT NOT NULL,
    order_type TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS s5_order (
    client_order_id TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    role TEXT NOT NULL,
    order_type TEXT NOT NULL,
    tif TEXT NOT NULL,
    placed_label TEXT NOT NULL,
    trading_session TEXT NOT NULL,
    price TEXT NOT NULL,
    quantity TEXT NOT NULL,
    filled_qty TEXT NOT NULL,
    filled_notional TEXT NOT NULL,
    total_cost_raw TEXT,
    rest_fee TEXT,
    status TEXT NOT NULL,
    shares_frozen INTEGER NOT NULL,
    code INTEGER,
    booked INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS s5_position (
    symbol TEXT PRIMARY KEY,
    state TEXT NOT NULL,
    arm TEXT NOT NULL,
    tif TEXT NOT NULL,
    generation INTEGER NOT NULL,
    buy_client_order_id TEXT NOT NULL,
    sell_client_order_id TEXT,
    buy_qty TEXT NOT NULL,
    filled_qty TEXT NOT NULL,
    sold_qty TEXT NOT NULL,
    limit_price TEXT NOT NULL,
    stop_price TEXT NOT NULL,
    take_profit_price TEXT NOT NULL,
    opened_at TEXT,
    exit_deadline TEXT NOT NULL,
    alert TEXT NOT NULL,
    last_reason TEXT NOT NULL,
    sell_lock INTEGER NOT NULL
);
"""


@dataclass
class ResearchOrder:
    client_order_id: str
    symbol: str
    side: str
    role: str
    order_type: str
    tif: str
    placed_label: str
    trading_session: str
    price: Decimal
    quantity: Decimal
    filled_qty: Decimal
    filled_notional: Decimal
    total_cost_raw: Decimal | None
    rest_fee: Decimal | None
    status: str
    shares_frozen: bool
    code: int | None
    booked: bool


@dataclass
class Held:
    symbol: str
    state: str
    arm: str
    tif: str
    generation: int
    buy_client_order_id: str
    sell_client_order_id: str | None
    buy_qty: Decimal
    filled_qty: Decimal
    sold_qty: Decimal
    limit_price: Decimal
    stop_price: Decimal
    take_profit_price: Decimal
    opened_at: str | None
    exit_deadline: str
    alert: str
    last_reason: str
    sell_lock: bool


@dataclass(frozen=True)
class EntryOutcome:
    accepted: bool
    reason: str
    state: str
    client_order_id: str | None
    sell_client_order_id: str | None
    adjustments: tuple[str, ...]
    filled_qty: Decimal
    quantity: Decimal
    limit_price: Decimal | None
    trading_session: str
    match_reason: str


class S5ResearchBook:
    def __init__(
        self,
        conn: sqlite3.Connection,
        risk: PortfolioRisk,
        calendar: TradingCalendar,
    ) -> None:
        self._conn = conn
        self._risk = risk
        self._calendar = calendar
        self._eligibility = SessionEligibility(calendar)
        self._guard = ConstitutionGuard()
        self._now: datetime | None = None

    @classmethod
    def open(cls, root: Path, risk: PortfolioRisk) -> "S5ResearchBook":
        if s5_live_enabled():
            raise RuntimeError("S5 code lock drifted")
        root.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(root / "s5_research.sqlite")
        conn.row_factory = sqlite3.Row
        conn.executescript(_SCHEMA)
        conn.commit()
        return cls(conn, risk, TradingCalendar.load())

    def close(self) -> None:
        self._conn.close()

    def live_place(self) -> None:
        refuse_live_route("/sapi/v1/equity/order/place")

    def live_cancel(self) -> None:
        refuse_live_route("/sapi/v1/equity/order/cancel")

    @property
    def halted(self) -> bool:
        return self._flag("halt_new_entries") == "1"

    def alerts(self) -> list[tuple[str, str, str]]:
        rows = self._conn.execute(
            "SELECT level, code, symbol FROM s5_alert ORDER BY id"
        ).fetchall()
        return [(str(row["level"]), str(row["code"]), str(row["symbol"])) for row in rows]

    def refusals(self) -> list[tuple[str, str, str]]:
        rows = self._conn.execute(
            "SELECT symbol, reason, order_type FROM s5_refusal ORDER BY id"
        ).fetchall()
        return [(str(row["symbol"]), str(row["reason"]), str(row["order_type"])) for row in rows]

    def orders(self) -> list[ResearchOrder]:
        rows = self._conn.execute("SELECT * FROM s5_order ORDER BY client_order_id").fetchall()
        return [_order_from(row) for row in rows]

    def get(self, symbol: str) -> Held:
        found = self._position(symbol)
        if found is None:
            raise OrderRejected("UNKNOWN_SYMBOL")
        return found

    def market_orders(self) -> list[ResearchOrder]:
        return [row for row in self.orders() if row.order_type == "MARKET"]

    def realized_fee(self, client_order_id: str) -> Decimal | None:
        """Blank until the order is filled. `total_cost - filled_notional` is never a fee."""

        order = self._must_order(client_order_id)
        if order.status != "FILLED":
            return None
        return order.rest_fee

    def enter(
        self,
        symbol: str,
        arm: str,
        now_et: datetime,
        row: object,
        quote: ResearchQuote,
        *,
        news: str,
        regime: str,
        tif: str = "DAY",
        bar: ClosedBar | None = None,
    ) -> EntryOutcome:
        self._stamp(now_et)
        empty = _empty()
        if self.halted:
            return _reject(empty, "HALT_NEW_ENTRIES")
        if not in_universe(symbol) or symbol.startswith("EQ_"):
            return _reject(empty, "NOT_IN_UNIVERSE")
        if tif not in {"DAY", "GTC"}:
            return _reject(empty, "TIF")
        if regime in {"UNKNOWN", "HIGH_VOL_PANIC"}:
            return _reject(empty, "REGIME_BLOCKED")
        if regime == "":
            return _reject(empty, "REGIME_NOT_ATTACHED")
        if news != "CLEAR":
            return _reject(empty, "NEWS_UNKNOWN")
        if self._position(symbol) is not None:
            return _reject(empty, "ALREADY_IN_POSITION")
        local = now_et.astimezone(NEW_YORK)
        if not self._calendar.is_session(local.date()):
            return _reject(empty, "MARKET_CLOSED")
        label = self._calendar.label(now_et)
        if label is not _ENTRY_LABEL.get(arm):
            return _reject(empty, "WINDOW")
        start, end = arm_window(arm, self._calendar.close_time(local.date()))
        if not clock_in_window(now_et, start, end):
            return _reject(empty, "WINDOW")
        blocked = quote_block(quote)
        if blocked is not None:
            return _reject(empty, blocked)
        assert quote.bid is not None
        session = required_session(label)
        if session is None:
            return _reject(empty, "MARKET_CLOSED")
        reason, _digest = eligibility_reason(
            self._calendar,
            self._eligibility,
            row,
            symbol=symbol,
            now_et=now_et,
            side=Side.BUY,
            order_type=OrderType.LIMIT,
            trading_session=session,
        )
        if reason != "OK":
            return _reject(empty, reason)
        if arm == ARM_C:
            overnight, _digest = eligibility_reason(
                self._calendar,
                self._eligibility,
                row,
                symbol=symbol,
                now_et=_overnight_probe(now_et),
                side=Side.BUY,
                order_type=OrderType.LIMIT,
                trading_session=TradingSession.H24,
            )
            if overnight != "OK":
                return _reject(empty, overnight)
        view = self._risk.snapshot()
        sized = size_limit(quote.bid, view.equity, quote.multiplier_down, quote.multiplier_up)
        if isinstance(sized, str):
            return _reject(empty, sized)
        client_id = research_client_id(symbol, "buy", 0)
        decision = self._risk.approve(
            EntryRequest(
                symbol=symbol,
                client_order_id=client_id,
                limit_price=sized.price,
                quantity=sized.quantity,
                stop_price=sized.stop_price,
                take_profit_price=sized.take_profit_price,
                now_et=now_et,
                is_half_day=self._calendar.is_half_day(local.date()),
                trading_session=session,
            ),
            session_policy=SESSION_POLICY_S5_RESEARCH,
        )
        if not decision.accepted:
            return _reject(empty, decision.reason)
        buy = ResearchOrder(
            client_id,
            symbol,
            "BUY",
            "buy",
            "LIMIT",
            tif,
            label.value,
            session.value,
            sized.price,
            sized.quantity,
            Decimal(0),
            Decimal(0),
            None,
            None,
            "NEW",
            False,
            None,
            False,
        )
        held = Held(
            symbol,
            "BUY_PENDING",
            arm,
            tif,
            0,
            client_id,
            None,
            sized.quantity,
            Decimal(0),
            Decimal(0),
            sized.price,
            sized.stop_price,
            sized.take_profit_price,
            None,
            _deadline(arm, now_et).isoformat(),
            "",
            "",
            False,
        )
        self._save_order(buy)
        self._save_pos(held)
        if bar is not None:
            self._match_buy(held, buy, bar, quote, now_et, row)
        return self._outcome(held, sized.adjustments, buy.trading_session)

    def on_clock(
        self,
        now_et: datetime,
        row: object,
        quote: ResearchQuote,
        *,
        trading_status: str,
        bar: ClosedBar | None = None,
    ) -> None:
        self._stamp(now_et)
        label = self._calendar.label(now_et)
        for pos in self._positions():
            self._advance(pos, now_et, label, row, quote, trading_status, bar)

    def request_stop(
        self,
        symbol: str,
        now_et: datetime,
        row: object,
        quote: ResearchQuote,
        *,
        bar: ClosedBar | None = None,
    ) -> str:
        self._stamp(now_et)
        pos = self._must(symbol)
        if pos.state == "FLAT_RECONCILED":
            return pos.state
        label = self._calendar.label(now_et)
        self._begin_stop(pos, now_et, label, row, quote, bar)
        return self._must(symbol).state

    def request_market_sell(self, symbol: str, now_et: datetime, row: object) -> str:
        self._stamp(now_et)
        reason, _digest = eligibility_reason(
            self._calendar,
            self._eligibility,
            row,
            symbol=symbol,
            now_et=now_et,
            side=Side.SELL,
            order_type=OrderType.MARKET,
            trading_session=None,
        )
        if reason == "OK":
            reason = "S5_LIMIT_ONLY"
        self._conn.execute(
            "INSERT INTO s5_refusal (symbol, reason, order_type) VALUES (?, ?, 'MARKET')",
            (symbol, reason),
        )
        self._conn.commit()
        return reason

    def try_sell(self, symbol: str, quantity: Decimal, now_et: datetime) -> None:
        self._stamp(now_et)
        pos = self._must(symbol)
        open_qty = pos.filled_qty - pos.sold_qty
        if quantity <= 0 or quantity > open_qty:
            self._reject_short(symbol, quantity, open_qty, now_et)
        raise OrderRejected("S5_LIMIT_ONLY")

    def apply_status(
        self,
        client_order_id: str,
        status: str,
        *,
        filled_qty: Decimal | None = None,
        shares_frozen: bool = False,
        code: int | None = None,
        rest_fee: Decimal | None = None,
        total_cost_raw: Decimal | None = None,
    ) -> None:
        order = self._must_order(client_order_id)
        if order.status != "FILLED" and rest_fee is not None:
            raise FeeTrapError("refusing tc-FN as realized commission before the order is filled")
        if filled_qty is not None:
            if filled_qty < order.filled_qty or filled_qty > order.quantity:
                raise OrderRejected("FILL_QUANTITY")
            order.filled_qty = filled_qty
            order.filled_notional = order.price * filled_qty
        order.status = status
        order.shares_frozen = shares_frozen
        order.code = code
        if total_cost_raw is not None:
            order.total_cost_raw = total_cost_raw
        if order.status == "FILLED":
            order.rest_fee = rest_fee
        self._save_order(order)
        if order.side == "BUY" and status == "REJECTED" and order.filled_qty == 0:
            self._risk.cancel_unfilled(order.client_order_id)
            pos = self._must(order.symbol)
            pos.state = "FLAT_RECONCILED"
            pos.alert = "EXCHANGE_REJECT"
            pos.last_reason = "EXCHANGE_REJECT"
            self._save_pos(pos)

    def note_cost(self, client_order_id: str, total_cost_raw: Decimal) -> None:
        order = self._must_order(client_order_id)
        if order.status == "FILLED":
            raise OrderRejected("ORDER_TERMINAL")
        if self._now is None:
            raise OrderRejected("CLOCK")
        bid = order.price
        self._risk.on_buy_report(
            order.client_order_id,
            status=order.status,
            filled_qty=order.filled_qty,
            filled_notional=order.filled_notional,
            total_cost_raw=total_cost_raw,
            rest_fee=None,
            bid=bid,
            now_et=self._now,
        )
        order.total_cost_raw = total_cost_raw
        self._save_order(order)

    def declare_fee(self, client_order_id: str, fee: Decimal) -> None:
        order = self._must_order(client_order_id)
        if order.status != "FILLED":
            raise FeeTrapError("refusing tc-FN as realized commission before the order is filled")
        order.rest_fee = fee
        self._save_order(order)

    def _advance(
        self,
        pos: Held,
        now_et: datetime,
        label: SessionLabel,
        row: object,
        quote: ResearchQuote,
        trading_status: str,
        bar: ClosedBar | None,
    ) -> None:
        if pos.state == "FLAT_RECONCILED":
            return
        if bar is not None and bar.symbol != pos.symbol:
            bar = None
        if pos.state in {"BUY_PENDING", "PARTIAL_POSITION"}:
            self._advance_buy(pos, now_et, label, bar, quote, row)
            return
        if trading_status == "TRADING_HALTED" or label is SessionLabel.NON_SESSION:
            self._expire_sell(pos, label)
            reason = "TRADING_HALTED" if trading_status == "TRADING_HALTED" else "MARKET_CLOSED"
            self._require(pos, reason)
            return
        if quote.bid is None:
            self._expire_sell(pos, label)
            self._require(pos, "NO_BID")
            return
        self._expire_sell(pos, label)
        pos = self._must(pos.symbol)
        if pos.state in {"FLAT_RECONCILED", "EXIT_REQUIRED"}:
            if pos.state == "EXIT_REQUIRED":
                reason = pos.alert or "EXIT_REQUIRED"
                self._try_limit_exit(pos, now_et, label, row, quote, bar, reason)
            return
        if pos.sell_lock or pos.state == "CANCEL_PENDING_FOR_SL":
            self._finish_cancel(pos, now_et, label, row, quote, bar)
            return
        if _stop_hit(pos, quote, bar):
            self._begin_stop(pos, now_et, label, row, quote, bar)
            return
        if now_et >= datetime.fromisoformat(pos.exit_deadline):
            self._begin_limit_exit(pos, now_et, label, row, quote, bar, "DEADLINE_UNFILLED")
            return
        if pos.sell_client_order_id is None:
            self._arm(pos, now_et, label, row, quote, pos.take_profit_price, "tp")
            pos = self._must(pos.symbol)
        if bar is not None and pos.sell_client_order_id is not None and not pos.sell_lock:
            self._match_sell(pos, bar, quote, now_et)

    def _advance_buy(
        self,
        pos: Held,
        now_et: datetime,
        label: SessionLabel,
        bar: ClosedBar | None,
        quote: ResearchQuote,
        row: object,
    ) -> None:
        order = self._must_order(pos.buy_client_order_id)
        if bar is not None and _mixed(self._calendar, bar):
            pos.last_reason = "MIXED_BAR"
            self._save_pos(pos)
            bar = None
        crossed = tif_cross(pos.tif, order.placed_label, label.value)
        if crossed is not None and order.status not in TERMINAL_STATUS:
            self._expire(order)
            if order.filled_qty == 0:
                self._risk.cancel_unfilled(order.client_order_id)
                pos.state = "FLAT_RECONCILED"
                pos.alert = crossed
                if pos.last_reason != "MIXED_BAR":
                    pos.last_reason = crossed
                self._save_pos(pos)
                return
            pos.state = "PROTECTED_POSITION"
            self._save_pos(pos)
        if bar is None or pos.state == "FLAT_RECONCILED":
            return
        self._match_buy(pos, self._must_order(pos.buy_client_order_id), bar, quote, now_et, row)

    def _match_buy(
        self,
        pos: Held,
        order: ResearchOrder,
        bar: ClosedBar,
        quote: ResearchQuote,
        now_et: datetime,
        row: object,
    ) -> None:
        if bar.symbol != pos.symbol:
            raise OrderRejected("BAR_SYMBOL")
        view = match_limit(
            self._calendar,
            side=Side.BUY,
            limit=order.price,
            bar=bar,
            placed_label=order.placed_label,
            now_et=now_et,
            displayed=bar.volume,
            remaining=order.quantity - order.filled_qty,
        )
        pos.last_reason = view.reason
        if view.qty <= 0 or view.price is None or view.status == "UNFILLED":
            self._save_pos(pos)
            return
        self._book_buy(pos, order, view, quote, row)

    def _book_buy(
        self,
        pos: Held,
        order: ResearchOrder,
        view: FillView,
        quote: ResearchQuote,
        row: object,
    ) -> None:
        if view.price is None or self._now is None:
            raise OrderRejected("FILL")
        filled = order.filled_qty + view.qty
        notional = order.filled_notional + view.price * view.qty
        status = "FILLED" if filled == order.quantity else "PARTIALLY_FILLED"
        fee = COMMISSION_RATE * notional if status == "FILLED" else None
        bid = quote.bid if quote.bid is not None else view.price
        self._risk.on_buy_report(
            order.client_order_id,
            status=status,
            filled_qty=filled,
            filled_notional=notional,
            total_cost_raw=notional,
            rest_fee=fee,
            bid=bid,
            now_et=self._now,
        )
        order.filled_qty = filled
        order.filled_notional = notional
        order.status = status
        order.rest_fee = fee
        order.total_cost_raw = notional
        pos.filled_qty = filled
        pos.opened_at = pos.opened_at or self._now.isoformat()
        pos.state = "PROTECTED_POSITION" if status == "FILLED" else "PARTIAL_POSITION"
        self._save_order(order)
        self._save_pos(pos)
        if status == "FILLED":
            self._arm(
                pos,
                self._now,
                self._calendar.label(self._now),
                row,
                quote,
                pos.take_profit_price,
                "tp",
            )

    def _arm(
        self,
        pos: Held,
        now_et: datetime,
        label: SessionLabel,
        row: object,
        quote: ResearchQuote,
        price: Decimal,
        role: str,
    ) -> None:
        if pos.sell_lock or pos.state == "CANCEL_PENDING_FOR_SL":
            return
        session = required_session(label)
        if session is None:
            self._require(pos, "MARKET_CLOSED")
            return
        reason, _digest = eligibility_reason(
            self._calendar,
            self._eligibility,
            row,
            symbol=pos.symbol,
            now_et=now_et,
            side=Side.SELL,
            order_type=OrderType.LIMIT,
            trading_session=session,
        )
        if reason != "OK":
            self._require(pos, reason)
            return
        qty = pos.filled_qty - pos.sold_qty
        if qty <= 0:
            pos.state = "FLAT_RECONCILED"
            self._save_pos(pos)
            return
        order = self._submit_sell(pos, role, price, session, label, qty)
        pos.sell_client_order_id = order.client_order_id
        pos.state = "TP_PENDING" if role == "tp" else "EXIT_REQUIRED"
        self._save_pos(pos)

    def _begin_stop(
        self,
        pos: Held,
        now_et: datetime,
        label: SessionLabel,
        row: object,
        quote: ResearchQuote,
        bar: ClosedBar | None,
    ) -> None:
        if pos.sell_client_order_id is not None:
            working = self._must_order(pos.sell_client_order_id)
            if working.role == "exit" and working.status not in TERMINAL_STATUS:
                if bar is not None:
                    self._match_sell(pos, bar, quote, now_et)
                fresh = self._must(pos.symbol)
                if fresh.state != "FLAT_RECONCILED" and fresh.sold_qty == 0:
                    self._require(fresh, "STOP_UNFILLED")
                return
            verdict = self._cancel(pos)
            if verdict == "FILLED":
                return
            if verdict == "UNCONFIRMED":
                self._require(pos, "CANCEL_486444_UNCONFIRMED")
                return
            if verdict == "PENDING":
                return
        pos = self._must(pos.symbol)
        self._place_exit(pos, now_et, label, row, quote, bar, pos.stop_price, "STOP_UNFILLED")

    def _begin_limit_exit(
        self,
        pos: Held,
        now_et: datetime,
        label: SessionLabel,
        row: object,
        quote: ResearchQuote,
        bar: ClosedBar | None,
        reason: str,
    ) -> None:
        if quote.bid is None:
            self._require(pos, "NO_BID")
            return
        if pos.sell_client_order_id is not None:
            verdict = self._cancel(pos)
            if verdict in {"FILLED", "UNCONFIRMED", "PENDING"}:
                if verdict == "UNCONFIRMED":
                    self._require(pos, "CANCEL_486444_UNCONFIRMED")
                return
        price = checked_price(quote.bid)
        if price < quote.multiplier_down or price > quote.multiplier_up:
            self._require(pos, "PRICE_CAGE")
            return
        self._place_exit(self._must(pos.symbol), now_et, label, row, quote, bar, price, reason)

    def _try_limit_exit(
        self,
        pos: Held,
        now_et: datetime,
        label: SessionLabel,
        row: object,
        quote: ResearchQuote,
        bar: ClosedBar | None,
        reason: str,
    ) -> None:
        if pos.sell_client_order_id is not None:
            order = self._must_order(pos.sell_client_order_id)
            if order.status not in TERMINAL_STATUS:
                if bar is not None:
                    self._match_sell(pos, bar, quote, now_et)
                return
        self._begin_limit_exit(pos, now_et, label, row, quote, bar, reason)

    def _place_exit(
        self,
        pos: Held,
        now_et: datetime,
        label: SessionLabel,
        row: object,
        quote: ResearchQuote,
        bar: ClosedBar | None,
        price: Decimal,
        unfilled_reason: str,
    ) -> None:
        self._arm(pos, now_et, label, row, quote, price, "exit")
        pos = self._must(pos.symbol)
        if pos.sell_client_order_id is None or pos.state == "EXIT_REQUIRED" and pos.alert:
            if pos.state == "EXIT_REQUIRED":
                return
        if bar is not None and pos.sell_client_order_id is not None:
            self._match_sell(pos, bar, quote, now_et)
            pos = self._must(pos.symbol)
        if pos.state != "FLAT_RECONCILED":
            order = None
            if pos.sell_client_order_id is not None:
                order = self._order(pos.sell_client_order_id)
            if order is None or order.filled_qty == order.quantity:
                return
            if order.filled_qty == 0:
                self._require(pos, unfilled_reason)

    def _match_sell(
        self,
        pos: Held,
        bar: ClosedBar,
        quote: ResearchQuote,
        now_et: datetime,
    ) -> None:
        if pos.sell_client_order_id is None:
            return
        order = self._must_order(pos.sell_client_order_id)
        if order.status in TERMINAL_STATUS:
            return
        view = match_limit(
            self._calendar,
            side=Side.SELL,
            limit=order.price,
            bar=bar,
            placed_label=order.placed_label,
            now_et=now_et,
            displayed=bar.volume,
            remaining=order.quantity - order.filled_qty,
        )
        pos.last_reason = view.reason
        if view.qty <= 0 or view.price is None:
            self._save_pos(pos)
            return
        order.filled_qty += view.qty
        order.filled_notional += view.price * view.qty
        order.status = "FILLED" if order.filled_qty == order.quantity else "PARTIALLY_FILLED"
        self._save_order(order)
        self._apply_sell(pos, order)

    def _apply_sell(self, pos: Held, order: ResearchOrder) -> None:
        if order.booked or self._now is None:
            return
        qty = order.filled_qty - pos.sold_qty
        if qty <= 0:
            order.booked = order.filled_qty > 0
            self._save_order(order)
            return
        if qty > pos.filled_qty - pos.sold_qty:
            raise OrderRejected("SHORT")
        self._risk.reduce_position(pos.symbol, qty, order.price, self._now)
        pos.sold_qty += qty
        order.booked = True
        self._save_order(order)
        if pos.sold_qty >= pos.filled_qty:
            pos.state = "FLAT_RECONCILED"
            pos.sell_lock = False
            pos.alert = "EXIT_FILLED"
        self._save_pos(pos)

    def _cancel(self, pos: Held) -> str:
        if pos.sell_client_order_id is None:
            return "NONE"
        order = self._must_order(pos.sell_client_order_id)
        if order.status == "FILLED":
            self._apply_sell(pos, order)
            return "FILLED"
        if order.code == 486444 and order.status != "FILLED":
            return "UNCONFIRMED"
        if order.shares_frozen or order.code == 486446:
            pos.state = "CANCEL_PENDING_FOR_SL"
            pos.sell_lock = True
            self._save_pos(pos)
            return "PENDING"
        if order.status not in TERMINAL_STATUS:
            order.status = "CANCELED"
            self._save_order(order)
        pos.sell_client_order_id = None
        pos.sell_lock = False
        self._save_pos(pos)
        return "CANCELED"

    def _finish_cancel(
        self,
        pos: Held,
        now_et: datetime,
        label: SessionLabel,
        row: object,
        quote: ResearchQuote,
        bar: ClosedBar | None,
    ) -> None:
        verdict = self._cancel(pos)
        if verdict == "PENDING":
            return
        if verdict == "UNCONFIRMED":
            self._require(pos, "CANCEL_486444_UNCONFIRMED")
            return
        if verdict == "FILLED":
            return
        self._begin_stop(self._must(pos.symbol), now_et, label, row, quote, bar)

    def _expire_sell(self, pos: Held, label: SessionLabel) -> None:
        if pos.sell_client_order_id is None:
            return
        order = self._must_order(pos.sell_client_order_id)
        crossed = tif_cross(pos.tif, order.placed_label, label.value)
        if crossed is None or order.status in TERMINAL_STATUS:
            return
        self._expire(order)
        pos.sell_client_order_id = None
        pos.sell_lock = False
        pos.last_reason = crossed
        if pos.state == "TP_PENDING":
            pos.state = "PROTECTED_POSITION"
        self._save_pos(pos)

    def _expire(self, order: ResearchOrder) -> None:
        order.status = "EXPIRED_UNVERIFIED"
        self._save_order(order)

    def _submit_sell(
        self,
        pos: Held,
        role: str,
        price: Decimal,
        session: TradingSession,
        label: SessionLabel,
        quantity: Decimal,
    ) -> ResearchOrder:
        if quantity <= 0 or quantity > pos.filled_qty - pos.sold_qty:
            raise OrderRejected("SHORT")
        checked_price(price)
        client_id = research_client_id(pos.symbol, role, pos.generation)
        existing = self._order(client_id)
        if existing is not None and existing.status not in TERMINAL_STATUS:
            return existing
        if existing is not None:
            pos.generation += 1
            self._save_pos(pos)
            client_id = research_client_id(pos.symbol, role, pos.generation)
        draft = OrderDraft(
            symbol=pos.symbol,
            side=Side.SELL,
            order_type=OrderType.LIMIT,
            client_order_id=client_id,
            quantity=quantity,
            price=price,
            trading_session=session,
            quote_asset="USDC",
            wallet_type="CARD",
            tokenize=False,
            is_opening=False,
            confirmed_long_qty=pos.filled_qty - pos.sold_qty,
            research_only=True,
        )
        now = self._now if self._now is not None else datetime.now(NEW_YORK)
        view = self._risk.snapshot()
        try:
            self._guard.check_order(
                draft,
                now_et=now,
                is_half_day=False,
                open_positions=view.slots,
                pending_buys=0,
                equity=view.equity,
                hwm=view.hwm,
                entries_halted=view.halt_new_entries,
                session_policy=SESSION_POLICY_S5_RESEARCH,
            )
        except ShortSellingForbiddenError as exc:
            raise OrderRejected("SHORT") from exc
        order = ResearchOrder(
            client_id,
            pos.symbol,
            "SELL",
            role,
            "LIMIT",
            pos.tif,
            label.value,
            session.value,
            price,
            quantity,
            Decimal(0),
            Decimal(0),
            None,
            None,
            "NEW",
            False,
            None,
            False,
        )
        self._save_order(order)
        return order

    def _require(self, pos: Held, reason: str) -> None:
        pos.state = "EXIT_REQUIRED"
        pos.alert = reason
        pos.last_reason = reason
        self._save_pos(pos)
        self._set_flag("halt_new_entries", "1")
        codes = {code for _level, code, _symbol in self.alerts()}
        if reason not in codes:
            self._conn.execute(
                "INSERT INTO s5_alert (level, code, symbol, detail) VALUES ('CRITICAL', ?, ?, ?)",
                (reason, pos.symbol, reason),
            )
            self._conn.commit()

    def _reject_short(
        self,
        symbol: str,
        quantity: Decimal,
        open_qty: Decimal,
        now_et: datetime,
    ) -> None:
        draft = OrderDraft(
            symbol=symbol,
            side=Side.SELL,
            order_type=OrderType.LIMIT,
            client_order_id=research_client_id(symbol, "probe", 9),
            quantity=quantity,
            price=Decimal("100"),
            trading_session=TradingSession.RTH,
            quote_asset="USDC",
            wallet_type="CARD",
            tokenize=False,
            is_opening=False,
            confirmed_long_qty=open_qty,
            research_only=True,
        )
        view = self._risk.snapshot()
        try:
            self._guard.check_order(
                draft,
                now_et=now_et,
                is_half_day=False,
                open_positions=view.slots,
                pending_buys=0,
                equity=view.equity,
                hwm=view.hwm,
                entries_halted=False,
                session_policy=SESSION_POLICY_S5_RESEARCH,
            )
        except ShortSellingForbiddenError as exc:
            raise OrderRejected("SHORT") from exc
        raise OrderRejected("SHORT")

    def _outcome(self, pos: Held, adjustments: tuple[str, ...], session: str) -> EntryOutcome:
        fresh = self._must(pos.symbol)
        return EntryOutcome(
            True,
            "ACCEPTED",
            fresh.state,
            fresh.buy_client_order_id,
            fresh.sell_client_order_id,
            adjustments,
            fresh.filled_qty,
            fresh.buy_qty,
            fresh.limit_price,
            session,
            fresh.last_reason,
        )

    def _stamp(self, now_et: datetime) -> None:
        if now_et.tzinfo is None:
            raise OrderRejected("NAIVE_DATETIME")
        self._now = now_et.astimezone(NEW_YORK)

    def _flag(self, key: str) -> str | None:
        row = self._conn.execute("SELECT value FROM s5_flag WHERE key = ?", (key,)).fetchone()
        if row is None:
            return None
        return str(row["value"])

    def _set_flag(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT INTO s5_flag (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        self._conn.commit()

    def _positions(self) -> list[Held]:
        rows = self._conn.execute("SELECT * FROM s5_position ORDER BY symbol").fetchall()
        return [_pos_from(row) for row in rows]

    def _position(self, symbol: str) -> Held | None:
        row = self._conn.execute(
            "SELECT * FROM s5_position WHERE symbol = ?",
            (symbol,),
        ).fetchone()
        if row is None:
            return None
        return _pos_from(row)

    def _must(self, symbol: str) -> Held:
        found = self._position(symbol)
        if found is None:
            raise OrderRejected("UNKNOWN_SYMBOL")
        return found

    def _order(self, client_order_id: str) -> ResearchOrder | None:
        row = self._conn.execute(
            "SELECT * FROM s5_order WHERE client_order_id = ?",
            (client_order_id,),
        ).fetchone()
        if row is None:
            return None
        return _order_from(row)

    def _must_order(self, client_order_id: str) -> ResearchOrder:
        found = self._order(client_order_id)
        if found is None:
            raise OrderRejected("UNKNOWN_ORDER")
        return found

    def _save_order(self, order: ResearchOrder) -> None:
        self._conn.execute(
            """
            INSERT INTO s5_order (
                client_order_id, symbol, side, role, order_type, tif, placed_label,
                trading_session, price, quantity, filled_qty, filled_notional,
                total_cost_raw, rest_fee, status, shares_frozen, code, booked
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(client_order_id) DO UPDATE SET
                filled_qty = excluded.filled_qty,
                filled_notional = excluded.filled_notional,
                total_cost_raw = excluded.total_cost_raw,
                rest_fee = excluded.rest_fee,
                status = excluded.status,
                shares_frozen = excluded.shares_frozen,
                code = excluded.code,
                booked = excluded.booked
            """,
            _order_tuple(order),
        )
        self._conn.commit()

    def _save_pos(self, pos: Held) -> None:
        self._conn.execute(
            """
            INSERT INTO s5_position (
                symbol, state, arm, tif, generation, buy_client_order_id, sell_client_order_id,
                buy_qty, filled_qty, sold_qty, limit_price, stop_price, take_profit_price,
                opened_at, exit_deadline, alert, last_reason, sell_lock
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(symbol) DO UPDATE SET
                state = excluded.state,
                generation = excluded.generation,
                sell_client_order_id = excluded.sell_client_order_id,
                filled_qty = excluded.filled_qty,
                sold_qty = excluded.sold_qty,
                opened_at = excluded.opened_at,
                alert = excluded.alert,
                last_reason = excluded.last_reason,
                sell_lock = excluded.sell_lock
            """,
            _pos_tuple(pos),
        )
        self._conn.commit()


def _overnight_probe(now_et: datetime) -> datetime:
    local = now_et.astimezone(NEW_YORK)
    probe = datetime.combine(local.date(), datetime.min.time(), tzinfo=NEW_YORK)
    return probe.replace(hour=20, minute=1)


def _deadline(arm: str, now_et: datetime) -> datetime:
    local = now_et.astimezone(NEW_YORK)
    if arm == ARM_C:
        return datetime.combine(local.date() + timedelta(days=1), OVERNIGHT_EXIT, tzinfo=NEW_YORK)
    return datetime.combine(local.date(), POST_EXIT, tzinfo=NEW_YORK)


def _mixed(calendar: TradingCalendar, bar: ClosedBar) -> bool:
    return calendar.label(bar.start) is not calendar.label(bar.end)


def _stop_hit(pos: Held, quote: ResearchQuote, bar: ClosedBar | None) -> bool:
    if bar is not None and bar.low <= pos.stop_price:
        return True
    return quote.bid is not None and quote.bid <= pos.stop_price


def _empty() -> EntryOutcome:
    return EntryOutcome(False, "", "", None, None, (), Decimal(0), Decimal(0), None, "", "")


def _reject(blank: EntryOutcome, reason: str) -> EntryOutcome:
    return EntryOutcome(
        False,
        reason,
        "",
        None,
        None,
        (),
        Decimal(0),
        Decimal(0),
        None,
        "",
        reason,
    )


def _opt(value: object) -> Decimal | None:
    if value is None:
        return None
    return parse_decimal(value)


def _order_from(row: sqlite3.Row) -> ResearchOrder:
    code = row["code"]
    return ResearchOrder(
        str(row["client_order_id"]),
        str(row["symbol"]),
        str(row["side"]),
        str(row["role"]),
        str(row["order_type"]),
        str(row["tif"]),
        str(row["placed_label"]),
        str(row["trading_session"]),
        parse_decimal(row["price"]),
        parse_decimal(row["quantity"]),
        parse_decimal(row["filled_qty"]),
        parse_decimal(row["filled_notional"]),
        _opt(row["total_cost_raw"]),
        _opt(row["rest_fee"]),
        str(row["status"]),
        bool(row["shares_frozen"]),
        None if code is None else int(code),
        bool(row["booked"]),
    )


def _pos_from(row: sqlite3.Row) -> Held:
    sell = row["sell_client_order_id"]
    opened = row["opened_at"]
    return Held(
        str(row["symbol"]),
        str(row["state"]),
        str(row["arm"]),
        str(row["tif"]),
        int(row["generation"]),
        str(row["buy_client_order_id"]),
        None if sell is None else str(sell),
        parse_decimal(row["buy_qty"]),
        parse_decimal(row["filled_qty"]),
        parse_decimal(row["sold_qty"]),
        parse_decimal(row["limit_price"]),
        parse_decimal(row["stop_price"]),
        parse_decimal(row["take_profit_price"]),
        None if opened is None else str(opened),
        str(row["exit_deadline"]),
        str(row["alert"]),
        str(row["last_reason"]),
        bool(row["sell_lock"]),
    )


def _order_tuple(order: ResearchOrder) -> tuple[object, ...]:
    return (
        order.client_order_id,
        order.symbol,
        order.side,
        order.role,
        order.order_type,
        order.tif,
        order.placed_label,
        order.trading_session,
        format(order.price, "f"),
        format(order.quantity, "f"),
        format(order.filled_qty, "f"),
        format(order.filled_notional, "f"),
        None if order.total_cost_raw is None else format(order.total_cost_raw, "f"),
        None if order.rest_fee is None else format(order.rest_fee, "f"),
        order.status,
        1 if order.shares_frozen else 0,
        order.code,
        1 if order.booked else 0,
    )


def _pos_tuple(pos: Held) -> tuple[object, ...]:
    return (
        pos.symbol,
        pos.state,
        pos.arm,
        pos.tif,
        pos.generation,
        pos.buy_client_order_id,
        pos.sell_client_order_id,
        format(pos.buy_qty, "f"),
        format(pos.filled_qty, "f"),
        format(pos.sold_qty, "f"),
        format(pos.limit_price, "f"),
        format(pos.stop_price, "f"),
        format(pos.take_profit_price, "f"),
        pos.opened_at,
        pos.exit_deadline,
        pos.alert,
        pos.last_reason,
        1 if pos.sell_lock else 0,
    )
