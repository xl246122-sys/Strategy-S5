"""Signal scans. Strategy thresholds stay at the frozen spec unless a neighbor is applied."""

from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from bstocks_v2.contracts.models import TradeIntent
from bstocks_v2.feature_store.bars import FeatureBar
from bstocks_v2.research_backtest.tape import ClosedBar
from bstocks_v2.research_data.calendar import XnysCalendar
from bstocks_v2.stage14.minutes import load_parent_minutes
from bstocks_v2.strategies.s1.evaluate import S1Trace
from bstocks_v2.strategies.s1.evaluate import _signals as s1_signals
from bstocks_v2.strategies.s1.signal import decide as s1_decide
from bstocks_v2.strategies.s2.evaluate import S2Trace
from bstocks_v2.strategies.s2.evaluate import _signals as s2_signals
from bstocks_v2.strategies.s2.pattern import PullbackBook
from bstocks_v2.strategies.s2.signal import UNCALIBRATED
from bstocks_v2.strategies.s2.signal import decide as s2_decide
from bstocks_v2.strategies.s2.signal import make_intent as s2_intent
from bstocks_v2.strategies.s3.evaluate import S3Trace
from bstocks_v2.strategies.s3.evaluate import _signals as s3_signals
from bstocks_v2.strategies.s3.sequential import SequentialResult, confirm
from bstocks_v2.strategies.s3.signal import Candidate as S3Candidate
from bstocks_v2.strategies.s3.signal import decide as s3_decide
from bstocks_v2.strategies.s3.signal import make_intent as s3_intent
from bstocks_v2.strategies.s3.spec import HORIZONS
from bstocks_v2.strategies.s4.bounce import BounceBook
from bstocks_v2.strategies.s4.evaluate import S4Trace
from bstocks_v2.strategies.s4.evaluate import _signals as s4_signals
from bstocks_v2.strategies.s4.signal import Candidate as S4Candidate
from bstocks_v2.strategies.s4.signal import decide as s4_decide
from bstocks_v2.strategies.s4.signal import make_intent as s4_intent


@contextmanager
def patched(module: object, **values: Decimal) -> Iterator[None]:
    previous = {name: getattr(module, name) for name in values}
    try:
        for name, value in values.items():
            setattr(module, name, value)
        yield
    finally:
        for name, value in previous.items():
            setattr(module, name, value)


def scan_s1(
    bars: list[FeatureBar],
    calendar: XnysCalendar,
    eligible: set[tuple[str, str]],
    models: tuple[str, ...] = ("kalman", "rls", "ols"),
    trace: S1Trace | None = None,
) -> tuple[dict[str, dict[tuple[str, datetime], TradeIntent]], dict[str, dict[str, int]]]:
    intents, gates, _updates, _nanos, _cosine = s1_signals(
        bars, calendar, eligible, models, trace
    )
    return intents, {name: dict(counter) for name, counter in gates.items()}


def scan_s2(
    bars: list[FeatureBar],
    calendar: XnysCalendar,
    eligible: set[tuple[str, str]],
    trace: S2Trace | None = None,
) -> tuple[dict[tuple[str, datetime], TradeIntent], dict[str, int]]:
    """Rule intents only. The probability gate stays unfitted."""

    candidates, gates, _after = s2_signals(bars, calendar, eligible, trace)
    intents: dict[tuple[str, datetime], TradeIntent] = {}
    for candidate in candidates:
        key = (candidate.symbol, candidate.signal_end)
        if key in intents:
            raise RuntimeError("duplicate S2 signal")
        intents[key] = s2_intent(
            candidate,
            kind="rule",
            probabilities=UNCALIBRATED,
            timeout_bps=Decimal(0),
            model_version="s2-v1-rule-uncalibrated",
        )
    return intents, dict(gates)


def scan_s3(
    bars: list[FeatureBar],
    calendar: XnysCalendar,
    eligible: set[tuple[str, str]],
    fresh_root: Path,
    *,
    with_sequential: bool = True,
    trace: S3Trace | None = None,
) -> tuple[
    dict[int, dict[tuple[str, datetime], TradeIntent]],
    dict[int, dict[tuple[str, datetime], TradeIntent]],
    dict[str, object],
]:
    candidates, gates, ranges = s3_signals(bars, calendar, eligible, trace)
    baseline: dict[int, dict[tuple[str, datetime], TradeIntent]] = {
        horizon: {} for horizon in HORIZONS
    }
    sequential: dict[int, dict[tuple[str, datetime], TradeIntent]] = {
        horizon: {} for horizon in HORIZONS
    }
    terminals: dict[str, dict[str, int]] = {}
    minutes = _minutes(fresh_root, calendar, candidates) if with_sequential else {}
    for horizon in HORIZONS:
        counted: Counter[str] = Counter()
        for candidate in candidates[horizon]:
            key = (candidate.symbol, candidate.signal_end)
            if key in baseline[horizon]:
                raise RuntimeError("duplicate S3 signal")
            baseline[horizon][key] = s3_intent(candidate, arm="baseline")
            if not with_sequential:
                continue
            result = _confirm(candidate, minutes)
            counted[result.terminal] += 1
            if result.accept:
                sequential[horizon][key] = s3_intent(candidate, arm="sequential")
        terminals[str(horizon)] = dict(counted)
    meta: dict[str, object] = {
        "gates": {str(horizon): dict(gates[horizon]) for horizon in HORIZONS},
        "ranges": {str(horizon): dict(ranges[horizon]) for horizon in HORIZONS},
        "sequential_terminals": terminals,
        "probability_gate": "not_used",
    }
    return baseline, sequential, meta


def scan_s4(
    bars: list[FeatureBar],
    calendar: XnysCalendar,
    eligible: set[tuple[str, str]],
    trace: S4Trace | None = None,
) -> tuple[
    dict[tuple[str, datetime], TradeIntent],
    dict[tuple[str, datetime], TradeIntent],
    dict[str, object],
]:
    baseline, guarded, gates, knives, regimes = s4_signals(bars, calendar, eligible, trace)
    meta = {
        "gates": {name: dict(counter) for name, counter in gates.items()},
        "knife_details": dict(knives),
        "baseline_regimes": dict(regimes),
        "live_locked": True,
    }
    return _index(baseline, "baseline"), _index(guarded, "guarded"), meta


def _index(
    rows: list[S4Candidate],
    arm: str,
) -> dict[tuple[str, datetime], TradeIntent]:
    intents: dict[tuple[str, datetime], TradeIntent] = {}
    for row in rows:
        key = (row.symbol, row.signal_end)
        if key in intents:
            raise RuntimeError("duplicate S4 signal")
        intents[key] = s4_intent(row, arm=arm)
    return intents


def _minutes(
    fresh: Path,
    calendar: XnysCalendar,
    candidates: dict[int, list[S3Candidate]],
) -> dict[tuple[str, datetime], tuple[ClosedBar, ...]]:
    parents: dict[str, set[datetime]] = {}
    for rows in candidates.values():
        for candidate in rows:
            parents.setdefault(candidate.symbol, set()).add(candidate.bar_start)
            parents.setdefault("SPY", set()).add(candidate.bar_start)
    if not parents:
        return {}
    requested = sum(len(item) for item in parents.values())
    print(f"loading 1-minute bars for {requested} parents", flush=True)
    return load_parent_minutes(fresh, parents, calendar)


def _confirm(
    candidate: S3Candidate,
    minutes: dict[tuple[str, datetime], tuple[ClosedBar, ...]],
) -> SequentialResult:
    stock = minutes.get((candidate.symbol, candidate.bar_start), ())
    expected = tuple(candidate.bar_start + timedelta(minutes=step) for step in range(5))
    if tuple(bar.start for bar in stock) != expected:
        return SequentialResult(
            False,
            0,
            Decimal(0),
            Decimal(0),
            "INCOMPLETE_MINUTES",
            None,
            None,
            None,
        )
    spy = minutes.get(("SPY", candidate.bar_start), ())
    return confirm(stock, orh=candidate.orh, atr=candidate.atr, spy=spy)


def replay_s1(trace: S1Trace) -> dict[tuple[str, datetime], TradeIntent]:
    intents: dict[tuple[str, datetime], TradeIntent] = {}
    for model, symbol, fit, strength, stock, regime, allowed in trace:
        intent, _reason = s1_decide(
            model=model,
            symbol=symbol,
            fit=fit,
            sector_strength=strength,
            stock=stock,
            regime=regime,
            research_eligible=allowed,
        )
        if intent is not None:
            intents[(symbol, stock.data_asof_utc)] = intent
    return intents


def replay_s2(trace: S2Trace) -> dict[tuple[str, datetime], TradeIntent]:
    book = PullbackBook()
    intents: dict[tuple[str, datetime], TradeIntent] = {}
    for bar, snap, regime, allowed, half in trace:
        pattern, reason = book.observe(bar, snap)
        if regime is None:
            continue
        candidate, _why = s2_decide(
            symbol=bar.symbol,
            pattern=pattern,
            pattern_reason=reason,
            snap=snap,
            regime=regime,
            research_eligible=allowed,
            half_day=half,
            book=book,
        )
        if candidate is None:
            continue
        key = (candidate.symbol, candidate.signal_end)
        if key in intents:
            raise RuntimeError("duplicate S2 signal")
        intents[key] = s2_intent(
            candidate,
            kind="rule",
            probabilities=UNCALIBRATED,
            timeout_bps=Decimal(0),
            model_version="s2-v1-rule-uncalibrated",
        )
    return intents


def replay_s3(trace: S3Trace) -> dict[int, dict[tuple[str, datetime], TradeIntent]]:
    intents: dict[int, dict[tuple[str, datetime], TradeIntent]] = {
        horizon: {} for horizon in HORIZONS
    }
    for horizon, symbol, snap, regime, allowed, half, opening in trace:
        if regime is None:
            continue
        candidate, _why = s3_decide(
            symbol=symbol,
            snap=snap,
            regime=regime,
            research_eligible=allowed,
            half_day=half,
            opening=opening,
        )
        if candidate is None:
            continue
        key = (candidate.symbol, candidate.signal_end)
        if key in intents[horizon]:
            raise RuntimeError("duplicate S3 signal")
        intents[horizon][key] = s3_intent(candidate, arm="baseline")
    return intents


def replay_s4(trace: S4Trace) -> dict[tuple[str, datetime], TradeIntent]:
    bounce = BounceBook()
    intents: dict[tuple[str, datetime], TradeIntent] = {}
    for bar, snap, regime, allowed, half, spy_bar, spy_snap in trace:
        pattern, why = bounce.observe(bar, snap)
        decision = s4_decide(
            arm="baseline",
            symbol=bar.symbol,
            pattern=pattern,
            pattern_reason=why,
            snap=snap,
            regime=regime,
            research_eligible=allowed,
            half_day=half,
            residuals=bounce.residuals(bar.symbol),
            breakdown=bounce.rvol_breakdown(bar.symbol),
            spy_bar=spy_bar,
            spy_snap=spy_snap,
        )
        if decision.candidate is None:
            continue
        key = (decision.candidate.symbol, decision.candidate.signal_end)
        if key in intents:
            raise RuntimeError("duplicate S4 signal")
        intents[key] = s4_intent(decision.candidate, arm="baseline")
    return intents


def agree(
    label: str,
    replayed: dict[tuple[str, datetime], TradeIntent],
    official: dict[tuple[str, datetime], TradeIntent],
) -> None:
    if set(replayed) != set(official):
        raise RuntimeError(f"{label} trace keys {len(replayed)} vs {len(official)}")
    for key, intent in replayed.items():
        if intent != official[key]:
            raise RuntimeError(f"{label} trace diverges")
    print(f"{label} trace matches {len(official)} intents", flush=True)
