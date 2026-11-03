"""Stage 7: S2 pullback rules, simplex calibration, and the causal label."""

import ast
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from bstocks_v2.constitution import live_trade_enabled
from bstocks_v2.contracts.models import TradeIntent
from bstocks_v2.feature_store.bars import FeatureBar, SymbolSnapshot, make_symbol_snapshot
from bstocks_v2.feature_store.engine import CausalFeatureBook
from bstocks_v2.feature_store.errors import LookaheadError
from bstocks_v2.market_ingestor.wal import wilder_rsi
from bstocks_v2.regime.classifier import FeatureSnapshot, Regime, route
from bstocks_v2.research_backtest.models import EngineConfig, FillMode
from bstocks_v2.research_backtest.replay import OrderReplayEngine
from bstocks_v2.research_backtest.tape import BarTape, CausalView, ClosedBar
from bstocks_v2.research_data.calendar import NEW_YORK, XnysCalendar
from bstocks_v2.strategies.s1.load import require_split_end
from bstocks_v2.strategies.s1.spec import LOCKBOX_START as S1_LOCK
from bstocks_v2.strategies.s1.spec import TRAIN_END as S1_TRAIN_END
from bstocks_v2.strategies.s1.spec import VAL_END as S1_VAL_END
from bstocks_v2.strategies.s2.label import label_path, train_timeout_bps
from bstocks_v2.strategies.s2.model import (
    Sample,
    deduped,
    fit,
    solve,
    split_role,
    usable,
)
from bstocks_v2.strategies.s2.pattern import Pattern, PullbackBook
from bstocks_v2.strategies.s2.score import SplitScore, prefer_gate
from bstocks_v2.strategies.s2.signal import (
    UNCALIBRATED,
    Candidate,
    decide,
    expected_net_bps,
    gate_open,
    make_intent,
    opening_qty,
)
from bstocks_v2.strategies.s2.simplex import (
    bootstrap_interval,
    choose_temperature,
    confidence_ece,
    softmax,
    temper,
)
from bstocks_v2.strategies.s2.spec import (
    LOCKBOX_START,
    PENETRATION,
    TRAIN_END,
    VAL_END,
)

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "src" / "bstocks_v2" / "strategies" / "s2"
DAY = date(2024, 11, 1)
ASOF = datetime(2024, 11, 1, 15, 0, tzinfo=UTC)


def test_sources_do_not_use_float_literals() -> None:
    for path in PACKAGE.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, float):
                raise AssertionError(path.name)
            called = node.func if isinstance(node, ast.Call) else None
            if isinstance(called, ast.Name) and called.id == "float":
                raise AssertionError(path.name)


def test_window_matches_stage6_and_live_trading_stays_off() -> None:
    with pytest.raises(RuntimeError, match="lockbox"):
        require_split_end(LOCKBOX_START)
    assert LOCKBOX_START == S1_LOCK
    assert TRAIN_END == S1_TRAIN_END
    assert VAL_END == S1_VAL_END
    assert live_trade_enabled() is False
    with pytest.raises(RuntimeError, match="open window"):
        split_role(LOCKBOX_START)
    assert split_role(TRAIN_END) == "train"
    assert split_role(date(2026, 1, 30)) == "cal"
    assert split_role(date(2026, 2, 2)) == "score"
    assert usable("train", "TP", Decimal(1), temperature=False)
    assert not usable("cal", "TP", Decimal(1), temperature=False)
    assert usable("cal", "TP", Decimal(1), temperature=True)
    assert not usable("score", "TO", Decimal(1), temperature=True)
    assert not usable("train", "UNFILLED", Decimal(1), temperature=False)
    assert sum(UNCALIBRATED) == 1


def test_vwap_resets_and_rsi_carries_across_the_session() -> None:
    day2 = date(2024, 11, 4)
    calendar = XnysCalendar({DAY: time(16, 0), day2: time(16, 0)})
    book = CausalFeatureBook(calendar)
    closes: list[Decimal] = []
    price = Decimal(50)
    last = None
    for slot in range(8):
        close = price + Decimal("0.50")
        last = book.update(_feature_bar(DAY, slot, price, close, Decimal(10000), Decimal(50)))
        price = close
        closes.append(close)
    assert last is not None and last.rsi6 is not None
    assert last.vwap == Decimal(50)
    second = book.update(
        _feature_bar(day2, 0, Decimal(80), Decimal("80.50"), Decimal(100), Decimal(80))
    )
    assert second is not None and second.vwap == Decimal(80)
    closes.append(Decimal("80.50"))
    assert second.rsi6 == wilder_rsi(closes)
    carried = second.rsi6
    later = book.update(
        _feature_bar(day2, 1, Decimal("80.50"), Decimal(70), Decimal(100), Decimal(80))
    )
    assert later is not None and later.rsi6 != carried
    assert second.rsi6 == carried
    fresh = CausalFeatureBook(calendar)
    with pytest.raises(LookaheadError, match="unclosed"):
        fresh.update(
            FeatureBar(
                symbol="SMH",
                start=datetime.combine(DAY, time(9, 30), tzinfo=NEW_YORK),
                end=datetime.combine(DAY, time(9, 35), tzinfo=NEW_YORK),
                open=Decimal(50),
                high=Decimal(51),
                low=Decimal(50),
                close=Decimal(51),
                volume=Decimal(100),
                bar_vwap=Decimal(50),
                closed=False,
                feed="sip",
            )
        )


def test_pullback_rules_cooldown_and_a_future_bar() -> None:
    book = PullbackBook()
    original: Pattern | None = None
    for slot, (rsi, volume) in enumerate(_path()):
        found, reason = _observe(book, slot, rsi, volume)
        if slot == 12:
            assert reason == "READY" and found is not None
            original = found
            book.commit("SMH")
    assert original is not None and original.limit_price == Decimal("101.00")
    blocked = _observe(book, 17, Decimal(55), Decimal(400))
    assert blocked[0] is None and blocked[1] == "COOLDOWN"
    _observe(book, 19, Decimal(80), Decimal(400), close=Decimal(90), vwap=Decimal(100))
    assert original.limit_price == Decimal("101.00")
    later = PullbackBook()
    _commit_through(later, 16)
    assert _observe(later, 17, Decimal(48), Decimal(400))[1] == "NO_CROSS"
    assert _observe(later, 18, Decimal(55), Decimal(400))[1] == "READY"
    other = PullbackBook()
    for slot, (rsi, volume) in enumerate(_path()[:12]):
        _observe(other, slot, rsi, volume)
    missed = _observe(other, 12, Decimal(55), Decimal(1000), close=Decimal(100), vwap=Decimal(100))
    assert missed[1] == "BELOW_VWAP"
    assert _observe(PullbackBook(), 0, Decimal(60), Decimal(1000), slope=Decimal(0))[1] == "WARMUP"
    long = PullbackBook()
    for slot, (rsi, volume) in enumerate(_path()[:12]):
        _observe(long, slot, rsi, volume)
    assert _observe(long, 12, Decimal(55), Decimal(1000), close=Decimal(110))[1] == "EXTENDED"


def test_regime_block_does_not_commit_the_cooldown() -> None:
    book = PullbackBook()
    pattern = None
    for slot, (rsi, volume) in enumerate(_path()[:13]):
        pattern, reason = _observe(book, slot, rsi, volume)
    assert reason == "READY" and pattern is not None
    snap = _snapshot(12, Decimal(55), Decimal(1000))
    blocked, why = decide(
        symbol="SMH",
        pattern=pattern,
        pattern_reason="READY",
        snap=snap,
        regime=_regime(Regime.UNKNOWN),
        research_eligible=True,
        half_day=False,
        book=book,
    )
    assert blocked is None and why == "REGIME"
    candidate, why = decide(
        symbol="SMH",
        pattern=pattern,
        pattern_reason="READY",
        snap=snap,
        regime=_regime(Regime.TREND_UP),
        research_eligible=True,
        half_day=False,
        book=book,
    )
    assert why == "SIGNAL" and candidate is not None
    assert candidate.sector == "XLK"
    outsider, why = decide(
        symbol="TSLA",
        pattern=pattern,
        pattern_reason="READY",
        snap=snap,
        regime=_regime(Regime.TREND_UP),
        research_eligible=True,
        half_day=False,
        book=book,
    )
    assert outsider is None and why == "NOT_IN_S2_UNIVERSE"


def test_simplex_temperature_and_the_gate_formula() -> None:
    flat = softmax((Decimal(0), Decimal(0), Decimal(0)))
    assert sum(flat) == 1
    hot = softmax((Decimal(2), Decimal(0), Decimal(0)))
    cooled = temper((Decimal(2), Decimal(0), Decimal(0)), Decimal(2))
    assert sum(hot) == 1 and sum(cooled) == 1
    third = Decimal(1) / Decimal(3)
    assert abs(cooled[0] - third) < abs(hot[0] - third)
    mixed = [(Decimal(3), Decimal(0), Decimal(0))] * 20
    grid = dict(low=Decimal("0.50"), high=Decimal(2), step=Decimal("0.50"))
    assert choose_temperature(mixed, [0] * 10 + [1] * 10, **grid) > 1
    assert choose_temperature(mixed, [0] * 20, **grid) == Decimal("0.50")
    net = expected_net_bps(
        (Decimal("0.50"), Decimal("0.25"), Decimal("0.25")),
        limit=Decimal(100),
        stop=Decimal(99),
        take_profit=Decimal(101),
        timeout_bps=Decimal(0),
    )
    # 0.50 * 100 - 0.25 * 100 - 12 bps of commission and spread.
    assert net == Decimal(13)
    assert gate_open(Decimal(0)) is False
    assert gate_open(Decimal("0.01")) is True
    assert opening_qty(Decimal(100), Decimal(99)) == Decimal(3)


def test_label_matches_the_conservative_replay() -> None:
    config = EngineConfig(mode=FillMode.CONSERVATIVE)
    assert config.fixed_delta == PENETRATION
    calendar = XnysCalendar({DAY: time(16, 0)})
    take_profit = _tape(_take_profit_overrides())
    stopped = _tape({1: (Decimal(100), Decimal(103), Decimal(98), Decimal(100))})
    both = _tape(
        {
            1: (Decimal("100.20"), Decimal("100.40"), Decimal("99.90"), Decimal("100.30")),
            2: (Decimal(100), Decimal(103), Decimal(98), Decimal(100)),
        }
    )
    timeout = _tape({index: _quiet() for index in range(2, 21)})
    timeout[20] = _minute(
        20, Decimal("100.10"), Decimal("100.40"), Decimal("99.95"), Decimal("100.20")
    )
    _agree(take_profit, calendar, config, "TP", "TAKE_PROFIT", Decimal("101.50"), False)
    _agree(stopped, calendar, config, "SL", "STOP", Decimal(99), True)
    _agree(both, calendar, config, "SL", "STOP", Decimal(99), True)
    _agree(timeout, calendar, config, "TO", "TIMEOUT", Decimal("100.10"), False)


def test_timeout_mean_and_zero_weight_do_not_leak() -> None:
    base = train_timeout_bps(
        [
            (date(2025, 6, 2), "TO", Decimal(10)),
            (date(2025, 6, 3), "TP", Decimal(50)),
            (date(2026, 3, 2), "TO", Decimal(80)),
        ]
    )
    assert base == Decimal(10)
    assert train_timeout_bps([(date(2026, 3, 2), "TO", Decimal(80))]) == 0
    first = fit(_pair(0), parents=9, vol_edges_pair=_EDGES, **_LOOSE)
    second = fit(_pair(0) + [_row(2, Decimal(0))], parents=9, vol_edges_pair=_EDGES, **_LOOSE)
    assert first.theta == second.theta
    solved = solve(
        [[Decimal(1) if col == row else Decimal(0) for col in range(3)] for row in range(3)],
        [Decimal(2), Decimal(3), Decimal(4)],
    )
    assert solved == [Decimal(2), Decimal(3), Decimal(4)]


def test_dedup_and_a_separable_simplex() -> None:
    start = datetime(2024, 11, 1, 15, 0, tzinfo=UTC)
    rows = [
        (start + timedelta(minutes=minutes), "SMH", _row(label, Decimal(1)))
        for minutes, label in ((0, 0), (10, 1), (20, 2), (30, 0))
    ]
    weights = [sample.weight for sample in deduped(rows, timedelta(minutes=30))]
    assert weights == [Decimal(1), Decimal(0), Decimal(0), Decimal(1)]
    samples = [_row_at(label) for label in range(3) for _ in range(15)]
    fitted = fit(samples, parents=9, vol_edges_pair=_EDGES, **_LOOSE)
    for label in range(3):
        probs = fitted.probabilities(_features(label), 0, Decimal(0), Decimal(1))
        assert sum(probs) == 1
        assert probs.index(max(probs)) == label


def test_ece_is_zero_when_confidence_matches_and_the_draw_is_stable() -> None:
    perfect = [((Decimal(1), Decimal(0), Decimal(0)), 0)] * 8
    assert confidence_ece(perfect) == 0
    bad = [((Decimal("0.90"), Decimal("0.05"), Decimal("0.05")), 1)] * 8
    assert confidence_ece(bad) == Decimal("0.90")
    values = [Decimal(index) for index in range(10)]
    left = bootstrap_interval(values, 20, 7)
    right = bootstrap_interval(values, 20, 7)
    assert left == right and left is not None and left[0] <= left[1]


def test_gate_needs_a_pre_registered_edge() -> None:
    quiet = _score(Decimal(1), Decimal(1), Decimal("1.2"), Decimal(5))
    assert prefer_gate(quiet, quiet) is False
    short = _score(Decimal(20), Decimal("0.1"), Decimal(2), Decimal(5), trades=10)
    assert prefer_gate(quiet, short) is False
    strong = _score(Decimal(20), Decimal("0.1"), Decimal(2), Decimal(5))
    assert prefer_gate(quiet, strong) is True


def _agree(
    bars: list[ClosedBar],
    calendar: XnysCalendar,
    config: EngineConfig,
    outcome: str,
    reason: str,
    price: Decimal,
    ambiguous: bool,
) -> None:
    candidate = _candidate(bars[0].end)
    intent = make_intent(
        candidate,
        kind="rule",
        probabilities=UNCALIBRATED,
        timeout_bps=Decimal(0),
        model_version="s2-v1-rule-uncalibrated",
    )
    assert intent.strategy_id == "S2"
    assert intent.model_version.endswith("uncalibrated")
    result = OrderReplayEngine(config, calendar=calendar).run(BarTape(bars), _Once(intent))
    record = result.records[0]
    label = label_path(
        bars,
        0,
        limit=candidate.limit_price,
        stop=candidate.stop_price,
        take_profit=candidate.take_profit_price,
        calendar=calendar,
    )
    assert record.status == "FILLED"
    assert record.exit_reason == reason
    assert record.exit_price == price
    assert record.ambiguous_bar_sl_assumed is ambiguous
    assert label.outcome == outcome
    assert label.exit_price == price
    assert label.ambiguous is ambiguous
    assert record.wallet_type == "CARD" and record.tokenize is False


def _commit_through(book: PullbackBook, last: int) -> None:
    for slot, (rsi, volume) in enumerate(_path()):
        if slot > last:
            break
        _observe(book, slot, rsi, volume)
        if slot == 12:
            book.commit("SMH")


def _path() -> list[tuple[Decimal, Decimal]]:
    rows: list[tuple[Decimal, Decimal]] = [(Decimal(60), Decimal(1000)) for _ in range(6)]
    rows.append((Decimal(35), Decimal(400)))
    rows.extend((Decimal(45), Decimal(1000)) for _ in range(5))
    rows.append((Decimal(55), Decimal(1000)))
    rows.append((Decimal(35), Decimal(400)))
    rows.extend((Decimal(45), Decimal(400)) for _ in range(3))
    return rows


def _observe(
    book: PullbackBook,
    slot: int,
    rsi: Decimal,
    volume: Decimal,
    *,
    close: Decimal = Decimal(101),
    vwap: Decimal = Decimal(100),
    slope: Decimal = Decimal("0.10"),
) -> tuple[Pattern | None, str]:
    start = datetime.combine(DAY, time(9, 30), tzinfo=NEW_YORK) + timedelta(minutes=5 * slot)
    end = start + timedelta(minutes=5)
    low = Decimal(100)
    bar = FeatureBar(
        symbol="SMH",
        start=start,
        end=end,
        open=close,
        high=max(close, low),
        low=low,
        close=close,
        volume=volume,
        bar_vwap=vwap,
        closed=True,
        feed="sip",
    )
    return book.observe(bar, _snapshot_at(end, start, close, vwap, rsi, slope, volume))


def _snapshot(slot: int, rsi: Decimal, volume: Decimal) -> SymbolSnapshot:
    start = datetime.combine(DAY, time(9, 30), tzinfo=NEW_YORK) + timedelta(minutes=5 * slot)
    return _snapshot_at(
        start + timedelta(minutes=5),
        start,
        Decimal(101),
        Decimal(100),
        rsi,
        Decimal("0.10"),
        volume,
    )


def _snapshot_at(
    end: datetime,
    start: datetime,
    close: Decimal,
    vwap: Decimal,
    rsi: Decimal,
    slope: Decimal,
    _volume: Decimal,
) -> SymbolSnapshot:
    return make_symbol_snapshot(
        symbol="SMH",
        data_asof_utc=end,
        bar_start=start,
        ny_date=DAY.isoformat(),
        bucket=start.astimezone(NEW_YORK).strftime("%H:%M"),
        vwap_intact=True,
        close=close,
        vwap=vwap,
        rsi6=rsi,
        atr14=Decimal(1),
        rvol_mean=Decimal(1),
        rvol_median=Decimal(1),
        ret=Decimal("0.001"),
        sigma_tod=Decimal("0.001"),
        ret_norm=Decimal(1),
        vwap_slope=slope,
        trend_z=Decimal(1),
        rms6=Decimal(1),
        rel_vol=Decimal(1),
        baseline_days=20,
        above_vwap=close > vwap,
        warmed=True,
    )


def _feature_bar(
    day: date, slot: int, opened: Decimal, close: Decimal, volume: Decimal, vwap: Decimal
) -> FeatureBar:
    start = datetime.combine(day, time(9, 30), tzinfo=NEW_YORK) + timedelta(minutes=5 * slot)
    return FeatureBar(
        symbol="SMH",
        start=start,
        end=start + timedelta(minutes=5),
        open=opened,
        high=max(opened, close),
        low=min(opened, close),
        close=close,
        volume=volume,
        bar_vwap=vwap,
        closed=True,
        feed="sip",
    )


def _regime(regime: Regime) -> FeatureSnapshot:
    chosen = route(regime, False)
    return FeatureSnapshot(
        data_asof_utc=_snapshot(12, Decimal(55), Decimal(1000)).data_asof_utc,
        vwap_intact=True,
        features_hash="abc",
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
        block_reason="OK",
        benchmarks_present=13,
        breadth=Decimal("0.6"),
        trend_z=Decimal("1.2"),
        spy_rvol=Decimal(1),
        spy_rms6=Decimal(1),
    )


def _candidate(signal_end: datetime) -> Candidate:
    return Candidate(
        symbol="SMH",
        signal_end=signal_end,
        ny_date=DAY,
        features_hash="abc",
        regime="TREND_UP",
        features=(Decimal(0), Decimal(0), Decimal(0), Decimal(0), Decimal("0.5")),
        rel_vol=Decimal(1),
        sector="XLK",
        limit_price=Decimal("100.00"),
        stop_price=Decimal("99.00"),
        take_profit_price=Decimal("101.50"),
    )


def _minute(index: int, opened: Decimal, high: Decimal, low: Decimal, close: Decimal) -> ClosedBar:
    start = datetime(2024, 11, 1, 10, 30, tzinfo=NEW_YORK) + timedelta(minutes=5 * index)
    return ClosedBar(
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


def _quiet() -> tuple[Decimal, Decimal, Decimal, Decimal]:
    return Decimal("100.30"), Decimal("100.50"), Decimal("99.95"), Decimal("100.30")


def _take_profit_overrides() -> dict[int, tuple[Decimal, Decimal, Decimal, Decimal]]:
    return {
        1: (Decimal("100.20"), Decimal(102), Decimal("99.90"), Decimal("100.40")),
        5: (Decimal("101.20"), Decimal("101.60"), Decimal(101), Decimal("101.40")),
    }


def _tape(overrides: dict[int, tuple[Decimal, Decimal, Decimal, Decimal]]) -> list[ClosedBar]:
    last = max(overrides) if overrides else 0
    count = max(6, last + 1)
    bars: list[ClosedBar] = []
    for index in range(count):
        prices = overrides.get(
            index,
            _quiet()
            if index
            else (Decimal(100), Decimal("100.20"), Decimal("99.95"), Decimal(100)),
        )
        bars.append(_minute(index, *prices))
    return bars


class _Once:
    def __init__(self, intent: TradeIntent) -> None:
        self._intent = intent

    def on_close(self, view: CausalView, bar: ClosedBar) -> TradeIntent | None:
        if view.asof != bar.end:
            raise RuntimeError("clock")
        if bar.end == self._intent.data_asof_utc:
            return self._intent
        return None


_EDGES = (Decimal(1), Decimal(2))
_LOOSE = {
    "prior_feature": Decimal(100),
    "prior_sector": Decimal(100),
    "prior_vol": Decimal(100),
}


def _features(index: int) -> tuple[Decimal, Decimal, Decimal, Decimal, Decimal]:
    values = [Decimal(0), Decimal(0), Decimal(0), Decimal(0), Decimal(0)]
    values[index] = Decimal(1)
    return (values[0], values[1], values[2], values[3], values[4])


def _row(label: int, weight: Decimal) -> Sample:
    return Sample(_features(label if label < 3 else 0), 0, 0, label if label < 3 else 0, weight)


def _row_at(label: int) -> Sample:
    return Sample(_features(label), 0, 0, label, Decimal(1))


def _pair(label: int) -> list[Sample]:
    return [_row_at(label), _row_at(label)]


def _score(
    mean: Decimal,
    se: Decimal,
    factor: Decimal,
    drawdown: Decimal,
    trades: int = 40,
) -> SplitScore:
    return SplitScore(trades, mean, se, factor, drawdown, Decimal(0))
