"""Desk clock, shared 4000 USDC, and next-session swing plans."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from bstocks_v2.console.calendar import LiveCalendar
from bstocks_v2.console.capital import DESK_EQUITY
from bstocks_v2.console.hub import Hub
from bstocks_v2.console.swing import DailyBar, SwingBook, _signals
from bstocks_v2.constitution.guard import desk_entry_open, entry_window_open
from bstocks_v2.feature_store.bars import FeatureBar


def _day(
    index: int,
    close: str,
    *,
    high: str | None = None,
    low: str | None = None,
    volume: str = "1000",
) -> DailyBar:
    price = Decimal(close)
    return DailyBar(
        symbol="SMH",
        day=date(2026, 1, 2) + timedelta(days=index),
        open=price,
        high=Decimal(high) if high is not None else price,
        low=Decimal(low) if low is not None else price,
        close=price,
        volume=Decimal(volume),
        complete=True,
    )


def test_research_window_stays_inside_the_cash_session() -> None:
    premarket = datetime(2026, 11, 4, 10, 0, tzinfo=UTC)
    regular = datetime(2026, 11, 4, 15, 0, tzinfo=UTC)
    post = datetime(2026, 11, 4, 22, 0, tzinfo=UTC)
    night = datetime(2026, 11, 5, 2, 0, tzinfo=UTC)
    assert entry_window_open(premarket, False) is False
    assert desk_entry_open(premarket, False) is True
    assert desk_entry_open(regular, False) is True
    assert desk_entry_open(post, False) is True
    assert desk_entry_open(night, False) is False
    assert entry_window_open(regular, False) is True


def test_each_desk_starts_at_four_thousand_and_shares_the_thirty_minute_book() -> None:
    hub = Hub(None)
    try:
        assert all(desk.paper.cash == DESK_EQUITY for desk in hub.desks)
        assert hub.m30.papers["S1"] is hub.desks[0].paper
        assert hub.state()["capital_usdc"] == "4000"
        assert hub.state()["live_orders"] is False
    finally:
        hub.close()


def test_swing_plans_wait_for_the_next_session_and_history_does_not_arm() -> None:
    book = SwingBook(LiveCalendar())
    rows = [_day(index, "100") for index in range(19)]
    rows.append(_day(19, "90", low="90", high="100"))
    spy = [
        DailyBar("SPY", item.day, item.open, item.high, item.low, item.close, item.volume, True)
        for item in rows
    ]
    found = _signals("SMH", rows, spy)
    assert "S5" in found
    book.seed(rows)
    assert book.due("SMH", rows[-1].day) == []
    order = found["S5"]
    book._plans[("S5", "SMH")] = order
    assert book.due("SMH", order.signal_day) == []


def test_five_minute_gap_does_not_become_a_daily_bar() -> None:
    book = SwingBook(LiveCalendar())
    start = datetime(2026, 10, 30, 13, 30, tzinfo=UTC)
    first = FeatureBar(
        symbol="SPY",
        start=start,
        end=start + timedelta(minutes=5),
        open=Decimal("100"),
        high=Decimal("101"),
        low=Decimal("99"),
        close=Decimal("100"),
        volume=Decimal("10"),
        bar_vwap=Decimal("100"),
        closed=True,
        feed="sip",
    )
    skipped = FeatureBar(
        symbol="SPY",
        start=start + timedelta(minutes=15),
        end=start + timedelta(minutes=20),
        open=Decimal("100"),
        high=Decimal("101"),
        low=Decimal("99"),
        close=Decimal("100"),
        volume=Decimal("10"),
        bar_vwap=Decimal("100"),
        closed=True,
        feed="sip",
    )
    assert book.observe(first, arm=True) is None
    assert book.observe(skipped, arm=True) is None
    assert book._history.get("SPY", []) == []
