"""Stage 10: one intent per symbol, five slots, and a structured reject log."""

import ast
import json
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from bstocks_v2.constitution.guard import (
    MAX_FRICTION_RATIO,
    MAX_OPEN_POSITIONS,
    entry_window_open,
    live_trade_enabled,
)
from bstocks_v2.contracts.models import Direction, FillHorizon, TradeIntent
from bstocks_v2.market_ingestor.staleness import KLINE_STALE_MS
from bstocks_v2.signal_arbitrator import SignalArbitrator, arbitrate
from bstocks_v2.signal_arbitrator.spec import (
    ADMITTED,
    ASSUMED_HALF_SPREAD,
    COMMISSION_RATE,
    COOLDOWN,
    MAX_PER_SECTOR,
)
from bstocks_v2.signal_arbitrator.stage10 import (
    Candidate,
    Holding,
    SlotBook,
    net_edge_blocked,
    round_trip_friction_ratio,
)
from bstocks_v2.strategies.s1.spec import COMMISSION_RATE as S1_COMMISSION
from bstocks_v2.strategies.s1.spec import HALF_SPREAD as S1_HALF_SPREAD
from bstocks_v2.strategies.s1.spec import SECTOR_PARENT
from bstocks_v2.strategies.s4.lock import LIVE_ENABLED

NY = ZoneInfo("America/New_York")
WHEN = datetime(2024, 11, 1, 15, 0, tzinfo=NY)
ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "src" / "bstocks_v2" / "signal_arbitrator"


def test_frozen_gates_and_no_float_or_network() -> None:
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
                assert node.module.split(".")[0] not in banned
    assert live_trade_enabled() is False
    assert LIVE_ENABLED is False
    assert MAX_OPEN_POSITIONS == 5
    assert MAX_PER_SECTOR == 2
    assert COOLDOWN == timedelta(minutes=30)
    assert MAX_FRICTION_RATIO == Decimal("0.20")
    assert COMMISSION_RATE == S1_COMMISSION == Decimal("0.0005")
    assert ASSUMED_HALF_SPREAD == S1_HALF_SPREAD == Decimal("0.01")
    assert KLINE_STALE_MS == 6 * 60 * 1000
    assert ADMITTED == frozenset({"S1", "S2", "S3", "S4"})
    assert arbitrate is not None


def test_net_edge_keeps_the_boundary_and_blocks_a_zero() -> None:
    assert net_edge_blocked(Decimal("0.01"), Decimal("0.20")) is False
    assert net_edge_blocked(Decimal("0.01"), Decimal("0.20") + Decimal("0.0000001")) is True
    assert net_edge_blocked(Decimal("0"), Decimal("0.01")) is True
    assert net_edge_blocked(Decimal("-1"), Decimal("0.01")) is True
    wide = round_trip_friction_ratio(Decimal("100"), Decimal("103"))
    tight = round_trip_friction_ratio(Decimal("100"), Decimal("100.10"))
    assert wide < Decimal("0.20") < tight


def test_same_symbol_keeps_the_better_risk_adjusted_edge() -> None:
    s1 = _candidate("SMH", "S1", net="20", lower="1")
    s3 = _candidate("SMH", "S3", net="50", lower="40", stop="90")
    result = SignalArbitrator().arbitrate(
        (s1, s3), SlotBook(), is_half_day=False, production_mode=False
    )
    assert [item.intent_id for item in result.allocated] == ["S1-SMH"]
    eliminated = _by_code(result, "SAME_BAR_OUTRANKED")
    assert [row.intent_id for row in eliminated] == ["S3-SMH"]
    assert "winner=S1-SMH" in eliminated[0].detail
    assert "winner_edge=0.10000000" in eliminated[0].detail
    assert "edge=0.05000000" in eliminated[0].detail


def test_equal_edge_uses_the_interval_then_spread_then_notional_then_name() -> None:
    lower = _candidate("SMH", "S1", intent_id="S1-low", lower="-10")
    higher = _candidate("SMH", "S3", intent_id="S3-high", lower="5")
    by_lower = SignalArbitrator().arbitrate(
        (lower, higher), SlotBook(), is_half_day=False, production_mode=False
    )
    assert [item.intent_id for item in by_lower.allocated] == ["S3-high"]

    wide = _candidate("XOP", "S1", intent_id="S1-wide", spread="0.02")
    tight = _candidate("XOP", "S3", intent_id="S3-tight", spread="0.01")
    by_spread = SignalArbitrator().arbitrate(
        (wide, tight), SlotBook(), is_half_day=False, production_mode=False
    )
    assert [item.intent_id for item in by_spread.allocated] == ["S3-tight"]

    heavy = _candidate("XBI", "S1", intent_id="S1-heavy", notional="300")
    light = _candidate("XBI", "S3", intent_id="S3-light", notional="120")
    by_cash = SignalArbitrator().arbitrate(
        (heavy, light), SlotBook(), is_half_day=False, production_mode=False
    )
    assert [item.intent_id for item in by_cash.allocated] == ["S3-light"]

    later = _candidate("KRE", "S2", intent_id="S2-b")
    earlier = _candidate("KRE", "S1", intent_id="S1-a")
    by_name = SignalArbitrator().arbitrate(
        (later, earlier), SlotBook(), is_half_day=False, production_mode=False
    )
    assert [item.intent_id for item in by_name.allocated] == ["S1-a"]


def test_a_tighter_spread_does_not_beat_a_better_edge() -> None:
    thin = _candidate("SMH", "S1", net="20", spread="0.01")
    rich = _candidate("SMH", "S3", net="50", stop="98", spread="0.05")
    result = SignalArbitrator().arbitrate(
        (thin, rich), SlotBook(), is_half_day=False, production_mode=False
    )
    assert [item.intent_id for item in result.allocated] == ["S3-SMH"]


def test_cooldown_starts_when_a_slot_is_taken_and_is_configurable() -> None:
    first = SignalArbitrator()
    opened = first.arbitrate(
        (_candidate("SMH", "S1"),), SlotBook(), is_half_day=False, production_mode=False
    )
    assert opened.allocated[0].symbol == "SMH"
    blocked = first.arbitrate(
        (_candidate("SMH", "S2", when=WHEN + timedelta(minutes=30)),),
        SlotBook(),
        is_half_day=False,
        production_mode=False,
    )
    assert blocked.allocated == ()
    assert _codes(blocked) == ("COOLDOWN_DUPLICATE",)
    released = first.arbitrate(
        (_candidate("SMH", "S2", when=WHEN + timedelta(minutes=30, seconds=1)),),
        SlotBook(),
        is_half_day=False,
        production_mode=False,
    )
    assert [item.strategy_id for item in released.allocated] == ["S2"]

    short = SignalArbitrator(cooldown=timedelta(minutes=5))
    short.arbitrate(
        (_candidate("XOP", "S1"),), SlotBook(), is_half_day=False, production_mode=False
    )
    inside = short.arbitrate(
        (_candidate("XOP", "S1", when=WHEN + timedelta(minutes=5)),),
        SlotBook(),
        is_half_day=False,
        production_mode=False,
    )
    assert _codes(inside) == ("COOLDOWN_DUPLICATE",)
    assert "cooldown_seconds=300" in inside.records[0].detail


def test_a_name_that_lost_the_slot_can_compete_on_the_next_bar() -> None:
    book = _pending("XOP", "XBI", "KRE", "ITA")
    arb = SignalArbitrator()
    first = arb.arbitrate(
        (_candidate("SMH", "S1", net="40"), _candidate("GDX", "S1", net="10")),
        book,
        is_half_day=False,
        production_mode=False,
    )
    assert [item.symbol for item in first.allocated] == ["SMH"]
    assert _codes(first) == ("OUTRANKED_FOR_SLOT",)
    later = arb.arbitrate(
        (_candidate("GDX", "S1", net="10", when=WHEN + timedelta(minutes=5)),),
        book,
        is_half_day=False,
        production_mode=False,
    )
    assert [item.symbol for item in later.allocated] == ["GDX"]


def test_pending_buys_occupy_slots_and_block_the_same_symbol() -> None:
    full = _pending("SMH", "SOXX", "XOP", "XBI", "KRE")
    assert full.available_slots == 0
    blocked = SignalArbitrator().arbitrate(
        (_candidate("ITA", "S1", net="40"),),
        full,
        is_half_day=False,
        production_mode=False,
    )
    assert blocked.allocated == ()
    assert _codes(blocked) == ("NO_AVAILABLE_SLOT",)

    four = _pending("SOXX", "XOP", "XBI", "KRE")
    reserved = SignalArbitrator().arbitrate(
        (_candidate("SOXX", "S2", net="40"), _candidate("ITA", "S1", net="10")),
        four,
        is_half_day=False,
        production_mode=False,
    )
    assert [item.symbol for item in reserved.allocated] == ["ITA"]
    assert _codes(reserved) == ("SYMBOL_ALREADY_RESERVED",)


def test_sector_cap_leaves_a_slot_empty_instead_of_stacking_one_parent() -> None:
    names = (
        _candidate("SOXX", "S1", net="40"),
        _candidate("IGV", "S1", net="38"),
        _candidate("HACK", "S1", net="36"),
        _candidate("SKYY", "S1", net="34"),
        _candidate("BOTZ", "S1", net="32"),
    )
    result = SignalArbitrator().arbitrate(
        names, SlotBook(), is_half_day=False, production_mode=False
    )
    assert [item.symbol for item in result.allocated] == ["SOXX", "IGV"]
    assert _codes(result) == (
        "SECTOR_CONCENTRATION",
        "SECTOR_CONCENTRATION",
        "SECTOR_CONCENTRATION",
    )
    held = SlotBook(
        open_positions=(Holding(symbol="SOXX", sector="XLK"), Holding(symbol="IGV", sector="XLK"))
    )
    mixed = SignalArbitrator().arbitrate(
        (_candidate("HACK", "S1", net="40"), _candidate("XOP", "S2", net="10")),
        held,
        is_half_day=False,
        production_mode=False,
    )
    assert [item.symbol for item in mixed.allocated] == ["XOP"]
    assert _codes(mixed) == ("SECTOR_CONCENTRATION",)


def test_cross_section_ranks_edges_and_writes_the_sample_log(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    result = SignalArbitrator(log_path=path).arbitrate(
        _sample_batch(), SlotBook(), is_half_day=False, production_mode=True
    )
    assert [item.symbol for item in result.allocated] == ["SOXX", "IGV", "XOP", "XBI", "KRE"]
    assert result.available_slots == 5
    codes = {(row.intent_id, row.reason_code) for row in result.records}
    assert ("S4-SMH", "STRATEGY_LIVE_DISABLED") in codes
    assert ("S3-SMH", "SAME_BAR_OUTRANKED") in codes
    assert ("S1-SMH", "SECTOR_CONCENTRATION") in codes
    assert ("S1-HACK", "SECTOR_CONCENTRATION") in codes
    assert ("S1-ITA", "OUTRANKED_FOR_SLOT") in codes
    assert ("S1-FDN", "DATA_STALE") in codes
    assert ("S2-VNQ", "NEGATIVE_OR_INSUFFICIENT_NET_EDGE") in codes
    assert ("S1-GDX", "VWAP_GAP_UNRECOVERED") in codes
    assert ("S2-OIH", "SYMBOL_NOT_TRADABLE") in codes
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == len(result.records)
    for line, record in zip(lines, result.records, strict=True):
        assert json.loads(line) == record.as_dict()
    restored = SignalArbitrator(log_path=path)
    again = restored.arbitrate(
        (_candidate("SOXX", "S2", when=WHEN + timedelta(minutes=10)),),
        SlotBook(),
        is_half_day=False,
        production_mode=True,
    )
    assert _codes(again) == ("COOLDOWN_DUPLICATE",)


def test_hard_gates_keep_the_first_reason_only() -> None:
    ugly = _candidate(
        "SMH",
        "S4",
        net="0",
        regime="HIGH_VOL_PANIC",
        vwap=False,
        fresh=KLINE_STALE_MS + 1,
        status="TRADING_HALTED",
        when=datetime(2024, 11, 1, 15, 45, tzinfo=NY),
    )
    result = SignalArbitrator().arbitrate(
        (ugly,), SlotBook(), is_half_day=False, production_mode=True
    )
    assert _codes(result) == ("DATA_STALE",)

    cases = (
        (_candidate("SMH", "S1", vwap=False), False, True, "VWAP_GAP_UNRECOVERED"),
        (_candidate("SMH", "S1", tradability="CLOSE_ONLY"), False, True, "SYMBOL_NOT_TRADABLE"),
        (_candidate("SMH", "S1", status="TRADING_PAUSED"), False, True, "SYMBOL_NOT_TRADABLE"),
        (_candidate("SMH", "S1", regime="UNKNOWN"), False, True, "MARKET_REGIME_BLOCKED"),
        (_candidate("SMH", "S1", regime="HIGH_VOL_PANIC"), False, True, "MARKET_REGIME_BLOCKED"),
        (
            _candidate("SMH", "S1", when=datetime(2024, 11, 1, 15, 45, tzinfo=NY)),
            False,
            True,
            "PAST_ENTRY_CUTOFF_TIME",
        ),
        (
            _candidate("SMH", "S1", when=datetime(2024, 11, 1, 12, 45, tzinfo=NY)),
            True,
            True,
            "PAST_ENTRY_CUTOFF_TIME",
        ),
        (_candidate("SMH", "S4"), False, True, "STRATEGY_LIVE_DISABLED"),
        (_candidate("SMH", "S4"), False, False, None),
        (_candidate("SPY", "S1", sector="XLK"), False, True, "SECTOR_UNKNOWN"),
        (_candidate("SMH", "S1", sector="XLE"), False, True, "SECTOR_MISSTATED"),
        (_candidate("SMH", "S1", net="0"), False, True, "NEGATIVE_OR_INSUFFICIENT_NET_EDGE"),
        (_candidate("SMH", "S1", take="100.10"), False, True, "NEGATIVE_OR_INSUFFICIENT_NET_EDGE"),
        (_candidate("SMH", "S1", reasons=["PRE_REJECTED"]), False, True, "PRE_REJECTED"),
        (_candidate("SMH", "S9"), False, True, "STRATEGY_NOT_ADMITTED"),
        (
            _candidate("SMH", "S1", when=datetime(2024, 11, 2, 15, 0, tzinfo=NY)),
            False,
            True,
            "NON_RTH",
        ),
        (
            _candidate("SMH", "S1", when=datetime(2024, 11, 1, 9, 29, tzinfo=NY)),
            False,
            True,
            "NON_RTH",
        ),
    )
    for candidate, half_day, production, code in cases:
        outcome = SignalArbitrator().arbitrate(
            (candidate,), SlotBook(), is_half_day=half_day, production_mode=production
        )
        if code is None:
            assert outcome.allocated[0].strategy_id == "S4"
        else:
            assert outcome.allocated == ()
            assert _codes(outcome) == (code,)


def test_cutoff_matches_the_constitution_window() -> None:
    regular = datetime(2024, 11, 1, 15, 44, 59, tzinfo=NY)
    closed = datetime(2024, 11, 1, 15, 45, tzinfo=NY)
    opened = datetime(2024, 11, 1, 9, 30, tzinfo=NY)
    assert entry_window_open(regular, False) is True
    assert entry_window_open(closed, False) is False
    assert entry_window_open(opened, False) is True
    allowed = SignalArbitrator().arbitrate(
        (_candidate("SMH", "S1", when=regular),),
        SlotBook(),
        is_half_day=False,
        production_mode=False,
    )
    denied = SignalArbitrator().arbitrate(
        (_candidate("SMH", "S1", when=closed),),
        SlotBook(),
        is_half_day=False,
        production_mode=False,
    )
    assert allowed.allocated and _codes(denied) == ("PAST_ENTRY_CUTOFF_TIME",)
    half_open = datetime(2024, 11, 1, 12, 44, 59, tzinfo=NY)
    half_shut = datetime(2024, 11, 1, 12, 45, tzinfo=NY)
    assert entry_window_open(half_open, True) is True
    assert entry_window_open(half_shut, True) is False


def test_trend_down_is_not_an_arbitrator_block() -> None:
    result = SignalArbitrator().arbitrate(
        (_candidate("XOP", "S2", regime="TREND_DOWN"),),
        SlotBook(),
        is_half_day=False,
        production_mode=False,
    )
    assert [item.symbol for item in result.allocated] == ["XOP"]


def test_two_passes_allocate_the_same_ids() -> None:
    batch = _sample_batch()
    left = SignalArbitrator().arbitrate(batch, SlotBook(), is_half_day=False, production_mode=True)
    right = SignalArbitrator().arbitrate(
        tuple(reversed(batch)), SlotBook(), is_half_day=False, production_mode=True
    )
    assert [item.intent_id for item in left.allocated] == [
        item.intent_id for item in right.allocated
    ]
    assert [(row.intent_id, row.reason_code) for row in left.records] == [
        (row.intent_id, row.reason_code) for row in right.records
    ]


def test_a_later_bar_in_the_same_call_does_not_open_a_second_order() -> None:
    early = _candidate("SMH", "S1", net="20")
    later = _candidate(
        "SMH",
        "S1",
        intent_id="S1-SMH-later",
        net="40",
        when=WHEN + timedelta(minutes=31),
    )
    result = SignalArbitrator().arbitrate(
        (early, later), SlotBook(), is_half_day=False, production_mode=False
    )
    assert [item.intent_id for item in result.allocated] == ["S1-SMH"]
    assert _codes(result) == ("SAME_SYMBOL_SUPERSEDED",)


def test_bad_books_and_duplicate_ids_are_refused() -> None:
    with pytest.raises(ValidationError):
        SlotBook(open_positions=(Holding(symbol="SMH", sector="XLE"),))
    with pytest.raises(ValidationError):
        _pending("SMH", "SOXX", "XOP", "XBI", "KRE", "ITA")
    with pytest.raises(ValueError, match="duplicate intent_id"):
        SignalArbitrator().arbitrate(
            (_candidate("SMH", "S1"), _candidate("XOP", "S1", intent_id="S1-SMH")),
            SlotBook(),
            is_half_day=False,
            production_mode=False,
        )
    with pytest.raises(ValueError, match="cooldown"):
        SignalArbitrator(cooldown=timedelta(minutes=-1))


def _sample_batch() -> tuple[Candidate, ...]:
    return (
        _candidate("SOXX", "S1", net="40"),
        _candidate("IGV", "S1", net="38"),
        _candidate("HACK", "S1", net="36"),
        _candidate("XOP", "S2", net="34"),
        _candidate("SMH", "S1", net="33"),
        _candidate("SMH", "S3", net="50", stop="90"),
        _candidate("SMH", "S4", net="20"),
        _candidate("XBI", "S1", net="32"),
        _candidate("KRE", "S2", net="30"),
        _candidate("ITA", "S1", net="28"),
        _candidate("FDN", "S1", fresh=KLINE_STALE_MS + 1),
        _candidate("VNQ", "S2", net="0"),
        _candidate("GDX", "S1", vwap=False),
        _candidate("OIH", "S2", status="TRADING_HALTED"),
    )


def _pending(*symbols: str) -> SlotBook:
    return SlotBook(
        pending_buys=tuple(
            Holding(symbol=symbol, sector=SECTOR_PARENT[symbol]) for symbol in symbols
        )
    )


def _candidate(
    symbol: str,
    strategy: str,
    *,
    intent_id: str | None = None,
    when: datetime = WHEN,
    net: str = "20",
    lower: str = "1",
    limit: str = "100",
    stop: str = "98",
    take: str = "103",
    regime: str = "TREND_UP",
    vwap: bool = True,
    fresh: int = 0,
    reasons: list[str] | None = None,
    sector: str | None = None,
    tradability: str = "BUY_SELL",
    status: str = "TRADING",
    spread: str = "0.01",
    notional: str = "320",
) -> Candidate:
    price = Decimal(limit)
    intent = TradeIntent(
        intent_id=intent_id or f"{strategy}-{symbol}",
        created_at_utc=when,
        data_asof_utc=when,
        symbol=symbol,
        strategy_id=strategy,
        model_version="stage10",
        regime=regime,
        features_hash="stage10",
        vwap_intact=vwap,
        signal_valid_until=when + timedelta(minutes=10),
        direction=Direction.LONG,
        p_win_calibrated=Decimal("0.50"),
        p_sl_calibrated=Decimal("0.30"),
        p_timeout_calibrated=Decimal("0.20"),
        expected_return_bps=Decimal(net),
        expected_net_bps=Decimal(net),
        prediction_interval=(Decimal(lower), Decimal("30")),
        candidate_limit_prices=[price],
        p_fill_by_price_horizon=[
            FillHorizon(price=price, horizon_seconds=300, p_fill=Decimal("0.40"))
        ],
        entry_price_ceiling=price,
        stop_price=Decimal(stop),
        take_profit_price=Decimal(take),
        max_holding_seconds=3600,
        risk_budget_usdc=Decimal("4"),
        min_viable_notional_usdc=Decimal("120"),
        expected_cost_usdc=Decimal("0.24"),
        requires_fractional=False,
        whole_share_fallback_qty=Decimal("0"),
        rejection_reasons=reasons or [],
        data_feed="sip",
        data_freshness_ms=fresh,
    )
    return Candidate(
        intent=intent,
        bar_close_time=when,
        sector=SECTOR_PARENT.get(symbol, "XLK") if sector is None else sector,
        tradability=tradability,
        trading_status=status,
        half_spread=Decimal(spread),
        notional_usdc=Decimal(notional),
    )


def _codes(result: object) -> tuple[str, ...]:
    records = result.records  # type: ignore[attr-defined]
    return tuple(row.reason_code for row in records if row.reason_code != "ALLOCATED")


def _by_code(result: object, code: str) -> tuple[object, ...]:
    records = result.records  # type: ignore[attr-defined]
    return tuple(row for row in records if row.reason_code == code)
