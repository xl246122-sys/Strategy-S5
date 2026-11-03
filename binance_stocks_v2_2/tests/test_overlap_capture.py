"""Live overlap rules. No network and no credentials."""

import os
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from bstocks_v2.market_ingestor.alpaca_backfill import MinuteBar
from bstocks_v2.market_ingestor.messages import ParsedEvent
from bstocks_v2.research_data.domain_shift import ComparedBar, align, summarize
from bstocks_v2.research_data.overlap_capture import (
    binance_bar,
    in_sip_window,
    is_xnys_session,
    load_env_file,
    next_listen,
    note_kline,
    redact,
    sip_bar,
    write_report,
)


def _event(open_ms: int, *, closed: bool, symbol: str = "spy") -> ParsedEvent:
    return ParsedEvent(
        kind="kline",
        stream=None,
        symbol=symbol,
        event_time_ms=open_ms,
        market_phase=None,
        bar_open_ms=open_ms,
        bar_close_ms=open_ms + 300_000,
        closed=closed,
        open=Decimal("100"),
        high=Decimal("101"),
        low=Decimal("99"),
        close=Decimal("100.5"),
        volume=Decimal("10"),
        vwap=Decimal("100.2"),
        vwap_source="wire",
        quote_time_ms=None,
        quote_time_source=None,
        bid=None,
        ask=None,
        raw_state=None,
    )


def test_next_listen_skips_the_overnight_gap() -> None:
    sessions = {date(2026, 11, 2), date(2026, 11, 3)}
    now = datetime(2026, 11, 3, 1, 57, tzinfo=UTC)
    nxt = next_listen(now, sessions.__contains__)
    assert nxt.isoformat() == "2026-11-03T04:00:00-05:00"
    assert is_xnys_session(date(2026, 11, 3)) is True
    assert is_xnys_session(date(2026, 11, 26)) is False


def test_a_new_bar_closes_the_previous_one() -> None:
    latest: dict[str, ParsedEvent] = {}
    closed: dict[tuple[str, int], ParsedEvent] = {}
    note_kline(latest, closed, _event(0, closed=False))
    note_kline(latest, closed, _event(300_000, closed=False))
    stored = closed[("SPY", 0)]
    assert stored.closed is True
    bar = binance_bar(stored)
    assert bar is not None
    assert bar.symbol == "SPY"
    assert binance_bar(_event(300_000, closed=False)) is None


def test_overnight_is_outside_the_sip_window() -> None:
    sessions = {date(2026, 11, 2)}
    overnight = datetime(2026, 11, 3, 1, 5, tzinfo=UTC)
    premarket = datetime(2026, 11, 2, 9, 0, tzinfo=UTC)
    assert in_sip_window(overnight, sessions.__contains__) is False
    assert in_sip_window(premarket, sessions.__contains__) is True


def test_sip_bucket_uses_the_same_open_time() -> None:
    open_ms = 1_762_000_000_000
    minute = MinuteBar(
        symbol="SPY",
        open_time_ms=open_ms,
        open=Decimal("10"),
        high=Decimal("11"),
        low=Decimal("9"),
        close=Decimal("10.5"),
        volume=Decimal("4"),
        vwap=Decimal("10.25"),
    )
    bar = sip_bar("spy", open_ms, [minute])
    assert bar is not None
    assert bar.start == datetime.fromtimestamp(open_ms / 1000, UTC)
    assert bar.close == Decimal("10.5")


def test_report_stays_aside_until_thirty_bars(tmp_path: Path) -> None:
    start = datetime(2026, 11, 3, 14, 30, tzinfo=UTC)
    left = [ComparedBar("SPY", start, Decimal("100"), Decimal("10"), Decimal("100"))]
    summary = summarize(align(left, []))
    write_report(tmp_path, summary, {"binance_closed": 1, "sip_rows": 0})
    assert (tmp_path / "domain_shift_live.json").exists()
    assert not (tmp_path / "domain_shift.json").exists()


def test_env_loader_does_not_echo_and_redacts_tokens(tmp_path: Path) -> None:
    path = tmp_path / "stage.env"
    path.write_text('export SAMPLE_TOKEN="abc"\n# note\n', encoding="utf-8")
    load_env_file(path)
    assert os.environ["SAMPLE_TOKEN"] == "abc"
    os.environ.pop("SAMPLE_TOKEN", None)
    assert redact("value " + "A" * 26) == "value redacted"
