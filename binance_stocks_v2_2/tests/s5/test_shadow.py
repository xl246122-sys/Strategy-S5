import sqlite3
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from bstocks_v2.contracts.errors import LiveTradingDisabledError
from bstocks_v2.research_backtest.tape import ClosedBar
from bstocks_v2.research_data.calendar import NEW_YORK
from bstocks_v2.research_data.s5_shadow import (
    ShadowTracker,
    assert_shadow_target,
    decide_launch,
    write_bundle,
)
from bstocks_v2.sessions.calendar import TradingCalendar
from bstocks_v2.strategies.s5.observe import (
    LIVE_SESSION_FLOOR,
    ShadowTape,
    estimate_interval,
    freeze_payload,
    observe,
    parameter_freeze,
)
from bstocks_v2.strategies.s5.spec import MIN_RESOLVED

CALENDAR = TradingCalendar.load()
FREEZE = parameter_freeze()


def test_freeze_keeps_the_registered_numbers() -> None:
    payload = freeze_payload()
    assert payload["b0"] == "-0.03"
    assert payload["b1"] == "-2"
    assert payload["target"] == "0.01"
    assert payload["stop"] == "0.015"
    assert payload["ttl_seconds"] == 300
    assert parameter_freeze() == FREEZE


def test_a_missing_or_wide_quote_is_not_an_estimated_fill() -> None:
    missing = observe(_tape(bid=None, ask=None), CALENDAR, freeze=FREEZE)
    wide = observe(_tape(bid=Decimal("100"), ask=Decimal("102")), CALENDAR, freeze=FREEZE)
    assert missing.reason == "SPREAD_UNKNOWN"
    assert wide.reason == "SPREAD_TOO_WIDE"
    assert missing.fill_rate_kind == "unobserved"
    assert wide.fill_rate_kind == "unobserved"
    assert missing.books_profit is False
    assert missing.exchange_fill_id is None
    assert "pnl" not in missing.as_dict()


def test_a_stale_quote_and_a_hidden_bar_do_not_fill() -> None:
    decision = _at(date(2024, 11, 1), 15, 40)
    stale = observe(
        _tape(
            decision_ts=decision,
            exchange_ts=decision - timedelta(seconds=6),
            capture_ts=decision,
            scored_ts=decision + timedelta(minutes=5),
        ),
        CALENDAR,
        freeze=FREEZE,
    )
    hidden = observe(
        _tape(entry_bar=_bar(decision + timedelta(minutes=5), low="99.90")),
        CALENDAR,
        freeze=FREEZE,
    )
    assert stale.reason == "STALE"
    assert stale.quality == "STALE"
    assert stale.fill_rate_kind == "unobserved"
    assert hidden.reason == "LOOKAHEAD"
    assert hidden.path_bar_close is None
    assert hidden.status == "UNFILLED"


def test_unknown_queue_and_the_wrong_arm_stay_unfilled() -> None:
    unknown = observe(_tape(queue_ahead=None, entry_bar=None), CALENDAR, freeze=FREEZE)
    wrong = observe(_tape(arm="B_POST_TO_POST"), CALENDAR, freeze=FREEZE)
    assert unknown.reason == "QUEUE_UNKNOWN"
    assert unknown.fill_rate_kind == "unobserved"
    assert wrong.reason == "ARM_SESSION"
    assert wrong.status == "UNFILLED"


def test_a_penetrated_bar_is_only_an_estimate() -> None:
    row = observe(
        _tape(exit_bid=Decimal("101"), exit_ts=_at(date(2024, 11, 1), 15, 44)),
        CALENDAR,
        freeze=FREEZE,
    )
    assert row.status == "FILLED_ESTIMATE"
    assert row.fill_rate_kind == "estimate"
    assert row.path == "PATH_RECORDED"
    assert Decimal(row.estimated_qty) == 2
    assert row.books_profit is False
    assert row.exchange_fill_id is None
    assert row.limit_price is not None
    assert Decimal(row.limit_price) == Decimal("100")


def test_a_queue_leaves_a_remainder_and_a_zero_bid_fails_the_exit() -> None:
    decision = _at(date(2024, 11, 1), 15, 40)
    partial = observe(
        _tape(queue_ahead=Decimal("9"), entry_bar=_bar(decision, low="99.90", volume="10")),
        CALENDAR,
        freeze=FREEZE,
    )
    failed = observe(
        _tape(exit_bid=Decimal("0"), exit_ts=decision + timedelta(minutes=1)),
        CALENDAR,
        freeze=FREEZE,
    )
    assert partial.status == "PARTIAL_ESTIMATE"
    assert Decimal(partial.estimated_qty) == 1
    assert Decimal(partial.remaining_qty or "0") == 1
    assert partial.fill_rate_kind == "estimate"
    assert failed.status == "FILLED_ESTIMATE"
    assert failed.path == "EXIT_FAILED"
    assert failed.exit_bid is None
    assert failed.books_profit is False


def test_a_freeze_mismatch_is_rejected() -> None:
    with pytest.raises(ValueError, match="freeze"):
        observe(_tape(), CALENDAR, freeze="0" * 64)


def test_an_interval_is_withheld_below_the_floor_and_labeled_when_present() -> None:
    assert estimate_interval(0, MIN_RESOLVED - 1) is None
    interval = estimate_interval(0, MIN_RESOLVED)
    assert interval is not None
    assert interval["kind"] == "estimate"
    assert interval["low"] == "0.000000"


def test_fixture_days_do_not_meet_the_live_floor_and_nothing_starts(tmp_path: Path) -> None:
    tapes = [_session(day, "fixture") for day in _sessions(LIVE_SESSION_FLOOR)]
    body = write_bundle(tmp_path / "out", _source(tmp_path), tapes, _assessment(), _probe())
    assert body["meets_engineering_floor"] is False
    assert body["live_session_count"] == 0
    assert body["process_started"] is False
    assert body["collection_authorized"] is False
    assert body["shadow_exchange_fills"] == 0
    assert body["exchange_fill_rate"] is None
    arms = {str(item["arm"]): item for item in body["arms"]}  # type: ignore[union-attr]
    assert arms["A_RTH_TO_POST"]["estimated_fill_rate_kind"] == "estimate"
    assert arms["B_POST_TO_POST"]["estimated_fill_rate"] is None
    assert arms["A_RTH_TO_POST"]["exchange_fill_rate"] is None
    assert "fill_rate" not in arms["A_RTH_TO_POST"]


def test_twenty_live_days_still_do_not_start_a_process(tmp_path: Path) -> None:
    tapes = [_session(day, "live_readonly") for day in _sessions(LIVE_SESSION_FLOOR)]
    body = write_bundle(
        tmp_path / "out",
        _source(tmp_path),
        tapes,
        _assessment(shadow_authorized=True, strategy_verdict="SHADOW_ONLY"),
        _probe(),
    )
    launch = body["launch"]
    assert body["meets_engineering_floor"] is True
    assert body["observation_status"] == "COMPLETE"
    assert body["strategy_verdict"] == "NO_GO"
    assert isinstance(launch, dict)
    assert launch["start_process"] is False
    assert launch["pid"] is None
    assert "NO_UNATTENDED_SPAWN" in launch["reasons"]
    assert body["books_profit"] == 0
    assert body["attribution"] == "EXCHANGE_FILLS_ABSENT"


def test_an_empty_replay_does_not_invent_a_zero_fill_rate(tmp_path: Path) -> None:
    body = write_bundle(tmp_path / "out", _source(tmp_path), (), _assessment(), _probe())
    assert body["observations"] == 0
    assert body["observation_status"] == "INCOMPLETE"
    assert body["attribution"] == "NO_LIVE_TAPE"
    assert "LIVE_SESSIONS_BELOW_FLOOR" in body["reasons"]
    arms = {str(item["arm"]): item for item in body["arms"]}  # type: ignore[union-attr]
    assert arms["C_POST_TO_OVERNIGHT"]["estimated_fill_rate"] is None
    assert arms["C_POST_TO_OVERNIGHT"]["estimated_fill_interval"] is None
    assert arms["C_POST_TO_OVERNIGHT"]["estimated_fill_rate_kind"] == "unobserved"


def test_a_duplicate_tape_is_stored_once(tmp_path: Path) -> None:
    tape = _tape()
    body = write_bundle(tmp_path / "out", _source(tmp_path), (tape, tape), _assessment(), _probe())
    assert body["observations"] == 1
    assert body["duplicates"] == 1
    connection = sqlite3.connect(tmp_path / "out" / "s5_06_shadow.sqlite")
    try:
        names = {str(row[0]) for row in connection.execute("SELECT name FROM sqlite_master")}
        assert "s5_06_observation" in names
        assert "intraday_bars_wal" not in names
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO s5_06_observation (
                    dedup_key, symbol, arm, status, fill_rate_kind,
                    books_profit, exchange_fill_id, payload
                ) VALUES ('other', 'AIQ', 'A_RTH_TO_POST', 'FILLED', 'estimate', 1, NULL, '{}')
                """
            )
    finally:
        connection.close()


def test_the_journal_stays_out_of_sip_and_production_files(tmp_path: Path) -> None:
    source = _source(tmp_path)
    with pytest.raises(ValueError, match="source SIP"):
        write_bundle(source / "inside", source, (), _assessment(), _probe())
    with pytest.raises(ValueError, match="production filename"):
        assert_shadow_target(tmp_path / "intraday.sqlite", source, ())
    journal = tmp_path / "out" / "s5_06_shadow.sqlite"
    with pytest.raises(ValueError, match="production path"):
        assert_shadow_target(journal, source, (journal,))
    poisoned = tmp_path / "wal" / "s5_06_shadow.sqlite"
    poisoned.parent.mkdir()
    connection = sqlite3.connect(poisoned)
    connection.execute("CREATE TABLE intraday_bars_wal (id INTEGER)")
    connection.commit()
    connection.close()
    with pytest.raises(ValueError, match="intraday WAL"):
        write_bundle(tmp_path / "wal", source, (), _assessment(), _probe())


def test_place_and_cancel_are_refused_and_launch_cannot_be_turned_on() -> None:
    tracker = ShadowTracker()
    with pytest.raises(LiveTradingDisabledError):
        tracker.place()
    with pytest.raises(LiveTradingDisabledError):
        tracker.cancel()
    opened = decide_launch(shadow_authorized=True, host_measured=True)
    closed = decide_launch(shadow_authorized=False, host_measured=False)
    assert opened["start_process"] is False
    assert opened["pid"] is None
    assert closed["start_process"] is False
    assert "G_S5_5_SHADOW_NOT_AUTHORIZED" in closed["reasons"]
    assert "HOST_UNMEASURED" in closed["reasons"]


def test_a_loaded_price_file_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="price file"):
        write_bundle(tmp_path / "out", _source(tmp_path), (), _assessment(), {"rows_loaded": 1})


def _assessment(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "shadow_authorized": False,
        "quote_backed_fills": 0,
        "strategy_verdict": "NO_GO",
    }
    payload.update(overrides)
    return payload


def _probe() -> dict[str, object]:
    return {"rows_loaded": 0, "has_bid_ask": False, "columns": ["o", "h", "l", "c", "v"]}


def _source(root: Path) -> Path:
    source = root / "sip"
    source.mkdir(exist_ok=True)
    return source


def _sessions(count: int) -> list[date]:
    found: list[date] = []
    day = date(2024, 11, 4)
    while len(found) < count:
        if CALENDAR.is_session(day):
            found.append(day)
        day += timedelta(days=1)
    return found


def _session(day: date, source: str) -> ShadowTape:
    decision = _at(day, 15, 40)
    return _tape(
        source=source,
        decision_ts=decision,
        exchange_ts=decision,
        capture_ts=decision,
        scored_ts=decision + timedelta(minutes=5),
        queue_ahead=None,
        entry_bar=None,
        exit_bid=None,
        exit_ts=None,
    )


def _tape(**overrides: object) -> ShadowTape:
    decision = _at(date(2024, 11, 1), 15, 40)
    fields: dict[str, object] = {
        "symbol": "AIQ",
        "arm": "A_RTH_TO_POST",
        "source": "fixture",
        "decision_ts": decision,
        "exchange_ts": decision,
        "capture_ts": decision,
        "scored_ts": decision + timedelta(minutes=5),
        "bid": Decimal("100"),
        "ask": Decimal("100.10"),
        "bid_size": Decimal("200"),
        "ask_size": Decimal("200"),
        "queue_ahead": Decimal("0"),
        "entry_bar": _bar(decision, low="99.90"),
        "exit_bid": None,
        "exit_ts": None,
        "multiplier_down": Decimal("50"),
        "multiplier_up": Decimal("200"),
        "equity": Decimal("1600"),
    }
    fields.update(overrides)
    return ShadowTape(**fields)  # type: ignore[arg-type]


def _bar(start: datetime, *, low: str, volume: str = "10") -> ClosedBar:
    return ClosedBar(
        symbol="AIQ",
        start=start,
        end=start + timedelta(minutes=1),
        open=Decimal("100"),
        high=Decimal("100.40"),
        low=Decimal(low),
        close=Decimal("100.10"),
        volume=Decimal(volume),
        timeframe="1m",
    )


def _at(day: date, hour: int, minute: int) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=NEW_YORK).astimezone(UTC)
