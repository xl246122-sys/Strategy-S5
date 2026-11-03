"""Stage 11: three limit utilities, whole-share and 120 USDC gates, and timeout recovery."""

import ast
import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from bstocks_v2.constitution.guard import (
    CLIENT_ORDER_ID,
    MAX_FRICTION_RATIO,
    live_trade_enabled,
)
from bstocks_v2.contracts.errors import LiveTradingDisabledError
from bstocks_v2.contracts.models import Direction, FillHorizon, TradeIntent
from bstocks_v2.execution_gateway.idempotent import (
    BrokerOrder,
    ExchangeTimeout,
    IdempotentMockGateway,
    ResendForbidden,
    ScriptedEquityBroker,
    read_wal_status,
)
from bstocks_v2.execution_gateway.mock import MockExecutionGateway
from bstocks_v2.limit_optimizer import LimitOptimizer, bind_limit
from bstocks_v2.limit_optimizer.book import QuoteBook, build_candidates, floor_cents
from bstocks_v2.limit_optimizer.fill_model import (
    FillCalibrator,
    conservative_full_fill,
    drawdowns_before,
)
from bstocks_v2.limit_optimizer.spec import (
    DEFAULT_HORIZON_BARS,
    LAMBDA_CVAR,
    MAX_POSITION_FRACTION,
    PENETRATION_DELTA,
    TICK,
)
from bstocks_v2.limit_optimizer.stage11 import (
    CandidateScore,
    EntryClock,
    client_order_id_for,
    prefer_score,
)
from bstocks_v2.limit_optimizer.utility import (
    cvar_loss,
    friction_exceeds_cap,
    round_trip_friction_ratio,
    utility_of,
)
from bstocks_v2.research_backtest.models import MAX_POSITION_FRACTION as RESEARCH_CAP
from bstocks_v2.research_backtest.models import EngineConfig, SymbolFilter
from bstocks_v2.research_backtest.sizing import SizedOrder
from bstocks_v2.research_backtest.tape import ClosedBar
from bstocks_v2.signal_arbitrator.stage10 import round_trip_friction_ratio as stage10_ratio
from bstocks_v2.strategies.s4.lock import LIVE_ENABLED

NY = ZoneInfo("America/New_York")
WHEN = datetime(2024, 11, 1, 15, 0, tzinfo=NY)
ROOT = Path(__file__).resolve().parents[1]
LIMITS = ROOT / "src" / "bstocks_v2" / "limit_optimizer"
GATEWAY = ROOT / "src" / "bstocks_v2" / "execution_gateway"


class _Table:
    def __init__(self, values: dict[str, Decimal]) -> None:
        self._values = values

    def probability(self, *, ask: Decimal, limit: Decimal, tier: str) -> Decimal:
        del ask, limit
        return self._values[tier]


def test_frozen_constants_and_no_float_or_network() -> None:
    banned = {"socket", "httpx", "requests", "urllib", "websockets"}
    for folder in (LIMITS, GATEWAY):
        for path in folder.rglob("*.py"):
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
    assert bind_limit is not None
    assert DEFAULT_HORIZON_BARS == 2
    assert TICK == Decimal("0.01")
    assert PENETRATION_DELTA == EngineConfig(mode="conservative").fixed_delta
    assert LAMBDA_CVAR == Decimal("1")
    assert MAX_FRICTION_RATIO == Decimal("0.20")
    assert MAX_POSITION_FRACTION == RESEARCH_CAP == Decimal("0.20")
    assert "bars" not in LimitOptimizer.optimize.__code__.co_varnames


def test_utility_identity_and_cvar_tail() -> None:
    limit = Decimal("100")
    stop = Decimal("98")
    take = Decimal("110")
    qty = Decimal("1")
    p_fill = Decimal("0.8")
    p_win = Decimal("0.5")
    p_sl = Decimal("0.3")
    p_timeout = Decimal("0.2")
    utility, net, missed, tail = utility_of(
        limit=limit,
        stop=stop,
        take_profit=take,
        qty=qty,
        p_fill=p_fill,
        p_win=p_win,
        p_sl=p_sl,
        p_timeout=p_timeout,
    )
    assert net == Decimal("4.2778")
    assert missed == Decimal("0.85556")
    assert tail == Decimal("2.119")
    assert utility == Decimal("0.44768")
    assert utility + tail == (Decimal(2) * p_fill - 1) * net
    spilled = cvar_loss(
        limit=limit,
        stop=stop,
        take_profit=take,
        qty=qty,
        p_fill=Decimal("0.10"),
        p_win=Decimal("0.30"),
        p_sl=Decimal("0.20"),
        p_timeout=Decimal("0.50"),
    )
    assert spilled == Decimal("0.9196")


def test_prices_floor_down_and_stay_inside_the_cage() -> None:
    intent = _intent(ceiling="160", stop="140", take="170")
    book = QuoteBook(
        bid=Decimal("149.009"),
        ask=Decimal("150.009"),
        reference=Decimal("150"),
        multiplier_down=Decimal("0.8"),
        multiplier_up=Decimal("1.2"),
        vwap=Decimal("148.129"),
    )
    rows = {row.tier: row for row in build_candidates(intent, book)}
    assert rows["passive"].price == Decimal("148.99")
    assert rows["retest"].price == Decimal("148.12")
    assert rows["aggressive"].price == Decimal("150.00")
    assert floor_cents(Decimal("100.129")) == Decimal("100.12")
    ceiling = _intent(ceiling="150", stop="140", take="170")
    crossed = QuoteBook(
        bid=Decimal("149"),
        ask=Decimal("151"),
        reference=Decimal("150"),
        multiplier_down=Decimal("0.8"),
        multiplier_up=Decimal("1.2"),
        structure=Decimal("149"),
    )
    aggressive = {row.tier: row for row in build_candidates(ceiling, crossed)}["aggressive"]
    assert aggressive.price == Decimal("150")
    caged = QuoteBook(
        bid=Decimal("90"),
        ask=Decimal("90.50"),
        reference=Decimal("100"),
        multiplier_down=Decimal("0.91"),
        multiplier_up=Decimal("1.01"),
        structure=Decimal("90"),
    )
    inside = _intent(ceiling="100", stop="80", take="110")
    refused = build_candidates(inside, caged)
    assert {row.reason for row in refused} == {"PRICE_CAGE"}
    with pytest.raises(ValueError, match="float"):
        QuoteBook(
            bid=1.5,
            ask=Decimal("2"),
            reference=Decimal("2"),
            multiplier_down=Decimal("0.9"),
            multiplier_up=Decimal("1.1"),
        )


def test_three_tiers_pick_the_highest_utility() -> None:
    model = _Table(
        {
            "passive": Decimal("0.40"),
            "retest": Decimal("0.90"),
            "aggressive": Decimal("0.95"),
        }
    )
    result = LimitOptimizer(model).optimize(_intent(), _book(), _whole_filter(), _clock())
    assert result.reason is None
    assert result.order is not None
    assert [row.tier for row in result.scores] == ["passive", "retest", "aggressive"]
    assert [row.price for row in result.scores] == [
        Decimal("148.99"),
        Decimal("148.50"),
        Decimal("150"),
    ]
    assert [row.quantity for row in result.scores] == [Decimal(2), Decimal(2), Decimal(2)]
    assert result.scores[0].utility == Decimal("-5.3310524")
    assert result.scores[1].utility == Decimal("11.34742")
    assert result.scores[2].utility == Decimal("7.76979")
    assert result.order.tier == "retest"
    assert result.order.utility == max(_utilities(result.scores))
    assert result.order.adjustments == ("486442",)
    assert result.order.ttl_seconds == 600
    draft = result.order.draft
    assert draft.side.value == "BUY"
    assert draft.order_type.value == "LIMIT"
    assert draft.wallet_type == "CARD"
    assert draft.tokenize is False
    assert draft.quote_asset == "USDC"
    assert draft.trading_session is not None
    assert draft.trading_session.value == "RTH"
    assert draft.notional is None
    assert draft.price == Decimal("148.50")
    assert CLIENT_ORDER_ID.fullmatch(result.order.client_order_id)
    assert len(result.order.client_order_id) == 35
    assert result.order.client_order_id == client_order_id_for("s11", "retest")
    gateway = MockExecutionGateway()
    accepted = gateway.accept(
        SizedOrder(
            quantity=draft.quantity or Decimal(0),
            adjustments=result.order.adjustments,
            gross_take_profit=draft.gross_take_profit_usdc or Decimal(0),
            round_trip_friction=draft.round_trip_friction_usdc or Decimal(0),
            draft=draft,
        )
    )
    assert accepted.status == "NEW"
    assert accepted.client_order_id == result.order.client_order_id


def test_tie_keeps_the_lower_price_then_the_passive_tier() -> None:
    passive = _scored("passive", "99", "1")
    aggressive = _scored("aggressive", "100", "1")
    richer = _scored("aggressive", "100", "2")
    assert prefer_score(passive, aggressive) is True
    assert prefer_score(aggressive, passive) is False
    assert prefer_score(_scored("passive", "100", "1"), aggressive) is True
    assert prefer_score(richer, passive) is True


def test_whole_share_safe_mode_blocks_a_high_price_name() -> None:
    intent = _intent(ceiling="500", stop="490", take="520")
    book = QuoteBook(
        bid=Decimal("499"),
        ask=Decimal("500"),
        reference=Decimal("500"),
        multiplier_down=Decimal("0.5"),
        multiplier_up=Decimal("1.5"),
        structure=Decimal("498"),
    )
    filters = SymbolFilter(
        step_size=Decimal("0.001"),
        min_qty=Decimal("0.001"),
        min_notional=Decimal("1"),
        fractional_limit_allowed=True,
    )
    result = LimitOptimizer(_flat_model()).optimize(intent, book, filters, _clock())
    assert result.order is None
    assert result.reason == "WHOLE_SHARE_EXCEEDS_RISK_OR_CAP"
    assert {row.reason for row in result.scores} == {"WHOLE_SHARE_EXCEEDS_RISK_OR_CAP"}


def test_fractional_below_120_usdc_is_refused() -> None:
    intent = _intent(ceiling="50", stop="10", take="80")
    book = QuoteBook(
        bid=Decimal("50"),
        ask=Decimal("50"),
        reference=Decimal("50"),
        multiplier_down=Decimal("0.5"),
        multiplier_up=Decimal("1.5"),
        structure=Decimal("40"),
    )
    filters = SymbolFilter(
        step_size=Decimal("0.01"),
        min_qty=Decimal("0.01"),
        min_notional=Decimal("1"),
        fractional_limit_allowed=True,
    )
    optimizer = LimitOptimizer(_flat_model(), whole_share_safe_mode=False)
    result = optimizer.optimize(intent, book, filters, _clock())
    assert result.order is None
    assert result.reason == "NOTIONAL_TOO_SMALL_FOR_FEE_EFFICIENCY"
    assert all(row.detail.startswith("notional<120") for row in result.scores)


def test_friction_above_20_percent_uses_the_same_refusal() -> None:
    left = Decimal("100")
    right = Decimal("103")
    assert round_trip_friction_ratio(left, right) == stage10_ratio(left, right)
    over = round_trip_friction_ratio(Decimal("150"), Decimal("150.50"))
    under = round_trip_friction_ratio(Decimal("150"), Decimal("165"))
    assert over > Decimal("0.20") > under
    assert friction_exceeds_cap(Decimal("150"), Decimal("150.50")) is True
    assert friction_exceeds_cap(Decimal("150"), Decimal("165")) is False
    intent = _intent(ceiling="150", stop="148", take="150.10")
    tight = QuoteBook(
        bid=Decimal("150"),
        ask=Decimal("150"),
        reference=Decimal("150"),
        multiplier_down=Decimal("0.8"),
        multiplier_up=Decimal("1.2"),
        structure=Decimal("150"),
    )
    result = LimitOptimizer(_flat_model()).optimize(intent, tight, _whole_filter(), _clock())
    assert result.order is None
    assert result.reason == "NOTIONAL_TOO_SMALL_FOR_FEE_EFFICIENCY"
    assert all("friction_ratio>0.20" in row.detail for row in result.scores)


def test_calibrator_uses_only_windows_that_closed_before_the_cutoff() -> None:
    bars = _session(lows=("1", "100", "100", "100", "100", "100"))
    cutoff = bars[3].end
    asks = {bar.end: Decimal("100") for bar in bars}
    trained = drawdowns_before(bars, asks, symbol="XLK", horizon_bars=2, cutoff=cutoff)
    assert trained == (Decimal(0), Decimal(0))
    future = bars + _session(lows=("1",), start=bars[-1].end)
    again = drawdowns_before(future, asks, symbol="XLK", horizon_bars=2, cutoff=cutoff)
    assert again == trained
    model = FillCalibrator(trained)
    high = model.probability(ask=Decimal("100"), limit=Decimal("100.02"), tier="aggressive")
    low = model.probability(ask=Decimal("100"), limit=Decimal("90"), tier="passive")
    assert high == Decimal("0.75")
    assert low == Decimal("0.25")
    assert high > low
    crashed = tuple(Decimal("50") for _ in trained)
    assert FillCalibrator(crashed).probability(
        ask=Decimal("100"),
        limit=Decimal("90"),
        tier="passive",
    ) != low
    with pytest.raises(ValueError, match="FILL_MODEL_UNTRAINED"):
        FillCalibrator(())
    first = LimitOptimizer(model).optimize(_intent(), _book(), _whole_filter(), _clock())
    second = LimitOptimizer(FillCalibrator(again)).optimize(
        _intent(),
        _book(),
        _whole_filter(),
        _clock(),
    )
    assert first.order is not None and second.order is not None
    assert first.order.tier == second.order.tier
    assert first.order.draft.price == second.order.draft.price
    later = future[-1]
    assert conservative_full_fill(Decimal("100"), (later.low,), PENETRATION_DELTA) is True


def test_timeout_keeps_the_client_id_and_reads_detail_before_any_new_send(tmp_path: Path) -> None:
    order = _prepared()
    path = str(tmp_path / "timeout.sqlite")
    gateway = IdempotentMockGateway(path)
    try:
        assert gateway.prepare(order) == order.client_order_id
        assert gateway.status_of(order.client_order_id) == "WAL_PREPARED"
        assert read_wal_status(path, order.client_order_id) == "WAL_PREPARED"
        broker = ScriptedEquityBroker(["timeout_accept"], wal_path=path)
        outcome = gateway.submit_mock(order.client_order_id, broker)
        assert broker.seen_status == ["SUBMITTING"]
        assert broker.paths == [
            ("POST", "/sapi/v1/equity/order/place"),
            ("GET", "/sapi/v1/equity/order/detail"),
        ]
        assert outcome.status == "DETAIL_CONFIRMED"
        assert outcome.detail_queried is True
        assert outcome.code == -1007
        assert outcome.place_attempts == 1
        assert outcome.client_order_id == order.client_order_id
        assert list(broker.orders) == [order.client_order_id]
        with pytest.raises(ResendForbidden, match="ALREADY_CONFIRMED"):
            gateway.submit_mock(order.client_order_id, broker)
        assert broker.place_ids == [order.client_order_id]
        other = replace(
            order,
            client_order_id="b" * 32,
            draft=order.draft.model_copy(update={"client_order_id": "b" * 32}),
        )
        with pytest.raises(ResendForbidden) as raised:
            gateway.prepare(other)
        assert raised.value.reason == "RESEND_NEW_ID_FORBIDDEN"
        assert raised.value.client_order_id == order.client_order_id
    finally:
        gateway.close()


def test_dropped_timeout_retries_the_same_id_and_minus_1006_does_too(tmp_path: Path) -> None:
    order = _prepared()
    path = str(tmp_path / "drop.sqlite")
    gateway = IdempotentMockGateway(path)
    try:
        gateway.prepare(order)
        missed = ScriptedEquityBroker(["timeout_drop", "ack"], wal_path=path)
        first = gateway.submit_mock(order.client_order_id, missed)
        assert first.status == "DETAIL_ABSENT"
        assert first.code == -1007
        assert list(missed.orders) == []
        second = gateway.submit_mock(order.client_order_id, missed)
        assert second.status == "ACKNOWLEDGED"
        assert second.detail_queried is False
        assert missed.place_ids == [order.client_order_id, order.client_order_id]
        assert missed.detail_ids == [order.client_order_id]
        assert list(missed.orders) == [order.client_order_id]
        assert gateway.attempts_of(order.client_order_id) == 2
    finally:
        gateway.close()

    unexpected_path = str(tmp_path / "unexpected.sqlite")
    unexpected = IdempotentMockGateway(unexpected_path)
    try:
        unexpected.prepare(order)
        broker = ScriptedEquityBroker(["unexpected_accept"], wal_path=unexpected_path)
        outcome = unexpected.submit_mock(order.client_order_id, broker)
        assert outcome.status == "DETAIL_CONFIRMED"
        assert outcome.code == -1006
        assert broker.place_ids == [order.client_order_id]
        assert list(broker.orders) == [order.client_order_id]
    finally:
        unexpected.close()


def test_a_crash_after_prepare_queries_detail_and_does_not_place(tmp_path: Path) -> None:
    order = _prepared()
    path = tmp_path / "crash.sqlite"
    gateway = IdempotentMockGateway(str(path))
    gateway.prepare(order)
    gateway.close()
    with sqlite3.connect(path) as db:
        assert str(db.execute("PRAGMA journal_mode").fetchone()[0]).lower() == "wal"
        db.execute(
            "UPDATE order_prepare_wal SET status = 'SUBMITTING' WHERE client_order_id = ?",
            (order.client_order_id,),
        )
        db.commit()
    resumed = IdempotentMockGateway(str(path))
    try:
        assert resumed.prepare(order) == order.client_order_id
        broker = ScriptedEquityBroker([])
        broker.orders[order.client_order_id] = BrokerOrder(
            order.client_order_id,
            order.draft.symbol,
            order.draft.price or Decimal(0),
            order.draft.quantity or Decimal(0),
        )
        outcome = resumed.submit_mock(order.client_order_id, broker)
        assert broker.place_ids == []
        assert broker.detail_ids == [order.client_order_id]
        assert outcome.status == "DETAIL_CONFIRMED"
        assert outcome.place_attempts == 0
        with pytest.raises(LiveTradingDisabledError):
            resumed.live_place()
        assert broker.place_ids == []
    finally:
        resumed.close()


def test_short_ttl_and_a_future_asof_never_reach_a_price() -> None:
    short = _intent(valid_for=timedelta(minutes=5))
    assert LimitOptimizer(_flat_model()).optimize(
        short, _book(), _whole_filter(), _clock()
    ).reason == "TTL_SHORTER_THAN_HORIZON"
    ahead = _intent()
    ahead = ahead.model_copy(update={"data_asof_utc": WHEN + timedelta(seconds=1)})
    assert LimitOptimizer(_flat_model()).optimize(
        ahead, _book(), _whole_filter(), _clock()
    ).reason == "LOOKAHEAD_ASOF"
    blocked = _intent(reasons=["DATA_STALE"])
    refused = LimitOptimizer(_flat_model()).optimize(blocked, _book(), _whole_filter(), _clock())
    assert refused.reason == "DATA_STALE"
    assert refused.scores == ()
    with pytest.raises(ValueError, match="1, 2, or 3"):
        LimitOptimizer(_flat_model(), horizon_bars=4)
    with pytest.raises(ValueError, match="timeout code"):
        ExchangeTimeout(-1000, "NO")


def _utilities(scores: tuple[CandidateScore, ...]) -> list[Decimal]:
    return [row.utility for row in scores if row.utility is not None]


def _scored(tier: str, price: str, utility: str) -> CandidateScore:
    return CandidateScore(
        tier,
        Decimal(price),
        Decimal(1),
        Decimal("0.5"),
        Decimal(1),
        Decimal(0),
        Decimal(0),
        Decimal(utility),
        None,
        "",
    )


def _flat_model() -> _Table:
    return _Table({tier: Decimal("0.5") for tier in ("passive", "retest", "aggressive")})


def _prepared() -> object:
    result = LimitOptimizer(_flat_model()).optimize(_intent(), _book(), _whole_filter(), _clock())
    assert result.order is not None
    return result.order


def _clock() -> EntryClock:
    return EntryClock(now_et=WHEN, equity=Decimal("1600"), hwm=Decimal("1600"))


def _book() -> QuoteBook:
    return QuoteBook(
        bid=Decimal("149"),
        ask=Decimal("150"),
        reference=Decimal("150"),
        multiplier_down=Decimal("0.8"),
        multiplier_up=Decimal("1.2"),
        structure=Decimal("148.50"),
    )


def _whole_filter() -> SymbolFilter:
    return SymbolFilter(
        step_size=Decimal(1),
        min_qty=Decimal(1),
        min_notional=Decimal(1),
        fractional_limit_allowed=False,
    )


def _intent(
    *,
    ceiling: str = "150",
    stop: str = "148",
    take: str = "165",
    valid_for: timedelta = timedelta(minutes=10),
    reasons: list[str] | None = None,
) -> TradeIntent:
    price = Decimal(ceiling)
    return TradeIntent(
        intent_id="s11",
        created_at_utc=WHEN,
        data_asof_utc=WHEN,
        symbol="XLK",
        strategy_id="S1",
        model_version="stage11",
        regime="TREND_UP",
        features_hash="stage11",
        vwap_intact=True,
        signal_valid_until=WHEN + valid_for,
        direction=Direction.LONG,
        p_win_calibrated=Decimal("0.50"),
        p_sl_calibrated=Decimal("0.30"),
        p_timeout_calibrated=Decimal("0.20"),
        expected_return_bps=Decimal("20"),
        expected_net_bps=Decimal("20"),
        prediction_interval=(Decimal("1"), Decimal("30")),
        candidate_limit_prices=[price],
        p_fill_by_price_horizon=[
            FillHorizon(price=price, horizon_seconds=600, p_fill=Decimal("0.40"))
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
        data_freshness_ms=0,
    )


def _session(lows: tuple[str, ...], start: datetime | None = None) -> tuple[ClosedBar, ...]:
    opened = start or datetime(2024, 11, 1, 10, 0, tzinfo=NY)
    rows: list[ClosedBar] = []
    for index, low in enumerate(lows):
        begin = opened + timedelta(minutes=5 * index)
        rows.append(
            ClosedBar(
                symbol="XLK",
                start=begin,
                end=begin + timedelta(minutes=5),
                open=Decimal("100"),
                high=Decimal("101"),
                low=Decimal(low),
                close=Decimal("100.50"),
                volume=Decimal("1000"),
                timeframe="5m",
            )
        )
    return tuple(rows)
