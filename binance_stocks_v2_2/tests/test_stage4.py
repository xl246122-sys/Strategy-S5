"""Stage 4: no same-bar signals, penetration fills, and pessimistic stop resolution."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from bstocks_v2.constitution import live_trade_enabled
from bstocks_v2.constitution.guard import CLIENT_ORDER_ID
from bstocks_v2.contracts.errors import ConstitutionBypassError, LiveTradingDisabledError
from bstocks_v2.contracts.models import TradeIntent
from bstocks_v2.research_backtest.backtrader_ref import loose_limit_fill
from bstocks_v2.research_backtest.errors import LookaheadError
from bstocks_v2.research_backtest.fixtures import (
    SAMPLES,
    bar,
    make_intent,
    run_sample,
)
from bstocks_v2.research_backtest.models import EngineConfig, FillMode
from bstocks_v2.research_backtest.replay import OrderReplayEngine
from bstocks_v2.research_backtest.tape import BarTape, CausalView, ClosedBar

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "src" / "bstocks_v2" / "research_backtest"


def _run(
    bars: list[ClosedBar],
    intent: TradeIntent,
    mode: FillMode = FillMode.CONSERVATIVE,
    **config: object,
) -> object:
    atr = config.pop("atr", None)
    engine = OrderReplayEngine(
        EngineConfig(mode=mode, **config),  # type: ignore[arg-type]
        atr=atr,  # type: ignore[arg-type]
    )
    return engine.run(BarTape(bars), _emit(intent))


class _Emit:
    def __init__(self, intent: TradeIntent) -> None:
        self.intent = intent

    def on_close(self, view: CausalView, closed: ClosedBar) -> TradeIntent | None:
        if closed.end == self.intent.data_asof_utc and closed.symbol == self.intent.symbol:
            return self.intent
        return None


def _emit(intent: TradeIntent) -> _Emit:
    return _Emit(intent)


def test_five_samples_reconcile_cash_and_the_two_modes() -> None:
    expected = {
        "penetration_then_take_profit": {
            FillMode.CONSERVATIVE: (
                "FILLED",
                Decimal("2"),
                Decimal("100"),
                Decimal("101"),
                "TAKE_PROFIT",
                False,
                Decimal("1.759"),
                Decimal("0"),
            ),
            FillMode.LOOSE: (
                "FILLED",
                Decimal("2"),
                Decimal("100"),
                Decimal("101"),
                "TAKE_PROFIT",
                False,
                Decimal("1.759"),
                Decimal("0"),
            ),
        },
        "touch_is_not_a_fill": {
            FillMode.CONSERVATIVE: (
                "UNFILLED",
                Decimal("0"),
                None,
                None,
                "NONE",
                False,
                Decimal("0"),
                Decimal("2.1588"),
            ),
            FillMode.LOOSE: (
                "FILLED",
                Decimal("2"),
                Decimal("100"),
                Decimal("101"),
                "TAKE_PROFIT",
                False,
                Decimal("1.759"),
                Decimal("0"),
            ),
        },
        "partial_queue": {
            FillMode.CONSERVATIVE: (
                "PARTIAL",
                Decimal("1"),
                Decimal("100"),
                Decimal("101"),
                "TAKE_PROFIT",
                False,
                Decimal("0.8795"),
                Decimal("0.929475"),
            ),
            FillMode.LOOSE: (
                "FILLED",
                Decimal("2"),
                Decimal("100"),
                Decimal("101"),
                "TAKE_PROFIT",
                False,
                Decimal("1.759"),
                Decimal("0"),
            ),
        },
        "one_minute_take_profit_first": {
            FillMode.CONSERVATIVE: (
                "FILLED",
                Decimal("2"),
                Decimal("100"),
                Decimal("101"),
                "TAKE_PROFIT",
                False,
                Decimal("1.759"),
                Decimal("0"),
            ),
            FillMode.LOOSE: (
                "FILLED",
                Decimal("2"),
                Decimal("100"),
                Decimal("101"),
                "TAKE_PROFIT",
                False,
                Decimal("1.759"),
                Decimal("0"),
            ),
        },
        "ambiguous_minute_stops_out": {
            FillMode.CONSERVATIVE: (
                "FILLED",
                Decimal("2"),
                Decimal("100"),
                Decimal("98"),
                "STOP",
                True,
                Decimal("-4.238"),
                Decimal("0"),
            ),
            FillMode.LOOSE: (
                "FILLED",
                Decimal("2"),
                Decimal("100"),
                Decimal("101"),
                "TAKE_PROFIT",
                False,
                Decimal("1.759"),
                Decimal("0"),
            ),
        },
    }
    for name in SAMPLES:
        for mode, spec in expected[name].items():
            result = run_sample(name, mode)
            row = result.records[0]
            status, qty, entry, exit_price, reason, ambiguous, net, missed = spec
            assert row.status == status
            assert row.filled_qty == qty
            assert row.entry_price == entry
            assert row.exit_price == exit_price
            assert row.exit_reason == reason
            assert row.ambiguous_bar_sl_assumed is ambiguous
            assert row.net_pnl == net
            assert row.opportunity_cost_usdc == missed
            assert row.wallet_type == "CARD"
            assert row.tokenize is False
            if row.client_order_id is not None:
                assert CLIENT_ORDER_ID.fullmatch(row.client_order_id)
            if row.open_qty == 0 and row.filled_qty > 0:
                assert result.ending_cash - result.starting_cash == row.net_pnl
                assert (
                    row.gross_pnl - row.entry_commission - row.exit_commission - row.spread_cost
                    == row.net_pnl
                )
            else:
                assert result.ending_cash == result.starting_cash


def test_one_minute_drill_changes_the_exit_clock() -> None:
    conservative = run_sample("one_minute_take_profit_first", FillMode.CONSERVATIVE).records[0]
    loose = run_sample("one_minute_take_profit_first", FillMode.LOOSE).records[0]
    assert conservative.exit_bar_end == datetime(2024, 11, 1, 14, 41, tzinfo=UTC)
    assert loose.exit_bar_end == datetime(2024, 11, 1, 14, 45, tzinfo=UTC)
    assert conservative.entry_bar_end == loose.entry_bar_end
    assert conservative.entry_bar_end is not None
    assert conservative.entry_bar_end > conservative.signal_bar_end


def test_signal_bar_cannot_fill_even_when_it_penetrates() -> None:
    signal = bar(14, 30, "100.10", "100.20", "99.00", "100.00")
    later = bar(14, 35, "100.40", "100.50", "100.30", "100.40")
    intent = make_intent(signal.end, later.end, intent_id="no-cheat")
    for mode in FillMode:
        result = _run([signal, later], intent, mode)
        assert result.records[0].filled_qty == 0
        assert not any(event.kind == "FILL" and event.time == signal.end for event in result.events)


def test_exact_boundary_is_a_full_fill_and_a_touch_is_not() -> None:
    signal = bar(14, 30, "100.10", "100.20", "100.05", "100.10")
    boundary = bar(14, 35, "100.00", "100.05", "99.98", "100.00")
    intent = make_intent(signal.end, boundary.end, intent_id="boundary")
    assert _run([signal, boundary], intent).records[0].filled_qty == Decimal("2")
    touch = bar(14, 35, "100.20", "100.40", "100.00", "100.30")
    missed = make_intent(signal.end, touch.end, intent_id="touch-only")
    assert _run([signal, touch], missed).records[0].filled_qty == Decimal("0")


def test_conservative_buy_does_not_take_the_better_open() -> None:
    signal = bar(14, 30, "100.10", "100.20", "100.05", "100.10")
    gap = bar(14, 35, "99.90", "100.00", "99.90", "99.95")
    intent = make_intent(signal.end, gap.end, intent_id="gap")
    conservative = _run([signal, gap], intent).records[0]
    loose = _run([signal, gap], intent, FillMode.LOOSE).records[0]
    assert conservative.entry_price == Decimal("100")
    assert loose.entry_price == Decimal("99.90")


def test_entry_bar_through_the_stop_is_a_loss_only_when_conservative() -> None:
    signal = bar(14, 30, "100.10", "100.20", "100.05", "100.10")
    pierced = bar(14, 35, "100.00", "100.20", "97.00", "98.00")
    rally = bar(14, 40, "100.40", "101.05", "100.30", "100.90")
    intent = make_intent(signal.end, rally.end, intent_id="pierced")
    conservative = _run([signal, pierced, rally], intent).records[0]
    loose = _run([signal, pierced, rally], intent, FillMode.LOOSE).records[0]
    assert conservative.exit_reason == "STOP"
    assert conservative.ambiguous_bar_sl_assumed is True
    assert conservative.net_pnl == Decimal("-4.238")
    assert loose.exit_reason == "TAKE_PROFIT"
    assert loose.net_pnl == Decimal("1.759")


def test_later_minute_stop_is_not_marked_ambiguous() -> None:
    signal = bar(14, 30, "100.10", "100.20", "100.05", "100.10")
    minutes = [
        bar(14, 35, "100.00", "100.10", "99.97", "100.00", timeframe="1m"),
        bar(14, 36, "100.00", "100.10", "97.50", "98.00", timeframe="1m"),
        bar(14, 37, "98.00", "98.40", "97.80", "98.10", timeframe="1m"),
        bar(14, 38, "98.10", "98.30", "97.90", "98.00", timeframe="1m"),
        bar(14, 39, "98.00", "98.20", "97.70", "97.90", timeframe="1m"),
    ]
    parent = bar(14, 35, "100.00", "100.10", "97.50", "97.90")
    intent = make_intent(signal.end, parent.end, intent_id="sequenced-stop")
    row = _run([signal, parent, *minutes], intent).records[0]
    assert row.exit_reason == "STOP"
    assert row.ambiguous_bar_sl_assumed is False
    assert row.exit_price == Decimal("98")


def test_missing_minutes_assume_the_stop() -> None:
    signal = bar(14, 30, "100.10", "100.20", "100.05", "100.10")
    entry = bar(14, 35, "100.00", "100.05", "99.97", "100.02")
    conflict = bar(14, 40, "100.20", "101.05", "97.50", "99.00")
    intent = make_intent(signal.end, conflict.end, intent_id="no-minutes")
    row = _run([signal, entry, conflict], intent).records[0]
    assert row.exit_reason == "STOP"
    assert row.ambiguous_bar_sl_assumed is True


def test_stop_minute_before_the_target_is_the_exit() -> None:
    signal = bar(14, 30, "100.10", "100.20", "100.05", "100.10")
    entry = bar(14, 35, "100.00", "100.05", "99.97", "100.02")
    minutes = [
        bar(14, 40, "100.00", "100.20", "97.80", "98.20", timeframe="1m"),
        bar(14, 41, "98.40", "101.20", "98.20", "101.00", timeframe="1m"),
        bar(14, 42, "101.00", "101.10", "100.40", "100.60", timeframe="1m"),
        bar(14, 43, "100.60", "100.80", "100.20", "100.40", timeframe="1m"),
        bar(14, 44, "100.40", "100.50", "100.10", "100.20", timeframe="1m"),
    ]
    parent = bar(14, 40, "100.00", "101.20", "97.80", "100.20")
    intent = make_intent(signal.end, parent.end, intent_id="stop-first")
    row = _run([signal, entry, parent, *minutes], intent).records[0]
    assert row.exit_reason == "STOP"
    assert row.ambiguous_bar_sl_assumed is False
    assert row.exit_price == Decimal("98")


def test_unclosed_bar_and_future_excursion_are_invisible() -> None:
    tape, _strategy = SAMPLES["penetration_then_take_profit"]()
    signal_end = datetime(2024, 11, 1, 14, 35, tzinfo=UTC)
    view = tape.view(signal_end)
    assert all(closed.end <= signal_end for closed in view.closed_bars("SPY"))
    future = next(closed for closed in tape.bars if closed.end > signal_end)
    with pytest.raises(LookaheadError):
        view.demand(future)
    with pytest.raises(LookaheadError):
        view.excursion()


def test_audit_stays_sealed_while_the_strategy_is_deciding() -> None:
    tape, _strategy = SAMPLES["penetration_then_take_profit"]()
    engine = OrderReplayEngine(EngineConfig(mode=FillMode.CONSERVATIVE))

    class _Sneak:
        def on_close(self, view: CausalView, closed: ClosedBar) -> None:
            engine.sealed_audit()

    with pytest.raises(LookaheadError):
        engine.run(tape, _Sneak())


def test_future_asof_is_rejected_and_logged() -> None:
    signal = bar(14, 30, "100.10", "100.20", "100.05", "100.10")
    intent = make_intent(signal.end, signal.end + timedelta(minutes=10), intent_id="future")
    leaked = intent.model_copy(update={"data_asof_utc": signal.end + timedelta(minutes=5)})

    class _Leak:
        def on_close(self, view: CausalView, closed: ClosedBar) -> TradeIntent | None:
            if closed.end == signal.end:
                return leaked
            return None

    result = OrderReplayEngine(EngineConfig(mode=FillMode.CONSERVATIVE)).run(
        BarTape([signal]), _Leak()
    )
    assert result.records[0].status == "RISK_REJECTED"
    assert result.records[0].rejection_reasons == ("LOOKAHEAD_ASOF",)


def test_session_gates_and_the_flatten_print() -> None:
    signal = bar(19, 40, "100.10", "100.20", "100.05", "100.10")
    late = make_intent(signal.end, signal.end + timedelta(minutes=5), intent_id="late")
    assert _run([signal], late).records[0].rejection_reasons == ("NON_RTH",)
    allowed = bar(19, 35, "100.10", "100.20", "100.05", "100.10")
    opened = make_intent(allowed.end, allowed.end + timedelta(minutes=10), intent_id="still-open")
    assert _run([allowed], opened).records[0].status == "UNFILLED"

    holiday = bar(14, 30, "100.10", "100.20", "100.05", "100.10", year=2025, month=1, day=9)
    closed = make_intent(holiday.end, holiday.end + timedelta(minutes=10), intent_id="holiday")
    assert _run([holiday], closed).records[0].rejection_reasons == ("NON_RTH",)
    saturday = bar(14, 30, "100.10", "100.20", "100.05", "100.10", day=2)
    weekend = make_intent(saturday.end, saturday.end + timedelta(minutes=10), intent_id="saturday")
    assert _run([saturday], weekend).records[0].rejection_reasons == ("NON_RTH",)

    half_late = bar(17, 40, "100.10", "100.20", "100.05", "100.10", day=29)
    half_intent = make_intent(
        half_late.end, half_late.end + timedelta(minutes=5), intent_id="half-late"
    )
    assert _run([half_late], half_intent).records[0].rejection_reasons == ("NON_RTH",)

    entry_signal = bar(18, 30, "100.10", "100.20", "100.05", "100.10")
    entry = bar(18, 35, "100.00", "100.08", "99.97", "100.02")
    flatten = bar(19, 55, "100.50", "100.80", "100.40", "100.60")
    held = make_intent(entry_signal.end, flatten.end, intent_id="flatten")
    flat = _run([entry_signal, entry, flatten], held).records[0]
    assert flat.exit_reason == "FLATTEN"
    assert flat.exit_price == Decimal("100.50")
    assert flat.net_pnl == Decimal("0.7595")

    half_signal = bar(17, 30, "100.10", "100.20", "100.05", "100.10", day=29)
    half_entry = bar(17, 35, "100.00", "100.08", "99.97", "100.02", day=29)
    half_flat = bar(17, 55, "100.40", "100.60", "100.20", "100.30", day=29)
    half_held = make_intent(half_signal.end, half_flat.end, intent_id="half-flat")
    assert _run([half_signal, half_entry, half_flat], half_held).records[0].exit_reason == "FLATTEN"


def test_latency_skips_the_bar_already_in_progress() -> None:
    signal = bar(14, 30, "100.10", "100.20", "100.05", "100.10")
    pierced = bar(14, 35, "100.00", "100.10", "99.90", "100.00")
    missed = bar(14, 40, "100.40", "100.50", "100.30", "100.40")
    intent = make_intent(signal.end, missed.end, intent_id="latency")
    result = _run([signal, pierced, missed], intent, latency=timedelta(seconds=60))
    assert result.records[0].filled_qty == Decimal("0")


def test_ttl_cancels_before_a_later_penetration() -> None:
    signal = bar(14, 30, "100.10", "100.20", "100.05", "100.10")
    quiet = bar(14, 35, "100.40", "100.50", "100.30", "100.40")
    pierced = bar(14, 40, "100.00", "100.10", "99.90", "100.00")
    intent = make_intent(signal.end, quiet.end, intent_id="ttl")
    row = _run([signal, quiet, pierced], intent).records[0]
    assert row.status == "UNFILLED"
    assert row.cancel_reason == "TTL"


def test_timeout_sells_the_open() -> None:
    signal = bar(14, 30, "100.10", "100.20", "100.05", "100.10")
    entry = bar(14, 35, "100.00", "100.05", "99.97", "100.02")
    quiet = bar(14, 40, "100.20", "100.30", "100.10", "100.20")
    expiry = bar(14, 45, "100.60", "100.80", "100.40", "100.70")
    intent = make_intent(
        signal.end,
        expiry.end,
        intent_id="timeout",
        holding=300,
        take_profit="110",
    )
    row = _run([signal, entry, quiet, expiry], intent).records[0]
    assert row.exit_reason == "TIMEOUT"
    assert row.exit_price == Decimal("100.60")
    assert row.net_pnl == Decimal("0.9594")


def test_a_short_tape_does_not_invent_an_exit_price() -> None:
    signal = bar(14, 30, "100.10", "100.20", "100.05", "100.10")
    entry = bar(14, 35, "100.00", "100.05", "99.97", "100.02")
    intent = make_intent(
        signal.end, entry.end + timedelta(hours=1), intent_id="open", take_profit="110"
    )
    result = _run([signal, entry], intent)
    row = result.records[0]
    assert row.exit_price is None
    assert row.open_qty == Decimal("2")
    assert result.ending_cash == Decimal("1399.88")


def test_fractional_limit_falls_back_and_a_sub_share_is_rejected() -> None:
    signal = bar(14, 30, "250.00", "250.20", "249.80", "250.00")
    intent = make_intent(
        signal.end,
        signal.end + timedelta(minutes=10),
        intent_id="whole",
        limit="250",
        stop="248",
        take_profit="260",
    )
    row = _run([signal], intent).records[0]
    assert row.ordered_qty == Decimal("1")
    assert "486442" in row.adjustments
    expensive = bar(14, 30, "400.00", "400.20", "399.80", "400.00")
    too_big = make_intent(
        expensive.end,
        expensive.end + timedelta(minutes=10),
        intent_id="too-big",
        limit="400",
        stop="398",
        take_profit="410",
    )
    assert _run([expensive], too_big).records[0].rejection_reasons == (
        "WHOLE_SHARE_EXCEEDS_RISK_OR_CAP",
    )


def test_notional_friction_hwm_and_slot_gates() -> None:
    signal = bar(14, 30, "100.10", "100.20", "100.05", "100.10")
    small = make_intent(signal.end, signal.end + timedelta(minutes=5), intent_id="small", stop="96")
    assert _run([signal], small).records[0].rejection_reasons == ("NOTIONAL_TOO_SMALL",)
    tight = make_intent(
        signal.end,
        signal.end + timedelta(minutes=5),
        intent_id="tight",
        take_profit="100.10",
    )
    assert _run([signal], tight).records[0].rejection_reasons == ("FRICTION",)
    locked = _run(
        [signal],
        make_intent(signal.end, signal.end + timedelta(minutes=5), intent_id="hwm"),
        initial_equity=Decimal("1472"),
        hwm_equity=Decimal("1600"),
    )
    assert locked.records[0].rejection_reasons == ("HWM_LOCK",)
    full = _run(
        [signal],
        make_intent(signal.end, signal.end + timedelta(minutes=5), intent_id="slots"),
        occupied_slots=5,
    )
    assert full.records[0].rejection_reasons == ("MAX_POSITIONS",)


def test_partial_commission_is_not_the_tc_minus_fn_trap() -> None:
    row = run_sample("partial_queue", FillMode.CONSERVATIVE).records[0]
    filled_notional = Decimal("100")
    trapped = (Decimal("2") * Decimal("100") + Decimal("10")) - filled_notional
    assert row.entry_commission == Decimal("0.05")
    assert row.entry_commission != trapped


def test_atr_and_spread_widths() -> None:
    signal = bar(14, 30, "100.10", "100.20", "100.05", "100.10")
    entry = bar(14, 35, "100.00", "100.05", "99.96", "100.00")
    intent = make_intent(signal.end, entry.end, intent_id="atr")
    result = _run(
        [signal, entry],
        intent,
        penetration="atr",
        atr={(intent.symbol, signal.end): Decimal("1")},
    )
    assert result.records[0].delta == Decimal("0.05")
    assert result.records[0].filled_qty == Decimal("1")
    spread = _run([signal, entry], intent, penetration="spread").records[0]
    assert spread.delta == Decimal("0.02")
    assert spread.filled_qty == Decimal("2")


def test_three_decimal_price_and_float_inputs_are_rejected() -> None:
    signal = bar(14, 30, "100.10", "100.20", "100.05", "100.10")
    base = make_intent(signal.end, signal.end + timedelta(minutes=5), intent_id="ticks")
    data = base.model_dump()
    data["candidate_limit_prices"] = [Decimal("100.001")]
    data["entry_price_ceiling"] = Decimal("100.001")
    data["stop_price"] = Decimal("99.00")
    intent = TradeIntent.model_construct(**data)
    assert _run([signal], intent).records[0].rejection_reasons == ("PRICE_PRECISION",)
    with pytest.raises(ValidationError):
        make_intent(signal.end, signal.end + timedelta(minutes=5), intent_id="bad", limit="100.001")
    with pytest.raises(ValueError):
        EngineConfig(mode=FillMode.LOOSE, commission_rate=0.1)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        ClosedBar(
            symbol="SPY",
            start=signal.start,
            end=signal.end,
            open=100.0,  # type: ignore[arg-type]
            high=Decimal("100.20"),
            low=Decimal("100.00"),
            close=Decimal("100.10"),
            volume=Decimal("1"),
            timeframe="5m",
        )


def test_config_cannot_enable_live_trading_or_a_larger_position() -> None:
    engine = OrderReplayEngine(EngineConfig(mode=FillMode.CONSERVATIVE))
    with pytest.raises(ConstitutionBypassError):
        engine.reject_external_config({"live_trade_enabled": True})
    with pytest.raises(ConstitutionBypassError):
        EngineConfig(mode=FillMode.CONSERVATIVE, max_position_fraction=Decimal("0.25"))
    EngineConfig(mode=FillMode.CONSERVATIVE, max_position_fraction=Decimal("0.10"))
    assert live_trade_enabled() is False


def test_replay_package_has_no_order_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    files = [path for path in PACKAGE.glob("*.py") if path.name != "backtrader_ref.py"]
    text = "\n".join(path.read_text(encoding="utf-8") for path in files)
    assert "/order/place" not in text
    assert "float(" not in text
    monkeypatch.setattr(
        "bstocks_v2.research_backtest.replay.live_trade_enabled",
        lambda: True,
    )
    with pytest.raises(LiveTradingDisabledError):
        OrderReplayEngine(EngineConfig(mode=FillMode.LOOSE))


def test_loose_entries_match_backtrader() -> None:
    signal = bar(14, 30, "100.00", "100.25", "99.75", "100.00")
    touch = bar(14, 35, "100.25", "100.50", "99.75", "100.00")
    gap = bar(14, 35, "99.50", "100.00", "99.50", "99.75")
    miss = bar(14, 35, "100.50", "100.75", "100.25", "100.50")
    cases = (
        ([signal, touch], Decimal("100")),
        ([signal, gap], Decimal("99.50")),
        ([signal, miss], None),
    )
    for bars, expected in cases:
        intent = make_intent(signal.end, bars[-1].end, intent_id=f"bt-{expected}")
        row = _run(
            bars,
            intent,
            FillMode.LOOSE,
            commission_rate=Decimal("0"),
            half_spread=Decimal("0"),
        ).records[0]
        reference = loose_limit_fill(bars, signal_len=1, limit=Decimal("100"), size=2)
        assert reference == expected
        assert row.entry_price == expected
