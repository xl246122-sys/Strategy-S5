"""Stage 9: dimensionless OU, the falling-knife guard, and the S4 live lock."""

import ast
import json
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from bstocks_v2.constitution import live_trade_enabled
from bstocks_v2.contracts.errors import ConstitutionBypassError, LiveTradingDisabledError
from bstocks_v2.feature_store.bars import FeatureBar, SymbolSnapshot, make_symbol_snapshot
from bstocks_v2.regime.classifier import FeatureSnapshot, Regime, route
from bstocks_v2.research_data.calendar import NEW_YORK
from bstocks_v2.strategies.s4.bounce import BounceBook
from bstocks_v2.strategies.s4.evaluate import _role
from bstocks_v2.strategies.s4.guard import KNIFE, market_selloff
from bstocks_v2.strategies.s4.lock import refuse_live_route, s4_live_enabled
from bstocks_v2.strategies.s4.ou import (
    adf_critical,
    dimensionless,
    fit_ar1,
    half_life_minutes,
    qualifies,
)
from bstocks_v2.strategies.s4.score import SplitScore, comparison, live_deployable
from bstocks_v2.strategies.s4.signal import UNCALIBRATED, decide, make_intent
from bstocks_v2.strategies.s4.spec import (
    HALF_LIFE_MAX_MINUTES,
    LOCKBOX_START,
    RSI_OVERSOLD,
    WINDOW,
    Z_STRETCH,
)

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "src" / "bstocks_v2" / "strategies" / "s4"
DAY = date(2024, 11, 1)
SYMBOL = "SMH"


def test_sources_do_not_use_float_literals_or_network_clients() -> None:
    banned = {"socket", "httpx", "requests", "urllib", "websockets"}
    for path in PACKAGE.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, float):
                raise AssertionError(path.name)
            called = node.func if isinstance(node, ast.Call) else None
            if isinstance(called, ast.Name) and called.id == "float":
                raise AssertionError(path.name)
            if isinstance(node, ast.Import):
                names = {alias.name.split(".")[0] for alias in node.names}
                assert names.isdisjoint(banned)
            if isinstance(node, ast.ImportFrom) and node.module is not None:
                root = node.module.split(".")[0]
                assert root not in banned
                assert not node.module.startswith("bstocks_v2.strategies.s2")
                assert not node.module.startswith("bstocks_v2.strategies.s3")


def test_window_stays_open_and_s4_cannot_go_live(tmp_path: Path) -> None:
    assert live_trade_enabled() is False
    assert s4_live_enabled() is False
    assert live_deployable() is False
    assert sum(UNCALIBRATED) == 1
    assert HALF_LIFE_MAX_MINUTES * 2 <= Decimal(90)
    assert RSI_OVERSOLD == Decimal(30)
    assert Z_STRETCH == Decimal("0.25")
    text = (PACKAGE / "lock.py").read_text(encoding="utf-8")
    assert "LIVE_ENABLED = False" in text
    assert "return LIVE_ENABLED" in text
    assert "return True" not in text
    assert _role(date(2025, 10, 31)) == "train"
    assert _role(date(2025, 11, 3)) == "validation"
    with pytest.raises(RuntimeError, match="open window"):
        _role(LOCKBOX_START)
    hostile = tmp_path / "s4.json"
    hostile.write_text(
        json.dumps({"live_enabled": True, "s4_live_enabled": True, "live_trade_enabled": True}),
        encoding="utf-8",
    )
    from bstocks_v2.config.loader import load_runtime_config

    with pytest.raises(ConstitutionBypassError):
        load_runtime_config(hostile)
    with pytest.raises(LiveTradingDisabledError):
        refuse_live_route("/sapi/v1/equity/order/place")
    assert hostile.exists()


def test_dimensionless_residual_and_exact_ar1() -> None:
    wide = dimensionless(Decimal(200), Decimal(100), Decimal(50))
    narrow = dimensionless(Decimal(4), Decimal(2), Decimal(1))
    assert wide == narrow == Decimal(2)
    fast = fit_ar1(tuple(_revert(Decimal("0.5"), WINDOW)))
    assert fast is not None
    assert abs(fast.phi - Decimal("0.5")) < Decimal("1e-24")
    assert fast.half_life_minutes == Decimal(5)
    assert qualifies(fast) == "OK"
    boundary = half_life_minutes((-Decimal(2).ln() / Decimal(5)).exp())
    assert boundary == Decimal(25)
    slow = fit_ar1(tuple(_revert(Decimal("0.95"), WINDOW)))
    assert slow is not None and abs(slow.phi - Decimal("0.95")) < Decimal("1e-20")
    assert slow.half_life_minutes is not None and slow.half_life_minutes > HALF_LIFE_MAX_MINUTES
    assert qualifies(slow) == "OU_HALF_LIFE"
    crit = adf_critical(23)
    assert Decimal("-3.1") < crit < Decimal("-2.9")
    with pytest.raises(RuntimeError, match="falling-knife"):
        qualifies(fast.__class__(**{**fast.__dict__, "phi": Decimal(1)}))


def test_a_later_residual_does_not_rewrite_the_earlier_fit() -> None:
    book, _bar, _snap, _pattern, reason = _drive(_revert(Decimal("0.5"), WINDOW))
    assert reason == "READY"
    earlier = book.residuals(SYMBOL)
    first = fit_ar1(earlier[-WINDOW:])
    assert first is not None and abs(first.phi - Decimal("0.5")) < Decimal("1e-24")
    _drive_more(book, Decimal("-0.40"), rsi=Decimal(40))
    second = fit_ar1(book.residuals(SYMBOL)[-WINDOW:])
    assert second is not None and abs(second.phi - first.phi) > Decimal("1e-6")
    assert fit_ar1(earlier[-WINDOW:]) == first
    assert abs(first.phi - Decimal("0.5")) < Decimal("1e-24")


def test_session_gap_and_unclosed_bar_are_refused() -> None:
    book, _bar, _snap, _pattern, _reason = _drive(_revert(Decimal("0.5"), 4))
    assert len(book.residuals(SYMBOL)) == 4
    broken = _bar_and_snap(Decimal("-1"), slot=4, intact=False)
    assert book.observe(*broken)[1] == "NOT_INTACT"
    assert book.residuals(SYMBOL) == ()
    nxt = _bar_and_snap(Decimal("-1"), slot=0, day=date(2024, 11, 4))
    assert book.observe(*nxt)[1] == "WARMUP"
    assert len(book.residuals(SYMBOL)) == 1
    stale = _bar_and_snap(Decimal("-1"), slot=0, day=date(2024, 11, 4))
    with pytest.raises(RuntimeError, match="not after"):
        book.observe(*stale)
    open_bar, open_snap = _bar_and_snap(Decimal("-1"), slot=2)
    open_bar = FeatureBar(
        symbol=open_bar.symbol,
        start=open_bar.start,
        end=open_bar.end,
        open=open_bar.open,
        high=open_bar.high,
        low=open_bar.low,
        close=open_bar.close,
        volume=open_bar.volume,
        bar_vwap=open_bar.bar_vwap,
        closed=False,
        feed="sip",
    )
    with pytest.raises(RuntimeError, match="not closed"):
        book.observe(open_bar, open_snap)


def test_falling_knife_blocks_regimes_rvol_and_a_unit_root() -> None:
    book, bar, snap, pattern, reason = _drive(_revert(Decimal("0.5"), WINDOW))
    assert reason == "READY" and pattern is not None
    down = _call("guarded", book, snap, pattern, reason, regime=Regime.TREND_DOWN)
    assert down.reason == KNIFE and down.knife == ("REGIME_TREND_DOWN",)
    panic = _call("guarded", book, snap, pattern, reason, regime=Regime.HIGH_VOL_PANIC)
    assert panic.reason == KNIFE and "REGIME_HIGH_VOL_PANIC" in panic.knife
    base = _call("baseline", book, snap, pattern, reason, regime=Regime.TREND_DOWN)
    assert base.reason == "SIGNAL" and base.candidate is not None
    crashed = _spy(snap.data_asof_utc, drop=True)
    sold = _call(
        "guarded",
        book,
        snap,
        pattern,
        reason,
        regime=Regime.SIDEWAYS,
        spy=crashed,
    )
    assert sold.reason == KNIFE and "MARKET_SELLOFF" in sold.knife
    flat_spy = _spy(snap.data_asof_utc, drop=False, ret_norm=Decimal("-1.5"))
    assert market_selloff(*flat_spy) is True
    root = _drive(_drift(WINDOW))
    rooted = _call("guarded", root[0], root[2], root[3], root[4], regime=Regime.SIDEWAYS)
    assert rooted.reason == KNIFE and "PHI_GE_ONE" in rooted.knife
    red_book, red_snap, red_pattern, red_reason = _breakdown()
    red = _call("guarded", red_book, red_snap, red_pattern, red_reason, regime=Regime.SIDEWAYS)
    assert red.reason == KNIFE and red.knife == ("RVOL_BREAKDOWN",)
    red_base = _call(
        "baseline", red_book, red_snap, red_pattern, red_reason, regime=Regime.SIDEWAYS
    )
    assert red_base.reason == "SIGNAL"


def test_sideways_fast_ou_signals_and_the_probability_triple_does_not_gate() -> None:
    book, _bar, snap, pattern, reason = _drive(_revert(Decimal("0.5"), WINDOW))
    assert pattern is not None
    chosen = _call("guarded", book, snap, pattern, reason, regime=Regime.SIDEWAYS)
    assert chosen.reason == "SIGNAL" and chosen.candidate is not None
    assert chosen.candidate.phi is not None
    assert abs(chosen.candidate.phi - Decimal("0.5")) < Decimal("1e-24")
    assert chosen.candidate.half_life_minutes == Decimal(5)
    assert chosen.candidate.limit_price < chosen.candidate.take_profit_price
    intent = make_intent(chosen.candidate, arm="guarded")
    assert intent.strategy_id == "S4"
    assert intent.expected_net_bps < 0
    assert intent.p_win_calibrated + intent.p_sl_calibrated + intent.p_timeout_calibrated == 1
    trend = _call("guarded", book, snap, pattern, reason, regime=Regime.TREND_UP)
    assert trend.reason == "S4_RESEARCH_OFF" and trend.knife == ()
    unknown = _call("guarded", book, snap, pattern, reason, regime=Regime.UNKNOWN)
    assert unknown.reason == "REGIME_UNKNOWN"
    late = _snapshot(Decimal("-1"), _start(WINDOW), DAY, intact=True, rsi=Decimal(31))
    late_asof = datetime.combine(DAY, time(15, 45), tzinfo=NEW_YORK)
    late = _retimed(late, late_asof)
    closed = decide(
        arm="baseline",
        symbol=SYMBOL,
        pattern=pattern,
        pattern_reason="READY",
        snap=late,
        regime=None,
        research_eligible=True,
        half_day=False,
        residuals=(),
        breakdown=False,
        spy_bar=None,
        spy_snap=None,
    )
    assert closed.reason == "ENTRY_WINDOW"
    outsider = decide(
        arm="baseline",
        symbol="TSLA",
        pattern=pattern,
        pattern_reason="READY",
        snap=snap,
        regime=None,
        research_eligible=True,
        half_day=False,
        residuals=(),
        breakdown=False,
        spy_bar=None,
        spy_snap=None,
    )
    assert outsider.reason == "NOT_IN_S4_UNIVERSE"


def test_comparison_stays_insufficient_below_thirty_closed_trades() -> None:
    thin = SplitScore(
        29, Decimal(1), Decimal(1), Decimal(2), Decimal(1), Decimal(1), Decimal("0.1")
    )
    full = SplitScore(
        30, Decimal(1), Decimal(1), Decimal(2), Decimal(1), Decimal(1), Decimal("0.1")
    )
    assert comparison(thin, full) == "insufficient"
    assert comparison(full, thin) == "insufficient"


def _revert(phi: Decimal, steps: int) -> list[Decimal]:
    """Exact AR(1) around -1. phi=0.5 from -2 is a dyadic series with no residual."""

    intercept = Decimal(-1) + phi
    values = [Decimal(-2)]
    for _ in range(steps - 1):
        values.append(intercept + phi * values[-1])
    return values


def _drift(steps: int) -> list[Decimal]:
    value = Decimal("-0.30")
    values: list[Decimal] = []
    for _ in range(steps):
        values.append(value)
        value -= Decimal("0.01")
    return values


def _drive(
    zs: list[Decimal],
    *,
    day: date = DAY,
) -> tuple[BounceBook, FeatureBar, SymbolSnapshot, object, str]:
    book = BounceBook()
    last: tuple[FeatureBar, SymbolSnapshot, object, str] | None = None
    for slot, z in enumerate(zs):
        bar, snap = _bar_and_snap(
            z,
            slot=slot,
            day=day,
            rsi=Decimal(31) if slot == len(zs) - 1 else Decimal(20),
            bounce=slot == len(zs) - 1,
        )
        pattern, why = book.observe(bar, snap)
        last = (bar, snap, pattern, why)
    if last is None:
        raise RuntimeError("empty series")
    return (book, last[0], last[1], last[2], last[3])


def _drive_more(book: BounceBook, z: Decimal, *, rsi: Decimal) -> None:
    slot = len(book.residuals(SYMBOL))
    bar, snap = _bar_and_snap(z, slot=slot, rsi=rsi, bounce=False)
    book.observe(bar, snap)


def _breakdown() -> tuple[BounceBook, SymbolSnapshot, object, str]:
    book = BounceBook()
    last: tuple[SymbolSnapshot, object, str] | None = None
    for slot in range(8):
        if slot < 6:
            z, rsi, rvol, open_, low = (
                Decimal("-0.50"),
                Decimal(20),
                Decimal(1),
                None,
                Decimal("99.50"),
            )
        elif slot == 6:
            z, rsi, rvol, open_, low = (
                Decimal(-2),
                Decimal(20),
                Decimal(3),
                Decimal(101),
                Decimal(97),
            )
        else:
            z, rsi, rvol, open_, low = (
                Decimal(-1),
                Decimal(31),
                Decimal(1),
                Decimal("98.90"),
                Decimal("98.80"),
            )
        close = Decimal(100) + z
        bar, snap = _bar_and_snap(
            z,
            slot=slot,
            rsi=rsi,
            rvol=rvol,
            open_=open_ if open_ is not None else close,
            low=low,
            bounce=False,
        )
        pattern, why = book.observe(bar, snap)
        last = (snap, pattern, why)
    if last is None:
        raise RuntimeError("empty breakdown")
    return book, last[0], last[1], last[2]


def _call(
    arm: str,
    book: BounceBook,
    snap: SymbolSnapshot,
    pattern: object,
    reason: str,
    *,
    regime: Regime,
    spy: tuple[FeatureBar, SymbolSnapshot] | None = None,
) -> object:
    if spy is None:
        spy = _spy(snap.data_asof_utc, drop=False, ret_norm=Decimal(0))
    market = _market(snap.data_asof_utc, regime)
    from bstocks_v2.strategies.s4.bounce import Pattern
    from bstocks_v2.strategies.s4.signal import Decision

    if pattern is not None and not isinstance(pattern, Pattern):
        raise RuntimeError("pattern")
    decision: Decision = decide(
        arm=arm,
        symbol=SYMBOL,
        pattern=pattern,
        pattern_reason=reason,
        snap=snap,
        regime=market,
        research_eligible=True,
        half_day=False,
        residuals=book.residuals(SYMBOL),
        breakdown=book.rvol_breakdown(SYMBOL),
        spy_bar=spy[0],
        spy_snap=spy[1],
    )
    return decision


def _bar_and_snap(
    z: Decimal,
    *,
    slot: int,
    day: date = DAY,
    rsi: Decimal = Decimal(20),
    rvol: Decimal = Decimal(1),
    intact: bool = True,
    bounce: bool = False,
    open_: Decimal | None = None,
    low: Decimal | None = None,
) -> tuple[FeatureBar, SymbolSnapshot]:
    close = Decimal(100) + z
    if open_ is None:
        open_ = close - Decimal("0.05") if bounce else close
    if low is None:
        low = min(open_, close)
    high = max(open_, close, low)
    start = _start(slot, day)
    bar = FeatureBar(
        symbol=SYMBOL,
        start=start,
        end=start + timedelta(minutes=5),
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=Decimal(1000),
        bar_vwap=close,
        closed=True,
        feed="sip",
    )
    return bar, _snapshot(z, start, day, intact=intact, rsi=rsi, rvol=rvol, close=close)


def _snapshot(
    z: Decimal,
    start: datetime,
    day: date,
    *,
    intact: bool,
    rsi: Decimal,
    rvol: Decimal = Decimal(1),
    close: Decimal | None = None,
) -> SymbolSnapshot:
    price = Decimal(100) + z if close is None else close
    return make_symbol_snapshot(
        symbol=SYMBOL,
        data_asof_utc=start + timedelta(minutes=5),
        bar_start=start,
        ny_date=day.isoformat(),
        bucket="11:30",
        vwap_intact=intact,
        close=price,
        vwap=Decimal(100) if intact else None,
        rsi6=rsi,
        atr14=Decimal(1),
        rvol_mean=rvol,
        rvol_median=rvol,
        ret=Decimal(0),
        sigma_tod=Decimal("0.01"),
        ret_norm=Decimal(0),
        vwap_slope=Decimal(0),
        trend_z=Decimal(0),
        rms6=Decimal(1),
        rel_vol=Decimal(1),
        baseline_days=20,
        above_vwap=False,
        warmed=True,
    )


def _retimed(snap: SymbolSnapshot, asof: datetime) -> SymbolSnapshot:
    return make_symbol_snapshot(
        symbol=snap.symbol,
        data_asof_utc=asof,
        bar_start=asof - timedelta(minutes=5),
        ny_date=snap.ny_date,
        bucket=snap.bucket,
        vwap_intact=snap.vwap_intact,
        close=snap.close,
        vwap=snap.vwap,
        rsi6=snap.rsi6,
        atr14=snap.atr14,
        rvol_mean=snap.rvol_mean,
        rvol_median=snap.rvol_median,
        ret=snap.ret,
        sigma_tod=snap.sigma_tod,
        ret_norm=snap.ret_norm,
        vwap_slope=snap.vwap_slope,
        trend_z=snap.trend_z,
        rms6=snap.rms6,
        rel_vol=snap.rel_vol,
        baseline_days=snap.baseline_days,
        above_vwap=snap.above_vwap,
        warmed=snap.warmed,
    )


def _spy(
    asof: datetime, *, drop: bool, ret_norm: Decimal | None = None
) -> tuple[FeatureBar, SymbolSnapshot]:
    start = asof - timedelta(minutes=5)
    open_ = Decimal(100)
    close = Decimal(99) if drop else open_
    if ret_norm is None:
        ret_norm = Decimal("-2") if drop else Decimal(0)
    bar = FeatureBar(
        symbol="SPY",
        start=start,
        end=asof,
        open=open_,
        high=max(open_, close),
        low=min(open_, close),
        close=close,
        volume=Decimal(1000),
        bar_vwap=close,
        closed=True,
        feed="sip",
    )
    snap = make_symbol_snapshot(
        symbol="SPY",
        data_asof_utc=asof,
        bar_start=start,
        ny_date=DAY.isoformat(),
        bucket="11:30",
        vwap_intact=True,
        close=close,
        vwap=Decimal(100),
        rsi6=Decimal(40),
        atr14=Decimal(1),
        rvol_mean=Decimal(1),
        rvol_median=Decimal(1),
        ret=close - open_,
        sigma_tod=Decimal("0.01"),
        ret_norm=ret_norm,
        vwap_slope=Decimal(0),
        trend_z=Decimal(0),
        rms6=Decimal(1),
        rel_vol=Decimal(1),
        baseline_days=20,
        above_vwap=True,
        warmed=True,
    )
    return bar, snap


def _market(asof: datetime, regime: Regime) -> FeatureSnapshot:
    chosen = route(regime, False)
    return FeatureSnapshot(
        data_asof_utc=asof,
        vwap_intact=True,
        features_hash="market",
        regime=regime,
        confidence=Decimal(0) if regime is Regime.UNKNOWN else Decimal(1),
        entries_allowed=chosen.entries_allowed,
        s1_enabled=chosen.s1_enabled,
        s2_enabled=chosen.s2_enabled,
        s3_enabled=chosen.s3_enabled,
        s4_research_enabled=chosen.s4_research_enabled,
        in_opening_window=False,
        data_ready=True,
        dwell_bars=3,
        block_reason="OK" if regime is not Regime.UNKNOWN else "UNKNOWN",
        benchmarks_present=13,
        breadth=Decimal("0.5"),
        trend_z=Decimal(0),
        spy_rvol=Decimal(1),
        spy_rms6=Decimal(1),
    )


def _start(slot: int, day: date = DAY) -> datetime:
    return datetime.combine(day, time(9, 30), tzinfo=NEW_YORK) + timedelta(minutes=5 * slot)
