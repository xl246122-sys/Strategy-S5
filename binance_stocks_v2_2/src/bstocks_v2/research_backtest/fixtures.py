"""Five hand-built sessions used to reconcile every cash field."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from bstocks_v2.contracts.models import Direction, FillHorizon, TradeIntent
from bstocks_v2.research_backtest.models import EngineConfig, FillMode, ReplayResult
from bstocks_v2.research_backtest.replay import DecisionStrategy, OrderReplayEngine
from bstocks_v2.research_backtest.tape import BarTape, CausalView, ClosedBar


def bar(
    hour: int,
    minute: int,
    opened: str,
    high: str,
    low: str,
    closed: str,
    *,
    timeframe: str = "5m",
    day: int = 1,
    month: int = 11,
    year: int = 2024,
    symbol: str = "SPY",
) -> ClosedBar:
    start = datetime(year, month, day, hour, minute, tzinfo=UTC)
    width = 5 if timeframe == "5m" else 1
    return ClosedBar(
        symbol=symbol,
        start=start,
        end=start + timedelta(minutes=width),
        open=Decimal(opened),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(closed),
        volume=Decimal("1000"),
        timeframe=timeframe,
    )


def make_intent(
    end: datetime,
    valid: datetime,
    *,
    intent_id: str,
    holding: int = 3600,
    limit: str = "100",
    stop: str = "98",
    take_profit: str = "101",
    risk: str = "4",
    symbol: str = "SPY",
    fallback: str = "100000",
) -> TradeIntent:
    price = Decimal(limit)
    return TradeIntent(
        intent_id=intent_id,
        created_at_utc=end,
        data_asof_utc=end,
        symbol=symbol,
        strategy_id="FIXTURE",
        model_version="stage4",
        regime="UNKNOWN",
        features_hash="stage4",
        vwap_intact=True,
        signal_valid_until=valid,
        direction=Direction.LONG,
        p_win_calibrated=Decimal("0.50"),
        p_sl_calibrated=Decimal("0.30"),
        p_timeout_calibrated=Decimal("0.20"),
        expected_return_bps=Decimal("20"),
        expected_net_bps=Decimal("8"),
        prediction_interval=(Decimal("1"), Decimal("3")),
        candidate_limit_prices=[price],
        p_fill_by_price_horizon=[
            FillHorizon(price=price, horizon_seconds=300, p_fill=Decimal("0.40"))
        ],
        entry_price_ceiling=price,
        stop_price=Decimal(stop),
        take_profit_price=Decimal(take_profit),
        max_holding_seconds=holding,
        risk_budget_usdc=Decimal(risk),
        min_viable_notional_usdc=Decimal("120"),
        expected_cost_usdc=Decimal("0.24"),
        requires_fractional=False,
        whole_share_fallback_qty=Decimal(fallback),
        data_feed="sip",
        data_freshness_ms=0,
    )


class OneShot:
    def __init__(self, intent: TradeIntent) -> None:
        self.intent = intent

    def on_close(self, view: CausalView, closed: ClosedBar) -> TradeIntent | None:
        if closed.symbol == self.intent.symbol and closed.end == self.intent.data_asof_utc:
            return self.intent
        return None


def _tape(*bars: ClosedBar) -> BarTape:
    return BarTape(bars)


def penetration_then_take_profit() -> tuple[BarTape, DecisionStrategy]:
    signal = bar(14, 30, "100.10", "100.20", "100.05", "100.10")
    entry = bar(14, 35, "100.00", "100.08", "99.97", "100.02")
    exit_bar = bar(14, 40, "100.40", "101.05", "100.30", "100.90")
    intent = make_intent(signal.end, exit_bar.end, intent_id="penetration-tp")
    return _tape(signal, entry, exit_bar), OneShot(intent)


def touch_is_not_a_fill() -> tuple[BarTape, DecisionStrategy]:
    signal = bar(14, 30, "100.10", "100.20", "99.90", "100.00")
    entry = bar(14, 35, "100.20", "100.40", "100.00", "100.30")
    rally = bar(14, 40, "100.50", "101.20", "100.40", "101.00")
    intent = make_intent(signal.end, entry.end, intent_id="touch", holding=1800)
    return _tape(signal, entry, rally), OneShot(intent)


def partial_queue() -> tuple[BarTape, DecisionStrategy]:
    signal = bar(14, 30, "100.10", "100.20", "100.05", "100.10")
    entry = bar(14, 35, "100.05", "100.10", "99.99", "100.00")
    exit_bar = bar(14, 40, "100.50", "101.05", "100.40", "100.80")
    intent = make_intent(signal.end, entry.end, intent_id="partial")
    return _tape(signal, entry, exit_bar), OneShot(intent)


def _minutes(rows: list[tuple[int, str, str, str, str]]) -> list[ClosedBar]:
    return [
        bar(14, 40 + offset, opened, high, low, closed, timeframe="1m")
        for offset, opened, high, low, closed in rows
    ]


def one_minute_take_profit_first() -> tuple[BarTape, DecisionStrategy]:
    signal = bar(14, 30, "100.10", "100.20", "100.05", "100.10")
    entry = bar(14, 35, "100.00", "100.05", "99.97", "100.02")
    minutes = _minutes(
        [
            (0, "100.40", "101.05", "100.30", "100.90"),
            (1, "100.80", "100.90", "100.20", "100.40"),
            (2, "100.20", "100.30", "99.00", "99.20"),
            (3, "99.10", "99.40", "98.00", "98.20"),
            (4, "98.20", "98.50", "97.80", "98.00"),
        ]
    )
    parent = bar(14, 40, "100.40", "101.05", "97.80", "98.00")
    intent = make_intent(signal.end, parent.end, intent_id="tp-first")
    return _tape(signal, entry, parent, *minutes), OneShot(intent)


def ambiguous_minute_stops_out() -> tuple[BarTape, DecisionStrategy]:
    signal = bar(14, 30, "100.10", "100.20", "100.05", "100.10")
    entry = bar(14, 35, "100.00", "100.05", "99.97", "100.02")
    minutes = _minutes(
        [
            (0, "100.10", "101.10", "97.90", "99.00"),
            (1, "99.00", "99.40", "98.40", "98.80"),
            (2, "98.80", "99.00", "98.20", "98.50"),
            (3, "98.50", "98.80", "98.10", "98.30"),
            (4, "98.30", "98.60", "97.90", "98.10"),
        ]
    )
    parent = bar(14, 40, "100.10", "101.10", "97.90", "98.10")
    intent = make_intent(signal.end, parent.end, intent_id="ambiguous-sl")
    return _tape(signal, entry, parent, *minutes), OneShot(intent)


SAMPLES = {
    "penetration_then_take_profit": penetration_then_take_profit,
    "touch_is_not_a_fill": touch_is_not_a_fill,
    "partial_queue": partial_queue,
    "one_minute_take_profit_first": one_minute_take_profit_first,
    "ambiguous_minute_stops_out": ambiguous_minute_stops_out,
}


def run_sample(name: str, mode: FillMode) -> ReplayResult:
    tape, strategy = SAMPLES[name]()
    return OrderReplayEngine(EngineConfig(mode=mode)).run(tape, strategy)
