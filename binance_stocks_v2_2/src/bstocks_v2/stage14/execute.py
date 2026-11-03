"""One lockbox pass. A second call refuses after the seal is written."""

import hashlib
import json
from collections.abc import Callable
from concurrent.futures import ProcessPoolExecutor
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from bstocks_v2.constitution.guard import live_trade_enabled
from bstocks_v2.contracts.models import TradeIntent
from bstocks_v2.feature_store.bars import FeatureBar
from bstocks_v2.limit_optimizer.fill_model import FillCalibrator
from bstocks_v2.research_backtest.models import EngineConfig
from bstocks_v2.research_backtest.tape import BarTape
from bstocks_v2.research_data.calendar import NEW_YORK, XnysCalendar, load_xnys_calendar
from bstocks_v2.research_data.paths import FRESH_DEFAULT, OUT_DEFAULT
from bstocks_v2.stage14.audit import safety_audit
from bstocks_v2.stage14.bands import Bands, isolation_bands
from bstocks_v2.stage14.bars import load_membership, load_universe, symbol_list
from bstocks_v2.stage14.replay import (
    closes_of,
    conservative_config,
    gap_open,
    loose_config,
    play,
    reprice,
    stress_configs,
    train_fill_models,
    windowed,
)
from bstocks_v2.stage14.scan import (
    agree,
    patched,
    replay_s1,
    replay_s2,
    replay_s3,
    replay_s4,
    scan_s1,
    scan_s2,
    scan_s3,
    scan_s4,
)
from bstocks_v2.stage14.seal import write_seal
from bstocks_v2.stage14.spec import (
    LOCKBOX_END,
    NEIGHBOR_DOWN,
    NEIGHBOR_UP,
    OPEN_P0_P1,
    STRESS_GAP,
    TRIALS,
)
from bstocks_v2.stage14.trials import validation_sharpes
from bstocks_v2.stage14.verdict import judge
from bstocks_v2.strategies.s1 import signal as s1_signal
from bstocks_v2.strategies.s1.evaluate import S1Trace
from bstocks_v2.strategies.s1.spec import LOCKBOX_START, TRAIN_END, TRAIN_START, VAL_END, VAL_START
from bstocks_v2.strategies.s2 import pattern as s2_pattern
from bstocks_v2.strategies.s2.evaluate import S2Trace
from bstocks_v2.strategies.s3 import signal as s3_signal
from bstocks_v2.strategies.s3.evaluate import S3Trace
from bstocks_v2.strategies.s3.spec import HORIZONS
from bstocks_v2.strategies.s4 import bounce as s4_bounce
from bstocks_v2.strategies.s4.evaluate import S4Trace
from bstocks_v2.strategies.s4.lock import s4_live_enabled

IntentBook = dict[tuple[str, datetime], TradeIntent]
Scanner = Callable[[], IntentBook]


def main() -> None:
    out = OUT_DEFAULT / "reports"
    payload = execute(out, FRESH_DEFAULT)
    decisions = payload["decisions"]
    print(json.dumps(decisions), flush=True)


def execute(out_dir: Path, fresh: Path) -> dict[str, object]:
    result_path = out_dir / "stage14_lockbox.json"
    seal_path = out_dir / "stage14_lockbox_seal.json"
    if result_path.exists() or seal_path.exists():
        raise RuntimeError("lockbox output already exists")
    if live_trade_enabled() or s4_live_enabled():
        raise RuntimeError("live trading is enabled")
    calendar = load_xnys_calendar()
    bands = isolation_bands(calendar)
    _sharpes, trial_rows = validation_sharpes()
    print("running S1 S3 S4 S2 one at a time", flush=True)
    jobs = [(name, str(fresh)) for name in ("S1", "S3", "S4", "S2")]
    with ProcessPoolExecutor(max_workers=1) as pool:
        finished = list(pool.map(_run_named, jobs))
    strategies: dict[str, dict[str, object]] = {}
    metas: list[dict[str, object]] = []
    for item in finished:
        meta = item.pop("_meta")
        if not isinstance(meta, dict) or not isinstance(meta.get("name"), str):
            raise RuntimeError("worker meta")
        strategies[str(meta["name"])] = item
        metas.append(meta)
    if len({row.get("bars") for row in metas}) != 1:
        raise RuntimeError("workers saw different bar counts")
    sample = metas[0]
    latest = sample["max_ny_date"]
    stocks = sample["stock_eligible"]
    ignored_splits = sample["splits_after_cutoff_ignored"]
    bar_count = sample["bars"]
    eligible_pairs = sample["eligible_pairs"]
    fill_model_count = sample["fill_models"]
    if not isinstance(latest, str) or not isinstance(bar_count, int):
        raise RuntimeError("worker meta types")
    if not isinstance(stocks, int) or not isinstance(ignored_splits, int):
        raise RuntimeError("worker meta types")
    if not isinstance(eligible_pairs, int) or not isinstance(fill_model_count, int):
        raise RuntimeError("worker meta types")
    decisions = {
        "S1": _decide(strategies["S1"], selected=True, live_locked=False),
        "S2": _decide(strategies["S2"], selected=True, live_locked=False),
        "S3": _decide(strategies["S3"], selected=False, live_locked=False),
        "S4": _decide(strategies["S4"], selected=True, live_locked=True),
    }
    payload: dict[str, object] = {
        "live_trade_enabled": False,
        "s4_live_enabled": False,
        "train": [TRAIN_START.isoformat(), TRAIN_END.isoformat()],
        "validation_scored": _dates(bands.validation),
        "lockbox_scored": _dates(bands.lockbox),
        "embargo": _dates(bands.embargo),
        "lockbox_bounds": [LOCKBOX_START.isoformat(), LOCKBOX_END.isoformat()],
        "validation_bounds": [VAL_START.isoformat(), VAL_END.isoformat()],
        "max_ny_date": latest,
        "bars": bar_count,
        "stock_eligible": stocks,
        "eligible_pairs": eligible_pairs,
        "splits_after_cutoff_ignored": ignored_splits,
        "fill_models": fill_model_count,
        "fill_horizon_bars": 2,
        "fill_fit_window": "train_only",
        "trials": list(TRIALS),
        "trial_sharpes": trial_rows,
        "open_p0_p1": list(OPEN_P0_P1),
        "p0_p1_clear": False,
        "spec_sha256": _spec_hashes(),
        "safety_audit": safety_audit(),
        "stage_notes": {
            "A": "frozen rule or simple baseline, loose touch fill",
            "B": "math arm, loose touch fill; S2 gate stays unfitted",
            "C": "frozen arm, conservative penetration, raw limit",
            "D": "frozen arm, conservative penetration, limit optimizer, account gates",
            "account_gates": "present on A through D; ConstitutionGuard is not switched off",
            "mfe_trail": "not newly fit; exits stay the frozen structural bracket",
            "account": "each split starts at 1600 USDC and does not inherit the other split",
            "neighborhood": (
                "one-at-a-time ±15% on the lockbox, stage D stack, not a retune; "
                "neighbors replay the scanned decision inputs after that replay matches the scan"
            ),
        },
        "strategies": _public(strategies),
        "decisions": decisions,
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, indent=2)
    result_path.write_text(text, encoding="utf-8")
    write_seal(seal_path, result_path, text)
    return payload


def _run_named(job: tuple[str, str]) -> dict[str, object]:
    name, fresh_text = job
    fresh = Path(fresh_text)
    calendar = load_xnys_calendar()
    bands = isolation_bands(calendar)
    sharpes, _rows = validation_sharpes()
    symbols = symbol_list()
    print(f"{name} loading {len(symbols)} symbols", flush=True)
    bars, ignored_splits = load_universe(fresh, calendar, symbols)
    bars.sort(key=lambda item: (item.start, item.symbol == "SPY", item.symbol))
    latest = max(bar.start.astimezone(NEW_YORK).date() for bar in bars)
    if latest > LOCKBOX_END or latest < LOCKBOX_END:
        raise RuntimeError(f"bar sample ends on {latest.isoformat()}, not the lockbox end")
    eligible, stocks = load_membership(OUT_DEFAULT / "universe" / "membership.parquet")
    tape = make_tape_local(bars)
    closes = closes_of(bars)
    models = train_fill_models(bars)
    loose = loose_config()
    strict = conservative_config()
    print(f"{name} scanning", flush=True)
    if name == "S1":
        s1_trace: S1Trace = []
        scanned, gates = scan_s1(bars, calendar, eligible, trace=s1_trace)
        agree("S1", replay_s1(s1_trace), scanned["ols"])
        built = _s1(
            tape, bands, scanned, gates, loose, strict, closes, models, calendar, sharpes
        )
        built["neighborhood"] = _neighbors(
            lambda: replay_s1(s1_trace),
            (
                (s1_signal, "Z_MIN"),
                (s1_signal, "STOP_ATR"),
                (s1_signal, "TARGET_MULTIPLE"),
                (s1_signal, "VWAP_EXTENSION_ATR"),
            ),
            tape,
            bands,
            strict,
            closes,
            models,
            calendar,
            sharpes,
            "S1 ",
        )
        s1_trace.clear()
        built["stress"] = _stress(
            built["priced_lockbox"],
            tape,
            bands,
            bars,
            calendar,
            eligible,
            lambda rows: scan_s1(rows, calendar, eligible, ("ols",))[0]["ols"],
            closes,
            models,
            sharpes,
        )
    elif name == "S2":
        s2_trace: S2Trace = []
        scanned_s2, gates_s2 = scan_s2(bars, calendar, eligible, s2_trace)
        agree("S2", replay_s2(s2_trace), scanned_s2)
        built = _s2(
            tape, bands, scanned_s2, gates_s2, loose, strict, closes, models, calendar, sharpes
        )
        built["neighborhood"] = _neighbors(
            lambda: replay_s2(s2_trace),
            (
                (s2_pattern, "RSI_DIP"),
                (s2_pattern, "RSI_RECLAIM"),
                (s2_pattern, "STOP_ATR"),
                (s2_pattern, "TARGET_MULTIPLE"),
            ),
            tape,
            bands,
            strict,
            closes,
            models,
            calendar,
            sharpes,
            "S2 ",
        )
        s2_trace.clear()
        built["stress"] = _stress(
            built["priced_lockbox"],
            tape,
            bands,
            bars,
            calendar,
            eligible,
            lambda rows: scan_s2(rows, calendar, eligible)[0],
            closes,
            models,
            sharpes,
        )
    elif name == "S3":
        s3_trace: S3Trace = []
        base, sequential, meta = scan_s3(bars, calendar, eligible, fresh, trace=s3_trace)
        replayed_s3 = replay_s3(s3_trace)
        for horizon in HORIZONS:
            agree(f"S3 {horizon}", replayed_s3[horizon], base[horizon])
        built = _s3(
            tape, bands, base, sequential, meta, loose, strict, closes, models, calendar, sharpes
        )
        built["neighborhood"] = _s3_neighbors(
            lambda: replay_s3(s3_trace), tape, bands, strict, closes, models, calendar, sharpes
        )
        s3_trace.clear()
        built["stress"] = _s3_stress(
            built["priced_lockbox"],
            tape,
            bands,
            bars,
            calendar,
            eligible,
            fresh,
            closes,
            models,
            sharpes,
        )
        _reshape_s3(built)
    elif name == "S4":
        s4_trace: S4Trace = []
        base_s4, guarded, meta_s4 = scan_s4(bars, calendar, eligible, s4_trace)
        agree("S4", replay_s4(s4_trace), base_s4)
        built = _s4(
            tape,
            bands,
            base_s4,
            guarded,
            meta_s4,
            loose,
            strict,
            closes,
            models,
            calendar,
            sharpes,
        )
        built["neighborhood"] = _neighbors(
            lambda: replay_s4(s4_trace),
            (
                (s4_bounce, "RSI_OVERSOLD"),
                (s4_bounce, "Z_STRETCH"),
                (s4_bounce, "STOP_ATR"),
            ),
            tape,
            bands,
            strict,
            closes,
            models,
            calendar,
            sharpes,
            "S4 ",
        )
        s4_trace.clear()
        built["stress"] = _stress(
            built["priced_lockbox"],
            tape,
            bands,
            bars,
            calendar,
            eligible,
            lambda rows: scan_s4(rows, calendar, eligible)[0],
            closes,
            models,
            sharpes,
        )
    else:
        raise RuntimeError(name)
    _assert_restored()
    built.pop("priced_lockbox", None)
    built["_meta"] = {
        "name": name,
        "max_ny_date": latest.isoformat(),
        "bars": len(bars),
        "stock_eligible": stocks,
        "splits_after_cutoff_ignored": ignored_splits,
        "eligible_pairs": len(eligible),
        "fill_models": len(models),
    }
    return built


def make_tape_local(bars: list[FeatureBar]) -> BarTape:
    from bstocks_v2.stage14.replay import make_tape

    return make_tape(bars)


def _s1(
    tape: BarTape,
    bands: Bands,
    models: dict[str, IntentBook],
    gates: dict[str, dict[str, int]],
    loose: EngineConfig,
    strict: EngineConfig,
    closes: dict[tuple[str, datetime], Decimal],
    fill_models: dict[str, FillCalibrator],
    calendar: object,
    sharpes: list[Decimal],
) -> dict[str, object]:
    ols = models["ols"]
    kalman = models["kalman"]
    stage_d, priced = _stage_d(tape, bands, ols, strict, closes, fill_models, calendar, sharpes)
    return {
        "frozen_arm": "ols",
        "math_arm": "kalman",
        "selected_before_lockbox": "ols",
        "scan_gates_all_dates": gates,
        "validation": {
            "A": play(tape, ols, bands.validation, loose, sharpes),
            "B": play(tape, kalman, bands.validation, loose, sharpes),
            "C": play(tape, ols, bands.validation, strict, sharpes),
            "D": stage_d["validation"],
        },
        "lockbox": {
            "A": play(tape, ols, bands.lockbox, loose, sharpes),
            "B": play(tape, kalman, bands.lockbox, loose, sharpes),
            "C": play(tape, ols, bands.lockbox, strict, sharpes),
            "D": stage_d["lockbox"],
        },
        "priced_lockbox": priced,
    }


def _s2(
    tape: BarTape,
    bands: Bands,
    rule: IntentBook,
    gates: dict[str, int],
    loose: EngineConfig,
    strict: EngineConfig,
    closes: dict[tuple[str, datetime], Decimal],
    fill_models: dict[str, FillCalibrator],
    calendar: object,
    sharpes: list[Decimal],
) -> dict[str, object]:
    empty_note = {
        "fitted": False,
        "reason": "MIN_FIT stays 30 and the gate is not refit on this run",
    }
    stage_d, priced = _stage_d(tape, bands, rule, strict, closes, fill_models, calendar, sharpes)
    return {
        "frozen_arm": "rule",
        "math_arm": "gate_unfitted",
        "selected_before_lockbox": "rule",
        "probability_gate": empty_note,
        "scan_gates_all_dates": gates,
        "validation": {
            "A": play(tape, rule, bands.validation, loose, sharpes),
            "B": {**play(tape, {}, bands.validation, loose, sharpes), **empty_note},
            "C": play(tape, rule, bands.validation, strict, sharpes),
            "D": stage_d["validation"],
        },
        "lockbox": {
            "A": play(tape, rule, bands.lockbox, loose, sharpes),
            "B": {**play(tape, {}, bands.lockbox, loose, sharpes), **empty_note},
            "C": play(tape, rule, bands.lockbox, strict, sharpes),
            "D": stage_d["lockbox"],
        },
        "priced_lockbox": priced,
    }


def _s3(
    tape: BarTape,
    bands: Bands,
    baseline: dict[int, IntentBook],
    sequential: dict[int, IntentBook],
    meta: dict[str, object],
    loose: EngineConfig,
    strict: EngineConfig,
    closes: dict[tuple[str, datetime], Decimal],
    fill_models: dict[str, FillCalibrator],
    calendar: object,
    sharpes: list[Decimal],
) -> dict[str, object]:
    horizons: dict[str, object] = {}
    priced: dict[str, IntentBook] = {}
    for horizon in HORIZONS:
        stage_d, kept = _stage_d(
            tape, bands, baseline[horizon], strict, closes, fill_models, calendar, sharpes
        )
        key = str(horizon)
        priced[key] = kept
        horizons[key] = {
            "frozen_arm": "baseline",
            "selected_before_lockbox": False,
            "validation": {
                "A": play(tape, baseline[horizon], bands.validation, loose, sharpes),
                "B": play(tape, sequential[horizon], bands.validation, loose, sharpes),
                "C": play(tape, baseline[horizon], bands.validation, strict, sharpes),
                "D": stage_d["validation"],
            },
            "lockbox": {
                "A": play(tape, baseline[horizon], bands.lockbox, loose, sharpes),
                "B": play(tape, sequential[horizon], bands.lockbox, loose, sharpes),
                "C": play(tape, baseline[horizon], bands.lockbox, strict, sharpes),
                "D": stage_d["lockbox"],
            },
        }
    return {
        "frozen_arm": None,
        "selected_before_lockbox": False,
        "scan": meta,
        "horizons": horizons,
        "priced_lockbox": priced,
        "lockbox": {"D": {"expectancy_bps": None, "dsr": {"dsr": None}}},
    }


def _s4(
    tape: BarTape,
    bands: Bands,
    baseline: IntentBook,
    guarded: IntentBook,
    meta: dict[str, object],
    loose: EngineConfig,
    strict: EngineConfig,
    closes: dict[tuple[str, datetime], Decimal],
    fill_models: dict[str, FillCalibrator],
    calendar: object,
    sharpes: list[Decimal],
) -> dict[str, object]:
    stage_d, priced = _stage_d(
        tape, bands, baseline, strict, closes, fill_models, calendar, sharpes
    )
    return {
        "frozen_arm": "baseline",
        "math_arm": "guarded",
        "selected_before_lockbox": "baseline",
        "live_locked": True,
        "scan": meta,
        "validation": {
            "A": play(tape, baseline, bands.validation, loose, sharpes),
            "B": play(tape, guarded, bands.validation, loose, sharpes),
            "C": play(tape, baseline, bands.validation, strict, sharpes),
            "D": stage_d["validation"],
        },
        "lockbox": {
            "A": play(tape, baseline, bands.lockbox, loose, sharpes),
            "B": play(tape, guarded, bands.lockbox, loose, sharpes),
            "C": play(tape, baseline, bands.lockbox, strict, sharpes),
            "D": stage_d["lockbox"],
        },
        "priced_lockbox": priced,
    }


def _stage_d(
    tape: BarTape,
    bands: Bands,
    raw: IntentBook,
    strict: EngineConfig,
    closes: dict[tuple[str, datetime], Decimal],
    fill_models: dict[str, FillCalibrator],
    calendar: object,
    sharpes: list[Decimal],
) -> tuple[dict[str, dict[str, object]], IntentBook]:
    from bstocks_v2.limit_optimizer.fill_model import FillCalibrator
    from bstocks_v2.research_data.calendar import XnysCalendar

    if not isinstance(calendar, XnysCalendar):
        raise RuntimeError("calendar")
    typed = {key: model for key, model in fill_models.items() if isinstance(model, FillCalibrator)}
    if len(typed) != len(fill_models):
        raise RuntimeError("fill model")
    out: dict[str, dict[str, object]] = {}
    priced_lock = IntentBook()
    for name, days in (("validation", bands.validation), ("lockbox", bands.lockbox)):
        subset = windowed(raw, days)
        priced, reasons = reprice(subset, closes, typed, calendar)
        body = play(tape, priced, days, strict, sharpes)
        body["candidates"] = len(subset)
        body["optimizer_reasons"] = dict(reasons)
        out[name] = body
        if name == "lockbox":
            priced_lock = priced
    return out, priced_lock


def _neighbors(
    scanner: Scanner,
    params: tuple[tuple[object, str], ...],
    tape: BarTape,
    bands: Bands,
    strict: EngineConfig,
    closes: dict[tuple[str, datetime], Decimal],
    fill_models: dict[str, FillCalibrator],
    calendar: object,
    sharpes: list[Decimal],
    prefix: str = "",
) -> dict[str, object]:
    matrix: dict[str, object] = {}
    for module, name in params:
        base = getattr(module, name)
        if not isinstance(base, Decimal):
            raise RuntimeError(name)
        for factor, side in ((NEIGHBOR_DOWN, "down_15"), (NEIGHBOR_UP, "up_15")):
            label = f"{name}:{side}"
            print(f"neighbor {prefix}{label}", flush=True)
            with patched(module, **{name: base * factor}):
                scanned = scanner()
            stage, _priced = _stage_d(
                tape, bands, scanned, strict, closes, fill_models, calendar, sharpes
            )
            matrix[label] = stage["lockbox"]
    return matrix


def _s3_neighbors(
    replay: Callable[[], dict[int, IntentBook]],
    tape: BarTape,
    bands: Bands,
    strict: EngineConfig,
    closes: dict[tuple[str, datetime], Decimal],
    fill_models: dict[str, FillCalibrator],
    calendar: object,
    sharpes: list[Decimal],
) -> dict[str, object]:
    params = (
        (s3_signal, "RVOL_MIN"),
        (s3_signal, "CEILING_ATR"),
        (s3_signal, "STOP_ATR"),
        (s3_signal, "TARGET_MULTIPLE"),
    )
    matrix: dict[str, object] = {}
    for module, name in params:
        base = getattr(module, name)
        if not isinstance(base, Decimal):
            raise RuntimeError(name)
        for factor, side in ((NEIGHBOR_DOWN, "down_15"), (NEIGHBOR_UP, "up_15")):
            label = f"{name}:{side}"
            print(f"neighbor S3 {label}", flush=True)
            with patched(module, **{name: base * factor}):
                baseline = replay()
            per_horizon: dict[str, object] = {}
            for horizon in HORIZONS:
                stage, _priced = _stage_d(
                    tape,
                    bands,
                    baseline[horizon],
                    strict,
                    closes,
                    fill_models,
                    calendar,
                    sharpes,
                )
                per_horizon[str(horizon)] = stage["lockbox"]
            matrix[label] = per_horizon
    return matrix


def _stress(
    priced: object,
    tape: BarTape,
    bands: Bands,
    bars: list[FeatureBar],
    calendar: object,
    eligible: set[tuple[str, str]],
    rescan: Callable[[list[FeatureBar]], IntentBook],
    closes: dict[tuple[str, datetime], Decimal],
    fill_models: dict[str, FillCalibrator],
    sharpes: list[Decimal],
) -> dict[str, object]:
    if not isinstance(priced, dict):
        raise RuntimeError("priced book")
    book: IntentBook = priced
    out: dict[str, object] = {}
    for name, config in stress_configs().items():
        print(f"stress {name}", flush=True)
        body = play(tape, book, bands.lockbox, config, sharpes)
        body["hwm_controlled"] = _controlled(body)
        out[name] = body
    print("stress gap_up_2pct", flush=True)
    gapped = gap_open(bars, STRESS_GAP)
    gapped.sort(key=lambda item: (item.start, item.symbol == "SPY", item.symbol))
    scanned = rescan(gapped)
    stage, _priced = _stage_d(
        make_tape_local(gapped),
        bands,
        scanned,
        conservative_config(),
        closes_of(gapped),
        fill_models,
        calendar,
        sharpes,
    )
    gap_body = stage["lockbox"]
    gap_body["hwm_controlled"] = _controlled(gap_body)
    out["gap_up_2pct"] = gap_body
    return out


def _s3_stress(
    priced: object,
    tape: BarTape,
    bands: Bands,
    bars: list[FeatureBar],
    calendar: object,
    eligible: set[tuple[str, str]],
    fresh: Path,
    closes: dict[tuple[str, datetime], Decimal],
    fill_models: dict[str, FillCalibrator],
    sharpes: list[Decimal],
) -> dict[str, object]:
    if not isinstance(priced, dict):
        raise RuntimeError("priced books")
    out: dict[str, object] = {}
    gapped = gap_open(bars, STRESS_GAP)
    gapped.sort(key=lambda item: (item.start, item.symbol == "SPY", item.symbol))
    print("stress S3 gap scan", flush=True)
    gapped_base, _sequential, _meta = scan_s3(
        gapped, _calendar(calendar), eligible, fresh, with_sequential=False
    )
    gapped_tape = make_tape_local(gapped)
    gapped_closes = closes_of(gapped)
    for horizon in HORIZONS:
        key = str(horizon)
        book = priced.get(key)
        if not isinstance(book, dict):
            raise RuntimeError("horizon book")
        horizon_out: dict[str, object] = {}
        for name, config in stress_configs().items():
            print(f"stress S3 {key} {name}", flush=True)
            body = play(tape, book, bands.lockbox, config, sharpes)
            body["hwm_controlled"] = _controlled(body)
            horizon_out[name] = body
        stage, _kept = _stage_d(
            gapped_tape,
            bands,
            gapped_base[horizon],
            conservative_config(),
            gapped_closes,
            fill_models,
            calendar,
            sharpes,
        )
        gap_body = stage["lockbox"]
        gap_body["hwm_controlled"] = _controlled(gap_body)
        horizon_out["gap_up_2pct"] = gap_body
        out[key] = horizon_out
    return out


def _reshape_s3(strategy: dict[str, object]) -> None:
    matrix = strategy.get("neighborhood")
    horizons = strategy.get("horizons")
    if not isinstance(matrix, dict) or not isinstance(horizons, dict):
        raise RuntimeError("S3 neighborhood")
    for label, per_horizon in matrix.items():
        if not isinstance(per_horizon, dict):
            raise RuntimeError("S3 neighbor row")
        for horizon, body in per_horizon.items():
            row = horizons.get(horizon)
            if not isinstance(row, dict):
                raise RuntimeError("S3 horizon")
            slot = row.get("neighborhood")
            if not isinstance(slot, dict):
                slot = {}
                row["neighborhood"] = slot
            slot[str(label)] = body


def _calendar(value: object) -> XnysCalendar:
    if not isinstance(value, XnysCalendar):
        raise RuntimeError("calendar")
    return value


def _controlled(body: dict[str, object]) -> bool:
    crossed = body.get("equity_crossed_hwm_line") is True
    halted = body.get("halt_new_entries") is True
    return (not crossed) or halted


def _decide(strategy: dict[str, object], *, selected: bool, live_locked: bool) -> dict[str, object]:
    if strategy.get("frozen_arm") is None and "horizons" in strategy:
        horizon_rows = strategy["horizons"]
        if not isinstance(horizon_rows, dict):
            raise RuntimeError("horizons")
        per_horizon = {
            key: _decide(row, selected=False, live_locked=False)
            for key, row in horizon_rows.items()
            if isinstance(row, dict)
        }
        return {
            "deployment": "NO_GO",
            "statistical": "NOT_SELECTED",
            "reasons": ["NOT_SELECTED", "P0_P1_OPEN"],
            "label": "NO_GO / RESEARCH_ONLY",
            "horizons": per_horizon,
        }
    lockbox = strategy["lockbox"]
    if not isinstance(lockbox, dict):
        raise RuntimeError("lockbox")
    stage = lockbox["D"]
    if not isinstance(stage, dict):
        raise RuntimeError("stage D")
    neighbors = strategy.get("neighborhood")
    means: list[Decimal | None] = []
    if isinstance(neighbors, dict):
        means = [_expectancy(row) for row in neighbors.values() if isinstance(row, dict)]
    decision = judge(
        mean_net_bps=_expectancy(stage),
        dsr=_dsr(stage),
        neighbor_means=tuple(means),
        selected=selected,
        live_locked=live_locked,
        p0_p1_clear=False,
    )
    return {
        "deployment": decision.deployment,
        "statistical": decision.statistical,
        "reasons": list(decision.reasons),
        "label": "GO" if decision.deployment == "GO" else "NO_GO / RESEARCH_ONLY",
    }


def _expectancy(body: dict[str, object]) -> Decimal | None:
    raw = body.get("expectancy_bps")
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise RuntimeError("expectancy")
    return Decimal(raw)


def _dsr(body: dict[str, object]) -> Decimal | None:
    block = body.get("dsr")
    if not isinstance(block, dict):
        raise RuntimeError("dsr")
    raw = block.get("dsr")
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise RuntimeError("dsr value")
    return Decimal(raw)


def _public(strategies: dict[str, dict[str, object]]) -> dict[str, object]:
    cleaned: dict[str, object] = {}
    for name, strategy in strategies.items():
        if not isinstance(strategy, dict):
            raise RuntimeError(name)
        cleaned[name] = {key: value for key, value in strategy.items() if key != "priced_lockbox"}
    return cleaned


def _dates(days: frozenset[date]) -> list[str]:
    return [day.isoformat() for day in sorted(days)]


def _spec_hashes() -> dict[str, str]:
    root = Path(__file__).resolve().parents[1] / "strategies"
    return {
        name: hashlib.sha256((root / name / "spec.py").read_bytes()).hexdigest()
        for name in ("s1", "s2", "s3", "s4")
    }


def _assert_restored() -> None:
    from bstocks_v2.strategies.s1.spec import STOP_ATR as s1_stop
    from bstocks_v2.strategies.s1.spec import TARGET_MULTIPLE as s1_target
    from bstocks_v2.strategies.s1.spec import VWAP_EXTENSION_ATR as s1_ext
    from bstocks_v2.strategies.s1.spec import Z_MIN
    from bstocks_v2.strategies.s2.spec import RSI_DIP, RSI_RECLAIM
    from bstocks_v2.strategies.s2.spec import STOP_ATR as s2_stop
    from bstocks_v2.strategies.s2.spec import TARGET_MULTIPLE as s2_target
    from bstocks_v2.strategies.s3.spec import CEILING_ATR, RVOL_MIN
    from bstocks_v2.strategies.s3.spec import STOP_ATR as s3_stop
    from bstocks_v2.strategies.s3.spec import TARGET_MULTIPLE as s3_target
    from bstocks_v2.strategies.s4.spec import RSI_OVERSOLD, Z_STRETCH
    from bstocks_v2.strategies.s4.spec import STOP_ATR as s4_stop

    pairs = (
        (_decimal_attr(s1_signal, "Z_MIN"), Z_MIN),
        (_decimal_attr(s1_signal, "STOP_ATR"), s1_stop),
        (_decimal_attr(s1_signal, "TARGET_MULTIPLE"), s1_target),
        (_decimal_attr(s1_signal, "VWAP_EXTENSION_ATR"), s1_ext),
        (_decimal_attr(s2_pattern, "RSI_DIP"), RSI_DIP),
        (_decimal_attr(s2_pattern, "RSI_RECLAIM"), RSI_RECLAIM),
        (_decimal_attr(s2_pattern, "STOP_ATR"), s2_stop),
        (_decimal_attr(s2_pattern, "TARGET_MULTIPLE"), s2_target),
        (_decimal_attr(s3_signal, "RVOL_MIN"), RVOL_MIN),
        (_decimal_attr(s3_signal, "CEILING_ATR"), CEILING_ATR),
        (_decimal_attr(s3_signal, "STOP_ATR"), s3_stop),
        (_decimal_attr(s3_signal, "TARGET_MULTIPLE"), s3_target),
        (_decimal_attr(s4_bounce, "RSI_OVERSOLD"), RSI_OVERSOLD),
        (_decimal_attr(s4_bounce, "Z_STRETCH"), Z_STRETCH),
        (_decimal_attr(s4_bounce, "STOP_ATR"), s4_stop),
    )
    if any(left != right for left, right in pairs):
        raise RuntimeError("a perturbed threshold was not restored")


def _decimal_attr(module: object, name: str) -> Decimal:
    value = getattr(module, name)
    if not isinstance(value, Decimal):
        raise RuntimeError(name)
    return value
