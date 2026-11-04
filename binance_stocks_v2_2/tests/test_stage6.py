"""Stage 6: causal S1 filters, orthogonal sector residual, and the entry gate."""

import ast
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from bstocks_v2.constitution import live_trade_enabled
from bstocks_v2.feature_store.bars import FeatureBar, make_symbol_snapshot
from bstocks_v2.feature_store.engine import CausalFeatureBook
from bstocks_v2.regime.classifier import SECTORS, FeatureSnapshot, Regime
from bstocks_v2.research_backtest.models import EngineConfig, FillMode
from bstocks_v2.research_backtest.replay import OrderReplayEngine
from bstocks_v2.research_backtest.tape import BarTape, ClosedBar
from bstocks_v2.research_data.calendar import NEW_YORK, XnysCalendar
from bstocks_v2.strategies.s1.book import FactorBoard
from bstocks_v2.strategies.s1.filters import (
    CausalKalman,
    Fit,
    RollingOLS,
    ThroughOrigin,
    make_filter,
)
from bstocks_v2.strategies.s1.load import require_split_end
from bstocks_v2.strategies.s1.score import ModelScore, recommend, summarize
from bstocks_v2.strategies.s1.signal import decide
from bstocks_v2.strategies.s1.spec import (
    LOCKBOX_START,
    MODELS,
    SECTOR_PARENT,
    TRADED,
    VAL_END,
    WINDOW,
)

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "src" / "bstocks_v2" / "strategies" / "s1"
TOLERANCE = Decimal("1e-9")
ASOF = datetime(2024, 11, 1, 14, 35, tzinfo=UTC)


def test_sources_do_not_use_float_literals_or_a_smoother() -> None:
    for path in PACKAGE.rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, float):
                raise AssertionError(path.name)
            called = node.func if isinstance(node, ast.Call) else None
            if isinstance(called, ast.Name) and called.id == "float":
                raise AssertionError(path.name)
        assert "def smooth" not in source
        assert "RTS" not in source
    assert not hasattr(CausalKalman, "smooth")


def test_lockbox_is_refused_and_the_map_is_sector_etfs_only() -> None:
    with pytest.raises(RuntimeError, match="lockbox"):
        require_split_end(LOCKBOX_START)
    require_split_end(VAL_END)
    assert live_trade_enabled() is False
    assert "SPY" not in TRADED
    assert "TSLA" not in SECTOR_PARENT
    for symbol, parent in SECTOR_PARENT.items():
        assert parent in SECTORS
        assert parent != symbol
        assert symbol not in SECTORS


def test_orthogonal_residual_is_uncorrelated_with_spy() -> None:
    slope = ThroughOrigin()
    last: tuple[Decimal, Decimal] | None = None
    for step in range(WINDOW + 5):
        spy = Decimal(step % 5 - 2) / Decimal(10)
        if spy == 0:
            spy = Decimal("0.05")
        last = slope.observe(spy, spy * 2, f"{step:04d}")
    assert last is not None
    gamma, perp = last
    assert abs(gamma - Decimal(2)) < TOLERANCE
    assert abs(perp) < TOLERANCE
    dot = slope.residual_dot()
    assert dot is not None
    assert abs(dot) < TOLERANCE


def test_repeating_the_same_bar_does_not_move_the_residual() -> None:
    board = FactorBoard()
    previous: Decimal | None = None
    for step in range(WINDOW):
        spy = Decimal(step + 1) / Decimal(10)
        sector = spy * 2 + Decimal("0.01")
        previous = board.update("XLK", spy, sector, f"{step:04d}")
    assert previous is not None
    spy = Decimal(WINDOW) / Decimal(10)
    sector = spy * 2 + Decimal("0.01")
    again = board.update("XLK", spy, sector, f"{WINDOW - 1:04d}")
    assert again == previous
    assert board.strength("XLK") == board.strength("XLK")


def test_ols_recovers_a_noiseless_line() -> None:
    model = RollingOLS(window=10)
    fit = Fit(Decimal(0), Decimal(0), Decimal(0), None, 0, False)
    for step in range(10):
        market = Decimal(step - 5) / Decimal(10)
        sector = Decimal((step * 3) % 5 - 2) / Decimal(10)
        target = Decimal("0.2") + Decimal("0.5") * market + Decimal("-0.25") * sector
        fit = model.update((Decimal(1), market, sector), target)
    assert fit.solved
    assert abs(fit.alpha - Decimal("0.2")) < TOLERANCE
    assert abs(fit.beta_market - Decimal("0.5")) < TOLERANCE
    assert abs(fit.beta_sector - Decimal("-0.25")) < TOLERANCE


def test_future_bar_does_not_change_alpha() -> None:
    base = _path(200)
    moved = _path(200, last_stock=Decimal("4"))
    shifted = _path(200, last_sector=Decimal("3"))
    for model in MODELS:
        original = _alphas(model, base)
        after_stock = _alphas(model, moved)
        after_sector = _alphas(model, shifted)
        assert original[:-1] == after_stock[:-1]
        assert original[:-1] == after_sector[:-1]
        assert original[-1] != after_stock[-1]
        assert original[-1] != after_sector[-1]


def test_equal_raw_returns_are_smaller_in_the_noisier_time_slot() -> None:
    days = _weekdays(22)
    first = _normalize(days)
    changed = list(days)
    second = _normalize(changed, future_open=Decimal("0.2"))
    day = days[20]
    assert first[(day, "09:30")] == second[(day, "09:30")]
    assert first[(day, "12:00")] == second[(day, "12:00")]
    assert first[(day, "09:30")] < first[(day, "12:00")]


def test_threshold_gates_block_before_an_intent_exists() -> None:
    stock = _stock()
    regime = _regime(Regime.TREND_UP, enabled=True)
    blocked, reason = decide(
        model="ols",
        symbol="SMH",
        fit=_fit(Decimal("0.2"), Decimal("0.01")),
        sector_strength=Decimal(1),
        stock=stock,
        regime=regime,
        research_eligible=True,
    )
    assert blocked is None
    assert reason == "Z"
    missing, why = decide(
        model="ols",
        symbol="TSLA",
        fit=_fit(Decimal("0.3"), Decimal("0.01")),
        sector_strength=Decimal(1),
        stock=stock,
        regime=regime,
        research_eligible=True,
    )
    assert missing is None and why == "NOT_IN_S1_UNIVERSE"
    ineligible, why = decide(
        model="ols",
        symbol="SMH",
        fit=_fit(Decimal("0.3"), Decimal("0.01")),
        sector_strength=Decimal(1),
        stock=stock,
        regime=regime,
        research_eligible=False,
    )
    assert ineligible is None and why == "NOT_ELIGIBLE"
    for state in (Regime.UNKNOWN, Regime.HIGH_VOL_PANIC):
        intent, why = decide(
            model="ols",
            symbol="SMH",
            fit=_fit(Decimal("0.3"), Decimal("0.01")),
            sector_strength=Decimal(1),
            stock=stock,
            regime=_regime(state, enabled=False),
            research_eligible=True,
        )
        assert intent is None and why == "REGIME"


def test_conservative_replay_fills_one_s1_intent() -> None:
    intent, reason = decide(
        model="kalman",
        symbol="SMH",
        fit=_fit(Decimal("0.3"), Decimal("0.01")),
        sector_strength=Decimal(1),
        stock=_stock(),
        regime=_regime(Regime.TREND_UP, enabled=True),
        research_eligible=True,
    )
    assert reason == "SIGNAL"
    assert intent is not None
    assert intent.strategy_id == "S1"
    assert intent.direction.value == "LONG"
    assert intent.model_version.endswith("uncalibrated")
    engine = OrderReplayEngine(EngineConfig(mode=FillMode.CONSERVATIVE))
    result = engine.run(BarTape(_tape()), _Once(intent))
    record = result.records[0]
    assert record.status == "FILLED"
    assert record.exit_reason == "TAKE_PROFIT"
    assert record.filled_qty == Decimal(3)
    assert record.wallet_type == "CARD"
    assert record.tokenize is False
    assert record.net_pnl == Decimal("4.13775")
    assert live_trade_enabled() is False


def test_another_symbol_cannot_stop_out_the_position() -> None:
    intent, reason = decide(
        model="kalman",
        symbol="SMH",
        fit=_fit(Decimal("0.3"), Decimal("0.01")),
        sector_strength=Decimal(1),
        stock=_stock(),
        regime=_regime(Regime.TREND_UP, enabled=True),
        research_eligible=True,
    )
    assert reason == "SIGNAL" and intent is not None
    foreign = ClosedBar(
        symbol="GDXJ",
        start=datetime(2024, 11, 1, 14, 40, tzinfo=UTC),
        end=datetime(2024, 11, 1, 14, 45, tzinfo=UTC),
        open=Decimal("10"),
        high=Decimal("11"),
        low=Decimal("1"),
        close=Decimal("10"),
        volume=Decimal(1000),
        timeframe="5m",
    )
    engine = OrderReplayEngine(EngineConfig(mode=FillMode.CONSERVATIVE))
    result = engine.run(BarTape([*_tape(), foreign]), _Once(intent))
    record = result.records[0]
    assert record.exit_reason == "TAKE_PROFIT"
    assert record.exit_price == Decimal("101.50")
    assert record.net_pnl == Decimal("4.13775")


def test_recommendation_keeps_the_simpler_model_without_a_clear_gain() -> None:
    quiet = _models(Decimal("1"), Decimal("1"), Decimal("1.2"), Decimal("5"))
    assert recommend(quiet) == "ols"
    kalman = _models(Decimal("1"), Decimal("1"), Decimal("1.2"), Decimal("5"))
    kalman["kalman"] = _row("kalman", Decimal("20"), Decimal("0.1"), Decimal("2"), Decimal("5"))
    assert recommend(kalman) == "kalman"
    rls = _models(Decimal("1"), Decimal("1"), Decimal("1.2"), Decimal("5"))
    rls["rls"] = _row("rls", Decimal("20"), Decimal("0.1"), Decimal("2"), Decimal("5"))
    rls["kalman"] = _row("kalman", Decimal("0"), Decimal("0.1"), Decimal("1.1"), Decimal("5"))
    assert recommend(rls) == "rls"
    short = _models(Decimal("1"), Decimal("1"), Decimal("1.2"), Decimal("5"), trades=10)
    short["kalman"] = _row("kalman", Decimal("20"), Decimal("0.1"), Decimal("2"), Decimal("5"), 10)
    assert recommend(short) == "ols"
    weak_pf = _models(Decimal("1"), Decimal("1"), Decimal("2"), Decimal("5"))
    weak_pf["kalman"] = _row("kalman", Decimal("20"), Decimal("0.1"), Decimal("0.5"), Decimal("5"))
    assert recommend(weak_pf) == "ols"
    deep = _models(Decimal("1"), Decimal("1"), Decimal("1.2"), Decimal("10"))
    deep["kalman"] = _row("kalman", Decimal("20"), Decimal("0.1"), Decimal("2"), Decimal("100"))
    assert recommend(deep) == "ols"
    draw = summarize(
        "ols",
        [Decimal(1), Decimal(-3), Decimal(1)],
        [Decimal(1), Decimal(1), Decimal(1)],
    )
    assert draw.max_drawdown_usdc == Decimal(3)
    stats = summarize("ols", [Decimal(1), Decimal(1)], [Decimal(10), Decimal(30)])
    assert stats.mean_net_bps == Decimal(20)
    assert stats.se_net_bps == Decimal(10)


def _path(
    count: int,
    last_stock: Decimal | None = None,
    last_sector: Decimal | None = None,
) -> list[tuple[Decimal, Decimal, Decimal]]:
    rows: list[tuple[Decimal, Decimal, Decimal]] = []
    for step in range(count):
        spy = Decimal(step % 5 - 2) / Decimal(10)
        ortho = Decimal(step % 3 - 1) / Decimal(10)
        sector = spy * 2 + ortho
        stock = Decimal("0.05") + spy * Decimal("0.4") + ortho * Decimal("0.3")
        rows.append((spy, sector, stock))
    spy, sector, stock = rows[-1]
    if last_stock is not None:
        stock = last_stock
    if last_sector is not None:
        sector = last_sector
    rows[-1] = (spy, sector, stock)
    return rows


def _alphas(model: str, rows: list[tuple[Decimal, Decimal, Decimal]]) -> list[Decimal | None]:
    board = FactorBoard()
    filt = make_filter(model)
    alphas: list[Decimal | None] = []
    for index, (spy, sector, stock) in enumerate(rows):
        perp = board.update("XLK", spy, sector, f"{index:04d}")
        if perp is None:
            alphas.append(None)
            continue
        fit = filt.update((Decimal(1), spy, perp), stock)
        alphas.append(fit.alpha if fit.solved else None)
    return alphas


def _weekdays(count: int) -> list[date]:
    days: list[date] = []
    cursor = date(2024, 11, 4)
    while len(days) < count:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor += timedelta(days=1)
    return days


def _normalize(
    days: list[date],
    future_open: Decimal | None = None,
) -> dict[tuple[date, str], Decimal]:
    calendar = XnysCalendar({day: time(16, 0) for day in days})
    book = CausalFeatureBook(calendar)
    found: dict[tuple[date, str], tuple[Decimal, Decimal, Decimal]] = {}
    for index, day in enumerate(days):
        price = Decimal(100)
        for slot in range(31):
            if index == 20 and slot in {0, 30}:
                ret = Decimal("0.01")
            elif slot == 0:
                ret = Decimal("0.02") if index % 2 == 0 else Decimal("0.01")
                if future_open is not None and index == 21:
                    ret = future_open
            elif slot == 30:
                ret = Decimal("0.004") if index % 2 == 0 else Decimal("0.005")
            else:
                ret = Decimal("0.001")
            close = price * (Decimal(1) + ret)
            snapshot = book.update(_feature(day, slot, price, close))
            price = close
            if (
                snapshot is None
                or snapshot.ret is None
                or snapshot.sigma_tod is None
                or snapshot.ret_norm is None
                or index != 20
                or slot not in {0, 30}
            ):
                continue
            found[(day, snapshot.bucket)] = (snapshot.ret, snapshot.sigma_tod, snapshot.ret_norm)
    assert set(found) == {(days[20], "09:30"), (days[20], "12:00")}
    for key in found:
        ret, sigma, norm = found[key]
        assert abs(ret - Decimal("0.01")) < TOLERANCE
        assert abs(norm - ret / sigma) < TOLERANCE
    open_ret, open_sigma, open_norm = found[(days[20], "09:30")]
    _noon_ret, noon_sigma, noon_norm = found[(days[20], "12:00")]
    assert open_sigma > noon_sigma
    assert open_norm < noon_norm
    return {key: found[key][2] for key in found}


def _feature(day: date, slot: int, opened: Decimal, close: Decimal) -> FeatureBar:
    start = datetime.combine(day, time(9, 30), tzinfo=NEW_YORK) + timedelta(minutes=5 * slot)
    return FeatureBar(
        symbol="SMH",
        start=start,
        end=start + timedelta(minutes=5),
        open=opened,
        high=max(opened, close),
        low=min(opened, close),
        close=close,
        volume=Decimal(1000),
        bar_vwap=close,
        closed=True,
        feed="sip",
    )


def _fit(alpha: Decimal, variance: Decimal) -> Fit:
    return Fit(alpha, Decimal("0.1"), Decimal("0.1"), variance, 80, True)


def _stock() -> object:
    start = ASOF - timedelta(minutes=5)
    return make_symbol_snapshot(
        symbol="SMH",
        data_asof_utc=ASOF,
        bar_start=start,
        ny_date="2024-11-01",
        bucket="10:30",
        vwap_intact=True,
        close=Decimal("100"),
        vwap=Decimal("99.50"),
        rsi6=Decimal(50),
        atr14=Decimal(1),
        rvol_mean=Decimal(1),
        rvol_median=Decimal(1),
        ret=Decimal("0.001"),
        sigma_tod=Decimal("0.001"),
        ret_norm=Decimal(1),
        vwap_slope=Decimal("0.1"),
        trend_z=Decimal(1),
        rms6=Decimal(1),
        rel_vol=Decimal(1),
        baseline_days=20,
        above_vwap=True,
        warmed=True,
    )


def _regime(regime: Regime, *, enabled: bool) -> FeatureSnapshot:
    return FeatureSnapshot(
        data_asof_utc=ASOF,
        vwap_intact=True,
        features_hash="abc",
        regime=regime,
        confidence=Decimal(0) if regime is Regime.UNKNOWN else Decimal(1),
        entries_allowed=enabled,
        s1_enabled=enabled,
        s2_enabled=enabled,
        s3_enabled=regime in {Regime.TREND_UP, Regime.SIDEWAYS},
        s4_research_enabled=False,
        in_opening_window=False,
        data_ready=True,
        dwell_bars=3,
        block_reason="OK" if enabled else regime.value,
        benchmarks_present=13,
        breadth=Decimal("0.6"),
        trend_z=Decimal("1.2"),
        spy_rvol=Decimal(1),
        spy_rms6=Decimal(1),
    )


class _Once:
    def __init__(self, intent: object) -> None:
        self.intent = intent

    def on_close(self, view: object, bar: ClosedBar) -> object:
        if bar.end == ASOF and bar.symbol == "SMH":
            return self.intent
        return None


def _tape() -> list[ClosedBar]:
    specs = (
        (30, Decimal("100"), Decimal("100.20"), Decimal("99.90"), Decimal("100")),
        (35, Decimal("100"), Decimal("100.20"), Decimal("99.97"), Decimal("100.10")),
        (40, Decimal("100.20"), Decimal("101.60"), Decimal("100.00"), Decimal("101.40")),
    )
    bars: list[ClosedBar] = []
    for minute, opened, high, low, close in specs:
        start = datetime(2024, 11, 1, 14, minute, tzinfo=UTC)
        bars.append(
            ClosedBar(
                symbol="SMH",
                start=start,
                end=start + timedelta(minutes=5),
                open=opened,
                high=high,
                low=low,
                close=close,
                volume=Decimal(1000),
                timeframe="5m",
            )
        )
    return bars


def _row(
    name: str,
    mean: Decimal,
    se: Decimal,
    factor: Decimal,
    drawdown: Decimal,
    trades: int = 40,
) -> ModelScore:
    return ModelScore(name, trades, mean, se, factor, drawdown, Decimal(0))


def _models(
    mean: Decimal,
    se: Decimal,
    factor: Decimal,
    drawdown: Decimal,
    trades: int = 40,
) -> dict[str, ModelScore]:
    return {name: _row(name, mean, se, factor, drawdown, trades) for name in MODELS}
