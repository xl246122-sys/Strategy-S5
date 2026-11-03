"""Stage 5: causal features, stream-batch parity, and the regime entry gate."""

from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from bstocks_v2.constitution import live_trade_enabled
from bstocks_v2.feature_store.bars import FeatureBar, SymbolSnapshot, make_symbol_snapshot
from bstocks_v2.feature_store.batch import batch_snapshots
from bstocks_v2.feature_store.engine import CausalFeatureBook
from bstocks_v2.feature_store.errors import LookaheadError
from bstocks_v2.feature_store.recover import bar_to_stored, recover_book
from bstocks_v2.market_ingestor.wal import IntradayBarStore
from bstocks_v2.regime.classifier import (
    BENCHMARKS,
    CONFIDENCE_MIN,
    FeatureSnapshot,
    MarketRegimeClassifier,
    Regime,
    RegimeObservation,
)
from bstocks_v2.research_data.calendar import NEW_YORK, XnysCalendar

ROOT = Path(__file__).resolve().parents[1]
TOLERANCE = Decimal("1e-9")
FIELDS = (
    "vwap",
    "rsi6",
    "atr14",
    "rvol_mean",
    "rvol_median",
    "ret",
    "sigma_tod",
    "ret_norm",
    "vwap_slope",
    "trend_z",
    "rms6",
    "rel_vol",
)


def test_unclosed_and_backwards_bars_are_rejected() -> None:
    calendar = _calendar([date(2024, 11, 1)])
    book = CausalFeatureBook(calendar)
    book.update(_bar(date(2024, 11, 1), 1, Decimal(10), Decimal(10)))
    with pytest.raises(LookaheadError):
        book.update(_bar(date(2024, 11, 1), 2, Decimal(10), Decimal(10), closed=False))
    with pytest.raises(LookaheadError):
        book.update(_bar(date(2024, 11, 1), 0, Decimal(10), Decimal(10)))


def test_duplicate_bar_is_idempotent_and_a_disagreement_is_rejected() -> None:
    calendar = _calendar([date(2024, 11, 1)])
    book = CausalFeatureBook(calendar)
    bar = _bar(date(2024, 11, 1), 0, Decimal(10), Decimal(11))
    first = book.update(bar)
    again = book.update(bar)
    assert first is not None and again is not None
    assert first.features_hash == again.features_hash
    assert first.vwap == again.vwap
    changed = _bar(date(2024, 11, 1), 0, Decimal(10), Decimal(12))
    with pytest.raises(ValueError, match="duplicate"):
        book.update(changed)


def test_extended_bar_is_ignored_and_a_half_day_stops_at_the_close() -> None:
    day = date(2024, 11, 29)
    book = CausalFeatureBook(XnysCalendar({day: time(13, 0)}))
    assert book.update(_at(day, time(8, 0), Decimal(10), Decimal(10))) is None
    kept = book.update(_at(day, time(9, 30), Decimal(10), Decimal(11)))
    assert kept is not None and kept.vwap_intact is True
    assert book.update(_at(day, time(13, 0), Decimal(11), Decimal(12))) is None
    assert book.latest["SPY"].vwap == kept.vwap


def test_wilder_rsi_and_session_vwap_match_the_closed_form() -> None:
    day = date(2024, 11, 1)
    book = CausalFeatureBook(_calendar([day]))
    closes = [Decimal(value) for value in (10, 11, 12, 11, 10, 9, 10)]
    price = closes[0]
    snapshot = None
    for index, close in enumerate(closes):
        snapshot = book.update(_bar(day, index, price, close, volume=Decimal(1)))
        price = close
    assert snapshot is not None
    assert snapshot.rsi6 == Decimal(50)
    assert snapshot.vwap == Decimal(73) / Decimal(7)
    assert snapshot.vwap_intact is True
    assert snapshot.atr14 is None


def test_a_later_gap_does_not_rewrite_the_earlier_bar() -> None:
    day = date(2024, 11, 1)
    calendar = _calendar([day])
    early = _bar(day, 0, Decimal(10), Decimal(10))
    jumped = _bar(day, 2, Decimal(10), Decimal(11))
    book = CausalFeatureBook(calendar)
    first = book.update(early)
    assert first is not None and first.vwap_intact is True
    broken = book.update(jumped)
    assert broken is not None and broken.vwap_intact is False
    assert first.vwap_intact is True
    alone = CausalFeatureBook(calendar).update(early)
    assert alone is not None
    assert alone.features_hash == first.features_hash


def test_time_of_day_sigma_does_not_include_the_current_session() -> None:
    days = [date(2024, 11, 1) + timedelta(days=offset) for offset in range(3)]
    returns = [Decimal("0.01"), Decimal("0.03"), Decimal("0.09")]
    book = CausalFeatureBook(_calendar(days))
    last = None
    price = Decimal(100)
    for day, ret in zip(days, returns, strict=True):
        close = price * (Decimal(1) + ret)
        last = book.update(_bar(day, 0, price, close))
        price = close
    assert last is not None and last.sigma_tod is not None
    expected = _stdev([Decimal("0.01"), Decimal("0.03")])
    assert last.sigma_tod == expected
    assert last.baseline_days == 2
    changed = CausalFeatureBook(_calendar(days))
    price = Decimal(100)
    for day, ret in zip(days, [Decimal("0.01"), Decimal("0.05"), Decimal("0.09")], strict=True):
        close = price * (Decimal(1) + ret)
        changed_last = changed.update(_bar(day, 0, price, close))
        price = close
    assert changed_last is not None and changed_last.sigma_tod == _stdev(
        [Decimal("0.01"), Decimal("0.05")]
    )


def test_stream_batch_and_wal_recovery_match(tmp_path: Path) -> None:
    days = [date(2024, 11, 1) + timedelta(days=offset) for offset in range(21)]
    calendar = _calendar(days)
    bars = _path("SPY", days, 8) + _path("QQQ", days, 8)
    bars.sort(key=lambda item: (item.start, item.symbol))
    streamed = _stream(bars, calendar)
    crashed = _stream_with_crash(bars, calendar, tmp_path, crash_at=80)
    assert len(streamed) == len(crashed) == len(bars)
    worst = Decimal(0)
    for left, right in zip(streamed, crashed, strict=True):
        worst = max(worst, _gap(left, right))
    for symbol in ("SPY", "QQQ"):
        selected = [bar for bar in bars if bar.symbol == symbol]
        batched = batch_snapshots(selected, calendar)
        seen = [item for item in streamed if item.symbol == symbol]
        assert len(batched) == len(seen)
        for left, right in zip(batched, seen, strict=True):
            worst = max(worst, _gap(left, right))
    assert worst < TOLERANCE


def test_a_future_close_does_not_change_the_past_snapshot() -> None:
    day = date(2024, 11, 1)
    calendar = _calendar([day])
    bars = [_bar(day, index, Decimal(10), Decimal(10 + index)) for index in range(4)]
    prefix = _stream(bars[:-1], calendar)
    changed = bars[:-1] + [_bar(day, 3, Decimal(13), Decimal(20))]
    longer = _stream(changed, calendar)
    assert prefix[-1].features_hash == longer[-2].features_hash
    assert longer[-1].close == Decimal(20)


def test_hysteresis_does_not_flip_on_every_bar_and_panic_cuts_through_it() -> None:
    moments = [_moment(index) for index in range(8)]
    flicker = [
        _obs(moments[0], trend_z=Decimal("1.2")),
        _obs(moments[1], trend_z=Decimal("0.90")),
        _obs(moments[2], trend_z=Decimal("1.1")),
        _obs(moments[3], trend_z=Decimal("0.80")),
        _obs(moments[4], trend_z=Decimal("1.3")),
    ]
    held = MarketRegimeClassifier().replay(flicker)
    assert [item.regime for item in held] == [Regime.TREND_UP] * 5
    assert all(item.entries_allowed and item.s1_enabled and item.s2_enabled for item in held)
    panic = _obs(
        moments[5],
        trend_z=Decimal("-0.8"),
        vwap_slope=Decimal("-0.2"),
        spy_above_vwap=False,
        qqq_above_vwap=False,
        breadth=Decimal("0.30"),
        rvol_mean=Decimal("2.4"),
        rms6=Decimal("1"),
    )
    cut = MarketRegimeClassifier().replay([*flicker, panic])
    assert cut[-1].regime == Regime.HIGH_VOL_PANIC
    assert cut[-1].entries_allowed is False
    assert cut[-1].s1_enabled is False
    assert cut[-2].regime == Regime.TREND_UP


def test_data_failures_and_low_confidence_block_every_new_long() -> None:
    moment = _moment(0)
    cases = [
        _obs(moment, benchmarks_complete=False, benchmarks_present=12),
        _obs(moment, fresh=False),
        _obs(moment, vwap_intact=False),
        _obs(moment, warmed=False),
        _obs(moment, trend_z=Decimal("0.80")),
    ]
    reasons = [
        "MISSING_BENCHMARK",
        "STALE",
        "VWAP_NOT_INTACT",
        "NOT_WARMED",
        "AMBIGUOUS",
    ]
    for observation, reason in zip(cases, reasons, strict=True):
        decision = MarketRegimeClassifier().step(observation)
        assert decision.regime == Regime.UNKNOWN
        assert decision.block_reason == reason
        assert decision.confidence < CONFIDENCE_MIN
        assert decision.entries_allowed is False
        assert decision.s1_enabled is False
        assert decision.s2_enabled is False
        assert decision.s3_enabled is False
        assert decision.s4_research_enabled is False
    soft_tape = _obs(
        moment,
        trend_z=Decimal("-0.8"),
        vwap_slope=Decimal("-0.1"),
        spy_above_vwap=False,
        qqq_above_vwap=False,
        breadth=Decimal("0.10"),
        rvol_mean=Decimal("1"),
        rms6=Decimal("0.4"),
    )
    soft = MarketRegimeClassifier().step(soft_tape)
    assert soft.regime == Regime.UNKNOWN
    assert soft.block_reason == "AMBIGUOUS"
    assert soft.entries_allowed is False


def test_route_matrix_and_the_opening_window() -> None:
    decisions = [
        MarketRegimeClassifier().step(item)
        for item in (
            _obs(_moment(0), trend_z=Decimal("1.2"), in_opening_window=True),
            _obs(_moment(1), trend_z=Decimal("1.2"), in_opening_window=False),
            _obs(_moment(2), trend_z=Decimal("0.1"), in_opening_window=True),
            _obs(_moment(3), trend_z=Decimal("0.1"), in_opening_window=False),
            _obs(
                _moment(4),
                trend_z=Decimal("-1.2"),
                vwap_slope=Decimal("-0.3"),
                spy_above_vwap=False,
                qqq_above_vwap=False,
                breadth=Decimal("0.36"),
                in_opening_window=True,
            ),
        )
    ]
    assert (decisions[0].s1_enabled, decisions[0].s3_enabled, decisions[0].s4_research_enabled) == (
        True,
        True,
        False,
    )
    assert (decisions[1].s1_enabled, decisions[1].s3_enabled) == (True, False)
    assert decisions[2].regime == Regime.SIDEWAYS
    assert (decisions[2].s3_enabled, decisions[2].s4_research_enabled, decisions[2].s1_enabled) == (
        True,
        True,
        False,
    )
    assert decisions[3].s3_enabled is False and decisions[3].s4_research_enabled is True
    assert decisions[4].regime == Regime.TREND_DOWN
    assert decisions[4].entries_allowed is True
    assert decisions[4].s1_enabled is False
    assert decisions[4].s3_enabled is False


def test_illegal_snapshot_and_a_future_observation_cannot_rewrite_history() -> None:
    with pytest.raises(ValidationError):
        _manual(Regime.UNKNOWN, confidence=Decimal("0.9"), entries=False)
    with pytest.raises(ValidationError):
        _manual(Regime.TREND_UP, confidence=Decimal(1), entries=True, s1=False)
    first = _obs(_moment(0), trend_z=Decimal("1.2"))
    second = _obs(_moment(1), trend_z=Decimal("1.2"))
    base = MarketRegimeClassifier().replay([first, second])
    mutated = _obs(
        _moment(1),
        trend_z=Decimal("-0.8"),
        vwap_slope=Decimal("-0.2"),
        spy_above_vwap=False,
        qqq_above_vwap=False,
        breadth=Decimal("0.30"),
        rvol_mean=Decimal("2.4"),
    )
    alt = MarketRegimeClassifier().replay([first, mutated])
    assert alt[0] == base[0]
    assert alt[1].regime == Regime.HIGH_VOL_PANIC
    assert base[1].regime == Regime.TREND_UP


def test_aligned_benchmarks_go_stale_five_minutes_after_the_close() -> None:
    moment = datetime(2024, 11, 1, 14, 35, tzinfo=UTC)
    start = datetime(2024, 11, 1, 9, 30, tzinfo=NEW_YORK)
    latest = {
        symbol: _snapshot(symbol, start, moment, warmed=True, close=Decimal(101), vwap=Decimal(100))
        for symbol in BENCHMARKS
    }
    latest["SPY"] = _snapshot(
        "SPY",
        start,
        moment,
        warmed=True,
        close=Decimal(101),
        vwap=Decimal(100),
        trend_z=Decimal("1.2"),
        slope=Decimal("0.2"),
        rvol=Decimal("1"),
        rms=Decimal("0.4"),
    )
    classifier = MarketRegimeClassifier()
    fresh = classifier.decide(latest, moment)
    assert fresh.regime == Regime.TREND_UP
    assert fresh.entries_allowed is True
    stale = classifier.decide(latest, moment + timedelta(minutes=5))
    assert stale.regime == Regime.UNKNOWN
    assert stale.block_reason == "STALE"
    assert stale.entries_allowed is False
    broken = dict(latest)
    broken["XLRE"] = _snapshot(
        "XLRE", start, moment, warmed=True, close=Decimal(101), vwap=Decimal(100), intact=False
    )
    blocked = MarketRegimeClassifier().decide(broken, moment)
    assert blocked.block_reason == "VWAP_NOT_INTACT"
    assert blocked.entries_allowed is False
    with pytest.raises(LookaheadError):
        MarketRegimeClassifier().decide(latest, moment - timedelta(minutes=1))


def test_thirteen_etfs_reach_sideways_only_after_the_feature_window() -> None:
    days = [date(2024, 11, 1) + timedelta(days=offset) for offset in range(21)]
    calendar = _calendar(days)
    bars: list[FeatureBar] = []
    for symbol in BENCHMARKS:
        bars.extend(_flat_ready(symbol, days))
    bars.sort(key=lambda item: (item.start, item.symbol == "SPY", item.symbol))
    book = CausalFeatureBook(calendar)
    classifier = MarketRegimeClassifier()
    last_day = days[-1]
    seen: list[FeatureSnapshot] = []
    for bar in bars:
        snapshot = book.update(bar)
        if bar.symbol != "SPY" or snapshot is None:
            continue
        if snapshot.bar_start.astimezone(NEW_YORK).date() != last_day:
            continue
        seen.append(classifier.decide(book.latest, snapshot.data_asof_utc))
    assert [item.block_reason for item in seen[:5]] == ["NOT_WARMED"] * 5
    assert all(item.entries_allowed is False for item in seen[:5])
    assert seen[5].regime == Regime.SIDEWAYS
    assert seen[5].in_opening_window is True
    assert seen[5].s3_enabled is True
    assert seen[5].s4_research_enabled is True
    assert seen[5].s1_enabled is False
    assert seen[6].regime == Regime.SIDEWAYS
    assert seen[6].in_opening_window is False
    assert seen[6].s3_enabled is False
    assert seen[6].entries_allowed is True


def test_feature_snapshot_is_frozen_and_live_trading_stays_off() -> None:
    decision = MarketRegimeClassifier().step(_obs(_moment(0), benchmarks_complete=False))
    with pytest.raises(ValidationError):
        decision.regime = Regime.TREND_UP  # type: ignore[misc]
    assert live_trade_enabled() is False
    for folder in ("feature_store", "regime"):
        for path in (ROOT / "src" / "bstocks_v2" / folder).glob("*.py"):
            assert "/order/place" not in path.read_text(encoding="utf-8")


def _stream(bars: list[FeatureBar], calendar: XnysCalendar) -> list[SymbolSnapshot]:
    book = CausalFeatureBook(calendar)
    snapshots: list[SymbolSnapshot] = []
    for bar in bars:
        snapshot = book.update(bar)
        if snapshot is None:
            raise AssertionError("expected a closed RTH snapshot")
        snapshots.append(snapshot)
    return snapshots


def _stream_with_crash(
    bars: list[FeatureBar],
    calendar: XnysCalendar,
    folder: Path,
    crash_at: int,
) -> list[SymbolSnapshot]:
    store = IntradayBarStore(str(folder / "bars.sqlite"))
    book = CausalFeatureBook(calendar)
    snapshots: list[SymbolSnapshot] = []
    try:
        for index, bar in enumerate(bars):
            if index == crash_at:
                book = recover_book(store, calendar)
            store.upsert_bar(bar_to_stored(bar))
            snapshot = book.update(bar)
            if snapshot is None:
                raise AssertionError("expected a closed RTH snapshot")
            snapshots.append(snapshot)
    finally:
        store.close()
    return snapshots


def _gap(left: SymbolSnapshot, right: SymbolSnapshot) -> Decimal:
    assert left.vwap_intact == right.vwap_intact
    assert left.baseline_days == right.baseline_days
    assert left.above_vwap == right.above_vwap
    assert left.warmed == right.warmed
    assert left.features_hash == right.features_hash
    worst = Decimal(0)
    for name in FIELDS:
        first = getattr(left, name)
        second = getattr(right, name)
        if first is None or second is None:
            assert first is None and second is None
            continue
        if not isinstance(first, Decimal) or not isinstance(second, Decimal):
            raise AssertionError(name)
        worst = max(worst, abs(first - second))
    assert worst < TOLERANCE
    return worst


def _flat_ready(symbol: str, days: list[date]) -> list[FeatureBar]:
    bars: list[FeatureBar] = []
    price = Decimal(100)
    for index, day in enumerate(days):
        if index == len(days) - 1:
            ret = Decimal(0)
        else:
            ret = (Decimal(index % 5) - Decimal(2)) * Decimal("0.001")
        for slot in range(8):
            close = price * (Decimal(1) + ret)
            bars.append(_bar(day, slot, price, close, symbol=symbol))
            price = close
    return bars


def _path(symbol: str, days: list[date], slots: int) -> list[FeatureBar]:
    bars: list[FeatureBar] = []
    price = Decimal("50")
    for index, day in enumerate(days):
        ret = (Decimal((index * 3 + 1) % 7) - Decimal(3)) * Decimal("0.002")
        for slot in range(slots):
            close = price * (Decimal(1) + ret + Decimal(slot) * Decimal("0.0001"))
            bars.append(_bar(day, slot, price, close, symbol=symbol, volume=Decimal(1000 + slot)))
            price = close
    return bars


def _obs(moment: datetime, **overrides: object) -> RegimeObservation:
    payload: dict[str, object] = {
        "data_asof_utc": moment,
        "fresh": True,
        "benchmarks_complete": True,
        "vwap_intact": True,
        "warmed": True,
        "trend_z": Decimal("0"),
        "vwap_slope": Decimal("0.2"),
        "spy_above_vwap": True,
        "qqq_above_vwap": True,
        "breadth": Decimal("0.70"),
        "rvol_mean": Decimal("1"),
        "rms6": Decimal("0.4"),
        "in_opening_window": False,
        "benchmarks_present": len(BENCHMARKS),
        "symbol_hashes": (("SPY", "abc"),),
    }
    payload.update(overrides)
    return RegimeObservation(**payload)  # type: ignore[arg-type]


def _manual(
    regime: Regime,
    *,
    confidence: Decimal,
    entries: bool,
    s1: bool = False,
) -> FeatureSnapshot:
    return FeatureSnapshot(
        data_asof_utc=_moment(0),
        vwap_intact=True,
        features_hash="0" * 64,
        regime=regime,
        confidence=confidence,
        entries_allowed=entries,
        s1_enabled=s1,
        s2_enabled=s1,
        s3_enabled=False,
        s4_research_enabled=False,
        in_opening_window=False,
        data_ready=True,
        dwell_bars=1,
        block_reason="TEST",
        benchmarks_present=13,
        breadth=None,
        trend_z=None,
        spy_rvol=None,
        spy_rms6=None,
    )


def _snapshot(
    symbol: str,
    start: datetime,
    end: datetime,
    *,
    warmed: bool,
    close: Decimal,
    vwap: Decimal,
    intact: bool = True,
    trend_z: Decimal | None = None,
    slope: Decimal | None = None,
    rvol: Decimal | None = None,
    rms: Decimal | None = None,
) -> SymbolSnapshot:
    return make_symbol_snapshot(
        symbol=symbol,
        data_asof_utc=end,
        bar_start=start,
        ny_date="2024-11-01",
        bucket="09:30",
        vwap_intact=intact,
        close=close,
        vwap=vwap,
        rsi6=Decimal(50),
        atr14=Decimal("1"),
        rvol_mean=rvol,
        rvol_median=rvol,
        ret=Decimal(0),
        sigma_tod=Decimal("0.01"),
        ret_norm=Decimal(0),
        vwap_slope=slope,
        trend_z=trend_z,
        rms6=rms,
        rel_vol=Decimal(1),
        baseline_days=20,
        above_vwap=close > vwap,
        warmed=warmed,
    )


def _bar(
    day: date,
    slot: int,
    open_: Decimal,
    close: Decimal,
    *,
    symbol: str = "SPY",
    volume: Decimal = Decimal(1000),
    closed: bool = True,
) -> FeatureBar:
    start = datetime.combine(day, time(9, 30), tzinfo=NEW_YORK) + timedelta(minutes=5 * slot)
    high = max(open_, close) + Decimal("0.5")
    low = min(open_, close) - Decimal("0.5")
    return FeatureBar(
        symbol=symbol,
        start=start,
        end=start + timedelta(minutes=5),
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=volume,
        bar_vwap=close,
        closed=closed,
    )


def _at(day: date, clock: time, open_: Decimal, close: Decimal) -> FeatureBar:
    start = datetime.combine(day, clock, tzinfo=NEW_YORK)
    high = max(open_, close) + Decimal("0.5")
    low = min(open_, close) - Decimal("0.5")
    return FeatureBar(
        symbol="SPY",
        start=start,
        end=start + timedelta(minutes=5),
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=Decimal(1000),
        bar_vwap=close,
    )


def _calendar(days: list[date]) -> XnysCalendar:
    return XnysCalendar({day: time(16, 0) for day in days})


def _moment(index: int) -> datetime:
    return datetime(2024, 11, 1, 15, index, tzinfo=UTC)


def _stdev(values: list[Decimal]) -> Decimal:
    center = sum(values, start=Decimal(0)) / Decimal(len(values))
    variance = sum((item - center) ** 2 for item in values) / Decimal(len(values) - 1)
    return variance.sqrt()
