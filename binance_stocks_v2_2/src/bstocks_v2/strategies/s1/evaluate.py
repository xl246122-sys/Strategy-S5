"""S1 ablation through 2026-04-30. This module does not place orders."""

import json
from collections import Counter
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from time import perf_counter, perf_counter_ns

from bstocks_v2.constitution import live_trade_enabled
from bstocks_v2.contracts.models import TradeIntent
from bstocks_v2.feature_store.bars import FeatureBar, SymbolSnapshot
from bstocks_v2.feature_store.engine import CausalFeatureBook
from bstocks_v2.regime.classifier import BENCHMARKS, FeatureSnapshot, MarketRegimeClassifier
from bstocks_v2.research_backtest.models import EngineConfig, FillMode, IntentRecord
from bstocks_v2.research_backtest.replay import OrderReplayEngine
from bstocks_v2.research_backtest.tape import BarTape, CausalView, ClosedBar
from bstocks_v2.research_data.calendar import NEW_YORK, XnysCalendar, load_xnys_calendar
from bstocks_v2.research_data.paths import FRESH_DEFAULT, OUT_DEFAULT
from bstocks_v2.strategies.s1.book import FactorBoard
from bstocks_v2.strategies.s1.filters import Fit, make_filter
from bstocks_v2.strategies.s1.load import load_bars, load_membership, load_splits_until
from bstocks_v2.strategies.s1.score import ModelScore, recommend, summarize
from bstocks_v2.strategies.s1.signal import decide
from bstocks_v2.strategies.s1.spec import (
    COMMISSION_RATE,
    HALF_SPREAD,
    LOCKBOX_START,
    MODELS,
    PARENTS,
    SECTOR_PARENT,
    TRADED,
    TRAIN_END,
    TRAIN_START,
    VAL_END,
    VAL_START,
)

S1Trace = list[
    tuple[str, str, Fit, Decimal | None, SymbolSnapshot, FeatureSnapshot, bool]
]
REPORT = OUT_DEFAULT / "reports" / "stage6_ablation.json"
_MEMBERSHIP = OUT_DEFAULT / "universe" / "membership.parquet"


def main() -> None:
    summary = run(FRESH_DEFAULT, _MEMBERSHIP, REPORT)
    print(
        json.dumps(
            {
                "recommended": summary["recommended_model"],
                "max_ny_date": summary["max_ny_date"],
                "stock_eligible": summary["stock_eligible"],
            }
        )
    )


def run(fresh: Path, membership: Path, out: Path) -> dict[str, object]:
    if live_trade_enabled():
        raise RuntimeError("live trading is enabled")
    calendar = load_xnys_calendar()
    splits, ignored_splits = load_splits_until(fresh)
    eligible, stock_eligible = load_membership(membership)
    if stock_eligible != 0:
        raise RuntimeError("a stock was marked eligible without a point-in-time cap")
    symbols = tuple(sorted(set(BENCHMARKS) | set(TRADED)))
    print(f"loading {len(symbols)} symbols through {VAL_END.isoformat()}", flush=True)
    bars = load_bars(fresh, symbols, calendar, splits)
    bars.sort(key=lambda item: (item.start, item.symbol == "SPY", item.symbol))
    max_day = max(bar.start.astimezone(NEW_YORK).date() for bar in bars)
    if max_day >= LOCKBOX_START or max_day > VAL_END:
        raise RuntimeError("loaded bar is inside the lockbox")
    print(f"bars {len(bars)} last {max_day.isoformat()}", flush=True)
    intents, gates, updates, nanos, cosine = _signals(bars, calendar, eligible)
    replayed: dict[str, dict[str, object]] = {}
    scores: dict[str, dict[str, ModelScore]] = {}
    config = EngineConfig(mode=FillMode.CONSERVATIVE)
    if config.commission_rate != COMMISSION_RATE or config.half_spread != HALF_SPREAD:
        raise RuntimeError("replay costs drifted from the S1 spec")
    tape = BarTape([_closed(bar) for bar in bars if bar.symbol in SECTOR_PARENT])
    for model in MODELS:
        print(f"replay {model} signals {gates[model]['SIGNAL']}", flush=True)
        started = perf_counter()
        result = OrderReplayEngine(config).run(tape, _Lookup(intents[model]))
        elapsed = Decimal(str(perf_counter() - started))
        split_scores, operational = _splits(model, result.records)
        scores[model] = split_scores
        operational["replay_seconds"] = format(elapsed, "f")
        operational["ending_cash"] = format(result.ending_cash, "f")
        operational["halt_new_entries"] = result.halt_new_entries
        operational["step_seconds"] = _step(nanos[model], updates[model])
        operational["filter_updates"] = updates[model]
        operational["gate_reasons"] = dict(gates[model])
        replayed[model] = operational
    validation = {model: scores[model]["validation"] for model in MODELS}
    choice = recommend(validation)
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
        "sector_cosine": _text(cosine),
        "probabilities": "uncalibrated_simplex",
        "recommended_model": choice,
        "models": {
            model: {
                "train": _score_json(scores[model]["train"]),
                "validation": _score_json(scores[model]["validation"]),
                **replayed[model],
            }
            for model in MODELS
        },
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary


def _signals(
    bars: list[FeatureBar],
    calendar: XnysCalendar,
    eligible: set[tuple[str, str]],
    models: tuple[str, ...] = MODELS,
    trace: S1Trace | None = None,
) -> tuple[
    dict[str, dict[tuple[str, datetime], TradeIntent]],
    dict[str, Counter[str]],
    dict[str, int],
    dict[str, int],
    Decimal | None,
]:
    if not models or any(model not in MODELS for model in models):
        raise ValueError("model")
    book = CausalFeatureBook(calendar)
    classifier = MarketRegimeClassifier()
    board = FactorBoard()
    filters = {model: {symbol: make_filter(model) for symbol in TRADED} for model in models}
    intents: dict[str, dict[tuple[str, datetime], TradeIntent]] = {model: {} for model in models}
    gates = {model: Counter[str]() for model in models}
    updates = {model: 0 for model in models}
    nanos = {model: 0 for model in models}
    dot = Decimal(0)
    spy_energy = Decimal(0)
    perp_energy = Decimal(0)
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
        if spy is None or spy.bar_start != start:
            continue
        regime = classifier.decide(book.latest, spy.data_asof_utc)
        perps: dict[str, Decimal | None] = {}
        for sector in PARENTS:
            sector_bar = book.latest.get(sector)
            if (
                sector_bar is None
                or sector_bar.bar_start != start
                or sector_bar.ret_norm is None
                or spy.ret_norm is None
            ):
                perps[sector] = None
                continue
            perp = board.update(
                sector,
                spy.ret_norm,
                sector_bar.ret_norm,
                spy.data_asof_utc.isoformat(),
            )
            perps[sector] = perp
            if perp is not None:
                dot += spy.ret_norm * perp
                spy_energy += spy.ret_norm * spy.ret_norm
                perp_energy += perp * perp
        for symbol in TRADED:
            stock = book.latest.get(symbol)
            sector = SECTOR_PARENT[symbol]
            perp = perps.get(sector)
            if (
                stock is None
                or stock.bar_start != start
                or stock.ret_norm is None
                or spy.ret_norm is None
                or perp is None
            ):
                continue
            strength = board.strength(sector)
            allowed = (symbol, stock.ny_date) in eligible
            regressors = (Decimal(1), spy.ret_norm, perp)
            for model in models:
                started = perf_counter_ns()
                fit: Fit = filters[model][symbol].update(regressors, stock.ret_norm)
                nanos[model] += perf_counter_ns() - started
                updates[model] += 1
                if trace is not None and model == "ols":
                    trace.append((model, symbol, fit, strength, stock, regime, allowed))
                intent, reason = decide(
                    model=model,
                    symbol=symbol,
                    fit=fit,
                    sector_strength=strength,
                    stock=stock,
                    regime=regime,
                    research_eligible=allowed,
                )
                gates[model][reason] += 1
                if intent is not None:
                    intents[model][(symbol, stock.data_asof_utc)] = intent
        if chunks % 2000 == 0:
            joined = ",".join(models)
            print(f"chunks {chunks} bar {index} of {total} models {joined}", flush=True)
    cosine = None
    if spy_energy > 0 and perp_energy > 0:
        cosine = dot / (spy_energy * perp_energy).sqrt()
    return intents, gates, updates, nanos, cosine


class _Lookup:
    def __init__(self, intents: dict[tuple[str, datetime], TradeIntent]) -> None:
        self._intents = intents

    def on_close(self, view: CausalView, bar: ClosedBar) -> TradeIntent | None:
        if view.asof != bar.end:
            raise RuntimeError("decision clock is not the closed bar")
        return self._intents.get((bar.symbol, bar.end))


def _splits(
    model: str,
    records: tuple[IntentRecord, ...],
) -> tuple[dict[str, ModelScore], dict[str, object]]:
    buckets: dict[str, list[IntentRecord]] = {"train": [], "validation": []}
    status: Counter[str] = Counter()
    rejected: Counter[str] = Counter()
    for record in records:
        status[record.status] += 1
        for reason in record.rejection_reasons:
            rejected[reason] += 1
        buckets[_split(record)].append(record)
    scores = {name: _score(model, rows) for name, rows in buckets.items()}
    return scores, {
        "status": dict(status),
        "engine_rejections": dict(rejected),
        "signals": len(records),
        "signals_by_split": {name: len(rows) for name, rows in buckets.items()},
        "closed": {name: _blotter(rows) for name, rows in buckets.items()},
    }


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


def _score(model: str, records: list[IntentRecord]) -> ModelScore:
    closed = [
        record
        for record in records
        if record.filled_qty > 0 and record.open_qty == 0 and record.entry_price is not None
    ]
    closed.sort(key=lambda record: (record.exit_bar_end or record.signal_bar_end, record.intent_id))
    pnls = [record.net_pnl for record in closed]
    bps = [_bps(record) for record in closed]
    return summarize(model, pnls, bps)


def _bps(record: IntentRecord) -> Decimal:
    if record.entry_price is None or record.filled_qty <= 0:
        raise RuntimeError("closed trade has no entry")
    notional = record.entry_price * record.filled_qty
    if notional <= 0:
        raise RuntimeError("closed trade has no notional")
    return record.net_pnl / notional * Decimal(10000)


def _split(record: IntentRecord) -> str:
    day = record.signal_bar_end.astimezone(NEW_YORK).date()
    if day < TRAIN_START or day > VAL_END:
        raise RuntimeError("trade is outside the open window")
    if day <= TRAIN_END:
        return "train"
    if day < VAL_START:
        raise RuntimeError("trade falls in the gap between splits")
    return "validation"


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


def _step(nanos: int, updates: int) -> str | None:
    if updates <= 0:
        return None
    return format(Decimal(nanos) / Decimal(updates) / Decimal("1000000000"), "f")


def _score_json(score: ModelScore) -> dict[str, object]:
    return {
        "closed_trades": score.closed_trades,
        "mean_net_bps": _text(score.mean_net_bps),
        "se_net_bps": _text(score.se_net_bps),
        "profit_factor": _text(score.profit_factor),
        "max_drawdown_usdc": format(score.max_drawdown_usdc, "f"),
        "net_pnl_usdc": format(score.net_pnl_usdc, "f"),
    }


def _text(value: Decimal | None) -> str | None:
    if value is None:
        return None
    if value.is_infinite():
        return "inf"
    return format(value, "f")


if __name__ == "__main__":
    main()
