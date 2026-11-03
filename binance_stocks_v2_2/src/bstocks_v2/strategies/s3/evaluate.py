"""S3 ablation through 2026-04-30. This module does not place orders."""

import json
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from time import perf_counter

from bstocks_v2.constitution import live_trade_enabled
from bstocks_v2.contracts.models import TradeIntent
from bstocks_v2.feature_store.bars import FeatureBar, SymbolSnapshot
from bstocks_v2.feature_store.engine import CausalFeatureBook
from bstocks_v2.regime.classifier import BENCHMARKS, FeatureSnapshot, MarketRegimeClassifier
from bstocks_v2.research_backtest.models import EngineConfig, FillMode, IntentRecord, ReplayResult
from bstocks_v2.research_backtest.replay import OrderReplayEngine
from bstocks_v2.research_backtest.tape import BarTape, CausalView, ClosedBar
from bstocks_v2.research_data.calendar import NEW_YORK, XnysCalendar, load_xnys_calendar
from bstocks_v2.research_data.paths import FRESH_DEFAULT, OUT_DEFAULT
from bstocks_v2.strategies.s1.load import load_bars, load_membership, load_splits_until
from bstocks_v2.strategies.s1.spec import LOCKBOX_START as S1_LOCK
from bstocks_v2.strategies.s1.spec import SECTOR_PARENT, TRADED
from bstocks_v2.strategies.s1.spec import TRAIN_END as S1_TRAIN_END
from bstocks_v2.strategies.s1.spec import TRAIN_START as S1_TRAIN_START
from bstocks_v2.strategies.s1.spec import VAL_END as S1_VAL_END
from bstocks_v2.strategies.s1.spec import VAL_START as S1_VAL_START
from bstocks_v2.strategies.s3.minutes import load_parent_minutes
from bstocks_v2.strategies.s3.orb import OpeningRangeBook, RangeState
from bstocks_v2.strategies.s3.score import SplitScore, comparison, deployable, summarize
from bstocks_v2.strategies.s3.sequential import SequentialResult, confirm
from bstocks_v2.strategies.s3.signal import Candidate, decide, make_intent
from bstocks_v2.strategies.s3.spec import (
    COMMISSION_RATE,
    CUSUM_H,
    CUSUM_K,
    HALF_SPREAD,
    HORIZONS,
    LOCKBOX_START,
    MAX_STEPS,
    PENETRATION,
    TRAIN_END,
    TRAIN_START,
    VAL_END,
    VAL_START,
)

REPORT = OUT_DEFAULT / "reports" / "stage8_ablation.json"


def main() -> None:
    summary = run(FRESH_DEFAULT, OUT_DEFAULT / "universe" / "membership.parquet", REPORT)
    print(
        json.dumps(
            {
                "max_ny_date": summary["max_ny_date"],
                "comparisons": summary["comparisons"],
                "deployable": summary["deployable"],
            }
        )
    )


def run(fresh: Path, membership: Path, out: Path) -> dict[str, object]:
    if live_trade_enabled():
        raise RuntimeError("live trading is enabled")
    _dates_agree()
    calendar = load_xnys_calendar()
    _splits, ignored_splits = load_splits_until(fresh)
    eligible, stock_eligible = load_membership(membership)
    if stock_eligible != 0:
        raise RuntimeError("a stock was marked eligible without a point-in-time cap")
    symbols = tuple(sorted(set(BENCHMARKS) | set(TRADED)))
    print(f"loading {len(symbols)} symbols through {VAL_END.isoformat()}", flush=True)
    bars = load_bars(fresh, symbols, calendar, _splits)
    bars.sort(key=lambda item: (item.start, item.symbol))
    max_day = max(bar.start.astimezone(NEW_YORK).date() for bar in bars)
    if max_day >= LOCKBOX_START or max_day > VAL_END:
        raise RuntimeError("loaded bar is inside the lockbox")
    print(f"bars {len(bars)} last {max_day.isoformat()}", flush=True)
    candidates, gates, ranges = _signals(bars, calendar, eligible)
    print(
        "signals " + " ".join(f"{horizon}:{len(candidates[horizon])}" for horizon in HORIZONS),
        flush=True,
    )
    minutes = _minutes(fresh, calendar, candidates)
    tape = BarTape([_closed(bar) for bar in bars if bar.symbol in SECTOR_PARENT])
    config = _config()
    horizons: dict[str, object] = {}
    comparisons: dict[str, str] = {}
    deploy: dict[str, bool] = {}
    for horizon in HORIZONS:
        print(f"horizon {horizon}", flush=True)
        block = _horizon(horizon, candidates[horizon], minutes, tape, config)
        horizons[str(horizon)] = block
        comparisons[str(horizon)] = str(block["comparison"])
        deploy[str(horizon)] = bool(block["deployable"])
    summary: dict[str, object] = {
        "live_trade_enabled": False,
        "train": [TRAIN_START.isoformat(), TRAIN_END.isoformat()],
        "validation": [VAL_START.isoformat(), VAL_END.isoformat()],
        "lockbox_start": LOCKBOX_START.isoformat(),
        "lockbox_touched": False,
        "max_ny_date": max_day.isoformat(),
        "symbols": list(symbols),
        "traded": list(TRADED),
        "stock_eligible": stock_eligible,
        "eligible_pairs": len(eligible),
        "splits_after_cutoff_ignored": ignored_splits,
        "bars": len(bars),
        "cusum_k": format(CUSUM_K, "f"),
        "cusum_h": format(CUSUM_H, "f"),
        "max_steps": MAX_STEPS,
        "probability_gate": False,
        "first_failure": {str(horizon): dict(gates[horizon]) for horizon in HORIZONS},
        "range_counts": {str(horizon): dict(ranges[horizon]) for horizon in HORIZONS},
        "horizons": horizons,
        "comparisons": comparisons,
        "deployable": deploy,
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary


def _dates_agree() -> None:
    if (
        TRAIN_START != S1_TRAIN_START
        or TRAIN_END != S1_TRAIN_END
        or VAL_START != S1_VAL_START
        or VAL_END != S1_VAL_END
        or LOCKBOX_START != S1_LOCK
    ):
        raise RuntimeError("research window drifted")


def _config() -> EngineConfig:
    config = EngineConfig(mode=FillMode.CONSERVATIVE)
    if (
        config.fixed_delta != PENETRATION
        or config.half_spread != HALF_SPREAD
        or config.commission_rate != COMMISSION_RATE
    ):
        raise RuntimeError("replay costs drifted from the S3 spec")
    return config


S3Trace = list[
    tuple[int, str, SymbolSnapshot, FeatureSnapshot | None, bool, bool, RangeState]
]


def _signals(
    bars: list[FeatureBar],
    calendar: XnysCalendar,
    eligible: set[tuple[str, str]],
    trace: S3Trace | None = None,
) -> tuple[dict[int, list[Candidate]], dict[int, Counter[str]], dict[int, Counter[str]]]:
    book = CausalFeatureBook(calendar)
    classifier = MarketRegimeClassifier()
    opening = OpeningRangeBook()
    candidates: dict[int, list[Candidate]] = {horizon: [] for horizon in HORIZONS}
    gates: dict[int, Counter[str]] = {horizon: Counter() for horizon in HORIZONS}
    ranges: dict[int, Counter[str]] = {horizon: Counter() for horizon in HORIZONS}
    index = 0
    total = len(bars)
    chunks = 0
    while index < total:
        start = bars[index].start
        chunk: list[FeatureBar] = []
        while index < total and bars[index].start == start:
            chunk.append(bars[index])
            index += 1
        chunks += 1
        for bar in chunk:
            book.update(bar)
        spy = book.latest.get("SPY")
        regime = None
        if spy is not None and spy.bar_start == start:
            regime = classifier.decide(book.latest, spy.data_asof_utc)
        for bar in chunk:
            if bar.symbol not in SECTOR_PARENT:
                continue
            snap = book.latest.get(bar.symbol)
            if snap is None or snap.bar_start != bar.start:
                continue
            view = opening.observe(bar)
            day = bar.start.astimezone(NEW_YORK).date()
            half = calendar.close_time(day).hour == 13
            allowed = (bar.symbol, snap.ny_date) in eligible
            for horizon in HORIZONS:
                ranges[horizon][view[horizon].reason] += 1
                if trace is not None:
                    trace.append(
                        (horizon, bar.symbol, snap, regime, allowed, half, view[horizon])
                    )
                if regime is None:
                    gates[horizon]["NO_REGIME"] += 1
                    continue
                candidate, why = decide(
                    symbol=bar.symbol,
                    snap=snap,
                    regime=regime,
                    research_eligible=allowed,
                    half_day=half,
                    opening=view[horizon],
                )
                gates[horizon][why] += 1
                if candidate is not None:
                    candidates[horizon].append(candidate)
        if chunks % 2000 == 0:
            print(f"chunks {chunks} bar {index} of {total}", flush=True)
    return candidates, gates, ranges


def _minutes(
    fresh: Path,
    calendar: XnysCalendar,
    candidates: dict[int, list[Candidate]],
) -> dict[tuple[str, datetime], tuple[ClosedBar, ...]]:
    parents: dict[str, set[datetime]] = defaultdict(set)
    for rows in candidates.values():
        for candidate in rows:
            parents[candidate.symbol].add(candidate.bar_start)
            parents["SPY"].add(candidate.bar_start)
    if not parents:
        return {}
    requested = sum(len(item) for item in parents.values())
    print(f"loading 1-minute bars for {requested} parents", flush=True)
    return load_parent_minutes(fresh, parents, calendar)


def _horizon(
    horizon: int,
    rows: list[Candidate],
    minutes: dict[tuple[str, datetime], tuple[ClosedBar, ...]],
    tape: BarTape,
    config: EngineConfig,
) -> dict[str, object]:
    baseline: dict[tuple[str, datetime], TradeIntent] = {}
    sequential: dict[tuple[str, datetime], TradeIntent] = {}
    terminals: Counter[str] = Counter()
    for candidate in rows:
        if candidate.horizon != horizon:
            raise RuntimeError("horizon mismatch")
        key = (candidate.symbol, candidate.signal_end)
        if key in baseline:
            raise RuntimeError("duplicate signal")
        baseline[key] = make_intent(candidate, arm="baseline")
        result = _confirm(candidate, minutes)
        terminals[result.terminal] += 1
        if result.accept:
            sequential[key] = make_intent(candidate, arm="sequential")
    base_result, base_seconds = _replay(config, tape, baseline)
    seq_result, seq_seconds = _replay(config, tape, sequential)
    base_scores = _splits_of(base_result.records)
    seq_scores = _splits_of(seq_result.records)
    winner = comparison(base_scores["validation"], seq_scores["validation"])
    chosen = seq_scores["validation"] if winner == "sequential" else base_scores["validation"]
    return {
        "signals": {"baseline": len(baseline), "sequential": len(sequential)},
        "sequential_terminals": dict(terminals),
        "comparison": winner,
        "deployable": winner != "insufficient" and deployable(chosen),
        "baseline": _arm_json(base_result, base_seconds, base_scores),
        "sequential": _arm_json(seq_result, seq_seconds, seq_scores),
    }


def _confirm(
    candidate: Candidate,
    minutes: dict[tuple[str, datetime], tuple[ClosedBar, ...]],
) -> SequentialResult:
    stock = minutes.get((candidate.symbol, candidate.bar_start), ())
    expected = tuple(candidate.bar_start + timedelta(minutes=step) for step in range(5))
    if tuple(bar.start for bar in stock) != expected:
        return SequentialResult(
            False, 0, Decimal(0), Decimal(0), "INCOMPLETE_MINUTES", None, None, None
        )
    spy = minutes.get(("SPY", candidate.bar_start), ())
    return confirm(stock, orh=candidate.orh, atr=candidate.atr, spy=spy)


def _replay(
    config: EngineConfig,
    tape: BarTape,
    intents: dict[tuple[str, datetime], TradeIntent],
) -> tuple[ReplayResult, Decimal]:
    started = perf_counter()
    result = OrderReplayEngine(config).run(tape, _Lookup(intents))
    return result, Decimal(str(perf_counter() - started))


class _Lookup:
    def __init__(self, intents: dict[tuple[str, datetime], TradeIntent]) -> None:
        self._intents = intents

    def on_close(self, view: CausalView, bar: ClosedBar) -> TradeIntent | None:
        if view.asof != bar.end:
            raise RuntimeError("decision clock is not the closed bar")
        return self._intents.get((bar.symbol, bar.end))


def _splits_of(records: tuple[IntentRecord, ...]) -> dict[str, SplitScore]:
    buckets: dict[str, list[IntentRecord]] = {"train": [], "validation": []}
    for record in records:
        buckets[_role(record.signal_bar_end.astimezone(NEW_YORK).date())].append(record)
    return {name: _score(rows) for name, rows in buckets.items()}


def _role(day: date) -> str:
    if day < TRAIN_START or day > VAL_END or day >= LOCKBOX_START:
        raise RuntimeError("outside the open window")
    if day <= TRAIN_END:
        return "train"
    return "validation"


def _score(records: list[IntentRecord]) -> SplitScore:
    closed = [
        record
        for record in records
        if record.filled_qty > 0 and record.open_qty == 0 and record.entry_price is not None
    ]
    closed.sort(key=lambda record: (record.exit_bar_end or record.signal_bar_end, record.intent_id))
    return summarize(
        [record.net_pnl for record in closed],
        [_bps(record) for record in closed],
        miss_rate=_miss(records),
    )


def _miss(records: list[IntentRecord]) -> Decimal | None:
    sent = [record for record in records if record.ordered_qty > 0]
    if not sent:
        return None
    missed = sum(1 for record in sent if record.filled_qty == 0)
    return Decimal(missed) / Decimal(len(sent))


def _bps(record: IntentRecord) -> Decimal:
    if record.entry_price is None or record.filled_qty <= 0:
        raise RuntimeError("closed trade has no entry")
    notional = record.entry_price * record.filled_qty
    return record.net_pnl / notional * Decimal(10000)


def _arm_json(
    result: ReplayResult,
    seconds: Decimal,
    scores: dict[str, SplitScore],
) -> dict[str, object]:
    status: Counter[str] = Counter()
    rejected: Counter[str] = Counter()
    for record in result.records:
        status[record.status] += 1
        for reason in record.rejection_reasons:
            rejected[reason] += 1
    return {
        "replay_seconds": format(seconds, "f"),
        "ending_cash": format(result.ending_cash, "f"),
        "halt_new_entries": result.halt_new_entries,
        "status": dict(status),
        "engine_rejections": dict(rejected),
        "scores": {name: _score_json(score) for name, score in scores.items()},
        "closed": {
            "train": _blotter(_bucket(result.records, "train")),
            "validation": _blotter(_bucket(result.records, "validation")),
        },
    }


def _bucket(records: tuple[IntentRecord, ...], role: str) -> list[IntentRecord]:
    return [row for row in records if _role(row.signal_bar_end.astimezone(NEW_YORK).date()) == role]


def _blotter(records: list[IntentRecord]) -> list[dict[str, object]]:
    closed = [
        record
        for record in records
        if record.filled_qty > 0 and record.open_qty == 0 and record.entry_price is not None
    ]
    closed.sort(key=lambda record: (record.exit_bar_end or record.signal_bar_end, record.intent_id))
    rows: list[dict[str, object]] = []
    for record in closed:
        rows.append(
            {
                "symbol": record.symbol,
                "signal_bar_end": record.signal_bar_end.isoformat(),
                "entry_price": None
                if record.entry_price is None
                else format(record.entry_price, "f"),
                "exit_price": None if record.exit_price is None else format(record.exit_price, "f"),
                "filled_qty": format(record.filled_qty, "f"),
                "exit_reason": record.exit_reason,
                "cancel_reason": record.cancel_reason,
                "ambiguous_stop": record.ambiguous_bar_sl_assumed,
                "net_pnl_usdc": format(record.net_pnl, "f"),
                "net_bps": format(_bps(record), "f"),
            }
        )
    return rows


def _closed(bar: FeatureBar) -> ClosedBar:
    if bar.end - bar.start != timedelta(minutes=5):
        raise RuntimeError(f"{bar.symbol} bar is not five minutes")
    return ClosedBar(
        symbol=bar.symbol,
        start=bar.start,
        end=bar.end,
        open=bar.open,
        high=bar.high,
        low=bar.low,
        close=bar.close,
        volume=bar.volume,
        timeframe="5m",
    )


def _score_json(score: SplitScore) -> dict[str, object]:
    return {
        "closed_trades": score.closed_trades,
        "mean_net_bps": _text(score.mean_net_bps),
        "se_net_bps": _text(score.se_net_bps),
        "profit_factor": _text(score.profit_factor),
        "max_drawdown_usdc": format(score.max_drawdown_usdc, "f"),
        "net_pnl_usdc": format(score.net_pnl_usdc, "f"),
        "miss_rate": _text(score.miss_rate),
    }


def _text(value: Decimal | None) -> str | None:
    if value is None:
        return None
    if value.is_infinite():
        return "inf"
    return format(value, "f")


if __name__ == "__main__":
    main()
