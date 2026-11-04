"""Thirty-minute books sit beside the frozen five-minute desks."""

from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

from bstocks_v2.console.hub import Hub
from bstocks_v2.feature_store.bars import FeatureBar, make_symbol_snapshot
from bstocks_v2.feature_store.engine import CausalFeatureBook
from bstocks_v2.regime.classifier import FeatureSnapshot, Regime
from bstocks_v2.research_data.calendar import XnysCalendar
from bstocks_v2.strategies.s1.filters import Fit
from bstocks_v2.strategies.s1.signal import decide
from bstocks_v2.tf30.aggregate import Aggregator
from bstocks_v2.tf30.board import Board
from bstocks_v2.tf30.rangebook import ThirtyRange
from bstocks_v2.tf30.spec import ATR_PERIOD, MIN_UPDATES, RSI_PERIOD

DAY = date(2026, 10, 30)
OPEN = datetime(2026, 10, 30, 13, 30, tzinfo=UTC)
ASOF = datetime(2024, 11, 1, 14, 35, tzinfo=UTC)


def _five(start: datetime, close: str, *, feed: str = "sip") -> FeatureBar:
    price = Decimal(close)
    return FeatureBar(
        symbol="SPY",
        start=start,
        end=start + timedelta(minutes=5),
        open=price,
        high=price,
        low=price,
        close=price,
        volume=Decimal("1000"),
        bar_vwap=price,
        closed=True,
        feed=feed,
    )


def _thirty(
    start: datetime,
    close: str,
    *,
    high: str | None = None,
    low: str | None = None,
) -> FeatureBar:
    price = Decimal(close)
    return FeatureBar(
        symbol="SPY",
        start=start,
        end=start + timedelta(minutes=30),
        open=price,
        high=Decimal(high) if high is not None else price,
        low=Decimal(low) if low is not None else price,
        close=price,
        volume=Decimal("6000"),
        bar_vwap=price,
        closed=True,
        feed="sip",
    )


def test_six_five_minute_bars_fold_and_a_gap_does_not() -> None:
    book = Aggregator(XnysCalendar({DAY: time(16, 0)}))
    start = OPEN
    bars = []
    for offset in range(6):
        bars.append(_five(start, str(10 + offset)))
        start += timedelta(minutes=5)
    assert book.push_group(bars[:5]) == []
    folded = book.push_group(bars[5:])
    assert len(folded) == 1
    bar = folded[0]
    assert bar.start == OPEN
    assert bar.end == OPEN + timedelta(minutes=30)
    assert bar.open == Decimal(10)
    assert bar.close == Decimal(15)
    assert bar.volume == Decimal(6000)

    gapped = Aggregator(XnysCalendar({DAY: time(16, 0)}))
    hole = [bars[0], bars[1], bars[3], bars[4], bars[5]]
    assert gapped.push_group(hole) == []


def test_opening_range_locks_once_and_breaks_on_the_next_close() -> None:
    book = ThirtyRange()
    locked = book.observe(_thirty(OPEN, "10", high="11", low="9"))
    assert locked.reason == "LOCK_BAR"
    assert locked.orh == Decimal(11)
    assert locked.first_break is False
    inside = book.observe(_thirty(OPEN + timedelta(minutes=30), "10.50", high="10.80", low="10"))
    assert inside.reason == "INSIDE_RANGE"
    broken = book.observe(_thirty(OPEN + timedelta(minutes=60), "12", high="12", low="10"))
    assert broken.reason == "FIRST_BREAK"
    assert broken.first_break is True
    again = book.observe(_thirty(OPEN + timedelta(minutes=90), "13", high="13", low="11"))
    assert again.reason == "ALREADY_BROKE"
    assert again.first_break is False


def test_thirty_minute_profile_does_not_change_the_five_minute_book() -> None:
    calendar = XnysCalendar({DAY: time(16, 0)})
    five = CausalFeatureBook(calendar)
    first = five.update(_five(OPEN, "10"))
    second = five.update(_five(OPEN + timedelta(minutes=5), "11"))
    assert first is not None and first.vwap_intact is True
    assert second is not None and second.vwap_intact is True

    profile = dict(
        minutes=30,
        rsi_period=RSI_PERIOD,
        atr_period=ATR_PERIOD,
        trend_bars=2,
        slope_lag=1,
        feeds=("sip", "binance"),
    )
    spaced = CausalFeatureBook(calendar, **profile)
    skipped = spaced.update(_five(OPEN, "10", feed="binance"))
    broken = spaced.update(_five(OPEN + timedelta(minutes=5), "11", feed="binance"))
    assert skipped is not None and skipped.vwap_intact is True
    assert broken is not None and broken.vwap_intact is False

    thirty = CausalFeatureBook(calendar, **profile)
    opened = thirty.update(_thirty(OPEN, "10", high="11", low="9"))
    nxt = thirty.update(_thirty(OPEN + timedelta(minutes=30), "12", high="12", low="10"))
    assert opened is not None and opened.atr14 is None
    assert nxt is not None and nxt.vwap_intact is True and nxt.atr14 is not None


def test_s1_warmup_default_stays_at_one_session_of_five_minute_bars() -> None:
    stock = make_symbol_snapshot(
        symbol="SMH",
        data_asof_utc=ASOF,
        bar_start=ASOF - timedelta(minutes=5),
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
    regime = FeatureSnapshot(
        data_asof_utc=ASOF,
        vwap_intact=True,
        features_hash="abc",
        regime=Regime.TREND_UP,
        confidence=Decimal(1),
        entries_allowed=True,
        s1_enabled=True,
        s2_enabled=True,
        s3_enabled=True,
        s4_research_enabled=False,
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
    ready = Fit(Decimal(1), Decimal(0), Decimal(0), Decimal("0.01"), MIN_UPDATES, True)
    short = Fit(Decimal(1), Decimal(0), Decimal(0), Decimal("0.01"), MIN_UPDATES - 1, True)
    _intent, why = decide(
        model="kalman",
        symbol="SMH",
        fit=ready,
        sector_strength=Decimal(1),
        stock=stock,
        regime=regime,
        research_eligible=True,
        min_updates=MIN_UPDATES,
    )
    assert why != "WARMUP"
    _intent, why = decide(
        model="kalman",
        symbol="SMH",
        fit=short,
        sector_strength=Decimal(1),
        stock=stock,
        regime=regime,
        research_eligible=True,
        min_updates=MIN_UPDATES,
    )
    assert why == "WARMUP"
    _intent, why = decide(
        model="kalman",
        symbol="SMH",
        fit=ready,
        sector_strength=Decimal(1),
        stock=stock,
        regime=regime,
        research_eligible=True,
    )
    assert why == "WARMUP"


def test_hub_keeps_the_thirty_minute_books_in_their_own_files(tmp_path: Path) -> None:
    hub = Hub(tmp_path)
    try:
        state = hub.state()
        desks = state["desks"]
        m30 = state["m30"]
        assert isinstance(desks, list) and len(desks) == 5
        assert isinstance(m30, list)
        assert [row["strategy"] for row in m30] == ["S1", "S2", "S3", "S4", "S5"]
        assert all(row["frame"] == "30m" for row in m30)
        assert hub.desks[0].paper.path == tmp_path / "S1.sqlite"
        assert hub.m30.papers["S1"] is hub.desks[0].paper
        assert hub.desks[0].paper.cash == Decimal("4000")
        board = Board({desk.strategy_id: desk.paper for desk in hub.desks}, hub.calendar)
        try:
            start = OPEN
            bars = [_five(start + timedelta(minutes=5 * step), "10") for step in range(6)]
            board.on_group(bars)
            snap = board.features.latest["SPY"]
            assert snap.bar_start == OPEN
            assert snap.data_asof_utc == OPEN + timedelta(minutes=30)
        finally:
            board.close()
    finally:
        hub.close()
