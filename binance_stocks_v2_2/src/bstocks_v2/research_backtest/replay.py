"""Event-driven offline replay. Nothing in this module can reach an exchange."""

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Protocol

from bstocks_v2.constitution.guard import (
    ConstitutionGuard,
    entry_window_open,
    flatten_deadline,
    live_trade_enabled,
)
from bstocks_v2.contracts.errors import LiveTradingDisabledError, OrderRejected
from bstocks_v2.contracts.models import TradeIntent
from bstocks_v2.research_backtest.audit import (
    holding_deadline,
    opportunity_cost,
    per_share,
    window_extremes,
)
from bstocks_v2.research_backtest.errors import LookaheadError
from bstocks_v2.research_backtest.exits import (
    EntryPrint,
    ExitFill,
    conservative_entry,
    resolve_bracket,
)
from bstocks_v2.research_backtest.fills import buy_limit, penetration_delta
from bstocks_v2.research_backtest.models import (
    EngineConfig,
    FillMode,
    IntentRecord,
    ReplayEvent,
    ReplayResult,
    SymbolFilter,
)
from bstocks_v2.research_backtest.sizing import SizedOrder, SizingRejection, plan_buy
from bstocks_v2.research_backtest.tape import BarTape, CausalView, ClosedBar
from bstocks_v2.research_data.calendar import NEW_YORK, XnysCalendar, load_xnys_calendar

_RETAINED = Decimal("0.92")


class DecisionStrategy(Protocol):
    def on_close(self, view: CausalView, bar: ClosedBar) -> TradeIntent | None:
        """Return one intent for this closed bar, or nothing."""


@dataclass
class _Leg:
    intent_id: str
    symbol: str
    signal_bar_end: datetime
    max_holding_seconds: int
    reasons: list[str] = field(default_factory=list)
    adjustments: list[str] = field(default_factory=list)
    cancel_reason: str | None = None
    client_order_id: str | None = None
    limit_price: Decimal | None = None
    delta: Decimal | None = None
    ordered_qty: Decimal = Decimal(0)
    filled_qty: Decimal = Decimal(0)
    open_qty: Decimal = Decimal(0)
    entry_cost: Decimal = Decimal(0)
    exit_proceeds: Decimal = Decimal(0)
    exit_qty: Decimal = Decimal(0)
    entry_commission: Decimal = Decimal(0)
    exit_commission: Decimal = Decimal(0)
    spread_cost: Decimal = Decimal(0)
    gross_pnl: Decimal = Decimal(0)
    exit_reason: str = "NONE"
    ambiguous: bool = False
    entry_bar_end: datetime | None = None
    exit_bar_end: datetime | None = None
    path_high: Decimal | None = None
    path_low: Decimal | None = None


@dataclass
class _Working:
    intent_id: str
    symbol: str
    limit_price: Decimal
    stop: Decimal
    take_profit: Decimal
    delta: Decimal
    qty_left: Decimal
    eligible: datetime
    valid_until: datetime
    step: Decimal
    max_holding_seconds: int
    client_order_id: str


@dataclass
class _Position:
    intent_id: str
    symbol: str
    qty: Decimal
    avg: Decimal
    stop: Decimal
    take_profit: Decimal
    delta: Decimal
    filled_at: datetime
    max_holding: timedelta
    entry_bar_end: datetime


class OrderReplayEngine:
    """Replay closed bars in time order. A signal on bar t can trade on bar t+1 at the earliest."""

    def __init__(
        self,
        config: EngineConfig,
        *,
        calendar: XnysCalendar | None = None,
        filters: SymbolFilter | None = None,
        atr: dict[tuple[str, datetime], Decimal] | None = None,
    ) -> None:
        if live_trade_enabled():
            raise LiveTradingDisabledError("research replay")
        self.config = config
        self.calendar = calendar if calendar is not None else load_xnys_calendar()
        self.filters = filters if filters is not None else SymbolFilter()
        self.atr = dict(atr or {})
        self._guard = ConstitutionGuard()
        self._running = False
        self._result: ReplayResult | None = None
        self._reset()

    def reject_external_config(self, incoming: dict[str, object]) -> None:
        self._guard.reject_config_override(incoming)

    def sealed_audit(self) -> tuple[IntentRecord, ...]:
        if self._running or self._result is None:
            raise LookaheadError("MFE is sealed until the replay finishes")
        return self._result.records

    def _reset(self) -> None:
        self._positions: dict[str, _Position] = {}
        self._cash = self.config.initial_equity
        self._hwm = self.config.hwm_equity
        self._halt = self._equity() <= self._hwm * _RETAINED
        self._legs: dict[str, _Leg] = {}
        self._working: list[_Working] = []
        self._events: list[ReplayEvent] = []
        self._clock_bars: tuple[ClosedBar, ...] = ()
        self._symbol_bars: dict[str, tuple[ClosedBar, ...]] = {}
        self._minute_index: dict[str, tuple[ClosedBar, ...]] = {}

    def _equity(self) -> Decimal:
        basis = sum((pos.avg * pos.qty for pos in self._positions.values()), Decimal(0))
        return self._cash + basis

    def _mark_equity(self) -> None:
        equity = self._equity()
        if equity > self._hwm:
            self._hwm = equity
        if equity <= self._hwm * _RETAINED:
            self._halt = True

    def _half_day(self, when: datetime) -> bool:
        day = when.astimezone(NEW_YORK).date()
        if not self.calendar.is_session(day):
            return False
        return self.calendar.close_time(day).hour == 13

    def _session_open(self, when: datetime) -> bool:
        day = when.astimezone(NEW_YORK).date()
        if day.weekday() >= 5 or not self.calendar.is_session(day):
            return False
        return entry_window_open(when, self._half_day(when))

    def _should_flatten(self, bar: ClosedBar) -> bool:
        day = bar.start.astimezone(NEW_YORK).date()
        if not self.calendar.is_session(day):
            return False
        return bar.start >= flatten_deadline(bar.start, self._half_day(bar.start))

    def _deadlines(self, bar: ClosedBar) -> bool:
        for order in self._working:
            if order.qty_left > 0 and order.valid_until <= bar.start:
                order.qty_left = Decimal(0)
                self._cancel(order.intent_id, "TTL")
                self._events.append(ReplayEvent(bar.start, "CANCEL", order.symbol, "TTL"))
        if self._should_flatten(bar):
            self._cancel_resting("FLATTEN")
            for pos in list(self._positions.values()):
                if pos.symbol == bar.symbol:
                    self._flatten(pos, bar, "FLATTEN")
            self._events.append(ReplayEvent(bar.start, "FLATTEN", bar.symbol, "deadline"))
            return True
        for pos in list(self._positions.values()):
            if pos.symbol == bar.symbol and bar.start >= pos.filled_at + pos.max_holding:
                self._flatten(pos, bar, "TIMEOUT")
        return False

    def _flatten(self, pos: _Position, bar: ClosedBar, reason: str) -> None:
        self._note(self._legs[pos.intent_id], bar.open, bar.open)
        self._book_sell(pos, ExitFill(bar.open, pos.qty, reason, False, bar))

    def _match_entries(self, tape: BarTape, bar: ClosedBar) -> None:
        for order in self._working:
            if order.symbol != bar.symbol or order.qty_left <= 0 or bar.start < order.eligible:
                continue
            if self.config.mode is FillMode.LOOSE:
                printed = EntryPrint(
                    buy_limit(
                        FillMode.LOOSE,
                        bar,
                        order.limit_price,
                        order.delta,
                        order.qty_left,
                        order.step,
                    ),
                    None,
                )
            else:
                printed = conservative_entry(
                    bar,
                    tape,
                    limit=order.limit_price,
                    stop=order.stop,
                    delta=order.delta,
                    qty=order.qty_left,
                    step=order.step,
                )
            if printed.fill.qty <= 0:
                continue
            self._book_buy(order, bar, printed)
            pos = self._positions.get(order.intent_id)
            if printed.immediate_stop is not None and pos is not None:
                self._note_slice(self._legs[order.intent_id], bar, printed.immediate_stop.bar)
                self._book_sell(pos, printed.immediate_stop)
                if order.qty_left > 0:
                    order.qty_left = Decimal(0)
                    self._cancel(order.intent_id, "STOP_ON_ENTRY_BAR")

    def _match_exits(self, tape: BarTape, bar: ClosedBar) -> None:
        for intent_id, pos in list(self._positions.items()):
            if pos.symbol != bar.symbol or pos.entry_bar_end >= bar.end:
                continue
            decision = resolve_bracket(
                self.config.mode,
                bar,
                tape,
                stop=pos.stop,
                take_profit=pos.take_profit,
                delta=pos.delta,
                qty=pos.qty,
                step=self.filters.step_size,
            )
            leg = self._legs[intent_id]
            if decision is None:
                self._note(leg, bar.high, bar.low)
                continue
            self._note_slice(leg, bar, decision.bar)
            self._book_sell(pos, decision)
            self._cancel_symbol(pos.symbol, "EXIT")

    def _book_buy(self, order: _Working, bar: ClosedBar, printed: EntryPrint) -> None:
        qty = printed.fill.qty if printed.fill.qty < order.qty_left else order.qty_left
        price = printed.fill.price
        notional = price * qty
        fee = self.config.commission_rate * notional
        spread = self.config.half_spread * qty
        self._cash -= notional + fee + spread
        order.qty_left -= qty
        leg = self._legs[order.intent_id]
        leg.filled_qty += qty
        leg.entry_cost += notional
        leg.entry_commission += fee
        leg.spread_cost += spread
        if leg.entry_bar_end is None:
            leg.entry_bar_end = bar.end
        held = self._positions.get(order.intent_id)
        if held is None:
            self._positions[order.intent_id] = _Position(
                intent_id=order.intent_id,
                symbol=order.symbol,
                qty=qty,
                avg=price,
                stop=order.stop,
                take_profit=order.take_profit,
                delta=order.delta,
                filled_at=bar.end,
                max_holding=timedelta(seconds=order.max_holding_seconds),
                entry_bar_end=bar.end,
            )
        else:
            total = held.qty + qty
            held.avg = (held.avg * held.qty + price * qty) / total
            held.qty = total
        leg.open_qty = self._positions[order.intent_id].qty
        if printed.immediate_stop is None:
            self._note(leg, bar.high, bar.low)
        self._events.append(ReplayEvent(bar.end, "FILL", order.symbol, f"BUY {qty}"))
        self._mark_equity()

    def _book_sell(self, pos: _Position, fill: ExitFill) -> None:
        qty = fill.qty if fill.qty < pos.qty else pos.qty
        if qty <= 0:
            return
        price = fill.price
        notional = price * qty
        fee = self.config.commission_rate * notional
        spread = self.config.half_spread * qty
        self._cash += notional - fee - spread
        leg = self._legs[pos.intent_id]
        leg.gross_pnl += notional - (pos.avg * qty)
        leg.exit_commission += fee
        leg.spread_cost += spread
        leg.exit_proceeds += notional
        leg.exit_qty += qty
        leg.exit_reason = fill.reason
        leg.ambiguous = leg.ambiguous or fill.ambiguous
        leg.exit_bar_end = fill.bar.end
        pos.qty -= qty
        leg.open_qty = pos.qty
        if pos.qty == 0:
            del self._positions[pos.intent_id]
        self._events.append(ReplayEvent(fill.bar.end, fill.reason, pos.symbol, f"SELL {qty}"))
        self._mark_equity()

    def _note(self, leg: _Leg, high: Decimal, low: Decimal) -> None:
        leg.path_high = high if leg.path_high is None or high > leg.path_high else leg.path_high
        leg.path_low = low if leg.path_low is None or low < leg.path_low else leg.path_low

    def _note_slice(self, leg: _Leg, parent: ClosedBar, last: ClosedBar) -> None:
        if last.timeframe != "1m":
            self._note(leg, parent.high, parent.low)
            return
        for minute in self._tape_minutes(parent):
            self._note(leg, minute.high, minute.low)
            if minute.start == last.start:
                break

    def _tape_minutes(self, parent: ClosedBar) -> tuple[ClosedBar, ...]:
        return tuple(
            bar
            for bar in self._minute_index.get(parent.symbol, ())
            if parent.start <= bar.start < parent.end
        )

    def _accept(self, intent: TradeIntent, bar: ClosedBar) -> None:
        if intent.intent_id in self._legs:
            raise ValueError("duplicate intent_id")
        leg = _Leg(
            intent_id=intent.intent_id,
            symbol=intent.symbol,
            signal_bar_end=bar.end,
            max_holding_seconds=intent.max_holding_seconds,
        )
        self._legs[intent.intent_id] = leg
        if intent.data_asof_utc > bar.end:
            self._reject(leg, "LOOKAHEAD_ASOF")
            return
        if intent.data_asof_utc != bar.end or intent.symbol != bar.symbol:
            self._reject(leg, "STALE_ASOF")
            return
        if self.calendar.classify(bar.start, bar.end) != "RTH" or not self._session_open(bar.end):
            self._reject(leg, "NON_RTH")
            return
        if intent.signal_valid_until <= bar.end:
            self._reject(leg, "TTL_ALREADY_EXPIRED")
            return
        if self._symbol_busy(intent.symbol):
            self._reject(leg, "ALREADY_IN_POSITION")
            return
        limit = _limit_of(intent)
        leg.limit_price = limit
        if limit > intent.entry_price_ceiling:
            self._reject(leg, "LIMIT_ABOVE_CEILING")
            return
        delta = penetration_delta(
            self.config,
            symbol=intent.symbol,
            signal_end=bar.end,
            atr=self.atr,
        )
        if delta is None:
            self._reject(leg, "ATR_UNAVAILABLE")
            return
        leg.delta = delta
        client_id = _client_order_id(intent.intent_id)
        sized = plan_buy(
            symbol=intent.symbol,
            client_order_id=client_id,
            limit=limit,
            stop=intent.stop_price,
            take_profit=intent.take_profit_price,
            risk_budget=intent.risk_budget_usdc,
            equity=self._equity(),
            max_position_fraction=self.config.max_position_fraction,
            whole_share_fallback=intent.whole_share_fallback_qty,
            filters=self.filters,
            commission_rate=self.config.commission_rate,
            half_spread=self.config.half_spread,
        )
        if isinstance(sized, SizingRejection):
            self._reject(leg, *sized.reasons)
            return
        self._guard_or_reject(leg, sized, bar, intent, client_id, delta)

    def _guard_or_reject(
        self,
        leg: _Leg,
        sized: SizedOrder,
        bar: ClosedBar,
        intent: TradeIntent,
        client_id: str,
        delta: Decimal,
    ) -> None:
        try:
            self._guard.check_order(
                sized.draft,
                now_et=bar.end,
                is_half_day=self._half_day(bar.end),
                open_positions=self.config.occupied_slots + len(self._positions),
                pending_buys=sum(1 for order in self._working if order.qty_left > 0),
                equity=self._equity(),
                hwm=self._hwm,
                entries_halted=self._halt,
            )
        except OrderRejected as exc:
            self._reject(leg, exc.reason)
            return
        leg.adjustments.extend(sized.adjustments)
        leg.ordered_qty = sized.quantity
        leg.client_order_id = client_id
        limit = leg.limit_price if leg.limit_price is not None else sized.draft.price
        if limit is None:
            self._reject(leg, "GEOMETRY")
            return
        self._working.append(
            _Working(
                intent_id=intent.intent_id,
                symbol=intent.symbol,
                limit_price=limit,
                stop=intent.stop_price,
                take_profit=intent.take_profit_price,
                delta=delta,
                qty_left=sized.quantity,
                eligible=bar.end + self.config.latency,
                valid_until=intent.signal_valid_until,
                step=self.filters.step_size,
                max_holding_seconds=intent.max_holding_seconds,
                client_order_id=client_id,
            )
        )
        self._events.append(ReplayEvent(bar.end, "ACCEPT", intent.symbol, client_id))

    def _symbol_busy(self, symbol: str) -> bool:
        if any(pos.symbol == symbol for pos in self._positions.values()):
            return True
        return any(order.symbol == symbol and order.qty_left > 0 for order in self._working)

    def _reject(self, leg: _Leg, *reasons: str) -> None:
        leg.ordered_qty = Decimal(0)
        leg.reasons.extend(reasons)
        self._events.append(
            ReplayEvent(leg.signal_bar_end, "REJECT", leg.symbol, ",".join(reasons))
        )

    def _cancel(self, intent_id: str, reason: str) -> None:
        leg = self._legs[intent_id]
        if leg.cancel_reason is None:
            leg.cancel_reason = reason

    def _cancel_resting(self, reason: str) -> None:
        for order in self._working:
            if order.qty_left > 0:
                order.qty_left = Decimal(0)
                self._cancel(order.intent_id, reason)

    def _cancel_symbol(self, symbol: str, reason: str) -> None:
        for order in self._working:
            if order.symbol == symbol and order.qty_left > 0:
                order.qty_left = Decimal(0)
                self._cancel(order.intent_id, reason)

    def _finish(self) -> ReplayResult:
        records: list[IntentRecord] = []
        for leg in self._legs.values():
            records.append(self._record(leg))
        net = sum((row.net_pnl for row in records), Decimal(0))
        return ReplayResult(
            mode=self.config.mode,
            records=tuple(records),
            events=tuple(self._events),
            starting_cash=self.config.initial_equity,
            ending_cash=self._cash,
            net_pnl=net,
            halt_new_entries=self._halt,
        )

    def _record(self, leg: _Leg) -> IntentRecord:
        entry = (leg.entry_cost / leg.filled_qty) if leg.filled_qty > 0 else None
        exit_price = (leg.exit_proceeds / leg.exit_qty) if leg.exit_qty > 0 else None
        closed = leg.filled_qty > 0 and leg.open_qty == 0 and leg.exit_qty > 0
        net = (
            leg.gross_pnl - leg.entry_commission - leg.exit_commission - leg.spread_cost
            if closed
            else Decimal(0)
        )
        mfe, mae, opp = self._excursions(leg, entry)
        if leg.reasons and leg.filled_qty == 0:
            status = "RISK_REJECTED"
        elif leg.filled_qty == 0:
            status = "UNFILLED"
        elif leg.filled_qty < leg.ordered_qty:
            status = "PARTIAL"
        else:
            status = "FILLED"
        return IntentRecord(
            intent_id=leg.intent_id,
            symbol=leg.symbol,
            mode=self.config.mode,
            status=status,
            rejection_reasons=tuple(leg.reasons),
            adjustments=tuple(leg.adjustments),
            cancel_reason=leg.cancel_reason,
            client_order_id=leg.client_order_id,
            wallet_type="CARD",
            tokenize=False,
            limit_price=leg.limit_price,
            delta=leg.delta,
            ordered_qty=leg.ordered_qty,
            filled_qty=leg.filled_qty,
            open_qty=leg.open_qty,
            entry_price=entry,
            exit_price=exit_price,
            exit_reason=leg.exit_reason,
            ambiguous_bar_sl_assumed=leg.ambiguous,
            entry_commission=leg.entry_commission,
            exit_commission=leg.exit_commission,
            spread_cost=leg.spread_cost,
            gross_pnl=leg.gross_pnl if closed or leg.exit_qty > 0 else Decimal(0),
            net_pnl=net,
            mfe_usdc=mfe,
            mae_usdc=mae,
            opportunity_cost_usdc=opp,
            signal_bar_end=leg.signal_bar_end,
            entry_bar_end=leg.entry_bar_end,
            exit_bar_end=leg.exit_bar_end,
        )

    def _excursions(self, leg: _Leg, entry: Decimal | None) -> tuple[Decimal, Decimal, Decimal]:
        if leg.ordered_qty <= 0 or leg.limit_price is None:
            return Decimal(0), Decimal(0), Decimal(0)
        if (
            leg.filled_qty > 0
            and entry is not None
            and leg.path_high is not None
            and leg.path_low is not None
        ):
            favorable, adverse = per_share(entry, leg.path_high, leg.path_low)
            mfe = favorable * leg.filled_qty
            mae = adverse * leg.filled_qty
        else:
            high, low = self._window(leg)
            favorable, adverse = per_share(leg.limit_price, high, low)
            mfe = favorable * leg.ordered_qty
            mae = adverse * leg.ordered_qty
        unfilled = leg.ordered_qty - leg.filled_qty
        best, _low = self._window(leg)
        missed = opportunity_cost(
            unfilled_qty=unfilled,
            limit_price=leg.limit_price,
            best_high=best,
            commission_rate=self.config.commission_rate,
            half_spread=self.config.half_spread,
        )
        return mfe, mae, missed

    def _window(self, leg: _Leg) -> tuple[Decimal | None, Decimal | None]:
        if not self.calendar.is_session(leg.signal_bar_end.astimezone(NEW_YORK).date()):
            return None, None
        flatten = flatten_deadline(leg.signal_bar_end, self._half_day(leg.signal_bar_end))
        end = holding_deadline(leg.signal_bar_end, leg.max_holding_seconds, flatten)
        start = leg.signal_bar_end + self.config.latency
        return window_extremes(
            self._symbol_bars.get(leg.symbol, ()),
            symbol=leg.symbol,
            start_inclusive=start,
            end_exclusive=end,
        )

    def run(self, tape: BarTape, strategy: DecisionStrategy) -> ReplayResult:
        if self._running:
            raise LookaheadError("nested replay")
        self._running = True
        self._reset()
        self._clock_bars = tape.clock()
        grouped: dict[str, list[ClosedBar]] = {}
        for bar in self._clock_bars:
            grouped.setdefault(bar.symbol, []).append(bar)
        self._symbol_bars = {symbol: tuple(rows) for symbol, rows in grouped.items()}
        minutes: dict[str, list[ClosedBar]] = {}
        for bar in tape.bars:
            if bar.timeframe == "1m":
                minutes.setdefault(bar.symbol, []).append(bar)
        self._minute_index = {symbol: tuple(rows) for symbol, rows in minutes.items()}
        try:
            for bar in self._clock_bars:
                stopped = self._deadlines(bar)
                if not stopped and self.calendar.classify(bar.start, bar.end) == "RTH":
                    self._match_entries(tape, bar)
                    self._match_exits(tape, bar)
                self._events.append(ReplayEvent(bar.end, "BAR_CLOSE", bar.symbol, bar.timeframe))
                intent = strategy.on_close(tape.view(bar.end), bar)
                if intent is not None:
                    self._accept(intent, bar)
            result = self._finish()
        finally:
            self._running = False
        self._result = result
        return result


def _limit_of(intent: TradeIntent) -> Decimal:
    if intent.candidate_limit_prices:
        return intent.candidate_limit_prices[0]
    return intent.entry_price_ceiling


def _client_order_id(intent_id: str) -> str:
    digest = hashlib.sha256(intent_id.encode("utf-8")).hexdigest()
    return f"v22{digest[:32]}"
