"""Five paper books on thirty-minute bars. They do not share the five-minute accounts."""

from datetime import date, time
from decimal import Decimal

from bstocks_v2.console.achievement import gate_label
from bstocks_v2.console.archive import SessionArchive
from bstocks_v2.console.board import listed, tier, visible
from bstocks_v2.console.book import PaperBook
from bstocks_v2.console.calendar import LiveCalendar
from bstocks_v2.console.desks import TITLES, _prices, _s1_pool_reason, _s1_prices
from bstocks_v2.console.factors import ConsoleFactors
from bstocks_v2.console.gates import opening_prices, ready_reason
from bstocks_v2.console.paper_s5 import s5_paper_plan
from bstocks_v2.console.pool import industry_of, kind_of, parent_of
from bstocks_v2.constitution.guard import entry_window_open
from bstocks_v2.contracts.errors import OrderRejected
from bstocks_v2.feature_store.bars import FeatureBar, SymbolSnapshot
from bstocks_v2.feature_store.engine import CausalFeatureBook
from bstocks_v2.feature_store.errors import LookaheadError
from bstocks_v2.regime.classifier import FeatureSnapshot, MarketRegimeClassifier, build_observation
from bstocks_v2.research_data.calendar import NEW_YORK
from bstocks_v2.strategies.s1.filters import CausalKalman
from bstocks_v2.strategies.s1.signal import decide as s1_decide
from bstocks_v2.strategies.s1.spec import SECTOR_PARENT
from bstocks_v2.strategies.s2.pattern import PullbackBook
from bstocks_v2.strategies.s2.signal import decide as s2_decide
from bstocks_v2.strategies.s3.orb import RangeState
from bstocks_v2.strategies.s3.signal import decide as s3_decide
from bstocks_v2.strategies.s3.spec import RVOL_BASELINE_DAYS, RVOL_MIN
from bstocks_v2.strategies.s4.bounce import BounceBook
from bstocks_v2.strategies.s4.signal import decide as s4_decide
from bstocks_v2.strategies.s5.lock import s5_live_enabled
from bstocks_v2.tf30.aggregate import Aggregator
from bstocks_v2.tf30.rangebook import ThirtyRange
from bstocks_v2.tf30.spec import (
    ATR_PERIOD,
    BASE_BARS,
    COOLDOWN_BARS,
    LOOKBACK,
    MIN_UPDATES,
    ORDER_TTL,
    RS_BARS,
    RSI_PERIOD,
    SLOPE_LAG,
    TREND_BARS,
)

STRATEGIES = ("S1", "S2", "S3", "S4", "S5")
_ARM = time(15, 30)
_ARM_HALF = time(12, 30)


class Board:
    """One shared thirty-minute tape and five separate paper accounts."""

    def __init__(
        self,
        papers: dict[str, PaperBook],
        calendar: LiveCalendar,
        archive: SessionArchive | None = None,
    ) -> None:
        self.calendar = calendar
        self.archive = archive
        self.papers = papers
        self.features = CausalFeatureBook(
            calendar,
            minutes=30,
            rsi_period=RSI_PERIOD,
            atr_period=ATR_PERIOD,
            trend_bars=TREND_BARS,
            slope_lag=SLOPE_LAG,
            feeds=("sip", "binance"),
        )
        self.regime_model = MarketRegimeClassifier()
        self._agg = Aggregator(calendar)
        self._range = ThirtyRange()
        self._pullback = PullbackBook(
            lookback=LOOKBACK,
            base_bars=BASE_BARS,
            cooldown_bars=COOLDOWN_BARS,
        )
        self._bounce = BounceBook()
        self._factors = ConsoleFactors(rs_bars=RS_BARS)
        self._kalman: dict[str, CausalKalman] = {}
        self._perp: dict[tuple[str, str], Decimal | None] = {}
        self._bars: dict[str, FeatureBar] = {}
        self._last: FeatureBar | None = None
        self.tradable: set[str] = set()
        self._score: dict[str, dict[str, int]] = {strategy: {} for strategy in STRATEGIES}
        self._gate: dict[str, dict[str, str]] = {strategy: {} for strategy in STRATEGIES}
        self._edge: dict[str, dict[str, Decimal]] = {strategy: {} for strategy in STRATEGIES}
        self._price: dict[str, str] = {}
        self._wave: dict[str, list[str]] = {}

    def close(self) -> None:
        return

    def on_tradable(self, symbol: str, state: str) -> None:
        if state == "BUY_SELL":
            self.tradable.add(symbol)
        else:
            self.tradable.discard(symbol)

    def on_mark(self, symbol: str, price: Decimal) -> None:
        if price <= 0:
            return
        for paper in self.papers.values():
            paper.mark(symbol, price)

    def absorb(self, bar: FeatureBar) -> None:
        folded = self._agg.push(bar)
        if folded is None:
            return
        try:
            self.features.update(folded)
        except (LookaheadError, ValueError):
            return

    def on_five(self, bar: FeatureBar) -> list[dict[str, str]]:
        folded = self._agg.push(bar)
        if folded is None:
            return []
        return self._decide_bars([folded])

    def on_group(self, bars: list[FeatureBar]) -> list[dict[str, str]]:
        folded: list[FeatureBar] = []
        for bar in bars:
            done = self._agg.push(bar)
            if done is not None:
                folded.append(done)
        return self._decide_bars(folded)

    def views(self) -> list[dict[str, object]]:
        last = None
        if self._last is not None:
            last = self._last.end.astimezone(NEW_YORK).isoformat()
        rows: list[dict[str, object]] = []
        for strategy in STRATEGIES:
            paper = self.papers[strategy]
            body = paper.snapshot()
            body["title"] = f"30分 · {TITLES[strategy]}"
            body["frame"] = "30m"
            body["last_bar"] = last
            body["runtime"] = "halted" if paper.halt else "running"
            body["names"] = self._names(strategy)
            rows.append(body)
        return rows

    def _decide_bars(self, folded: list[FeatureBar]) -> list[dict[str, str]]:
        if not folded:
            return []
        alerts: list[dict[str, str]] = []
        updated: list[FeatureBar] = []
        sessions = self._sessions()
        for bar in folded:
            self._bars[bar.symbol] = bar
            self._last = bar
            self._price[bar.symbol] = format(bar.close, "f")
            self._wave.setdefault(bar.symbol, []).append(format(bar.close, "f"))
            self._wave[bar.symbol] = self._wave[bar.symbol][-24:]
            try:
                snap = self.features.update(bar)
            except (LookaheadError, ValueError) as exc:
                for paper in self.papers.values():
                    paper.reason = exc.__class__.__name__
                continue
            if snap is None:
                continue
            updated.append(bar)
        if not updated:
            return alerts
        asof = updated[-1].end
        regime = self.regime_model.step(build_observation(self.features.latest, asof))
        half = self.calendar.is_half_day(asof.astimezone(NEW_YORK).date())
        for bar in updated:
            snap = self.features.latest.get(bar.symbol)
            if snap is None or snap.data_asof_utc != bar.end:
                continue
            for strategy in STRATEGIES:
                try:
                    decision = self._decide(strategy, bar, snap, regime, half)
                except (OrderRejected, RuntimeError, ValueError) as exc:
                    self.papers[strategy].reason = getattr(exc, "reason", exc.__class__.__name__)
                    self._remember(strategy, bar.symbol, self.papers[strategy].reason, snap)
                    continue
                self._remember(strategy, bar.symbol, self.papers[strategy].reason, snap)
                if decision is None:
                    continue
                symbol, limit, stop, take_profit, research = decision
                alert = self.papers[strategy].submit_entry(
                    symbol=symbol,
                    limit=limit,
                    stop=stop,
                    take_profit=take_profit,
                    now_et=bar.end,
                    is_half_day=half,
                    session_days=sessions,
                    research=research,
                    ttl=ORDER_TTL,
                )
                if alert is not None:
                    alerts.append(alert)
        return alerts

    def _decide(
        self,
        strategy: str,
        bar: FeatureBar,
        snap: SymbolSnapshot,
        regime: FeatureSnapshot,
        half: bool,
    ) -> tuple[str, Decimal, Decimal, Decimal, bool] | None:
        paper = self.papers[strategy]
        if strategy == "S5":
            if s5_live_enabled():
                raise RuntimeError("S5 code lock drifted")
            return self._s5(bar, half)
        venue = bar.symbol in self.tradable if self.tradable else True
        eligible = venue and bar.symbol in SECTOR_PARENT
        if strategy == "S1":
            return self._s1(bar, snap, regime, eligible, venue)
        if strategy == "S2":
            return self._s2(bar, snap, regime, half, eligible, venue)
        if strategy == "S3":
            opening = self._range.observe(bar)
            if bar.symbol in SECTOR_PARENT:
                choice, reason = s3_decide(
                    symbol=bar.symbol,
                    snap=snap,
                    regime=regime,
                    research_eligible=eligible,
                    half_day=half,
                    opening=opening,
                )
                paper.reason = reason
                if choice is None:
                    return None
                return _prices(
                    bar.symbol,
                    choice.limit_price,
                    choice.stop_price,
                    choice.take_profit_price,
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
            paper.reason = decision.reason
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
        paper.reason = reason
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
        paper = self.papers["S1"]
        parent = SECTOR_PARENT.get(bar.symbol) or parent_of(bar.symbol)
        if parent is None:
            paper.reason = "NO_SECTOR"
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
            paper.reason = "FACTOR_WARMUP"
            return None
        asof = snap.data_asof_utc.isoformat()
        try:
            perp = self._perp_once(parent, spy.ret_norm, sector.ret_norm, asof)
        except KeyError:
            paper.reason = "NO_SECTOR"
            return None
        if perp is None:
            paper.reason = "FACTOR_WARMUP"
            return None
        fit = self._kalman.setdefault(bar.symbol, CausalKalman()).update(
            (Decimal(1), spy.ret_norm, perp),
            snap.ret_norm,
        )
        if bar.symbol in SECTOR_PARENT:
            _intent, reason = s1_decide(
                model="kalman",
                symbol=bar.symbol,
                fit=fit,
                sector_strength=self._factors.strength(parent),
                stock=snap,
                regime=regime,
                research_eligible=eligible,
                min_updates=MIN_UPDATES,
            )
            paper.reason = reason
            if _intent is None:
                return None
            return _prices(
                bar.symbol,
                _intent.entry_price_ceiling,
                _intent.stop_price,
                _intent.take_profit_price,
            )
        reason = _s1_pool_reason(
            fit,
            snap,
            regime,
            self._factors.strength(parent),
            MIN_UPDATES,
        )
        paper.reason = reason
        if reason != "SIGNAL":
            return None
        if not venue:
            paper.reason = "AWAITING_VENUE"
            return None
        return _s1_prices(bar.symbol, snap)

    def _s2(
        self,
        bar: FeatureBar,
        snap: SymbolSnapshot,
        regime: FeatureSnapshot,
        half: bool,
        eligible: bool,
        venue: bool,
    ) -> tuple[str, Decimal, Decimal, Decimal, bool] | None:
        paper = self.papers["S2"]
        pullback, why = self._pullback.observe(bar, snap)
        if bar.symbol in SECTOR_PARENT:
            choice, reason = s2_decide(
                symbol=bar.symbol,
                pattern=pullback,
                pattern_reason=why,
                snap=snap,
                regime=regime,
                research_eligible=eligible,
                half_day=half,
                book=self._pullback,
            )
            paper.reason = reason
            if choice is None:
                return None
            return _prices(
                bar.symbol,
                choice.limit_price,
                choice.stop_price,
                choice.take_profit_price,
            )
        regime_ok = regime.entries_allowed and regime.s2_enabled and regime.vwap_intact
        reason = ready_reason(
            why,
            regime_ok=regime_ok,
            window_open=entry_window_open(snap.data_asof_utc, half),
            eligible=venue,
        )
        paper.reason = reason
        if reason != "SIGNAL" or pullback is None:
            return None
        self._pullback.commit(bar.symbol)
        return _prices(
            bar.symbol,
            pullback.limit_price,
            pullback.stop_price,
            pullback.take_profit_price,
        )

    def _s3_pool(
        self,
        bar: FeatureBar,
        snap: SymbolSnapshot,
        regime: FeatureSnapshot,
        half: bool,
        opening: RangeState,
        venue: bool,
    ) -> tuple[str, Decimal, Decimal, Decimal, bool] | None:
        paper = self.papers["S3"]
        if not opening.first_break or opening.orh is None or opening.orl is None:
            paper.reason = opening.reason
            return None
        if not regime.s3_enabled or not regime.entries_allowed:
            paper.reason = "S3_DISABLED"
            return None
        if not snap.vwap_intact or snap.vwap is None:
            paper.reason = "VWAP_GAP"
            return None
        if not entry_window_open(snap.data_asof_utc, half):
            paper.reason = "ENTRY_WINDOW"
            return None
        atr = snap.atr14
        if atr is None or atr <= 0:
            paper.reason = "NO_ATR"
            return None
        if snap.baseline_days < RVOL_BASELINE_DAYS or snap.rvol_median is None:
            paper.reason = "NO_RVOL"
            return None
        if snap.rvol_median < RVOL_MIN:
            paper.reason = "RVOL_LOW"
            return None
        if snap.close <= snap.vwap:
            paper.reason = "BELOW_VWAP"
            return None
        prices = opening_prices(opening.orh, opening.orl, atr)
        if prices is None:
            paper.reason = "GEOMETRY"
            return None
        if not venue:
            paper.reason = "AWAITING_VENUE"
            return None
        paper.reason = "SIGNAL"
        limit, stop, take_profit = prices
        return _prices(bar.symbol, limit, stop, take_profit)

    def _s5(
        self,
        bar: FeatureBar,
        half: bool,
    ) -> tuple[str, Decimal, Decimal, Decimal, bool] | None:
        paper = self.papers["S5"]
        clock = bar.end.astimezone(NEW_YORK).timetz().replace(tzinfo=None)
        if clock != (_ARM_HALF if half else _ARM):
            paper.reason = "OUT_OF_WINDOW"
            return None
        if self.archive is None:
            paper.reason = "NO_PREV_CLOSE"
            return None
        reason, prices = s5_paper_plan(
            self.archive,
            bar.symbol,
            bar.close,
            bar.end.astimezone(NEW_YORK).date(),
        )
        paper.reason = reason
        if prices is None:
            return None
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

    def _remember(self, strategy: str, symbol: str, reason: str, snap: SymbolSnapshot) -> None:
        if not listed(symbol):
            return
        self._score[strategy][symbol] = tier(strategy, reason)
        self._gate[strategy][symbol] = reason
        if snap.vwap is None:
            self._edge[strategy][symbol] = Decimal(0)
        else:
            self._edge[strategy][symbol] = snap.close - snap.vwap

    def _names(self, strategy: str) -> list[dict[str, object]]:
        rows: list[dict[str, object]] = []
        for symbol, progress in self._score[strategy].items():
            if not listed(symbol):
                continue
            rows.append(
                {
                    "symbol": symbol,
                    "industry": industry_of(symbol),
                    "kind": kind_of(symbol),
                    "progress": progress,
                    "edge": format(self._edge[strategy].get(symbol, Decimal(0)), "f"),
                    "gate": gate_label(self._gate[strategy].get(symbol, "")),
                    "price": self._price.get(symbol, ""),
                    "source": "30m",
                    "wave": self._wave.get(symbol, [])[-24:],
                }
            )
        return visible(rows)
