"""Five independent desks. Each owns its feature book, regime, and paper account."""

from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from pathlib import Path

from bstocks_v2.console.achievement import gate_label, quote_achievement
from bstocks_v2.console.archive import SessionArchive
from bstocks_v2.console.board import (
    choose_wave,
    listed,
    money,
    reached,
    remember,
    tier,
    track,
    visible,
    wave_points,
)
from bstocks_v2.console.book import PaperBook
from bstocks_v2.console.calendar import LiveCalendar
from bstocks_v2.console.capital import DESK_EQUITY
from bstocks_v2.console.factors import ConsoleFactors
from bstocks_v2.console.gates import opening_prices, ready_reason
from bstocks_v2.console.paper_s5 import s5_paper_plan
from bstocks_v2.console.pool import SYMBOLS as POOL
from bstocks_v2.console.pool import industry_of, kind_of, parent_of
from bstocks_v2.constitution.guard import entry_window_open
from bstocks_v2.contracts.errors import OrderRejected
from bstocks_v2.feature_store.bars import FeatureBar, SymbolSnapshot
from bstocks_v2.feature_store.engine import CausalFeatureBook
from bstocks_v2.feature_store.errors import LookaheadError
from bstocks_v2.market_ingestor.messages import ParsedEvent
from bstocks_v2.regime.classifier import FeatureSnapshot, MarketRegimeClassifier, build_observation
from bstocks_v2.research_backtest.tape import ClosedBar
from bstocks_v2.research_data.calendar import NEW_YORK
from bstocks_v2.strategies.s1.filters import CausalKalman, Fit
from bstocks_v2.strategies.s1.signal import decide as s1_decide
from bstocks_v2.strategies.s1.spec import (
    MIN_UPDATES,
    SECTOR_PARENT,
    STOP_ATR,
    TARGET_MULTIPLE,
    VWAP_EXTENSION_ATR,
    Z_MIN,
)
from bstocks_v2.strategies.s2.pattern import PullbackBook
from bstocks_v2.strategies.s2.signal import decide as s2_decide
from bstocks_v2.strategies.s3.orb import OpeningRangeBook
from bstocks_v2.strategies.s3.signal import decide as s3_decide
from bstocks_v2.strategies.s3.spec import BARS_IN_RANGE, RVOL_BASELINE_DAYS, RVOL_MIN
from bstocks_v2.strategies.s4.bounce import BounceBook
from bstocks_v2.strategies.s4.signal import decide as s4_decide
from bstocks_v2.strategies.s5.lock import s5_live_enabled

TITLES = {
    "S1": "残差趋势",
    "S2": "回踩",
    "S3": "开盘区间",
    "S4": "超卖反弹",
    "S5": "尾盘研究",
}


class Desk:
    def __init__(
        self,
        strategy_id: str,
        root: Path | None,
        calendar: LiveCalendar,
        archive: SessionArchive | None = None,
    ) -> None:
        self.strategy_id = strategy_id
        self.calendar = calendar
        self.archive = archive
        path = None if root is None else root / f"{strategy_id}.sqlite"
        self.paper = PaperBook(strategy_id, path, opening_cash=DESK_EQUITY, desk_clock=True)
        self.features = CausalFeatureBook(calendar)
        self.regime_model = MarketRegimeClassifier()
        self.tradable: set[str] = set()
        self._bars: dict[str, FeatureBar] = {}
        self._open_ms: int | None = None
        self._pending: list[ParsedEvent] = []
        self._factors = ConsoleFactors()
        self._kalman: dict[str, CausalKalman] = {}
        self._perp: dict[tuple[str, str], Decimal | None] = {}
        self._pullback = PullbackBook()
        self._opening = OpeningRangeBook()
        self._bounce = BounceBook()
        self._last_bar: FeatureBar | None = None
        self.touched_at: datetime | None = None
        self._samples: dict[str, list[tuple[datetime, Decimal]]] = {}
        self._prints: dict[str, list[tuple[datetime, Decimal]]] = {}
        self._marks: dict[str, Decimal] = {}
        self._score: dict[str, int] = {}
        self._gate: dict[str, str] = {}
        self._edge: dict[str, Decimal] = {}
        self._locked: set[str] = set()

    def close(self) -> None:
        self.paper.close()

    def on_tradable(self, symbol: str, state: str) -> None:
        if state == "BUY_SELL":
            self.tradable.add(symbol)
        else:
            self.tradable.discard(symbol)

    def on_quote(
        self,
        symbol: str,
        bid: Decimal,
        ask: Decimal,
        when: datetime | None = None,
    ) -> None:
        if bid <= 0 or ask <= 0:
            return
        mid = ((bid + ask) / 2).quantize(Decimal("0.01"))
        if listed(symbol):
            self._marks[symbol] = mid
        self.paper.mark(symbol, mid)
        self.on_print(symbol, mid, when or datetime.now(UTC))

    def on_print(self, symbol: str, price: Decimal, when: datetime) -> None:
        if not listed(symbol) or price <= 0:
            return
        track(self._prints.setdefault(symbol, []), when, price)
        self._review(symbol)

    def review_quoted(self) -> None:
        for symbol in tuple(self._prints):
            self._review(symbol)

    def on_kline(self, event: ParsedEvent) -> list[dict[str, str]]:
        if event.bar_open_ms is None or event.symbol is None:
            return []
        if self._open_ms is None:
            self._open_ms = event.bar_open_ms
        if event.bar_open_ms != self._open_ms:
            alerts = self._flush()
            self._open_ms = event.bar_open_ms
            self._pending.append(event)
            return alerts
        self._pending.append(event)
        return []

    def flush(self) -> list[dict[str, str]]:
        return self._flush()

    def _flush(self) -> list[dict[str, str]]:
        events = self._pending
        self._pending = []
        self.touched_at = datetime.now(UTC)
        updated: list[FeatureBar] = []
        alerts: list[dict[str, str]] = []
        for event in events:
            bar = _feature(event)
            if bar is None:
                continue
            self._remember(bar)
            self._bars[bar.symbol] = bar
            self._last_bar = bar
            try:
                snap = self.features.update(bar)
            except (LookaheadError, ValueError) as exc:
                self.paper.reason = exc.__class__.__name__
                continue
            if snap is None:
                self.paper.reason = self.calendar.classify(bar.start, bar.end)
                continue
            updated.append(bar)
            tape = _tape(bar)
            half = self.calendar.is_half_day(bar.end.astimezone(NEW_YORK).date())
            alerts.extend(self.paper.on_bar(tape, is_half_day=half, session_days=self._sessions()))
        if not updated:
            return alerts
        return alerts + self._decide_slice(updated)

    def replay_group(self, bars: list[FeatureBar]) -> list[dict[str, str]]:
        self.touched_at = datetime.now(UTC)
        updated: list[FeatureBar] = []
        alerts: list[dict[str, str]] = []
        for bar in bars:
            self._remember(bar)
            self._bars[bar.symbol] = bar
            self._last_bar = bar
            try:
                snap = self.features.update(bar)
            except (LookaheadError, ValueError) as exc:
                self.paper.reason = exc.__class__.__name__
                continue
            if snap is None:
                continue
            updated.append(bar)
            half = self.calendar.is_half_day(bar.end.astimezone(NEW_YORK).date())
            alerts.extend(
                self.paper.on_bar(_tape(bar), is_half_day=half, session_days=self._sessions())
            )
        if not updated:
            return alerts
        return alerts + self._decide_slice(updated)

    def _decide_slice(self, updated: list[FeatureBar]) -> list[dict[str, str]]:
        asof = updated[-1].end
        regime = self.regime_model.step(build_observation(self.features.latest, asof))
        half = self.calendar.is_half_day(asof.astimezone(NEW_YORK).date())
        alerts: list[dict[str, str]] = []
        for bar in updated:
            snap = self.features.latest.get(bar.symbol)
            if snap is None or snap.data_asof_utc != bar.end:
                continue
            try:
                decision = self._decide(bar, snap, regime, half)
            except (OrderRejected, RuntimeError, ValueError) as exc:
                self.paper.reason = getattr(exc, "reason", exc.__class__.__name__)
                self._take_gate(bar.symbol, alerts)
                continue
            self._take_gate(bar.symbol, alerts)
            if decision is None:
                continue
            symbol, limit, stop, take_profit, research = decision
            alert = self.paper.submit_entry(
                symbol=symbol,
                limit=limit,
                stop=stop,
                take_profit=take_profit,
                now_et=bar.end,
                is_half_day=half,
                session_days=self._sessions(),
                research=research,
            )
            if alert is not None:
                alerts.append(alert)
        return alerts

    def _decide(
        self,
        bar: FeatureBar,
        snap: SymbolSnapshot,
        regime: FeatureSnapshot,
        half: bool,
    ) -> tuple[str, Decimal, Decimal, Decimal, bool] | None:
        if self.strategy_id == "S5":
            if s5_live_enabled():
                raise RuntimeError("S5 code lock drifted")
            return self._s5_paper(bar)
        venue = bar.symbol in self.tradable if self.tradable else True
        eligible = venue and bar.symbol in SECTOR_PARENT
        horizon = min(BARS_IN_RANGE)
        if self.strategy_id == "S1":
            return self._s1(bar, snap, regime, eligible, venue)
        if self.strategy_id == "S2":
            pullback, why = self._pullback.observe(bar, snap)
            if bar.symbol in SECTOR_PARENT:
                pullback_choice, reason = s2_decide(
                    symbol=bar.symbol,
                    pattern=pullback,
                    pattern_reason=why,
                    snap=snap,
                    regime=regime,
                    research_eligible=eligible,
                    half_day=half,
                    book=self._pullback,
                )
                self.paper.reason = reason
                if pullback_choice is None:
                    return None
                return _prices(
                    bar.symbol,
                    pullback_choice.limit_price,
                    pullback_choice.stop_price,
                    pullback_choice.take_profit_price,
                )
            regime_ok = regime.entries_allowed and regime.s2_enabled and regime.vwap_intact
            reason = ready_reason(
                why,
                regime_ok=regime_ok,
                window_open=entry_window_open(snap.data_asof_utc, half),
                eligible=venue,
            )
            self.paper.reason = reason
            if reason != "SIGNAL" or pullback is None:
                return None
            self._pullback.commit(bar.symbol)
            return _prices(
                bar.symbol,
                pullback.limit_price,
                pullback.stop_price,
                pullback.take_profit_price,
            )
        if self.strategy_id == "S3":
            view = self._opening.observe(bar)
            opening = view.get(horizon)
            if opening is None:
                self.paper.reason = "NO_RANGE"
                return None
            if bar.symbol in SECTOR_PARENT:
                opening_choice, reason = s3_decide(
                    symbol=bar.symbol,
                    snap=snap,
                    regime=regime,
                    research_eligible=eligible,
                    half_day=half,
                    opening=opening,
                )
                self.paper.reason = reason
                if opening_choice is None:
                    return None
                return _prices(
                    bar.symbol,
                    opening_choice.limit_price,
                    opening_choice.stop_price,
                    opening_choice.take_profit_price,
                )
            return self._s3_pool(bar, snap, regime, half, opening, venue)
        bounce, why = self._bounce.observe(bar, snap)
        if bar.symbol in SECTOR_PARENT:
            decision = s4_decide(
                arm="baseline",
                symbol=bar.symbol,
                pattern=bounce,
                pattern_reason=why,
                snap=snap,
                regime=regime if regime.data_asof_utc == snap.data_asof_utc else None,
                research_eligible=eligible,
                half_day=half,
                residuals=self._bounce.residuals(bar.symbol),
                breakdown=self._bounce.rvol_breakdown(bar.symbol),
                spy_bar=self._bars.get("SPY"),
                spy_snap=self.features.latest.get("SPY"),
            )
            self.paper.reason = decision.reason
            if decision.candidate is None:
                return None
            picked = decision.candidate
            return _prices(
                bar.symbol,
                picked.limit_price,
                picked.stop_price,
                picked.take_profit_price,
            )
        reason = ready_reason(
            why,
            regime_ok=regime.data_asof_utc == snap.data_asof_utc,
            window_open=entry_window_open(snap.data_asof_utc, half),
            eligible=venue,
        )
        self.paper.reason = reason
        if reason != "SIGNAL" or bounce is None:
            return None
        return _prices(bar.symbol, bounce.limit_price, bounce.stop_price, bounce.take_profit_price)

    def _s1(
        self,
        bar: FeatureBar,
        snap: SymbolSnapshot,
        regime: FeatureSnapshot,
        eligible: bool,
        venue: bool,
    ) -> tuple[str, Decimal, Decimal, Decimal, bool] | None:
        parent = SECTOR_PARENT.get(bar.symbol) or parent_of(bar.symbol)
        if parent is None:
            self.paper.reason = "NO_SECTOR"
            return None
        spy = self.features.latest.get("SPY")
        sector = self.features.latest.get(parent)
        if (
            spy is None
            or sector is None
            or spy.bar_start != snap.bar_start
            or sector.bar_start != snap.bar_start
            or snap.ret_norm is None
            or spy.ret_norm is None
            or sector.ret_norm is None
        ):
            self.paper.reason = "FACTOR_WARMUP"
            return None
        asof = snap.data_asof_utc.isoformat()
        perp = self._perp_once(parent, spy.ret_norm, sector.ret_norm, asof)
        if perp is None:
            self.paper.reason = "FACTOR_WARMUP"
            return None
        fit = self._kalman.setdefault(bar.symbol, CausalKalman()).update(
            (Decimal(1), spy.ret_norm, perp),
            snap.ret_norm,
        )
        if bar.symbol in SECTOR_PARENT:
            intent, reason = s1_decide(
                model="kalman",
                symbol=bar.symbol,
                fit=fit,
                sector_strength=self._factors.strength(parent),
                stock=snap,
                regime=regime,
                research_eligible=eligible,
            )
            self.paper.reason = reason
            if intent is None:
                return None
            return _prices(
                bar.symbol,
                intent.entry_price_ceiling,
                intent.stop_price,
                intent.take_profit_price,
            )
        reason = _s1_pool_reason(fit, snap, regime, self._factors.strength(parent))
        self.paper.reason = reason
        if reason != "SIGNAL":
            return None
        if not venue:
            self.paper.reason = "AWAITING_VENUE"
            return None
        return _s1_prices(bar.symbol, snap)

    def _s5_paper(
        self,
        bar: FeatureBar,
    ) -> tuple[str, Decimal, Decimal, Decimal, bool] | None:
        if self.archive is None:
            self.paper.reason = "NO_PREV_CLOSE"
            return None
        reason, prices = s5_paper_plan(
            self.archive,
            bar.symbol,
            bar.close,
            bar.end.astimezone(NEW_YORK).date(),
        )
        self.paper.reason = reason
        if prices is None:
            return None
        limit, stop, take_profit = prices
        return _prices(bar.symbol, limit, stop, take_profit)

    def _s3_pool(
        self,
        bar: FeatureBar,
        snap: SymbolSnapshot,
        regime: FeatureSnapshot,
        half: bool,
        opening: object,
        venue: bool,
    ) -> tuple[str, Decimal, Decimal, Decimal, bool] | None:
        first = bool(getattr(opening, "first_break", False))
        orh = getattr(opening, "orh", None)
        orl = getattr(opening, "orl", None)
        if not first or not isinstance(orh, Decimal) or not isinstance(orl, Decimal):
            self.paper.reason = str(getattr(opening, "reason", "NO_RANGE"))
            return None
        if not regime.s3_enabled or not regime.entries_allowed:
            self.paper.reason = "S3_DISABLED"
            return None
        if not snap.vwap_intact or snap.vwap is None:
            self.paper.reason = "VWAP_GAP"
            return None
        if not entry_window_open(snap.data_asof_utc, half):
            self.paper.reason = "ENTRY_WINDOW"
            return None
        atr = snap.atr14
        if atr is None or atr <= 0:
            self.paper.reason = "NO_ATR"
            return None
        if snap.baseline_days < RVOL_BASELINE_DAYS or snap.rvol_median is None:
            self.paper.reason = "NO_RVOL"
            return None
        if snap.rvol_median < RVOL_MIN:
            self.paper.reason = "RVOL_LOW"
            return None
        if snap.close <= snap.vwap:
            self.paper.reason = "BELOW_VWAP"
            return None
        prices = opening_prices(orh, orl, atr)
        if prices is None:
            self.paper.reason = "GEOMETRY"
            return None
        if not venue:
            self.paper.reason = "AWAITING_VENUE"
            return None
        self.paper.reason = "SIGNAL"
        limit, stop, take_profit = prices
        return _prices(bar.symbol, limit, stop, take_profit)

    def _perp_once(
        self,
        parent: str,
        spy: Decimal,
        sector: Decimal,
        asof: str,
    ) -> Decimal | None:
        key = (parent, asof)
        if key not in self._perp:
            self._perp[key] = self._factors.update(parent, spy, sector, asof)
        return self._perp[key]

    def _sessions(self) -> list[date]:
        days = set(self.calendar.closes())
        days.update(self.calendar._extra)
        return sorted(days)

    def view(self) -> dict[str, object]:
        body = self.paper.snapshot()
        body["title"] = TITLES[self.strategy_id]
        last = None
        if self._last_bar is not None:
            last = self._last_bar.end.astimezone(NEW_YORK).isoformat()
        body["last_bar"] = last
        body["tradable"] = len(self.tradable)
        body["runtime"] = "halted" if self.paper.halt else "running"
        touched = None if self.touched_at is None else self.touched_at.isoformat()
        body["touched_at"] = touched
        body["names"] = self._names()
        return body

    def note_gate(self, symbol: str, reason: str, *, locked: bool = False) -> dict[str, str] | None:
        if not listed(symbol):
            return None
        if self.strategy_id == "S5" and reason == "S5_ROUTE_CLOSED":
            return None
        progress = tier(self.strategy_id, reason)
        previous = self._score.get(symbol, 0)
        self._score[symbol] = progress
        self._gate[symbol] = reason
        if locked and progress == 100:
            self._locked.add(symbol)
        if not reached(previous, progress):
            return None
        return {
            "strategy": self.strategy_id,
            "kind": "SIGNAL",
            "side": "BUY",
            "symbol": symbol,
            "qty": "",
            "price": self._price(symbol),
            "text": "信号达成 100%",
        }

    def _remember(self, bar: FeatureBar) -> None:
        if not listed(bar.symbol):
            return
        remember(self._samples.setdefault(bar.symbol, []), bar.start, bar.close)

    def _take_gate(self, symbol: str, alerts: list[dict[str, str]]) -> None:
        alert = self.note_gate(symbol, self.paper.reason, locked=True)
        if alert is not None:
            alerts.append(alert)

    def _price(self, symbol: str) -> str:
        mark = self._marks.get(symbol)
        if mark is not None:
            return money(mark)
        samples = self._samples.get(symbol, [])
        if not samples:
            return ""
        return money(samples[-1][1])

    def _review(self, symbol: str) -> None:
        if symbol in self._locked or not listed(symbol):
            return
        parent = parent_of(symbol)
        prices = tuple(price for _, price in self._prints.get(symbol, []))
        parent_prices: tuple[Decimal, ...] = ()
        if parent is not None:
            parent_prices = tuple(price for _, price in self._prints.get(parent, []))
        now = datetime.now(UTC)
        try:
            session = self.calendar.classify(now, now + timedelta(seconds=1))
        except ValueError:
            session = "OVERNIGHT"
        reason, edge, progress = quote_achievement(
            self.strategy_id,
            prices,
            parent_prices,
            has_parent=parent is not None,
            session=session,
        )
        self._edge[symbol] = edge
        self._score[symbol] = progress
        self._gate[symbol] = reason

    def _names(self) -> list[dict[str, object]]:
        return visible(self._rows())

    def row(self, symbol: str) -> dict[str, object]:
        return next(item for item in self._rows() if item["symbol"] == symbol)

    def _rows(self) -> list[dict[str, object]]:
        rows: list[dict[str, object]] = []
        for symbol in POOL:
            mark = self._marks.get(symbol)
            prints = self._prints.get(symbol, [])
            samples = self._samples.get(symbol, [])
            if mark is not None:
                price = money(mark)
                source = "quote"
            elif prints:
                price = money(prints[-1][1])
                source = "print"
            elif samples:
                price = money(samples[-1][1])
                source = "close"
            else:
                price = ""
                source = ""
            wave = choose_wave(wave_points(prints), wave_points(samples))
            rows.append(
                {
                    "symbol": symbol,
                    "industry": industry_of(symbol),
                    "kind": kind_of(symbol),
                    "progress": self._score.get(symbol, 0),
                    "edge": format(self._edge.get(symbol, Decimal(0)), "f"),
                    "gate": gate_label(self._gate.get(symbol, "")),
                    "price": price,
                    "source": source,
                    "wave": wave[-24:],
                }
            )
        return rows


def _feature(event: ParsedEvent) -> FeatureBar | None:
    if (
        event.symbol is None
        or event.bar_open_ms is None
        or event.open is None
        or event.high is None
        or event.low is None
        or event.close is None
        or event.volume is None
    ):
        return None
    start = datetime(1970, 1, 1, tzinfo=UTC) + timedelta(milliseconds=event.bar_open_ms)
    return FeatureBar(
        symbol=event.symbol.upper(),
        start=start,
        end=start + timedelta(minutes=5),
        open=event.open,
        high=event.high,
        low=event.low,
        close=event.close,
        volume=event.volume,
        bar_vwap=event.vwap,
        closed=True,
        feed="binance",
    )


def _tape(bar: FeatureBar) -> ClosedBar:
    return ClosedBar(
        symbol=bar.symbol,
        start=bar.start,
        end=bar.end,
        open=bar.open,
        high=bar.high,
        low=bar.low,
        close=bar.close,
        volume=bar.volume,
        timeframe="5m",
    )


def _prices(
    symbol: str,
    limit: Decimal,
    stop: Decimal,
    take_profit: Decimal,
) -> tuple[str, Decimal, Decimal, Decimal, bool]:
    return (symbol, limit, stop, take_profit, False)


def _s1_pool_reason(
    fit: Fit,
    snap: SymbolSnapshot,
    regime: FeatureSnapshot,
    strength: Decimal | None,
    min_updates: int = MIN_UPDATES,
) -> str:
    if regime.data_asof_utc != snap.data_asof_utc:
        return "REGIME_ASOF"
    if not regime.entries_allowed or not regime.s1_enabled or not regime.vwap_intact:
        return "REGIME"
    if not snap.vwap_intact or snap.vwap is None:
        return "VWAP"
    atr = snap.atr14
    if atr is None or atr <= 0:
        return "ATR"
    if snap.close <= snap.vwap:
        return "BELOW_VWAP"
    if snap.close - snap.vwap > atr * VWAP_EXTENSION_ATR:
        return "EXTENDED"
    if fit.updates < min_updates:
        return "WARMUP"
    if not fit.solved or fit.variance_alpha is None or fit.variance_alpha <= 0:
        return "VARIANCE"
    if fit.alpha <= 0:
        return "ALPHA"
    z_alpha = fit.z_after(min_updates)
    if z_alpha is None or z_alpha <= Z_MIN:
        return "Z"
    if strength is None:
        return "STRENGTH_WARMUP"
    if strength <= 0:
        return "SECTOR"
    return "SIGNAL"


def _s1_prices(
    symbol: str,
    snap: SymbolSnapshot,
) -> tuple[str, Decimal, Decimal, Decimal, bool] | None:
    atr = snap.atr14
    if atr is None:
        return None
    limit = snap.close.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    distance = atr * STOP_ATR
    stop = (limit - distance).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
    take_profit = (limit + distance * TARGET_MULTIPLE).quantize(
        Decimal("0.01"),
        rounding=ROUND_DOWN,
    )
    if not stop < limit < take_profit:
        return None
    return _prices(symbol, limit, stop, take_profit)
