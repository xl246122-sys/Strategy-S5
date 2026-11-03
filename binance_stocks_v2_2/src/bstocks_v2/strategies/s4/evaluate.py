"""S4 ablation through 2026-04-30. This module does not place orders."""

import json
from collections import Counter
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
from bstocks_v2.strategies.s4.bounce import BounceBook
from bstocks_v2.strategies.s4.lock import s4_live_enabled
from bstocks_v2.strategies.s4.score import SplitScore, comparison, live_deployable, summarize
from bstocks_v2.strategies.s4.signal import Candidate, decide, make_intent
from bstocks_v2.strategies.s4.spec import (
    COMMISSION_RATE,
    HALF_LIFE_MAX_MINUTES,
    HALF_SPREAD,
    LOCKBOX_START,
    PENETRATION,
    RSI_OVERSOLD,
    RVOL_KNIFE,
    TRAIN_END,
    TRAIN_START,
    VAL_END,
    VAL_START,
    WINDOW,
    Z_STRETCH,
)

REPORT = OUT_DEFAULT / "reports" / "stage9_ablation.json"


def main() -> None:
    summary = run(FRESH_DEFAULT, OUT_DEFAULT / "universe" / "membership.parquet", REPORT)
    print(
        json.dumps(
            {
                "max_ny_date": summary["max_ny_date"],
                "signals": summary["signals"],
                "comparison": summary["comparison"],
                "live_deployable": summary["live_deployable"],
            }
        )
    )


def run(fresh: Path, membership: Path, out: Path) -> dict[str, object]:
    if live_trade_enabled() or s4_live_enabled():
        raise RuntimeError("live trading is enabled")
    if live_deployable():
        raise RuntimeError("S4 was marked deployable")
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
    baseline, guarded, gates, knives, regimes = _signals(bars, calendar, eligible)
    print(f"signals baseline {len(baseline)} guarded {len(guarded)}", flush=True)
    tape = BarTape([_closed(bar) for bar in bars if bar.symbol in SECTOR_PARENT])
    config = _config()
    base_map = {
        (item.symbol, item.signal_end): make_intent(item, arm="baseline") for item in baseline
    }
    guard_map = {
        (item.symbol, item.signal_end): make_intent(item, arm="guarded") for item in guarded
    }
    print(f"replay baseline {len(base_map)}", flush=True)
    base_result, base_seconds = _replay(config, tape, base_map)
    print(f"replay guarded {len(guard_map)}", flush=True)
    guard_result, guard_seconds = _replay(config, tape, guard_map)
    base_scores = _splits_of(base_result.records)
    guard_scores = _splits_of(guard_result.records)
    winner = comparison(base_scores["validation"], guard_scores["validation"])
    summary: dict[str, object] = {
        "live_trade_enabled": False,
        "live_enabled": False,
        "s4_live_enabled": False,
        "live_deployable": False,
        "probability_gate": False,
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
        "window": WINDOW,
        "rsi_oversold": format(RSI_OVERSOLD, "f"),
        "z_stretch": format(Z_STRETCH, "f"),
        "half_life_max_minutes": format(HALF_LIFE_MAX_MINUTES, "f"),
        "rvol_knife": format(RVOL_KNIFE, "f"),
        "signals": {"baseline": len(baseline), "guarded": len(guarded)},
        "baseline_regimes": dict(regimes),
        "first_failure": {"baseline": dict(gates["baseline"]), "guarded": dict(gates["guarded"])},
        "knife_details": dict(knives),
        "guarded_phi": _phi_summary(guarded),
        "comparison": winner,
        "baseline": _arm_json(base_result, base_seconds, base_scores),
        "guarded": _arm_json(guard_result, guard_seconds, guard_scores),
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
        raise RuntimeError("replay costs drifted from the S4 spec")
    return config


S4Trace = list[
    tuple[
        FeatureBar,
        SymbolSnapshot,
        FeatureSnapshot | None,
        bool,
        bool,
        FeatureBar | None,
        SymbolSnapshot | None,
    ]
]


def _signals(
    bars: list[FeatureBar],
    calendar: XnysCalendar,
    eligible: set[tuple[str, str]],
    trace: S4Trace | None = None,
) -> tuple[
    list[Candidate],
    list[Candidate],
    dict[str, Counter[str]],
    Counter[str],
    Counter[str],
]:
    book = CausalFeatureBook(calendar)
    classifier = MarketRegimeClassifier()
    bounce = BounceBook()
    baseline: list[Candidate] = []
    guarded: list[Candidate] = []
    gates: dict[str, Counter[str]] = {"baseline": Counter(), "guarded": Counter()}
    knives: Counter[str] = Counter()
    regimes: Counter[str] = Counter()
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
        spy_bar = next((item for item in chunk if item.symbol == "SPY"), None)
        for bar in chunk:
            if bar.symbol not in SECTOR_PARENT:
                continue
            snap = book.latest.get(bar.symbol)
            if snap is None or snap.bar_start != bar.start:
                continue
            pattern, why = bounce.observe(bar, snap)
            day = bar.start.astimezone(NEW_YORK).date()
            half = calendar.close_time(day).hour == 13
            allowed = (bar.symbol, snap.ny_date) in eligible
            spy_snap = spy if spy is not None and spy.bar_start == start else None
            if trace is not None:
                trace.append((bar, snap, regime, allowed, half, spy_bar, spy_snap))
            base = decide(
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
            guard = decide(
                arm="guarded",
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
            gates["baseline"][base.reason] += 1
            gates["guarded"][guard.reason] += 1
            for detail in guard.knife:
                knives[detail] += 1
            if base.candidate is not None:
                baseline.append(base.candidate)
                regimes[base.candidate.regime] += 1
            if guard.candidate is not None:
                guarded.append(guard.candidate)
        if chunks % 2000 == 0:
            print(f"chunks {chunks} bar {index} of {total}", flush=True)
    return baseline, guarded, gates, knives, regimes


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


def _phi_summary(rows: list[Candidate]) -> dict[str, object]:
    lives = [item.half_life_minutes for item in rows if item.half_life_minutes is not None]
    phis = [item.phi for item in rows if item.phi is not None]
    return {
        "count": len(rows),
        "mean_phi": _mean_text(phis),
        "mean_half_life_minutes": _mean_text(lives),
        "max_half_life_minutes": None if not lives else format(max(lives), "f"),
    }


def _mean_text(values: list[Decimal]) -> str | None:
    if not values:
        return None
    return format(sum(values, start=Decimal(0)) / Decimal(len(values)), "f")


def _arm_json(
    result: ReplayResult,
    seconds: Decimal,
    scores: dict[str, SplitScore],
) -> dict[str, object]:
    status: Counter[str] = Counter()
    rejected: Counter[str] = Counter()
    exits: Counter[str] = Counter()
    for record in result.records:
        status[record.status] += 1
        for reason in record.rejection_reasons:
            rejected[reason] += 1
        if record.filled_qty > 0 and record.open_qty == 0:
            exits[record.exit_reason] += 1
    return {
        "replay_seconds": format(seconds, "f"),
        "ending_cash": format(result.ending_cash, "f"),
        "halt_new_entries": result.halt_new_entries,
        "status": dict(status),
        "engine_rejections": dict(rejected),
        "exit_reasons": dict(exits),
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
