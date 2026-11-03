"""Regime time shares for the research window. This script does not place orders."""

import json
from collections import Counter
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from bstocks_v2.constitution import live_trade_enabled
from bstocks_v2.feature_store.bars import FeatureBar
from bstocks_v2.feature_store.engine import CausalFeatureBook
from bstocks_v2.regime.classifier import (
    BENCHMARKS,
    BREADTH_DOWN,
    BREADTH_UP,
    CONFIDENCE_MIN,
    ENTER_TREND,
    EXIT_TREND,
    MIN_DWELL,
    PANIC_RMS,
    PANIC_RVOL,
    PANIC_TREND,
    SIDEWAYS_BAND,
    FeatureSnapshot,
    MarketRegimeClassifier,
    Regime,
)
from bstocks_v2.research_data.adjust import backward_price_factors, load_splits
from bstocks_v2.research_data.build import read_five_minute
from bstocks_v2.research_data.calendar import (
    NEW_YORK,
    RESEARCH_END,
    RESEARCH_START,
    load_xnys_calendar,
)
from bstocks_v2.research_data.paths import FRESH_DEFAULT
from bstocks_v2.research_data.quality import PriceBar

OUT_DEFAULT = Path(r"D:\Strategy\data_v2_2\reports\stage5_regimes.json")
ORDER = (
    Regime.TREND_UP,
    Regime.SIDEWAYS,
    Regime.TREND_DOWN,
    Regime.HIGH_VOL_PANIC,
    Regime.UNKNOWN,
)


def main() -> None:
    summary = diagnose(FRESH_DEFAULT, OUT_DEFAULT)
    print(json.dumps({"bars": summary["bars"], "shares": summary["shares"]}))


def diagnose(fresh: Path, out: Path) -> dict[str, object]:
    if live_trade_enabled():
        raise RuntimeError("live trading is enabled")
    calendar = load_xnys_calendar()
    sessions = calendar.sessions_between(RESEARCH_START, RESEARCH_END)
    action = fresh / "corporate_actions" / "corporate_actions_20241101_20261030.csv"
    splits = load_splits(action) if action.exists() else []
    loaded: list[FeatureBar] = []
    counts_by_symbol: dict[str, int] = {}
    for symbol in BENCHMARKS:
        raw, mismatches, feeds = read_five_minute(
            fresh, symbol, RESEARCH_START, RESEARCH_END, calendar
        )
        if mismatches:
            raise RuntimeError(f"{symbol} session labels disagree with the calendar")
        if feeds != {"sip"}:
            raise RuntimeError(f"{symbol} feed is not sip-only: {sorted(feeds)}")
        if not raw:
            raise RuntimeError(f"{symbol} has no RTH bars")
        factors = backward_price_factors(
            [item for item in splits if item.symbol == symbol],
            sessions,
        )
        bars = [_feature_bar(item, factors) for item in raw]
        counts_by_symbol[symbol] = len(bars)
        loaded.extend(bars)
        print(f"loaded {symbol} {len(bars)}", flush=True)
    loaded.sort(key=lambda item: (item.start, item.symbol == "SPY", item.symbol))
    book = CausalFeatureBook(calendar)
    classifier = MarketRegimeClassifier()
    decisions: list[FeatureSnapshot] = []
    last_spy: datetime | None = None
    for index, bar in enumerate(loaded):
        snapshot = book.update(bar)
        if bar.symbol != "SPY" or snapshot is None or snapshot.bar_start == last_spy:
            continue
        last_spy = snapshot.bar_start
        decision = classifier.decide(book.latest, snapshot.data_asof_utc)
        _assert_gate(decision)
        decisions.append(decision)
        if len(decisions) % 5000 == 0:
            print(f"decisions {len(decisions)} of bar {index}", flush=True)
    summary = _summarize(decisions, counts_by_symbol, action.exists())
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary


def _feature_bar(bar: PriceBar, factors: dict[date, Decimal]) -> FeatureBar:
    day = bar.start.astimezone(NEW_YORK).date()
    return FeatureBar(
        symbol=bar.symbol,
        start=bar.start,
        end=bar.end,
        open=bar.open,
        high=bar.high,
        low=bar.low,
        close=bar.close,
        volume=bar.volume,
        bar_vwap=bar.vwap,
        closed=True,
        feed=bar.feed,
        price_factor=factors.get(day, Decimal(1)),
    )


def _summarize(
    decisions: list[FeatureSnapshot],
    counts_by_symbol: dict[str, int],
    splits_loaded: bool,
) -> dict[str, object]:
    total = len(decisions)
    counts = Counter(item.regime.value for item in decisions)
    reasons = Counter(item.block_reason for item in decisions)
    ready = [item for item in decisions if item.data_ready]
    ready_counts = Counter(item.regime.value for item in ready)
    transitions: dict[str, Counter[str]] = {item.value: Counter() for item in ORDER}
    switches = 0
    runs: list[int] = []
    run = 0
    previous: str | None = None
    trend_sum: dict[str, Decimal] = {item.value: Decimal(0) for item in ORDER}
    trend_n: dict[str, int] = {item.value: 0 for item in ORDER}
    for item in decisions:
        if previous is not None:
            transitions[previous][item.regime.value] += 1
            if item.regime.value != previous:
                switches += 1
                runs.append(run)
                run = 1
            else:
                run += 1
        else:
            run = 1
        previous = item.regime.value
        if item.trend_z is not None:
            trend_sum[item.regime.value] += item.trend_z
            trend_n[item.regime.value] += 1
    if run:
        runs.append(run)
    opening = sum(1 for item in decisions if item.in_opening_window)
    s3 = sum(1 for item in decisions if item.s3_enabled)
    blocked = sum(1 for item in decisions if not item.entries_allowed)
    return {
        "live_trade_enabled": False,
        "window": [RESEARCH_START.isoformat(), RESEARCH_END.isoformat()],
        "symbols": list(BENCHMARKS),
        "bars_by_symbol": counts_by_symbol,
        "splits_loaded": splits_loaded,
        "bars": total,
        "counts": {item.value: counts[item.value] for item in ORDER},
        "shares": {item.value: _share(counts[item.value], total) for item in ORDER},
        "ready_bars": len(ready),
        "ready_counts": {item.value: ready_counts[item.value] for item in ORDER},
        "ready_shares": {
            item.value: _share(ready_counts[item.value], len(ready)) for item in ORDER
        },
        "reasons": dict(reasons),
        "transitions": {
            source: {item.value: transitions[source][item.value] for item in ORDER}
            for source in transitions
        },
        "switches": switches,
        "median_run_bars": _median_int(runs),
        "opening_window_bars": opening,
        "s3_enabled_bars": s3,
        "entries_blocked_bars": blocked,
        "mean_trend_z": {
            item.value: _mean(trend_sum[item.value], trend_n[item.value]) for item in ORDER
        },
        "first_asof": None if not decisions else decisions[0].data_asof_utc.isoformat(),
        "last_asof": None if not decisions else decisions[-1].data_asof_utc.isoformat(),
        "first_ready_asof": None if not ready else ready[0].data_asof_utc.isoformat(),
        "thresholds": {
            "enter_trend": format(ENTER_TREND, "f"),
            "exit_trend": format(EXIT_TREND, "f"),
            "sideways_band": format(SIDEWAYS_BAND, "f"),
            "breadth_up": format(BREADTH_UP, "f"),
            "breadth_down": format(BREADTH_DOWN, "f"),
            "panic_rvol": format(PANIC_RVOL, "f"),
            "panic_rms": format(PANIC_RMS, "f"),
            "panic_trend": format(PANIC_TREND, "f"),
            "confidence_min": format(CONFIDENCE_MIN, "f"),
            "min_dwell_bars": MIN_DWELL,
            "baseline_days": 20,
            "trend_bars": 6,
        },
    }


def _assert_gate(decision: FeatureSnapshot) -> None:
    blocked = decision.regime in {Regime.UNKNOWN, Regime.HIGH_VOL_PANIC}
    if blocked and decision.entries_allowed:
        raise RuntimeError("blocked regime allowed a new long")
    if blocked and any(
        (
            decision.s1_enabled,
            decision.s2_enabled,
            decision.s3_enabled,
            decision.s4_research_enabled,
        )
    ):
        raise RuntimeError("blocked regime enabled a strategy")


def _share(count: int, total: int) -> str:
    if total == 0:
        return "0"
    return format((Decimal(count) / Decimal(total)).quantize(Decimal("0.000001")), "f")


def _mean(total: Decimal, count: int) -> str | None:
    if count == 0:
        return None
    return format((total / Decimal(count)).quantize(Decimal("0.0001")), "f")


def _median_int(values: list[int]) -> str | None:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2 == 1:
        return str(ordered[mid])
    average = (Decimal(ordered[mid - 1]) + Decimal(ordered[mid])) / Decimal(2)
    return format(average, "f")


if __name__ == "__main__":
    main()
