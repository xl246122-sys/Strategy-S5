"""Causal S5 research signals. Synthetic rows are not a market-edge claim."""

from dataclasses import replace
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from bstocks_v2.contracts.errors import LiveTradingDisabledError
from bstocks_v2.feature_store.errors import LookaheadError
from bstocks_v2.signal_arbitrator.spec import ADMITTED
from bstocks_v2.strategies.s5.event_gate import NewsSnapshot, NewsState, QuoteSnapshot
from bstocks_v2.strategies.s5.features import (
    ClosedBar,
    PriorDay,
    arm_window,
    clock_in_window,
)
from bstocks_v2.strategies.s5.lock import refuse_live_route
from bstocks_v2.strategies.s5.model import (
    FrequencyModel,
    Sample,
    brier_score,
    expected_calibration_error,
    insufficient_model,
    select_model,
)
from bstocks_v2.strategies.s5.signals import CausalBook, CausalView, ForwardTape, evaluate
from bstocks_v2.strategies.s5.spec import ARM_A, ARM_B, ARM_C, STRATEGY_ID

ET = ZoneInfo("America/New_York")
DAY = date(2025, 1, 15)
DECISION = datetime(2025, 1, 15, 15, 50, tzinfo=ET)
ROOT = Path(__file__).resolve().parents[2] / "src" / "bstocks_v2" / "strategies" / "s5"


def test_future_inputs_are_rejected() -> None:
    view = _view()
    future = view.bars[-1]
    leaked = replace(future, end=future.end + timedelta(seconds=1))
    with pytest.raises(LookaheadError, match="BAR_AFTER_DECISION"):
        evaluate(replace(view, bars=(*view.bars[:-1], leaked)), insufficient_model())
    later_news = NewsSnapshot(NewsState.CLEAR, DECISION + timedelta(minutes=1), "later")
    with pytest.raises(LookaheadError, match="NEWS_AFTER_DECISION"):
        evaluate(replace(view, news=later_news), insufficient_model())
    assert view.quote is not None
    later_quote = replace(view.quote, exchange_ts=DECISION + timedelta(seconds=1))
    with pytest.raises(LookaheadError, match="QUOTE_AFTER_DECISION"):
        evaluate(replace(view, quote=later_quote), insufficient_model())
    leaked_prior = PriorDay(DAY, Decimal("0"), Decimal("0"), Decimal("0"))
    with pytest.raises(LookaheadError, match="PRIOR_NOT_PRIOR"):
        evaluate(replace(view, prior=(*view.prior, leaked_prior)), insufficient_model())
    with pytest.raises(LookaheadError, match="REGIME_AFTER_DECISION"):
        evaluate(replace(view, regime_asof=DECISION + timedelta(seconds=1)), insufficient_model())


def test_missing_news_and_quote_remain_in_the_sample() -> None:
    record = evaluate(
        replace(_view(), news=None, quote=None),
        insufficient_model(),
        ForwardTape((), DECISION + timedelta(hours=3), False),
    )
    assert record.intent is None
    assert record.admitted_level == ""
    assert "NEWS_UNKNOWN" in record.rejection_reasons
    assert "SPREAD_UNKNOWN" in record.rejection_reasons
    assert "DEPTH_UNKNOWN" in record.rejection_reasons
    assert record.path_label == "UNFILLABLE"
    assert record.features_hash
    assert record.bar_ends
    assert record.decision_ts == DECISION
    assert record.screens[0] == ("B0", True)


def test_blocked_regimes_and_a_falling_knife_do_not_admit() -> None:
    blocked = evaluate(replace(_view(), regime="UNKNOWN"), insufficient_model())
    panic = evaluate(replace(_view(), regime="HIGH_VOL_PANIC"), insufficient_model())
    assert blocked.admitted_level == ""
    assert panic.admitted_level == ""
    assert "REGIME_BLOCKED" in blocked.rejection_reasons
    assert "REGIME_BLOCKED" in panic.rejection_reasons
    detached = evaluate(replace(_view(), regime="NOT_ATTACHED"), insufficient_model())
    assert "REGIME_NOT_ATTACHED" in detached.rejection_reasons
    assert "REGIME_BLOCKED" not in detached.rejection_reasons
    knife = evaluate(_knife_view(), insufficient_model())
    assert "FALLING_KNIFE" in knife.rejection_reasons
    assert knife.tape_state == "FALLING_KNIFE"


def test_an_unknown_symbol_is_rejected_without_a_special_case() -> None:
    plain = evaluate(_renamed("ZZZZ"), insufficient_model())
    named = evaluate(_renamed("INTC"), insufficient_model())
    assert plain.rejection_reasons[0] == "NOT_IN_UNIVERSE"
    assert named.rejection_reasons == plain.rejection_reasons
    assert named.admitted_level == ""


def test_arms_do_not_share_a_window() -> None:
    assert arm_window(ARM_A, time(16, 0)) == (time(15, 30), time(15, 54))
    assert arm_window(ARM_A, time(13, 0)) == (time(12, 30), time(12, 54))
    assert arm_window(ARM_B, time(16, 0)) == (time(16, 5), time(17, 0))
    assert arm_window(ARM_C, time(16, 0)) == (time(16, 5), time(19, 30))
    summer = datetime(2024, 11, 1, 15, 40, tzinfo=ET)
    winter = datetime(2024, 11, 5, 15, 40, tzinfo=ET)
    assert clock_in_window(summer, time(15, 30), time(15, 54))
    assert clock_in_window(winter, time(15, 30), time(15, 54))
    early = datetime(2025, 1, 15, 15, 10, tzinfo=ET)
    base = _view()
    assert base.quote is not None
    outside = evaluate(
        replace(
            base,
            decision_ts=early,
            bars=tuple(bar for bar in base.bars if bar.end <= early),
            regime_asof=early,
            quote=replace(base.quote, exchange_ts=early),
            news=NewsSnapshot(NewsState.CLEAR, early, "research-fixture"),
        ),
        insufficient_model(),
    )
    assert "OUTSIDE_WINDOW" in outside.rejection_reasons
    post = _post_view()
    assert evaluate(post, insufficient_model()).arm == ARM_B
    unsupported = evaluate(replace(post, arm=ARM_C), insufficient_model())
    assert "OVERNIGHT_UNSUPPORTED" in unsupported.rejection_reasons
    supported = evaluate(replace(post, arm=ARM_C, overnight_supported=True), insufficient_model())
    assert "OVERNIGHT_UNSUPPORTED" not in supported.rejection_reasons
    assert supported.arm != post.arm


def test_batch_matches_the_stream_and_ignores_a_later_bar() -> None:
    bars = _bars()
    book = CausalBook()
    for bar in bars:
        book.add(bar)
    streamed = replace(_view(), bars=book.asof("SMH", DECISION))
    batched = _view()
    model = insufficient_model()
    left = evaluate(streamed, model)
    right = evaluate(batched, model)
    assert left.features_hash == right.features_hash
    assert left.screens == right.screens
    later = replace(
        bars[-1],
        start=DECISION,
        end=DECISION + timedelta(minutes=5),
        session="RTH",
    )
    book.add(later)
    again = evaluate(replace(_view(), bars=book.asof("SMH", DECISION)), model)
    assert again.features_hash == left.features_hash
    with pytest.raises(LookaheadError, match="BAR_AFTER_DECISION"):
        evaluate(replace(_view(), bars=(*bars, later)), model)
    with pytest.raises(LookaheadError, match="OUT_OF_ORDER"):
        book.add(bars[0])


def test_execution_labels_do_not_treat_a_touch_as_a_fill() -> None:
    view = _view()
    deadline = datetime(2025, 1, 15, 19, 30, tzinfo=ET)
    stopped = _forward(deadline, low=Decimal("93"), high=Decimal("95"))
    assert evaluate(view, insufficient_model(), stopped).path_label == "SL_FIRST"
    touched = _forward(deadline, low=Decimal("94.70"), high=Decimal("95.10"))
    assert evaluate(view, insufficient_model(), touched).path_label == "UNFILLABLE"
    both = ForwardTape(
        (
            _forward_bar(datetime(2025, 1, 15, 16, 5, tzinfo=ET), Decimal("94"), Decimal("95")),
            _forward_bar(datetime(2025, 1, 15, 16, 10, tzinfo=ET), Decimal("93"), Decimal("96.5")),
        ),
        deadline,
        True,
    )
    assert evaluate(view, insufficient_model(), both).path_label == "SL_FIRST"
    target = ForwardTape(
        (
            _forward_bar(datetime(2025, 1, 15, 16, 5, tzinfo=ET), Decimal("94"), Decimal("95")),
            _forward_bar(
                datetime(2025, 1, 15, 16, 10, tzinfo=ET),
                Decimal("94.2"),
                Decimal("96.5"),
            ),
        ),
        deadline,
        True,
    )
    assert evaluate(view, insufficient_model(), target).path_label == "TP_FIRST"
    dark = ForwardTape(
        (_forward_bar(datetime(2025, 1, 15, 16, 5, tzinfo=ET), Decimal("1"), Decimal("1000")),),
        deadline,
        False,
    )
    labeled = evaluate(view, insufficient_model(), dark)
    assert labeled.path_label == "UNFILLABLE"
    assert labeled.features_hash == evaluate(view, insufficient_model(), target).features_hash


def test_a_research_pass_is_not_an_order() -> None:
    record = evaluate(_view(), _edge_model())
    assert record.admitted_level == "B3"
    assert record.rejection_reasons == ()
    assert record.intent is not None
    assert record.routable is False
    assert record.intent.strategy_id == STRATEGY_ID
    assert record.intent.research_arm == ARM_A
    assert record.intent.news_event_state == "NEWS_CLEAR"
    total = (
        record.intent.p_win_calibrated
        + record.intent.p_sl_calibrated
        + record.intent.p_timeout_calibrated
    )
    assert total == 1
    assert STRATEGY_ID not in ADMITTED
    with pytest.raises(LiveTradingDisabledError):
        refuse_live_route("/sapi/v1/equity/order/place")


def test_calibration_stays_on_its_own_split() -> None:
    train = _labeled(date(2025, 1, 15), (40, 20, 10, 10))
    validation = _labeled(date(2025, 12, 1), (8, 8, 8, 8))
    model = select_model(train, validation)
    assert model.kind == "frequency"
    assert model.validation_brier == brier_score(validation, model)
    assert model.validation_ece == expected_calibration_error(validation, model)
    assert model.validation_brier is not None and model.validation_brier > 0
    lockbox = _labeled(date(2026, 6, 1), (30, 30, 30, 30))
    with pytest.raises(ValueError, match="lockbox"):
        select_model(lockbox, validation)
    with pytest.raises(ValueError, match="validation"):
        select_model(validation, validation)
    with pytest.raises(ValueError, match="train"):
        select_model(train, train)


def test_logistic_is_kept_only_after_an_out_of_sample_gain() -> None:
    separated = _separated(date(2025, 1, 15), 40)
    held_out = _separated(date(2025, 12, 1), 10)
    kept = select_model(separated, held_out)
    assert kept.kind == "logistic"
    assert kept.validation_brier is not None
    flat_train = _labeled(date(2025, 1, 15), (40, 40, 40, 40))
    flat_validation = _labeled(date(2025, 12, 1), (10, 10, 10, 10))
    discarded = select_model(flat_train, flat_validation)
    assert discarded.kind == "frequency"
    assert "did not improve" in discarded.note


def test_strategy_sources_have_no_order_route_or_single_name_exception() -> None:
    for path in ROOT.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "INTC" not in text
        assert "execution_gateway" not in text
        assert "/order/place" not in text


def _view() -> CausalView:
    quote = QuoteSnapshot(
        bid=Decimal("94.65"),
        ask=Decimal("94.70"),
        bid_size=Decimal("200"),
        ask_size=Decimal("200"),
        exchange_ts=DECISION,
        prior_spread=Decimal("0.20"),
    )
    return CausalView(
        symbol="SMH",
        arm=ARM_A,
        decision_ts=DECISION,
        bars=_bars(),
        prev_close=Decimal("100"),
        prior=_prior(),
        market_log_return=Decimal("0"),
        sector_log_return=Decimal("0"),
        regime="SIDEWAYS",
        entries_allowed=True,
        regime_asof=DECISION,
        news=NewsSnapshot(NewsState.CLEAR, DECISION, "research-fixture"),
        quote=quote,
        overnight_supported=None,
        rth_close=time(16, 0),
        prior_slot_volumes=tuple(Decimal("10000") for _ in range(20)),
    )


def _knife_view() -> CausalView:
    overrides = {
        71: (Decimal("96"), Decimal("96.2"), Decimal("95.5"), Decimal("95.8")),
        72: (Decimal("95.8"), Decimal("95.8"), Decimal("95.0"), Decimal("95.2")),
        73: (Decimal("95.2"), Decimal("95.2"), Decimal("94.4"), Decimal("94.6")),
        74: (Decimal("94.6"), Decimal("94.6"), Decimal("93.8"), Decimal("94.0")),
        75: (Decimal("94.0"), Decimal("94.0"), Decimal("93.0"), Decimal("93.2")),
    }
    return replace(_view(), bars=_bars(overrides))


def _post_view() -> CausalView:
    post_end = datetime(2025, 1, 15, 16, 10, tzinfo=ET)
    post = ClosedBar(
        symbol="SMH",
        start=datetime(2025, 1, 15, 16, 5, tzinfo=ET),
        end=post_end,
        open=Decimal("94.5"),
        high=Decimal("94.8"),
        low=Decimal("94.2"),
        close=Decimal("94.6"),
        volume=Decimal("1000"),
        session="POST",
    )
    base = _view()
    assert base.quote is not None
    return replace(
        base,
        arm=ARM_B,
        decision_ts=post_end,
        bars=(*_bars(), post),
        regime_asof=post_end,
        quote=replace(base.quote, exchange_ts=post_end),
        news=NewsSnapshot(NewsState.CLEAR, post_end, "research-fixture"),
    )


def _renamed(symbol: str) -> CausalView:
    base = _view()
    bars = tuple(replace(bar, symbol=symbol) for bar in base.bars)
    return replace(base, symbol=symbol, bars=bars)


def _bars(
    overrides: dict[int, tuple[Decimal, Decimal, Decimal, Decimal]] | None = None,
) -> tuple[ClosedBar, ...]:
    if overrides is None:
        overrides = {
            71: (Decimal("95"), Decimal("95.2"), Decimal("94.5"), Decimal("94.8")),
            72: (Decimal("94.8"), Decimal("94.9"), Decimal("94.0"), Decimal("94.2")),
            73: (Decimal("94.2"), Decimal("94.6"), Decimal("94.2"), Decimal("94.4")),
            74: (Decimal("94.4"), Decimal("94.7"), Decimal("94.3"), Decimal("94.5")),
            75: (Decimal("94.5"), Decimal("94.9"), Decimal("94.4"), Decimal("94.7")),
        }
    start = datetime(2025, 1, 15, 9, 30, tzinfo=ET)
    bars: list[ClosedBar] = []
    for index in range(76):
        bar_start = start + timedelta(minutes=5 * index)
        prices = overrides.get(
            index,
            (Decimal("100"), Decimal("100.20"), Decimal("99.80"), Decimal("100")),
        )
        bars.append(
            ClosedBar(
                symbol="SMH",
                start=bar_start,
                end=bar_start + timedelta(minutes=5),
                open=prices[0],
                high=prices[1],
                low=prices[2],
                close=prices[3],
                volume=Decimal("1000"),
                session="RTH",
            )
        )
    return tuple(bars)


def _prior() -> tuple[PriorDay, ...]:
    rows: list[PriorDay] = []
    for index in range(40):
        session = date(2024, 11, 1) + timedelta(days=index)
        market = Decimal(index + 1) / Decimal(100)
        sector = Decimal((index * 3) % 11 + 1) / Decimal(100)
        noise = Decimal("0.01") if index % 2 == 0 else Decimal("-0.01")
        rows.append(PriorDay(session, market + noise, market, sector))
    return tuple(rows)


def _forward(deadline: datetime, low: Decimal, high: Decimal) -> ForwardTape:
    return ForwardTape(
        (_forward_bar(datetime(2025, 1, 15, 16, 5, tzinfo=ET), low, high),),
        deadline,
        True,
    )


def _forward_bar(end: datetime, low: Decimal, high: Decimal) -> ClosedBar:
    close = min(max(low, (low + high) / 2), high)
    opened = close
    return ClosedBar(
        symbol="SMH",
        start=end - timedelta(minutes=5),
        end=end,
        open=opened,
        high=high,
        low=low,
        close=close,
        volume=Decimal("100"),
        session="POST",
    )


def _edge_model() -> FrequencyModel:
    return FrequencyModel(
        kind="frequency",
        note="fixture",
        probabilities=(Decimal("0.70"), Decimal("0.10"), Decimal("0.10"), Decimal("0.10")),
        validation_brier=None,
        validation_ece=None,
        train_rows=80,
    )


def _labeled(session: date, counts: tuple[int, int, int, int]) -> tuple[Sample, ...]:
    labels = ("TP_FIRST", "SL_FIRST", "TIMEOUT", "UNFILLABLE")
    rows: list[Sample] = []
    for label, count in zip(labels, counts, strict=True):
        for _ in range(count):
            rows.append(Sample(session, label, (Decimal("0"), Decimal("-0.04"))))
    return tuple(rows)


def _separated(session: date, count: int) -> tuple[Sample, ...]:
    points = {
        "TP_FIRST": (Decimal("2"), Decimal("-0.08")),
        "SL_FIRST": (Decimal("-2"), Decimal("-0.02")),
        "TIMEOUT": (Decimal("0"), Decimal("0")),
        "UNFILLABLE": (Decimal("0.5"), Decimal("-0.01")),
    }
    rows: list[Sample] = []
    for label, features in points.items():
        for _ in range(count):
            rows.append(Sample(session, label, features))
    return tuple(rows)
