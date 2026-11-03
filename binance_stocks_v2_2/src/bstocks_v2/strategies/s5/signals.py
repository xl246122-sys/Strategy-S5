"""S5 research decisions. This module does not place or cancel orders."""

from dataclasses import dataclass
from datetime import datetime, time
from decimal import ROUND_HALF_UP, Decimal

from bstocks_v2.contracts.models import Direction, FillHorizon, TradeIntent
from bstocks_v2.feature_store.errors import LookaheadError
from bstocks_v2.research_data.calendar import NEW_YORK
from bstocks_v2.strategies.s5.event_gate import (
    NewsSnapshot,
    QuoteSnapshot,
    classify_news,
    classify_quote,
    liquidity_recovered,
)
from bstocks_v2.strategies.s5.features import (
    ClosedBar,
    PriorDay,
    arm_window,
    clock_in_window,
    decision_deadline,
    drop_screen,
    feature_hash,
    has_rth_gap,
    log_return,
    require_closed,
    residual_z,
    rvol_faded,
    simple_return,
    tape_state,
    vwap_fading,
    z_screen,
)
from bstocks_v2.strategies.s5.model import PathModel
from bstocks_v2.strategies.s5.spec import (
    ARM_A,
    ARM_B,
    ARM_C,
    COMMISSION_RATE,
    LEVELS,
    MIN_NOTIONAL,
    MODEL_VERSION,
    NET_TARGETS,
    PATHS,
    PENETRATION,
    PLANNED_STOP,
    PRIMARY_NET_TARGET,
    REGIME_STALE_AFTER,
    RISK_BUDGET,
    STRATEGY_ID,
    TTL,
    research_split,
)
from bstocks_v2.strategies.s5.universe import in_universe

_CENT = Decimal("0.01")
_BLOCKED_REGIMES = frozenset({"UNKNOWN", "HIGH_VOL_PANIC"})
_EXHAUSTED = frozenset({"SELLING_EXHAUSTION", "RECOVERY_CONFIRMATION"})
_ARM_SESSION = {ARM_A: "RTH", ARM_B: "POST", ARM_C: "POST"}


@dataclass(frozen=True)
class ForwardTape:
    """Bars strictly after the decision. Prices are ignored when executable is false."""

    bars: tuple[ClosedBar, ...]
    deadline: datetime
    executable: bool


@dataclass(frozen=True)
class CausalView:
    symbol: str
    arm: str
    decision_ts: datetime
    bars: tuple[ClosedBar, ...]
    prev_close: Decimal | None
    prior: tuple[PriorDay, ...]
    market_log_return: Decimal | None
    sector_log_return: Decimal | None
    regime: str
    entries_allowed: bool
    regime_asof: datetime
    news: NewsSnapshot | None
    quote: QuoteSnapshot | None
    overnight_supported: bool | None
    rth_close: time
    prior_slot_volumes: tuple[Decimal, ...] = ()


@dataclass(frozen=True)
class ResearchRecord:
    symbol: str
    arm: str
    decision_ts: datetime
    session_date: str
    split: str
    features_hash: str
    bar_ends: tuple[str, ...]
    regime: str
    news_state: str
    tape_state: str
    simple_return: Decimal | None
    residual_z: Decimal | None
    screens: tuple[tuple[str, bool], ...]
    rejection_reasons: tuple[str, ...]
    admitted_level: str
    path_label: str
    probabilities: tuple[Decimal, Decimal, Decimal, Decimal]
    model_kind: str
    expected_net_bps: Decimal | None
    planned_take_profits: tuple[Decimal, ...]
    intent: TradeIntent | None

    @property
    def routable(self) -> bool:
        return False


class CausalBook:
    """Streaming tape. A later bar is not visible to an earlier decision."""

    def __init__(self) -> None:
        self._bars: list[ClosedBar] = []
        self._last: datetime | None = None

    def add(self, bar: ClosedBar) -> None:
        if self._last is not None and bar.end < self._last:
            raise LookaheadError("OUT_OF_ORDER")
        if any(item.symbol == bar.symbol and item.end == bar.end for item in self._bars):
            raise ValueError("duplicate bar")
        self._last = bar.end
        self._bars.append(bar)

    def asof(self, symbol: str, decision_ts: datetime) -> tuple[ClosedBar, ...]:
        if self._last is None or decision_ts > self._last:
            raise LookaheadError("DECISION_AHEAD_OF_TAPE")
        chosen = tuple(
            bar for bar in self._bars if bar.symbol == symbol and bar.end <= decision_ts
        )
        require_closed(chosen, decision_ts)
        return chosen


def evaluate(
    view: CausalView,
    model: PathModel,
    forward: ForwardTape | None = None,
) -> ResearchRecord:
    """One causal decision. Rejected names stay in the record."""

    if view.arm not in _ARM_SESSION:
        raise ValueError("arm")
    _guard_view(view)
    decision_day = view.decision_ts.astimezone(NEW_YORK).date()
    decision_bar = _decision_bar(view)
    simple = simple_return(decision_bar.close, view.prev_close)
    symbol_log = log_return(decision_bar.close, view.prev_close)
    z_value = residual_z(
        symbol_log,
        view.market_log_return,
        view.sector_log_return,
        view.prior,
        decision_day,
    )
    window = _window_bars(view)
    state = tape_state(window)
    news_state = classify_news(view.news, view.decision_ts)
    quote_reasons = classify_quote(view.quote, view.decision_ts)
    hard = _hard_blocks(view, decision_bar, state, news_state, quote_reasons)
    screens = _screens(view, simple, z_value, state, window)
    probs = model.predict((z_value or Decimal(0), simple or Decimal(0)))
    _check_four(probs)
    prices = _planned_prices(decision_bar.close, view.quote)
    net = _expected_net(probs, prices is not None)
    if model.kind == "insufficient" or net is None or net <= 0:
        screens = (screens[0], screens[1], screens[2], (LEVELS[3], False))
    label = _path_label(view, forward, prices)
    admitted = _admitted(hard, screens)
    digest = _hash(view, decision_bar, simple, z_value, state, news_state)
    intent = None
    if admitted and prices is not None:
        intent = _intent(
            view,
            decision_bar,
            digest,
            prices,
            probs,
            net,
            admitted,
        )
    return ResearchRecord(
        symbol=view.symbol,
        arm=view.arm,
        decision_ts=view.decision_ts,
        session_date=decision_day.isoformat(),
        split=research_split(decision_day),
        features_hash=digest,
        bar_ends=tuple(bar.end.isoformat() for bar in sorted(view.bars, key=lambda item: item.end)),
        regime=view.regime,
        news_state=news_state,
        tape_state=state,
        simple_return=simple,
        residual_z=z_value,
        screens=screens,
        rejection_reasons=hard,
        admitted_level=admitted,
        path_label=label,
        probabilities=probs,
        model_kind=model.kind,
        expected_net_bps=net,
        planned_take_profits=prices[1] if prices is not None else tuple(),
        intent=intent,
    )


def evaluate_batch(
    views: tuple[CausalView, ...],
    model: PathModel,
    forward: ForwardTape | None = None,
) -> tuple[ResearchRecord, ...]:
    return tuple(evaluate(view, model, forward) for view in views)


def _guard_view(view: CausalView) -> None:
    require_closed(view.bars, view.decision_ts)
    for bar in view.bars:
        if bar.symbol != view.symbol:
            raise ValueError("bar symbol")
    if view.regime_asof.tzinfo is None:
        raise ValueError("regime timestamps must be timezone-aware")
    if view.regime_asof > view.decision_ts:
        raise LookaheadError("REGIME_AFTER_DECISION")
    day = view.decision_ts.astimezone(NEW_YORK).date()
    for row in view.prior:
        if row.session >= day:
            raise LookaheadError("PRIOR_NOT_PRIOR")
    if view.prev_close is not None and view.prev_close <= 0:
        raise ValueError("prev close")


def _decision_bar(view: CausalView) -> ClosedBar:
    matches = [bar for bar in view.bars if bar.end == view.decision_ts]
    if len(matches) != 1:
        raise ValueError("decision_ts must equal one closed bar end")
    return matches[0]


def _window_bars(view: CausalView) -> tuple[ClosedBar, ...]:
    start, end = arm_window(view.arm, view.rth_close)
    wanted = _ARM_SESSION[view.arm]
    chosen = [
        bar
        for bar in view.bars
        if bar.session == wanted and clock_in_window(bar.end, start, end)
    ]
    return tuple(sorted(chosen, key=lambda bar: bar.end))


def _hard_blocks(
    view: CausalView,
    decision_bar: ClosedBar,
    state: str,
    news_state: str,
    quote_reasons: tuple[str, ...],
) -> tuple[str, ...]:
    reasons: list[str] = []
    if not in_universe(view.symbol):
        reasons.append("NOT_IN_UNIVERSE")
    start, end = arm_window(view.arm, view.rth_close)
    if not clock_in_window(view.decision_ts, start, end):
        reasons.append("OUTSIDE_WINDOW")
    if decision_bar.session != _ARM_SESSION[view.arm]:
        reasons.append("WRONG_SESSION")
    if view.arm == ARM_C and view.overnight_supported is not True:
        reasons.append("OVERNIGHT_UNSUPPORTED")
    if view.regime == "NOT_ATTACHED":
        reasons.append("REGIME_NOT_ATTACHED")
    elif view.regime in _BLOCKED_REGIMES or not view.entries_allowed:
        reasons.append("REGIME_BLOCKED")
    if view.decision_ts - view.regime_asof > REGIME_STALE_AFTER:
        reasons.append("REGIME_STALE")
    if has_rth_gap(view.bars, view.decision_ts, time(9, 30)):
        reasons.append("VWAP_GAP")
    if news_state != "NEWS_CLEAR":
        reasons.append(news_state)
    reasons.extend(quote_reasons)
    if state == "FALLING_KNIFE":
        reasons.append("FALLING_KNIFE")
    if view.prev_close is None:
        reasons.append("NO_PREV_CLOSE")
    return tuple(reasons)


def _screens(
    view: CausalView,
    simple: Decimal | None,
    z_value: Decimal | None,
    state: str,
    window: tuple[ClosedBar, ...],
) -> tuple[tuple[str, bool], ...]:
    passed_drop = drop_screen(simple)
    passed_z = z_screen(passed_drop, z_value)
    slot = sum((bar.volume for bar in window), Decimal(0)) if window else None
    passed_tape = (
        passed_z
        and state in _EXHAUSTED
        and liquidity_recovered(view.quote)
        and vwap_fading(view.bars, view.decision_ts)
        and rvol_faded(slot, view.prior_slot_volumes)
    )
    return (
        (LEVELS[0], passed_drop),
        (LEVELS[1], passed_z),
        (LEVELS[2], passed_tape),
        (LEVELS[3], passed_tape),
    )


def _admitted(hard: tuple[str, ...], screens: tuple[tuple[str, bool], ...]) -> str:
    if hard:
        return ""
    admitted = ""
    for level, passed in screens:
        if not passed:
            break
        admitted = level
    return admitted


def _planned_prices(
    close: Decimal,
    quote: QuoteSnapshot | None,
) -> tuple[Decimal, tuple[Decimal, Decimal, Decimal], Decimal] | None:
    entry = close.quantize(_CENT, rounding=ROUND_HALF_UP)
    stop = (entry * (1 - PLANNED_STOP)).quantize(_CENT, rounding=ROUND_HALF_UP)
    if quote is None or quote.ask <= quote.bid or entry <= 0 or stop >= entry:
        return None
    spread = quote.ask - quote.bid
    cost_fraction = COMMISSION_RATE * 2 + spread / entry
    targets = (
        _gross(entry, NET_TARGETS[0], cost_fraction),
        _gross(entry, NET_TARGETS[1], cost_fraction),
        _gross(entry, NET_TARGETS[2], cost_fraction),
    )
    if any(price <= entry for price in targets):
        return None
    return entry, targets, stop


def _gross(entry: Decimal, net: Decimal, cost_fraction: Decimal) -> Decimal:
    return (entry * (1 + net + cost_fraction)).quantize(_CENT, rounding=ROUND_HALF_UP)


def _expected_net(
    probs: tuple[Decimal, Decimal, Decimal, Decimal],
    cost_known: bool,
) -> Decimal | None:
    if not cost_known:
        return None
    target_bps = PRIMARY_NET_TARGET * Decimal(10000)
    stop_bps = PLANNED_STOP * Decimal(10000)
    return probs[0] * target_bps + probs[1] * (-stop_bps)


def _path_label(
    view: CausalView,
    forward: ForwardTape | None,
    prices: tuple[Decimal, tuple[Decimal, Decimal, Decimal], Decimal] | None,
) -> str:
    if forward is None:
        return "UNLABELED"
    _guard_forward(view.decision_ts, forward)
    if not forward.executable or prices is None:
        return "UNFILLABLE"
    entry, targets, stop = prices
    return _walk(forward, entry, targets[0], stop)


def _guard_forward(decision_ts: datetime, forward: ForwardTape) -> None:
    if forward.deadline.tzinfo is None:
        raise ValueError("deadline must be timezone-aware")
    for bar in forward.bars:
        if bar.end <= decision_ts or bar.start < decision_ts:
            raise LookaheadError("FORWARD_OVERLAPS_DECISION")


def _walk(forward: ForwardTape, entry: Decimal, take_profit: Decimal, stop: Decimal) -> str:
    ordered = tuple(sorted(forward.bars, key=lambda bar: bar.end))
    filled = False
    for bar in ordered:
        if bar.end > forward.deadline:
            break
        hit_stop = bar.low <= stop - PENETRATION
        hit_target = bar.high >= take_profit + PENETRATION
        if not filled:
            if bar.low <= entry - PENETRATION:
                filled = True
                if hit_stop:
                    return "SL_FIRST"
            continue
        if hit_stop:
            return "SL_FIRST"
        if hit_target:
            return "TP_FIRST"
    if not filled:
        return "UNFILLABLE"
    return "TIMEOUT"


def _hash(
    view: CausalView,
    decision_bar: ClosedBar,
    simple: Decimal | None,
    z_value: Decimal | None,
    state: str,
    news_state: str,
) -> str:
    rows: list[tuple[str, str]] = [
        ("symbol", view.symbol),
        ("arm", view.arm),
        ("decision", view.decision_ts.isoformat()),
        ("prev_close", _num(view.prev_close)),
        ("market", _num(view.market_log_return)),
        ("sector", _num(view.sector_log_return)),
        ("regime", view.regime),
        ("regime_asof", view.regime_asof.isoformat()),
        ("entries", "1" if view.entries_allowed else "0"),
        ("news", news_state),
        ("news_at", "" if view.news is None else view.news.visible_at.isoformat()),
        ("quote", _quote_text(view.quote)),
        ("overnight", _flag(view.overnight_supported)),
        ("close", _num(decision_bar.close)),
        ("simple", _num(simple)),
        ("z", _num(z_value)),
        ("tape", state),
    ]
    for bar in sorted(view.bars, key=lambda item: item.end):
        rows.append((bar.end.isoformat(), _num(bar.close)))
    for row in sorted(view.prior, key=lambda item: item.session):
        rows.append((row.session.isoformat(), _num(row.symbol_log)))
    return feature_hash(tuple(rows))


def _intent(
    view: CausalView,
    decision_bar: ClosedBar,
    digest: str,
    prices: tuple[Decimal, tuple[Decimal, Decimal, Decimal], Decimal],
    probs: tuple[Decimal, Decimal, Decimal, Decimal],
    net: Decimal | None,
    admitted: str,
) -> TradeIntent:
    entry, targets, stop = prices
    conditional = _conditional(probs)
    gross = (targets[0] - entry) / entry * Decimal(10000)
    risk = (entry - stop) / entry * Decimal(10000)
    spread = Decimal(0) if view.quote is None else view.quote.ask - view.quote.bid
    share_cost = COMMISSION_RATE * (entry + targets[0]) + spread
    stamp = view.decision_ts.astimezone(NEW_YORK).strftime("%Y%m%dT%H%M")
    freshness = _elapsed_ms(view.regime_asof, view.decision_ts)
    quote_age = None
    if view.quote is not None:
        quote_age = _elapsed_ms(view.quote.exchange_ts, view.decision_ts)
    deadline = decision_deadline(view.decision_ts, view.arm)
    holding = _elapsed_seconds(view.decision_ts, deadline)
    return TradeIntent(
        intent_id=f"s5{view.arm[0]}{view.symbol}{stamp}",
        created_at_utc=view.decision_ts,
        data_asof_utc=view.decision_ts,
        symbol=view.symbol,
        strategy_id=STRATEGY_ID,
        model_version=f"{MODEL_VERSION}-{admitted}",
        regime=view.regime,
        features_hash=digest,
        vwap_intact=True,
        signal_valid_until=view.decision_ts + TTL,
        direction=Direction.LONG,
        p_win_calibrated=conditional[0],
        p_sl_calibrated=conditional[1],
        p_timeout_calibrated=conditional[2],
        expected_return_bps=probs[0] * gross - probs[1] * risk,
        expected_net_bps=net if net is not None else Decimal(0),
        prediction_interval=(-risk, gross),
        candidate_limit_prices=[entry],
        p_fill_by_price_horizon=[
            FillHorizon(
                price=entry,
                horizon_seconds=int(TTL.total_seconds()),
                p_fill=Decimal(1) - probs[3],
            )
        ],
        entry_price_ceiling=entry,
        stop_price=stop,
        take_profit_price=targets[0],
        max_holding_seconds=holding,
        risk_budget_usdc=RISK_BUDGET,
        min_viable_notional_usdc=MIN_NOTIONAL,
        expected_cost_usdc=share_cost,
        requires_fractional=False,
        whole_share_fallback_qty=Decimal(0),
        rejection_reasons=[],
        data_feed=decision_bar.feed,
        data_freshness_ms=freshness,
        session=decision_bar.session,
        capture_ts=view.decision_ts,
        exchange_ts=decision_bar.end,
        feed=decision_bar.feed,
        quote_freshness_ms=quote_age,
        research_arm=view.arm,
        news_event_state=classify_news(view.news, view.decision_ts),
    )


def _conditional(
    probs: tuple[Decimal, Decimal, Decimal, Decimal],
) -> tuple[Decimal, Decimal, Decimal]:
    resolved = probs[0] + probs[1] + probs[2]
    if resolved <= 0:
        third = Decimal(1) / Decimal(3)
        return (third, third, Decimal(1) - third - third)
    first = probs[0] / resolved
    second = probs[1] / resolved
    return (first, second, Decimal(1) - first - second)


def _check_four(probs: tuple[Decimal, Decimal, Decimal, Decimal]) -> None:
    if len(probs) != len(PATHS):
        raise ValueError("probability width")
    if abs(sum(probs, Decimal(0)) - 1) > Decimal("0.000000001"):
        raise ValueError("probabilities must sum to 1")


def _quote_text(quote: QuoteSnapshot | None) -> str:
    if quote is None:
        return ""
    return f"{quote.bid}/{quote.ask}/{quote.bid_size}/{quote.ask_size}"


def _num(value: Decimal | None) -> str:
    if value is None:
        return ""
    return format(value, "f")


def _flag(value: bool | None) -> str:
    if value is None:
        return ""
    return "1" if value else "0"


def _elapsed_ms(start: datetime, end: datetime) -> int:
    delta = end - start
    return delta.days * 86_400_000 + delta.seconds * 1000 + delta.microseconds // 1000


def _elapsed_seconds(start: datetime, end: datetime) -> int:
    delta = end - start
    return max(1, delta.days * 86_400 + delta.seconds)
