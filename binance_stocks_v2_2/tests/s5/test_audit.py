from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from bstocks_v2.market_ingestor.messages import ParsedEvent
from bstocks_v2.market_ingestor.shards import PoolLimitError
from bstocks_v2.research_data.s5_audit import summarize_table, write_audit
from bstocks_v2.research_data.s5_recorder import ShadowRecorder, focus_quote_streams
from bstocks_v2.sessions.calendar import SessionLabel, TradingCalendar

CALENDAR = TradingCalendar.load()


def _event(**overrides: object) -> ParsedEvent:
    fields: dict[str, object] = {
        "kind": "kline",
        "stream": "AAPL@kline_5m",
        "symbol": "AAPL",
        "event_time_ms": 1_730_462_400_000,
        "market_phase": None,
        "bar_open_ms": 1_730_462_400_000,
        "bar_close_ms": 1_730_462_700_000,
        "closed": True,
        "open": Decimal("100"),
        "high": Decimal("101"),
        "low": Decimal("99"),
        "close": Decimal("100.5"),
        "volume": Decimal("10"),
        "vwap": Decimal("100.2"),
        "vwap_source": "wire",
        "quote_time_ms": None,
        "quote_time_source": None,
        "bid": None,
        "ask": None,
        "raw_state": None,
    }
    fields.update(overrides)
    return ParsedEvent(**fields)  # type: ignore[arg-type]


def _table() -> pa.Table:
    # 10:00 EDT, 10:00 EST, 09:00 EDT, 17:00 EDT, 21:00 EDT with a stored post label.
    instants = [
        datetime(2024, 11, 1, 14, 0, tzinfo=UTC),
        datetime(2024, 11, 4, 15, 0, tzinfo=UTC),
        datetime(2024, 11, 1, 13, 0, tzinfo=UTC),
        datetime(2024, 11, 1, 21, 0, tzinfo=UTC),
        datetime(2024, 11, 2, 1, 0, tzinfo=UTC),
    ]
    return pa.table(
        {
            "t": pa.array(instants, type=pa.timestamp("us", tz="UTC")),
            "session": ["rth", "rth", "pre", "post", "post"],
            "v": [10, 10, 0, 4, 0],
            "feed": ["sip", "sip", "sip", "sip", "iex"],
            "adjustment": ["raw", "raw", "raw", "raw", "raw"],
        }
    )


def test_summary_splits_post_from_overnight_and_keeps_dst() -> None:
    summary = summarize_table(_table())
    assert summary.clock["RTH_CLOCK"] == 2
    assert summary.clock["PRE"] == 1
    assert summary.clock["POST"] == 1
    assert summary.clock["OVERNIGHT"] == 1
    assert summary.clock_positive["OVERNIGHT"] == 0
    assert summary.zero_volume == 2
    assert summary.feed == {"sip": 4, "iex": 1}
    assert summary.stored["post"] == 2
    assert "bid" not in summary.columns


def test_dst_boundaries_stay_on_the_exchange_calendar() -> None:
    assert CALENDAR.label(datetime(2024, 11, 1, 14, 0, tzinfo=UTC)) is SessionLabel.RTH
    assert CALENDAR.label(datetime(2024, 11, 4, 15, 0, tzinfo=UTC)) is SessionLabel.RTH
    assert CALENDAR.label(datetime(2025, 3, 7, 14, 30, tzinfo=UTC)) is SessionLabel.RTH
    assert CALENDAR.label(datetime(2025, 3, 10, 13, 30, tzinfo=UTC)) is SessionLabel.RTH
    assert CALENDAR.label(datetime(2024, 11, 2, 1, 0, tzinfo=UTC)) is SessionLabel.OVERNIGHT


def test_audit_refuses_to_write_into_the_sip_store(tmp_path: Path) -> None:
    source = tmp_path / "sip"
    folder = source / "parquet" / "5Min" / "AAPL"
    folder.mkdir(parents=True)
    pq.write_table(_table(), folder / "AAPL_5Min_2024-11.parquet")
    with pytest.raises(ValueError, match="source SIP"):
        write_audit(source, source / "s5", ("5Min",))
    manifest = write_audit(source, tmp_path / "out", ("5Min",))
    text = manifest.read_text(encoding="utf-8")
    assert "prospective_forward" in text
    assert "NO_SIP_REFERENCE" in text
    assert '"source_opened_for_write": false' in text


def test_shadow_recorder_keeps_rejections_and_does_not_connect(tmp_path: Path) -> None:
    source = tmp_path / "sip"
    source.mkdir()
    recorder = ShadowRecorder(tmp_path / "s5" / "shadow.sqlite", source, CALENDAR)
    capture = datetime(2024, 11, 1, 14, 5, tzinfo=UTC)
    decision = datetime(2024, 11, 1, 14, 5, tzinfo=UTC)
    opened = int(datetime(2024, 11, 1, 14, 0, tzinfo=UTC).timestamp() * 1000)
    first = recorder.record(
        _event(bar_open_ms=opened, event_time_ms=opened),
        capture_ts=capture,
        decision_ts=decision,
        feed="binance_equity",
    )
    assert first.quality == "OK"
    assert first.session is SessionLabel.RTH
    again = recorder.record(
        _event(bar_open_ms=opened, event_time_ms=opened),
        capture_ts=capture,
        decision_ts=decision,
        feed="binance_equity",
    )
    assert again.stored is False
    assert again.quality == "DUPLICATE"
    empty = recorder.record(
        _event(symbol="MSFT", bar_open_ms=opened, event_time_ms=opened, volume=Decimal("0")),
        capture_ts=capture,
        decision_ts=decision,
        feed="binance_equity",
    )
    assert empty.quality == "EMPTY_PRINT"
    quote = recorder.record(
        _event(
            kind="quote",
            symbol="NVDA",
            bar_open_ms=None,
            quote_time_ms=opened,
            event_time_ms=opened,
            bid=None,
            ask=None,
            volume=None,
        ),
        capture_ts=capture,
        decision_ts=decision,
        feed="binance_equity",
    )
    assert quote.quality == "NO_QUOTE"
    holiday_ms = int(datetime(2025, 1, 9, 15, 0, tzinfo=UTC).timestamp() * 1000)
    closed = recorder.record(
        _event(symbol="SPY", bar_open_ms=holiday_ms, event_time_ms=holiday_ms),
        capture_ts=datetime(2025, 1, 9, 15, 5, tzinfo=UTC),
        decision_ts=datetime(2025, 1, 9, 15, 5, tzinfo=UTC),
        feed="binance_equity",
    )
    assert closed.quality == "CLOSED"
    later = opened + 20 * 60 * 1000
    gap = recorder.record(
        _event(bar_open_ms=later, event_time_ms=later),
        capture_ts=capture + timedelta(minutes=20),
        decision_ts=decision + timedelta(minutes=20),
        feed="binance_equity",
    )
    assert gap.quality == "GAP"
    leaked = recorder.record(
        _event(symbol="QQQ", bar_open_ms=opened, event_time_ms=opened),
        capture_ts=capture,
        decision_ts=capture - timedelta(minutes=10),
        feed="binance_equity",
    )
    assert leaked.quality == "LEAKED_FUTURE"
    with pytest.raises(PoolLimitError):
        focus_quote_streams(tuple(f"S{index:02d}" for index in range(21)))
    streams = focus_quote_streams(("AAPL", "MSFT"))
    assert streams == ("AAPL@quote", "MSFT@quote")
    module = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "bstocks_v2"
        / "research_data"
        / "s5_recorder.py"
    )
    text = module.read_text(encoding="utf-8")
    assert "websockets" not in text
    assert "/order/place" not in text
    recorder.close()
