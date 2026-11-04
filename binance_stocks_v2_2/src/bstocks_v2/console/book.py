"""One strategy's paper account. Nothing in this module opens a socket."""

import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import ROUND_DOWN, Decimal
from pathlib import Path

from bstocks_v2.console.capital import SWING_HOLD_SESSIONS
from bstocks_v2.constitution.guard import (
    MIN_VIABLE_NOTIONAL,
    SESSION_POLICY_DESK,
    SESSION_POLICY_GLOBAL,
    SESSION_POLICY_S5_RESEARCH,
    ConstitutionGuard,
    desk_flatten_deadline,
    desk_order_session,
    flatten_deadline,
    rth_cancel_deadline,
)
from bstocks_v2.contracts.errors import LiveTradingDisabledError, OrderRejected
from bstocks_v2.contracts.models import OrderDraft, OrderType, Side, TradingSession
from bstocks_v2.portfolio_risk.spec import (
    COMMISSION_RATE,
    HWM_RETAINED,
    MAX_POSITION_FRACTION,
    PDT_MAX_DAY_TRADES,
    PDT_WINDOW_SESSIONS,
    PLANNED_EQUITY,
    RISK_FRACTION,
)
from bstocks_v2.research_backtest.fills import buy_limit
from bstocks_v2.research_backtest.models import FillMode
from bstocks_v2.research_backtest.tape import ClosedBar
from bstocks_v2.research_data.calendar import NEW_YORK

_PLACE = "https://api.binance.com/sapi/v1/equity/order/place"
_CENT = Decimal("0.01")
_STEP = Decimal(1)
_DELTA = Decimal("0.02")
_TTL = timedelta(minutes=10)


@dataclass
class _Position:
    symbol: str
    qty: Decimal
    avg: Decimal
    stop: Decimal
    take_profit: Decimal
    opened_on: date
    horizon: str = "day"


@dataclass
class _Working:
    client_id: str
    symbol: str
    qty: Decimal
    limit: Decimal
    stop: Decimal
    take_profit: Decimal
    reserved: Decimal
    eligible_at: datetime
    expire_at: datetime
    opened_on: date
    session: TradingSession = TradingSession.RTH
    horizon: str = "day"


class PaperBook:
    """Isolated simulated CARD account. Live placement is checked and refused."""

    def __init__(
        self,
        strategy_id: str,
        path: Path | None = None,
        *,
        opening_cash: Decimal | None = None,
        desk_clock: bool = False,
    ) -> None:
        if strategy_id not in {"S1", "S2", "S3", "S4", "S5"}:
            raise ValueError("strategy")
        self.strategy_id = strategy_id
        self.path = path
        self.desk_clock = desk_clock
        self.opening_cash = PLANNED_EQUITY if opening_cash is None else opening_cash
        self._guard = ConstitutionGuard()
        self.cash = self.opening_cash
        self.hwm = self.opening_cash
        self.halt = False
        self.halt_reason = ""
        self.reason = "等待币安已收盘 K 线"
        self.marks: dict[str, Decimal] = {}
        self.positions: dict[str, _Position] = {}
        self.working: dict[str, _Working] = {}
        self.day_trades: list[date] = []
        self._seq = 0
        self._conn: sqlite3.Connection | None = None
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(path), check_same_thread=False)
            self._setup()
            self._load()
            self._adopt_desk_equity()

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def live_route_closed(self) -> None:
        try:
            self._guard.check_endpoint("POST", _PLACE)
        except LiveTradingDisabledError:
            return
        raise RuntimeError("live order route is open")

    def mark(self, symbol: str, price: Decimal) -> None:
        if price <= 0:
            return
        self.marks[symbol] = price

    def equity(self) -> Decimal:
        total = self.cash
        for position in self.positions.values():
            total += self.marks.get(position.symbol, position.avg) * position.qty
        return total

    def available(self) -> Decimal:
        reserved = sum((item.reserved for item in self.working.values()), Decimal(0))
        return self.cash - reserved

    def submit_entry(
        self,
        *,
        symbol: str,
        limit: Decimal,
        stop: Decimal,
        take_profit: Decimal,
        now_et: datetime,
        is_half_day: bool,
        session_days: list[date],
        research: bool = False,
        ttl: timedelta = _TTL,
        horizon: str = "day",
    ) -> dict[str, str] | None:
        self.live_route_closed()
        limit = self._guard.check_price(limit)
        stop = self._guard.check_price(stop)
        take_profit = self._guard.check_price(take_profit)
        if not stop < limit < take_profit:
            self.reason = "GEOMETRY"
            return None
        if symbol in self.positions or any(item.symbol == symbol for item in self.working.values()):
            self.reason = "SYMBOL_ALREADY_RESERVED"
            return None
        span = limit - stop
        equity = self.equity()
        room = equity * MAX_POSITION_FRACTION
        raw = min((equity * RISK_FRACTION) / span, room / limit)
        qty = raw.to_integral_value(rounding=ROUND_DOWN)
        if qty < 1 or limit * qty < MIN_VIABLE_NOTIONAL:
            self.reason = "NOTIONAL_TOO_SMALL"
            return None
        fee = COMMISSION_RATE * limit * qty
        gross = (take_profit - limit) * qty
        friction = COMMISSION_RATE * (limit + take_profit) * qty + _CENT * 2 * qty
        local_day = now_et.astimezone(NEW_YORK).date()
        if self._pdt(local_day, session_days):
            self.reason = "PDT"
            return None
        self._seq += 1
        client_id = _client_id(self.strategy_id, symbol, "buy", now_et, self._seq)
        draft = OrderDraft(
            symbol=symbol,
            side=Side.BUY,
            order_type=OrderType.LIMIT,
            client_order_id=client_id,
            quantity=qty,
            price=limit,
            trading_session=_entry_session(self.desk_clock, now_et, is_half_day),
            quote_asset="USDC",
            wallet_type="CARD",
            tokenize=False,
            is_opening=True,
            confirmed_long_qty=Decimal(0),
            gross_take_profit_usdc=gross,
            round_trip_friction_usdc=friction,
            research_only=research,
        )
        policy = _entry_policy(self.desk_clock, research)
        try:
            self._guard.check_order(
                draft,
                now_et=now_et,
                is_half_day=is_half_day,
                open_positions=len(self.positions),
                pending_buys=len(self.working),
                equity=equity,
                hwm=self.hwm,
                entries_halted=self.halt,
                session_policy=policy,
            )
        except OrderRejected as exc:
            self.reason = exc.reason
            return None
        if self.available() < limit * qty + fee:
            self.reason = "CASH"
            return None
        self.working[client_id] = _Working(
            client_id=client_id,
            symbol=symbol,
            qty=qty,
            limit=limit,
            stop=stop,
            take_profit=take_profit,
            reserved=limit * qty + fee,
            eligible_at=now_et,
            expire_at=now_et + ttl,
            opened_on=local_day,
            session=_entry_session(self.desk_clock, now_et, is_half_day),
            horizon=horizon,
        )
        self.reason = "PAPER_WORKING"
        alert = self._alert("WORKING", symbol, "BUY", qty, limit, "模拟委托已记下，未发送到交易所")
        self._save()
        return alert

    def on_bar(
        self,
        bar: ClosedBar,
        *,
        is_half_day: bool,
        session_days: list[date],
    ) -> list[dict[str, str]]:
        self.live_route_closed()
        self.mark(bar.symbol, bar.close)
        alerts: list[dict[str, str]] = []
        alerts.extend(self._pull_rth(bar, is_half_day=is_half_day))
        alerts.extend(self._fill_buys(bar))
        alerts.extend(self._exits(bar, is_half_day=is_half_day, session_days=session_days))
        self._mark_hwm()
        if alerts:
            self._save()
        return alerts

    def snapshot(self) -> dict[str, object]:
        return {
            "strategy": self.strategy_id,
            "mode": "PAPER",
            "live_route": "closed",
            "cash": _money(self.cash),
            "equity": _money(self.equity()),
            "hwm": _money(self.hwm),
            "available": _money(self.available()),
            "halt": self.halt,
            "halt_reason": self.halt_reason,
            "reason": self.reason,
            "positions": [
                {
                    "symbol": item.symbol,
                    "qty": _money(item.qty),
                    "avg": _money(item.avg),
                    "stop": _money(item.stop),
                    "take_profit": _money(item.take_profit),
                    "mark": _money(self.marks.get(item.symbol, item.avg)),
                }
                for item in self.positions.values()
            ],
            "working": [
                {
                    "symbol": item.symbol,
                    "qty": _money(item.qty),
                    "limit": _money(item.limit),
                }
                for item in self.working.values()
            ],
        }

    def _fill_buys(self, bar: ClosedBar) -> list[dict[str, str]]:
        alerts: list[dict[str, str]] = []
        done: list[str] = []
        for client_id, order in self.working.items():
            if order.symbol != bar.symbol:
                continue
            if bar.end <= order.eligible_at:
                continue
            if bar.end > order.expire_at:
                done.append(client_id)
                self.reason = "PAPER_EXPIRED"
                continue
            filled = buy_limit(FillMode.CONSERVATIVE, bar, order.limit, _DELTA, order.qty, _STEP)
            if filled.qty <= 0:
                continue
            fee = COMMISSION_RATE * filled.price * filled.qty
            cost = filled.price * filled.qty + fee
            self.cash -= cost
            self.positions[order.symbol] = _Position(
                symbol=order.symbol,
                qty=filled.qty,
                avg=filled.price,
                stop=order.stop,
                take_profit=order.take_profit,
                opened_on=order.opened_on,
                horizon=order.horizon,
            )
            done.append(client_id)
            self.reason = "PAPER_FILLED"
            alerts.append(
                self._alert("FILLED", order.symbol, "BUY", filled.qty, filled.price, "模拟买入成交")
            )
        for client_id in done:
            self.working.pop(client_id, None)
        return alerts

    def _exits(
        self,
        bar: ClosedBar,
        *,
        is_half_day: bool,
        session_days: list[date],
    ) -> list[dict[str, str]]:
        position = self.positions.get(bar.symbol)
        if position is None:
            return []
        deadline = _flatten_at(self.desk_clock, bar.end, is_half_day)
        both = bar.low <= position.stop and bar.high >= position.take_profit
        flatten = _should_flatten(
            self.desk_clock,
            position,
            bar.end,
            deadline,
            session_days,
        )
        if both or bar.low <= position.stop or flatten:
            if bar.open < position.stop:
                raw = bar.open
            else:
                raw = position.stop
            reason = "BOTH_TOUCHED_STOP" if both else "STOP"
            if flatten and not both and bar.low > position.stop:
                raw = bar.close
                reason = "FLATTEN"
        elif bar.high >= position.take_profit:
            raw = position.take_profit
            reason = "TAKE_PROFIT"
        else:
            return []
        price = raw.quantize(_CENT, rounding=ROUND_DOWN)
        if price <= 0:
            self.reason = "EXIT_PRICE"
            return []
        try:
            self._guard_sell(position, price, bar.end, is_half_day)
        except OrderRejected as exc:
            self.reason = exc.reason
            return []
        fee = COMMISSION_RATE * price * position.qty
        self.cash += price * position.qty - fee
        if position.opened_on == bar.end.astimezone(NEW_YORK).date():
            self.day_trades.append(position.opened_on)
        if self._pdt(bar.end.astimezone(NEW_YORK).date(), session_days):
            self.halt = True
            self.halt_reason = "PDT"
        del self.positions[bar.symbol]
        self.reason = reason
        text = f"模拟卖出 {reason}"
        return [self._alert("FILLED", position.symbol, "SELL", position.qty, price, text)]

    def _guard_sell(
        self,
        position: _Position,
        price: Decimal,
        now_et: datetime,
        is_half_day: bool,
    ) -> None:
        client_id = _client_id(self.strategy_id, position.symbol, "sell", now_et, self._seq + 1)
        draft = OrderDraft(
            symbol=position.symbol,
            side=Side.SELL,
            order_type=OrderType.LIMIT,
            client_order_id=client_id,
            quantity=position.qty,
            price=price,
            trading_session=_entry_session(self.desk_clock, now_et, is_half_day),
            quote_asset="USDC",
            wallet_type="CARD",
            tokenize=False,
            is_opening=False,
            confirmed_long_qty=position.qty,
            research_only=self.strategy_id == "S5",
        )
        policy = _exit_policy(self.desk_clock, self.strategy_id)
        self._guard.check_order(
            draft,
            now_et=now_et,
            is_half_day=is_half_day,
            open_positions=len(self.positions),
            pending_buys=len(self.working),
            equity=self.equity(),
            hwm=self.hwm,
            entries_halted=False,
            session_policy=policy,
        )

    def _pdt(self, day: date, session_days: list[date]) -> bool:
        window = [item for item in session_days if item <= day][-PDT_WINDOW_SESSIONS:]
        allowed = set(window)
        return sum(1 for item in self.day_trades if item in allowed) >= PDT_MAX_DAY_TRADES

    def _mark_hwm(self) -> None:
        equity = self.equity()
        if equity > self.hwm:
            self.hwm = equity
        if equity <= self.hwm * HWM_RETAINED:
            self.halt = True
            self.halt_reason = "HWM_LOCK"

    def _alert(
        self,
        kind: str,
        symbol: str,
        side: str,
        qty: Decimal,
        price: Decimal,
        text: str,
    ) -> dict[str, str]:
        return {
            "strategy": self.strategy_id,
            "kind": kind,
            "symbol": symbol,
            "side": side,
            "qty": _money(qty),
            "price": _money(price),
            "text": text,
        }

    def _setup(self) -> None:
        assert self._conn is not None
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS paper_account (
                strategy TEXT PRIMARY KEY,
                cash TEXT NOT NULL,
                hwm TEXT NOT NULL,
                halt INTEGER NOT NULL,
                halt_reason TEXT NOT NULL,
                seq INTEGER NOT NULL,
                day_trades TEXT NOT NULL
            )
            """
        )
        self._conn.commit()

    def _load(self) -> None:
        assert self._conn is not None
        row = self._conn.execute(
            """
            SELECT cash, hwm, halt, halt_reason, seq, day_trades
            FROM paper_account WHERE strategy = ?
            """,
            (self.strategy_id,),
        ).fetchone()
        if row is None:
            return
        self.cash = Decimal(row[0])
        self.hwm = Decimal(row[1])
        self.halt = bool(row[2])
        self.halt_reason = str(row[3])
        self._seq = int(row[4])
        self.day_trades = [date.fromisoformat(item) for item in str(row[5]).split(",") if item]

    def _adopt_desk_equity(self) -> None:
        if not self.desk_clock:
            return
        if self.cash == PLANNED_EQUITY and self.hwm == PLANNED_EQUITY:
            self.cash = self.opening_cash
            self.hwm = self.opening_cash

    def _pull_rth(self, bar: ClosedBar, *, is_half_day: bool) -> list[dict[str, str]]:
        if not self.desk_clock or bar.end < rth_cancel_deadline(bar.end, is_half_day):
            return []
        alerts: list[dict[str, str]] = []
        for client_id, order in list(self.working.items()):
            if order.session is not TradingSession.RTH:
                continue
            self.working.pop(client_id, None)
            self.reason = "RTH_PULLED"
            alerts.append(
                self._alert("WORKING", order.symbol, "BUY", order.qty, order.limit, "盘中委托已撤")
            )
        if alerts:
            self._save()
        return alerts

    def _save(self) -> None:
        if self._conn is None:
            return
        trades = ",".join(item.isoformat() for item in self.day_trades)
        self._conn.execute(
            """
            INSERT INTO paper_account (strategy, cash, hwm, halt, halt_reason, seq, day_trades)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(strategy) DO UPDATE SET
                cash = excluded.cash,
                hwm = excluded.hwm,
                halt = excluded.halt,
                halt_reason = excluded.halt_reason,
                seq = excluded.seq,
                day_trades = excluded.day_trades
            """,
            (
                self.strategy_id,
                _money(self.cash),
                _money(self.hwm),
                int(self.halt),
                self.halt_reason,
                self._seq,
                trades,
            ),
        )
        self._conn.commit()


def _entry_session(desk_clock: bool, now_et: datetime, is_half_day: bool) -> TradingSession:
    if not desk_clock:
        return TradingSession.RTH
    name = desk_order_session(now_et, is_half_day)
    if name == "EXTENDED":
        return TradingSession.EXTENDED
    return TradingSession.RTH


def _entry_policy(desk_clock: bool, research: bool) -> str:
    if desk_clock:
        return SESSION_POLICY_DESK
    if research:
        return SESSION_POLICY_S5_RESEARCH
    return SESSION_POLICY_GLOBAL


def _exit_policy(desk_clock: bool, strategy_id: str) -> str:
    if desk_clock:
        return SESSION_POLICY_DESK
    if strategy_id == "S5":
        return SESSION_POLICY_S5_RESEARCH
    return SESSION_POLICY_GLOBAL


def _flatten_at(desk_clock: bool, moment: datetime, is_half_day: bool) -> datetime:
    if desk_clock:
        return desk_flatten_deadline(moment)
    return flatten_deadline(moment, is_half_day)


def _should_flatten(
    desk_clock: bool,
    position: _Position,
    moment: datetime,
    deadline: datetime,
    session_days: list[date],
) -> bool:
    if moment < deadline:
        return False
    if not desk_clock or position.horizon != "swing":
        return True
    day = moment.astimezone(NEW_YORK).date()
    held = sum(1 for item in session_days if position.opened_on <= item <= day)
    return held >= SWING_HOLD_SESSIONS


def _client_id(strategy: str, symbol: str, role: str, moment: datetime, seq: int) -> str:
    prefix = {"S1": "s01", "S2": "s02", "S3": "s03", "S4": "s04", "S5": "s05"}[strategy]
    raw = f"{strategy}|{symbol}|{role}|{moment.isoformat()}|{seq}"
    digest = hashlib.sha256(raw.encode()).hexdigest()
    return prefix + digest[:32]


def _money(value: Decimal) -> str:
    return format(value, "f")
