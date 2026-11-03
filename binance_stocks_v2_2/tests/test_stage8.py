"""Stage 8: causal ORB, a three-step sequential test, and the mock order chain."""

import ast
import json
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from bstocks_v2.constitution import live_trade_enabled
from bstocks_v2.constitution.guard import live_trade_enabled as guard_live
from bstocks_v2.contracts.errors import LiveTradingDisabledError, OrderRejected
from bstocks_v2.contracts.models import TradeIntent
from bstocks_v2.execution_gateway.mock import MockExecutionGateway
from bstocks_v2.feature_store.bars import FeatureBar, make_symbol_snapshot
from bstocks_v2.feature_store.errors import LookaheadError
from bstocks_v2.limit_optimizer.stage8 import replace_limit
from bstocks_v2.regime.classifier import FeatureSnapshot, Regime, route
from bstocks_v2.research_backtest.tape import ClosedBar
from bstocks_v2.research_data.calendar import NEW_YORK
from bstocks_v2.signal_arbitrator.stage8 import arbitrate
from bstocks_v2.strategies.s3.chain import Stage8Session
from bstocks_v2.strategies.s3.evaluate import _role
from bstocks_v2.strategies.s3.orb import OpeningRangeBook
from bstocks_v2.strategies.s3.score import SplitScore, comparison
from bstocks_v2.strategies.s3.sequential import confirm, judge
from bstocks_v2.strategies.s3.signal import UNCALIBRATED, Candidate, decide, make_intent
from bstocks_v2.strategies.s3.spec import CUSUM_H, CUSUM_K, LOCKBOX_START, MAX_STEPS

ROOT = Path(__file__).resolve().parents[1]
PACKAGES = (
    ROOT / "src" / "bstocks_v2" / "strategies" / "s3",
    ROOT / "src" / "bstocks_v2" / "signal_arbitrator",
    ROOT / "src" / "bstocks_v2" / "limit_optimizer",
    ROOT / "src" / "bstocks_v2" / "portfolio_risk",
    ROOT / "src" / "bstocks_v2" / "execution_gateway",
    ROOT / "src" / "bstocks_v2" / "guardian",
)
DAY = date(2024, 11, 1)
ASOF = datetime(2024, 11, 1, 14, 0, tzinfo=UTC)
SYMBOLS = ("SMH", "SOXX", "XOP", "XBI", "KRE", "IGV")


def test_sources_do_not_use_float_literals_or_network_clients() -> None:
    banned = {"socket", "httpx", "requests", "urllib", "websockets"}
    for package in PACKAGES:
        for path in package.rglob("*.py"):
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


def test_window_stays_inside_stage6_and_live_trading_stays_off() -> None:
    assert live_trade_enabled() is False
    assert guard_live() is False
    assert _role(date(2025, 10, 31)) == "train"
    assert _role(date(2025, 11, 3)) == "validation"
    with pytest.raises(RuntimeError, match="open window"):
        _role(LOCKBOX_START)
    assert sum(UNCALIBRATED) == 1
    assert MAX_STEPS == 3
    assert CUSUM_K == Decimal("0.50")
    assert CUSUM_H == Decimal("1.00")


def test_range_locks_only_after_the_window_and_ignores_later_extremes() -> None:
    book = OpeningRangeBook()
    assert (
        book.observe(_bar(0, Decimal("10"), Decimal("9"), Decimal("9.50")))[15].reason
        == "WINDOW_OPEN"
    )
    book.observe(_bar(1, Decimal("10.20"), Decimal("9"), Decimal("9.60")))
    locked = book.observe(_bar(2, Decimal("10.40"), Decimal("9"), Decimal("9.80")))[15]
    assert locked.reason == "LOCK_BAR" and locked.orh == Decimal("10.40")
    assert locked.first_break is False
    broken = book.observe(_bar(3, Decimal("11"), Decimal("9"), Decimal("10.50")))[15]
    assert broken.reason == "FIRST_BREAK" and broken.orh == Decimal("10.40")
    later = book.observe(_bar(4, Decimal("20"), Decimal("9"), Decimal("12")))[15]
    assert later.reason == "ALREADY_BROKE" and later.orh == Decimal("10.40")
    assert broken.orh == Decimal("10.40")


def test_an_early_break_is_not_reissued_on_the_only_s3_bar() -> None:
    book = OpeningRangeBook()
    for slot in range(3):
        book.observe(_bar(slot, Decimal("10") + Decimal(slot) / 10, Decimal("9"), Decimal("9.50")))
    early = book.observe(_bar(3, Decimal("11"), Decimal("9"), Decimal("10.80")))[5]
    assert early.reason == "FIRST_BREAK"
    held = book.observe(_bar(4, Decimal("11"), Decimal("9"), Decimal("10.90")))
    opening = book.observe(_bar(5, Decimal("12"), Decimal("9"), Decimal("11")))
    assert held[5].reason == "ALREADY_BROKE"
    assert opening[5].reason == "ALREADY_BROKE"
    assert opening[15].reason == "ALREADY_BROKE"
    assert opening[30].reason == "LOCK_BAR"
    assert opening[30].first_break is False


def test_a_15_minute_break_can_still_be_the_0955_bar() -> None:
    book = OpeningRangeBook()
    for slot, close in enumerate(
        (Decimal("9.50"), Decimal("9.60"), Decimal("9.80"), Decimal("10"), Decimal("10.20"))
    ):
        book.observe(_bar(slot, Decimal("10.40"), Decimal("9"), close))
    opening = book.observe(_bar(5, Decimal("11"), Decimal("9"), Decimal("10.60")))
    assert opening[15].reason == "FIRST_BREAK" and opening[15].orh == Decimal("10.40")
    assert opening[5].reason == "FIRST_BREAK"


def test_thirty_minute_break_falls_outside_the_s3_gate() -> None:
    chosen = route(Regime.SIDEWAYS, False)
    assert chosen.s3_enabled is False
    book = OpeningRangeBook()
    for slot in range(6):
        view = book.observe(_bar(slot, Decimal("10"), Decimal("9"), Decimal("10")))
    assert view[30].reason == "LOCK_BAR"
    nxt = book.observe(_bar(6, Decimal("11"), Decimal("9"), Decimal("10.50")))[30]
    assert nxt.reason == "FIRST_BREAK"
    candidate, why = decide(
        symbol="SMH",
        snap=_snapshot(_start(6) + timedelta(minutes=5)),
        regime=_market(_start(6) + timedelta(minutes=5), Regime.SIDEWAYS, False),
        research_eligible=True,
        half_day=False,
        opening=nxt,
    )
    assert candidate is None and why == "S3_DISABLED"


def test_gap_and_rewind_do_not_publish_a_range() -> None:
    book = OpeningRangeBook()
    book.observe(_bar(0, Decimal("10"), Decimal("9"), Decimal("9.50")))
    gapped = book.observe(_bar(2, Decimal("10"), Decimal("9"), Decimal("9.50")))
    assert gapped[5].reason == "DAY_INVALID"
    fresh = OpeningRangeBook()
    fresh.observe(_bar(0, Decimal("10"), Decimal("9"), Decimal("9.50")))
    earlier = _start(0) - timedelta(minutes=5)
    with pytest.raises(LookaheadError):
        fresh.observe(
            FeatureBar(
                symbol="SMH",
                start=earlier,
                end=earlier + timedelta(minutes=5),
                open=Decimal("9"),
                high=Decimal("10"),
                low=Decimal("9"),
                close=Decimal("9.50"),
                volume=Decimal(1000),
                bar_vwap=Decimal("9.50"),
                closed=True,
                feed="sip",
            )
        )


def test_unclosed_bar_is_refused() -> None:
    bar = _bar(0, Decimal("10"), Decimal("9"), Decimal("9.50"))
    with pytest.raises(LookaheadError, match="unclosed"):
        OpeningRangeBook().observe(
            FeatureBar(
                symbol=bar.symbol,
                start=bar.start,
                end=bar.end,
                open=bar.open,
                high=bar.high,
                low=bar.low,
                close=bar.close,
                volume=bar.volume,
                bar_vwap=bar.bar_vwap,
                closed=False,
                feed="sip",
            )
        )


def test_sequential_stops_at_three_minutes_and_rejects_when_undecided() -> None:
    with pytest.raises(ValueError, match="too many steps"):
        judge((Decimal(1), Decimal(1), Decimal(1), Decimal(1)))
    weak = confirm(
        _path(Decimal("10.10"), Decimal("10.20"), Decimal("10.30"), Decimal("1"), Decimal("1")),
        orh=Decimal("10"),
        atr=_atr(),
    )
    assert weak.accept is False and weak.terminal == "UNDECIDED_REJECT" and weak.steps == 3
    loud = confirm(
        _path(Decimal("12"), Decimal("1"), Decimal("1"), Decimal("1"), Decimal("100")),
        orh=Decimal("10"),
        atr=_atr(),
        spy=_spy(up=True),
    )
    crashed = confirm(
        _path(Decimal("12"), Decimal("1"), Decimal("1"), Decimal("1"), Decimal("1")),
        orh=Decimal("10"),
        atr=_atr(),
        spy=_spy(up=True),
    )
    assert loud.accept and crashed.accept
    assert loud.terminal == crashed.terminal
    assert loud.steps == 1
    low = confirm(
        _location_path(),
        orh=Decimal("10"),
        atr=Decimal("0.2") * Decimal(5).sqrt(),
        spy=_spy(up=True),
    )
    assert low.accept is False and low.terminal == "LOCATION"
    down = confirm(
        _path(Decimal("12"), Decimal("11"), Decimal("11"), Decimal("11"), Decimal("11")),
        orh=Decimal("10"),
        atr=_atr(),
        spy=_spy(up=False),
    )
    assert down.accept is False and down.terminal == "SPY_SYNC"
    missing = confirm(
        _path(Decimal("12"), Decimal("11"), Decimal("11"), Decimal("11"), Decimal("11")),
        orh=Decimal("10"),
        atr=_atr(),
    )
    assert missing.terminal == "SPY_MINUTE_MISSING"


def test_chain_reserves_a_slot_writes_audit_and_flattens_without_a_live_order(
    tmp_path: Path,
) -> None:
    log = tmp_path / "audit.jsonl"
    session = Stage8Session(log)
    first = session.submit(
        _intent("SMH"), now_et=_start(5) + timedelta(minutes=5), is_half_day=False
    )
    assert first.state == "ENTRY_WORKING"
    assert first.pending_buys == 1 and first.open_positions == 0
    assert first.limit_price == Decimal("100.00")
    assert session.fill(first.client_order_id or "") == "OPEN"
    assert session.book.pending_buys == 0 and session.book.open_positions == 1
    early = datetime(2024, 11, 1, 15, 0, tzinfo=NEW_YORK)
    with pytest.raises(OrderRejected, match="FLATTEN_EARLY"):
        session.exit(first.client_order_id or "", "FLATTEN", now_et=early, is_half_day=False)
    assert session.book.open_positions == 1
    done = session.exit(
        first.client_order_id or "",
        "FLATTEN",
        now_et=datetime(2024, 11, 1, 15, 55, tzinfo=NEW_YORK),
        is_half_day=False,
    )
    assert done == "FLAT_RECONCILED"
    assert session.book.open_positions == 0
    rows = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert [row["stage"] for row in rows[:6]] == [
        "arbitrator",
        "limit_optimizer",
        "portfolio_risk",
        "gateway",
        "guardian",
        "gateway",
    ]
    gateway = MockExecutionGateway()
    with pytest.raises(LiveTradingDisabledError):
        gateway.live_place("https://api.binance.com/sapi/v1/equity/order/place")


def test_pending_buys_occupy_slots_and_a_higher_limit_is_refused() -> None:
    session = Stage8Session()
    working: list[str] = []
    for symbol in SYMBOLS[:5]:
        result = session.submit(
            _intent(symbol), now_et=_start(5) + timedelta(minutes=5), is_half_day=False
        )
        assert result.state == "ENTRY_WORKING"
        assert result.client_order_id is not None
        working.append(result.client_order_id)
    blocked = session.submit(
        _intent("IGV"), now_et=_start(5) + timedelta(minutes=5), is_half_day=False
    )
    assert blocked.reason == "MAX_POSITIONS"
    assert session.book.pending_buys == 5
    assert session.expire(working[0]) == "FLAT"
    assert session.book.pending_buys == 4
    plan = replace_limit(
        session._plans[next(iter(session._plans))],
        Decimal("101"),
    )
    assert plan.refused_chase is True and plan.limit == Decimal("100.00")
    negative = _intent("XHB").model_copy(update={"expected_net_bps": Decimal("-5")})
    assert arbitrate(negative).accepted is True
    assert negative.expected_net_bps < 0


def test_comparison_does_not_promote_a_thin_sequential_book() -> None:
    baseline = _score(30, Decimal(10), Decimal(1))
    thin = _score(3, Decimal(50), Decimal(1))
    assert comparison(baseline, thin) == "insufficient"
    worse = _score(30, Decimal(9), Decimal(1))
    assert comparison(baseline, worse) == "baseline"


def _score(count: int, mean: Decimal, se: Decimal) -> SplitScore:
    return SplitScore(count, mean, se, Decimal(2), Decimal(1), mean, Decimal("0.10"))


def _intent(symbol: str) -> TradeIntent:
    start = _start(5)
    end = start + timedelta(minutes=5)
    candidate = Candidate(
        symbol=symbol,
        horizon=15,
        signal_end=end,
        bar_start=start,
        ny_date=DAY,
        features_hash="stage8",
        regime="SIDEWAYS",
        orh=Decimal("100"),
        orl=Decimal("98"),
        atr=Decimal("1"),
        rvol_median=Decimal("1.2"),
        limit_price=Decimal("100.00"),
        ceiling=Decimal("100.25"),
        stop_price=Decimal("98.00"),
        take_profit_price=Decimal("103.00"),
    )
    intent = make_intent(candidate, arm="baseline")
    if symbol == "SMH":
        return intent
    return intent.model_copy(update={"intent_id": f"s315b{symbol}20241101T150000Z"})


def _bar(slot: int, high: Decimal, low: Decimal, close: Decimal) -> FeatureBar:
    start = _start(slot)
    return FeatureBar(
        symbol="SMH",
        start=start,
        end=start + timedelta(minutes=5),
        open=low,
        high=high,
        low=low,
        close=close,
        volume=Decimal(1000),
        bar_vwap=close,
        closed=True,
        feed="sip",
    )


def _start(slot: int) -> datetime:
    return datetime.combine(DAY, time(9, 30), tzinfo=NEW_YORK) + timedelta(minutes=5 * slot)


def _snapshot(asof: datetime) -> object:
    return make_symbol_snapshot(
        symbol="SMH",
        data_asof_utc=asof,
        bar_start=asof - timedelta(minutes=5),
        ny_date=DAY.isoformat(),
        bucket="10:00",
        vwap_intact=True,
        close=Decimal("10.50"),
        vwap=Decimal("10"),
        rsi6=Decimal(55),
        atr14=Decimal(1),
        rvol_mean=Decimal("1.2"),
        rvol_median=Decimal("1.2"),
        ret=Decimal("0.01"),
        sigma_tod=Decimal("0.01"),
        ret_norm=Decimal(1),
        vwap_slope=Decimal("0.01"),
        trend_z=Decimal(1),
        rms6=Decimal(1),
        rel_vol=Decimal(1),
        baseline_days=20,
        above_vwap=True,
        warmed=True,
    )


def _market(asof: datetime, regime: Regime, opening: bool) -> FeatureSnapshot:
    chosen = route(regime, opening)
    return FeatureSnapshot(
        data_asof_utc=asof,
        vwap_intact=True,
        features_hash="market",
        regime=regime,
        confidence=Decimal(1) if regime is not Regime.UNKNOWN else Decimal(0),
        entries_allowed=chosen.entries_allowed,
        s1_enabled=chosen.s1_enabled,
        s2_enabled=chosen.s2_enabled,
        s3_enabled=chosen.s3_enabled,
        s4_research_enabled=chosen.s4_research_enabled,
        in_opening_window=opening,
        data_ready=True,
        dwell_bars=3,
        block_reason="OK",
        benchmarks_present=13,
        breadth=Decimal("0.5"),
        trend_z=Decimal(0),
        spy_rvol=Decimal(1),
        spy_rms6=Decimal(1),
    )


def _atr() -> Decimal:
    return Decimal(5).sqrt()


def _path(
    one: Decimal, two: Decimal, three: Decimal, four: Decimal, five: Decimal
) -> tuple[ClosedBar, ...]:
    bars: list[ClosedBar] = []
    for step, close in enumerate((one, two, three, four, five)):
        start = _start(5) + timedelta(minutes=step)
        low = min(Decimal("10"), close)
        high = max(Decimal("12"), close)
        bars.append(
            ClosedBar(
                symbol="SMH",
                start=start,
                end=start + timedelta(minutes=1),
                open=Decimal("10"),
                high=high,
                low=low,
                close=close,
                volume=Decimal(100),
                timeframe="1m",
            )
        )
    return tuple(bars)


def _location_path() -> tuple[ClosedBar, ...]:
    start = _start(5)
    first = ClosedBar(
        symbol="SMH",
        start=start,
        end=start + timedelta(minutes=1),
        open=Decimal("10"),
        high=Decimal("14"),
        low=Decimal("9"),
        close=Decimal("10.50"),
        volume=Decimal(100),
        timeframe="1m",
    )
    rest = list(_path(Decimal("10"), Decimal("10"), Decimal("10"), Decimal("10"), Decimal("10")))[
        1:
    ]
    return (first, *rest)


def _spy(up: bool) -> tuple[ClosedBar, ...]:
    close = Decimal("11") if up else Decimal("9")
    bars: list[ClosedBar] = []
    for step in range(5):
        start = _start(5) + timedelta(minutes=step)
        bars.append(
            ClosedBar(
                symbol="SPY",
                start=start,
                end=start + timedelta(minutes=1),
                open=Decimal("10"),
                high=Decimal("11"),
                low=Decimal("9"),
                close=close,
                volume=Decimal(100),
                timeframe="1m",
            )
        )
    return tuple(bars)
