"""S2 ablation through 2026-04-30. This module does not place orders."""

import json
from collections import Counter
from datetime import datetime, timedelta
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
from bstocks_v2.strategies.s1.spec import (
    LOCKBOX_START as S1_LOCK,
)
from bstocks_v2.strategies.s1.spec import (
    PARENTS,
    SECTOR_PARENT,
    TRADED,
)
from bstocks_v2.strategies.s1.spec import (
    TRAIN_END as S1_TRAIN_END,
)
from bstocks_v2.strategies.s1.spec import (
    TRAIN_START as S1_TRAIN_START,
)
from bstocks_v2.strategies.s1.spec import (
    VAL_END as S1_VAL_END,
)
from bstocks_v2.strategies.s1.spec import (
    VAL_START as S1_VAL_START,
)
from bstocks_v2.strategies.s2.label import PathLabel, label_path, train_timeout_bps
from bstocks_v2.strategies.s2.model import (
    FitError,
    Fitted,
    Sample,
    deduped,
    fit,
    split_role,
    usable,
    vol_bucket,
    vol_edges,
)
from bstocks_v2.strategies.s2.pattern import PullbackBook
from bstocks_v2.strategies.s2.score import SplitScore, prefer_gate, summarize
from bstocks_v2.strategies.s2.signal import (
    UNCALIBRATED,
    Candidate,
    decide,
    expected_net_bps,
    gate_open,
    make_intent,
)
from bstocks_v2.strategies.s2.simplex import (
    BinRow,
    bootstrap_ece_gap,
    bootstrap_interval,
    brier,
    choose_temperature,
    confidence_ece,
    reliability,
    softmax,
    temper,
)
from bstocks_v2.strategies.s2.spec import (
    BOOTSTRAP_DRAWS,
    BOOTSTRAP_SEED,
    CAL_END,
    COMMISSION_RATE,
    COOLDOWN_BARS,
    HALF_SPREAD,
    LOCKBOX_START,
    MIN_CALIBRATION,
    MIN_FIT,
    MIN_SCORE_METRICS,
    PENETRATION,
    PRIOR_VAR_FEATURE,
    PRIOR_VAR_SECTOR,
    PRIOR_VAR_VOL,
    SCORE_START,
    TEMPERATURE_MAX,
    TEMPERATURE_MIN,
    TEMPERATURE_STEP,
    TRAIN_END,
    TRAIN_START,
    VAL_END,
    VAL_START,
)

REPORT = OUT_DEFAULT / "reports" / "stage7_ablation.json"
_MEMBERSHIP = OUT_DEFAULT / "universe" / "membership.parquet"
_LABEL = {"TP": 0, "SL": 1, "TO": 2}
_EARLY = {"NOT_IN_S2_UNIVERSE", "NOT_ELIGIBLE", "REGIME_ASOF", "REGIME", "NO_REGIME"}


def main() -> None:
    summary = run(FRESH_DEFAULT, _MEMBERSHIP, REPORT)
    print(
        json.dumps(
            {
                "recommended": summary["recommended"],
                "max_ny_date": summary["max_ny_date"],
                "signals": summary["signals"],
                "ece_improved": summary["ece_improved"],
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
    candidates, gates, after = _signals(bars, calendar, eligible)
    print(f"signals {len(candidates)}", flush=True)
    labels = _labels(candidates, bars, calendar)
    timeout_bps = train_timeout_bps(
        [
            (item.ny_date, label.outcome, label.gross_bps)
            for item, label in zip(candidates, labels, strict=True)
        ]
    )
    fitted, temperature, fit_meta = _fit_model(candidates, labels)
    rule_map, gate_map, gate_counts = _books(candidates, fitted, temperature, timeout_bps)
    config = _config()
    tape = BarTape([_closed(bar) for bar in bars if bar.symbol in SECTOR_PARENT])
    print(f"replay rule {len(rule_map)}", flush=True)
    rule_result, rule_seconds = _replay(config, tape, rule_map)
    print(f"replay gate {len(gate_map)}", flush=True)
    gate_result, gate_seconds = _replay(config, tape, gate_map)
    rule_scores, rule_ops = _splits_of(rule_result.records)
    gate_scores, gate_ops = _splits_of(gate_result.records)
    metrics = _metrics(candidates, labels, fitted, temperature)
    recommended = "rule"
    if (
        "score" in rule_scores
        and "score" in gate_scores
        and prefer_gate(rule_scores["score"], gate_scores["score"])
    ):
        recommended = "gate"
    summary: dict[str, object] = {
        "live_trade_enabled": False,
        "train": [TRAIN_START.isoformat(), TRAIN_END.isoformat()],
        "calibration": [VAL_START.isoformat(), CAL_END.isoformat()],
        "score": [SCORE_START.isoformat(), VAL_END.isoformat()],
        "lockbox_start": LOCKBOX_START.isoformat(),
        "lockbox_touched": False,
        "max_ny_date": max_day.isoformat(),
        "symbols": list(symbols),
        "traded": list(TRADED),
        "stock_eligible": stock_eligible,
        "eligible_pairs": len(eligible),
        "splits_after_cutoff_ignored": ignored_splits,
        "bars": len(bars),
        "signals": len(candidates),
        "gate_counts": gate_counts,
        "first_failure": dict(gates),
        "after_regime": dict(after),
        "timeout_bps_train": format(timeout_bps, "f"),
        "fit": fit_meta,
        "temperature": format(temperature, "f"),
        "calibration_metrics": metrics,
        "ece_improved": metrics["ece_improved"],
        "recommended": recommended,
        "rule": {
            "replay_seconds": format(rule_seconds, "f"),
            "ending_cash": format(rule_result.ending_cash, "f"),
            "halt_new_entries": rule_result.halt_new_entries,
            **rule_ops,
            "scores": {name: _score_json(score) for name, score in rule_scores.items()},
        },
        "gate": {
            "replay_seconds": format(gate_seconds, "f"),
            "ending_cash": format(gate_result.ending_cash, "f"),
            "halt_new_entries": gate_result.halt_new_entries,
            **gate_ops,
            "scores": {name: _score_json(score) for name, score in gate_scores.items()},
        },
        "labels": _label_counts(candidates, labels),
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
        raise RuntimeError("replay costs drifted from the S2 spec")
    return config


S2Trace = list[tuple[FeatureBar, SymbolSnapshot, FeatureSnapshot | None, bool, bool]]


def _signals(
    bars: list[FeatureBar],
    calendar: XnysCalendar,
    eligible: set[tuple[str, str]],
    trace: S2Trace | None = None,
) -> tuple[list[Candidate], Counter[str], Counter[str]]:
    book = CausalFeatureBook(calendar)
    classifier = MarketRegimeClassifier()
    pullback = PullbackBook()
    candidates: list[Candidate] = []
    gates: Counter[str] = Counter()
    after: Counter[str] = Counter()
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
            pattern, reason = pullback.observe(bar, snap)
            day = bar.start.astimezone(NEW_YORK).date()
            half = calendar.close_time(day).hour == 13
            allowed = (bar.symbol, snap.ny_date) in eligible
            if trace is not None:
                trace.append((bar, snap, regime, allowed, half))
            if regime is None:
                gates["NO_REGIME"] += 1
                continue
            candidate, why = decide(
                symbol=bar.symbol,
                pattern=pattern,
                pattern_reason=reason,
                snap=snap,
                regime=regime,
                research_eligible=allowed,
                half_day=half,
                book=pullback,
            )
            gates[why] += 1
            if why not in _EARLY:
                after[why] += 1
            if candidate is not None:
                candidates.append(candidate)
        if chunks % 2000 == 0:
            print(f"chunks {chunks} bar {index} of {total}", flush=True)
    return candidates, gates, after


def _labels(
    candidates: list[Candidate],
    bars: list[FeatureBar],
    calendar: XnysCalendar,
) -> list[PathLabel]:
    grouped: dict[str, list[ClosedBar]] = {}
    for bar in bars:
        if bar.symbol in SECTOR_PARENT:
            grouped.setdefault(bar.symbol, []).append(_closed(bar))
    located = {
        (symbol, row.end): cursor
        for symbol, rows in grouped.items()
        for cursor, row in enumerate(rows)
    }
    labels: list[PathLabel] = []
    for candidate in candidates:
        cursor = located.get((candidate.symbol, candidate.signal_end))
        if cursor is None:
            raise RuntimeError("signal bar is missing from the tape")
        labels.append(
            label_path(
                grouped[candidate.symbol],
                cursor,
                limit=candidate.limit_price,
                stop=candidate.stop_price,
                take_profit=candidate.take_profit_price,
                calendar=calendar,
            )
        )
    return labels


def _fit_model(
    candidates: list[Candidate],
    labels: list[PathLabel],
) -> tuple[Fitted | None, Decimal, dict[str, object]]:
    packed: list[tuple[datetime, str, Candidate, PathLabel]] = []
    for candidate, label in zip(candidates, labels, strict=True):
        role = split_role(candidate.ny_date)
        if usable(role, label.outcome, candidate.rel_vol, temperature=False):
            packed.append((candidate.signal_end, candidate.symbol, candidate, label))
    packed.sort(key=lambda item: (item[1], item[0]))
    weighted = deduped(
        [
            (
                when,
                symbol,
                Sample(
                    item.features, PARENTS.index(item.sector), 0, _LABEL[label.outcome], Decimal(1)
                ),
            )
            for when, symbol, item, label in packed
        ],
        timedelta(minutes=5 * COOLDOWN_BARS),
    )
    zeros = sum(1 for sample in weighted if sample.weight == 0)
    active = [
        (sample, item.rel_vol)
        for sample, (_when, _symbol, item, _label) in zip(weighted, packed, strict=True)
        if sample.weight > 0 and item.rel_vol is not None
    ]
    meta: dict[str, object] = {
        "train_labeled": len(active),
        "downweighted": zeros,
        "fitted": False,
        "temperature_fitted": False,
    }
    if len(active) < MIN_FIT:
        return None, Decimal(1), meta
    edges = vol_edges([rel for _sample, rel in active])
    samples = [
        Sample(
            sample.features,
            sample.sector_index,
            vol_bucket(rel, edges),
            sample.label,
            sample.weight,
        )
        for sample, rel in active
    ]
    try:
        fitted = fit(
            samples,
            parents=len(PARENTS),
            vol_edges_pair=edges,
            prior_feature=PRIOR_VAR_FEATURE,
            prior_sector=PRIOR_VAR_SECTOR,
            prior_vol=PRIOR_VAR_VOL,
        )
    except FitError as exc:
        meta["error"] = str(exc)
        return None, Decimal(1), meta
    temperature, fitted_flag = _temperature(candidates, labels, fitted)
    meta["fitted"] = True
    meta["temperature_fitted"] = fitted_flag
    meta["vol_edges"] = [format(edges[0], "f"), format(edges[1], "f")]
    meta["width"] = fitted.width
    return fitted, temperature, meta


def _temperature(
    candidates: list[Candidate],
    labels: list[PathLabel],
    fitted: Fitted,
) -> tuple[Decimal, bool]:
    logits: list[tuple[Decimal, Decimal, Decimal]] = []
    classes: list[int] = []
    for candidate, label in zip(candidates, labels, strict=True):
        if candidate.rel_vol is None:
            continue
        if not usable(
            split_role(candidate.ny_date), label.outcome, candidate.rel_vol, temperature=True
        ):
            continue
        logits.append(
            fitted.logits(candidate.features, PARENTS.index(candidate.sector), candidate.rel_vol)
        )
        classes.append(_LABEL[label.outcome])
    if len(logits) < MIN_CALIBRATION:
        return Decimal(1), False
    chosen = choose_temperature(
        logits,
        classes,
        low=TEMPERATURE_MIN,
        high=TEMPERATURE_MAX,
        step=TEMPERATURE_STEP,
    )
    return chosen, True


def _books(
    candidates: list[Candidate],
    fitted: Fitted | None,
    temperature: Decimal,
    timeout_bps: Decimal,
) -> tuple[
    dict[tuple[str, datetime], TradeIntent], dict[tuple[str, datetime], TradeIntent], dict[str, int]
]:
    rule: dict[tuple[str, datetime], TradeIntent] = {}
    gate: dict[tuple[str, datetime], TradeIntent] = {}
    counts = {"train_raw": 0, "score_temperature": 0, "skipped_calibration": 0, "no_model": 0}
    for candidate in candidates:
        key = (candidate.symbol, candidate.signal_end)
        rule[key] = make_intent(
            candidate,
            kind="rule",
            probabilities=UNCALIBRATED,
            timeout_bps=Decimal(0),
            model_version="s2-v1-rule-uncalibrated",
        )
        role = split_role(candidate.ny_date)
        if role == "cal":
            counts["skipped_calibration"] += 1
            continue
        if fitted is None or candidate.rel_vol is None:
            counts["no_model"] += 1
            continue
        scale = Decimal(1) if role == "train" else temperature
        kind = "raw" if role == "train" else "temp"
        version = "s2-v1-simplex-raw" if role == "train" else "s2-v1-simplex-temperature"
        probabilities = fitted.probabilities(
            candidate.features,
            PARENTS.index(candidate.sector),
            candidate.rel_vol,
            scale,
        )
        net = expected_net_bps(
            probabilities,
            limit=candidate.limit_price,
            stop=candidate.stop_price,
            take_profit=candidate.take_profit_price,
            timeout_bps=timeout_bps,
        )
        if not gate_open(net):
            continue
        gate[key] = make_intent(
            candidate,
            kind=kind,
            probabilities=probabilities,
            timeout_bps=timeout_bps,
            model_version=version,
        )
        counts["train_raw" if role == "train" else "score_temperature"] += 1
    return rule, gate, counts


def _metrics(
    candidates: list[Candidate],
    labels: list[PathLabel],
    fitted: Fitted | None,
    temperature: Decimal,
) -> dict[str, object]:
    empty: dict[str, object] = {
        "score_labeled": 0,
        "ece_improved": False,
        "ece_raw": None,
        "ece_calibrated": None,
        "ece_gap_ci95": None,
        "brier_raw": None,
        "brier_calibrated": None,
        "brier_ci95": None,
        "reliability": None,
    }
    if fitted is None:
        return empty
    raw_rows: list[tuple[tuple[Decimal, Decimal, Decimal], int]] = []
    cal_rows: list[tuple[tuple[Decimal, Decimal, Decimal], int]] = []
    for candidate, label in zip(candidates, labels, strict=True):
        if candidate.rel_vol is None or label.outcome not in _LABEL:
            continue
        if split_role(candidate.ny_date) != "score":
            continue
        logits = fitted.logits(
            candidate.features, PARENTS.index(candidate.sector), candidate.rel_vol
        )
        raw_rows.append((softmax(logits), _LABEL[label.outcome]))
        cal_rows.append((temper(logits, temperature), _LABEL[label.outcome]))
    empty["score_labeled"] = len(raw_rows)
    if len(raw_rows) < MIN_SCORE_METRICS:
        return empty
    raw_ece = confidence_ece(raw_rows)
    cal_ece = confidence_ece(cal_rows)
    gap = bootstrap_ece_gap(raw_rows, cal_rows, BOOTSTRAP_DRAWS, BOOTSTRAP_SEED)
    raw_brier = brier(raw_rows)
    cal_brier = brier(cal_rows)
    brier_ci = bootstrap_interval(
        [_one_brier(raw_rows[index]) for index in range(len(raw_rows))],
        BOOTSTRAP_DRAWS,
        BOOTSTRAP_SEED + 1,
    )
    return {
        "score_labeled": len(raw_rows),
        "ece_improved": gap[0] > 0,
        "ece_raw": format(raw_ece, "f"),
        "ece_calibrated": format(cal_ece, "f"),
        "ece_gap_ci95": [format(gap[0], "f"), format(gap[1], "f")],
        "brier_raw": format(raw_brier, "f"),
        "brier_calibrated": format(cal_brier, "f"),
        "brier_ci95": None
        if brier_ci is None
        else [format(brier_ci[0], "f"), format(brier_ci[1], "f")],
        "reliability": {
            name: [_bin_json(row) for row in rows] for name, rows in reliability(cal_rows).items()
        },
    }


def _one_brier(row: tuple[tuple[Decimal, Decimal, Decimal], int]) -> Decimal:
    return brier([row])


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


def _splits_of(
    records: tuple[IntentRecord, ...],
) -> tuple[dict[str, SplitScore], dict[str, object]]:
    buckets: dict[str, list[IntentRecord]] = {"train": [], "cal": [], "score": []}
    status: Counter[str] = Counter()
    rejected: Counter[str] = Counter()
    for record in records:
        status[record.status] += 1
        for reason in record.rejection_reasons:
            rejected[reason] += 1
        buckets[split_role(record.signal_bar_end.astimezone(NEW_YORK).date())].append(record)
    return {name: _score(rows) for name, rows in buckets.items()}, {
        "status": dict(status),
        "engine_rejections": dict(rejected),
        "signals": len(records),
        "signals_by_split": {name: len(rows) for name, rows in buckets.items()},
        "closed": {name: _blotter(rows) for name, rows in buckets.items()},
    }


def _score(records: list[IntentRecord]) -> SplitScore:
    closed = [
        record
        for record in records
        if record.filled_qty > 0 and record.open_qty == 0 and record.entry_price is not None
    ]
    closed.sort(key=lambda record: (record.exit_bar_end or record.signal_bar_end, record.intent_id))
    return summarize([record.net_pnl for record in closed], [_bps(record) for record in closed])


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
                "entry_bar_end": None
                if record.entry_bar_end is None
                else record.entry_bar_end.isoformat(),
                "exit_bar_end": None
                if record.exit_bar_end is None
                else record.exit_bar_end.isoformat(),
                "entry_price": format(record.entry_price, "f") if record.entry_price else None,
                "exit_price": format(record.exit_price, "f") if record.exit_price else None,
                "filled_qty": format(record.filled_qty, "f"),
                "exit_reason": record.exit_reason,
                "ambiguous_stop": record.ambiguous_bar_sl_assumed,
                "net_pnl_usdc": format(record.net_pnl, "f"),
                "net_bps": format(_bps(record), "f"),
            }
        )
    return rows


def _bps(record: IntentRecord) -> Decimal:
    if record.entry_price is None or record.filled_qty <= 0:
        raise RuntimeError("closed trade has no entry")
    notional = record.entry_price * record.filled_qty
    if notional <= 0:
        raise RuntimeError("closed trade has no notional")
    return record.net_pnl / notional * Decimal(10000)


def _label_counts(
    candidates: list[Candidate], labels: list[PathLabel]
) -> dict[str, dict[str, int]]:
    counts: dict[str, Counter[str]] = {"train": Counter(), "cal": Counter(), "score": Counter()}
    ambiguous = 0
    for candidate, label in zip(candidates, labels, strict=True):
        counts[split_role(candidate.ny_date)][label.outcome] += 1
        if label.ambiguous:
            ambiguous += 1
    return {name: dict(counter) for name, counter in counts.items()} | {
        "ambiguous_stops": {"count": ambiguous}
    }


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
    }


def _bin_json(row: BinRow) -> dict[str, object]:
    return {
        "lower": format(row.lower, "f"),
        "upper": format(row.upper, "f"),
        "count": row.count,
        "mean_probability": _text(row.mean_probability),
        "observed_rate": _text(row.observed_rate),
    }


def _text(value: Decimal | None) -> str | None:
    if value is None:
        return None
    if value.is_infinite():
        return "inf"
    return format(value, "f")


if __name__ == "__main__":
    main()
