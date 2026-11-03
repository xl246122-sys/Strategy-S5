"""Offline S5 pre-assessment.

A screen count is not a fill. A net is booked only when a quote-backed
round trip cleared the penetration rule. Losing rows stay in the sample.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal

from bstocks_v2.stage14.spec import LOCKBOX_END
from bstocks_v2.stage14.stats import summarize
from bstocks_v2.strategies.s1.spec import TRADED
from bstocks_v2.strategies.s5.spec import (
    ARM_C,
    ARMS,
    B0_MAX_SIMPLE_RETURN,
    B1_MAX_Z,
    LEVELS,
    LOCKBOX_START,
    MIN_RESOLVED,
    MODEL_VERSION,
    PATHS,
    PENETRATION,
    PLANNED_STOP,
    PRIMARY_NET_TARGET,
    STRATEGY_ID,
    TRAIN_END,
    TRAIN_START,
    VAL_END,
    VAL_START,
    research_split,
)
from bstocks_v2.strategies.s5.universe import sector_parent

FINAL_EVALUATIONS = 1
V22_LOCKBOX_SEAL = "82474de9f00ada7edd975bef5d103bb31db662fb5dcc7f37f9d5449e7332821e"
_LOOSER = Decimal("0.85")
_STRICTER = Decimal("1.15")


@dataclass(frozen=True)
class Evidence:
    """One research row. ``gross_usdc`` is the price move before fees and spread."""

    symbol: str
    arm: str
    sector: str
    session: date
    split: str
    event_id: str
    path: str
    quote_backed: bool
    gross_usdc: Decimal
    fee_usdc: Decimal
    spread_usdc: Decimal
    notional: Decimal
    penetration_cleared: Decimal
    quote_lag_ms: int
    exit_bid: Decimal | None
    halted: bool = False
    gap_without_print: bool = False

    def __post_init__(self) -> None:
        if self.arm not in ARMS or self.path not in PATHS:
            raise ValueError("row")
        if research_split(self.session) != self.split or self.split == "lockbox":
            raise ValueError("lockbox")
        if self.quote_lag_ms < 0 or self.notional <= 0:
            raise ValueError("row")
        if self.fee_usdc < 0 or self.spread_usdc < 0 or self.penetration_cleared < 0:
            raise ValueError("row")
        if self.exit_bid is not None and self.exit_bid < 0:
            raise ValueError("row")


@dataclass(frozen=True)
class ScreenPoint:
    simple_return: Decimal
    residual_z: Decimal


def drop_losses(rows: Sequence[Evidence]) -> Sequence[Evidence]:
    """Losing rows are part of the sample. This entry point does not remove them."""

    del rows
    raise ValueError("LOSS_FILTER_FORBIDDEN")


def pooled_expectancy(rows: Sequence[Evidence]) -> Decimal:
    """A, B, and C are separate experiments. Their nets are not added together."""

    del rows
    raise ValueError("ARMS_NOT_POOLED")


def book_match(
    status: str,
    qty: Decimal,
    price: Decimal | None,
    *,
    quote_backed: bool,
) -> Decimal | None:
    """Return the filled quantity only when a quote backs the match."""

    if not quote_backed or price is None or qty <= 0:
        return None
    if status not in {"FILLED", "PARTIALLY_FILLED"}:
        return None
    return qty


def fill_account(ordered: Decimal, filled: Decimal) -> dict[str, object]:
    """A partial keeps the unfilled remainder in the signal count."""

    if ordered <= 0 or filled < 0 or filled > ordered:
        raise ValueError("qty")
    return {"signals": 1, "closed_qty": filled, "unfilled_qty": ordered - filled}


def closed_net(
    row: Evidence,
    *,
    fee_multiple: Decimal = Decimal(1),
    spread_multiple: Decimal = Decimal(1),
    penetration_required: Decimal = PENETRATION,
    min_lag_ms: int = 0,
) -> Decimal | None:
    """Net USDC of one quote-backed close. A missing or zero bid is not a fill."""

    if not row.quote_backed or row.path == "UNFILLABLE":
        return None
    if row.halted or row.gap_without_print:
        return None
    if row.penetration_cleared < penetration_required or row.quote_lag_ms < min_lag_ms:
        return None
    if row.exit_bid is None or row.exit_bid <= 0:
        return None
    return row.gross_usdc - (row.fee_usdc * fee_multiple) - (row.spread_usdc * spread_multiple)


def score_rows(
    rows: Sequence[Evidence],
    *,
    fee_multiple: Decimal = Decimal(1),
    spread_multiple: Decimal = Decimal(1),
    penetration_required: Decimal = PENETRATION,
    min_lag_ms: int = 0,
) -> dict[str, object]:
    """Per-arm scores. The result has no combined expectancy."""

    arms: list[dict[str, object]] = []
    for name in sorted({row.arm for row in rows}):
        chosen = [row for row in rows if row.arm == name]
        nets: list[Decimal] = []
        notionals: list[Decimal] = []
        labels: Counter[str] = Counter()
        for row in chosen:
            net = closed_net(
                row,
                fee_multiple=fee_multiple,
                spread_multiple=spread_multiple,
                penetration_required=penetration_required,
                min_lag_ms=min_lag_ms,
            )
            if net is None:
                labels["UNFILLABLE"] += 1
                continue
            labels[row.path] += 1
            nets.append(net)
            notionals.append(row.notional)
        if nets:
            stats = summarize(nets, notionals)
            expectancy = _decimal_text(stats["expectancy_usdc"])
            record: dict[str, object] = {
                "profit_factor": stats["profit_factor"],
                "max_drawdown_usdc": stats["max_drawdown_usdc"],
                "cvar95_usdc": stats["cvar95_usdc"],
                "bootstrap_bps_ci95": stats["bootstrap_bps_ci95"],
            }
        else:
            expectancy = None
            record = {
                "profit_factor": None,
                "max_drawdown_usdc": None,
                "cvar95_usdc": None,
                "bootstrap_bps_ci95": [None, None],
            }
        closed = len(nets)
        record.update(
            {
                "arm": name,
                "signals": len(chosen),
                "closed_trades": closed,
                "expectancy_usdc": None if expectancy is None else format(expectancy, "f"),
                "path_labels": dict(labels),
                "sample_sufficient": closed >= MIN_RESOLVED and expectancy is not None,
                "slippage_known": closed > 0 and all(row.quote_backed for row in chosen),
                "exit_acceptable": _exit_ok(chosen),
            }
        )
        arms.append(record)
    return {"arms": arms}


def neighbor_report(points: Sequence[ScreenPoint]) -> dict[str, object]:
    """Count the frozen thresholds. A larger looser count is not adopted."""

    frozen = _count_band(points, B0_MAX_SIMPLE_RETURN, B1_MAX_Z)
    looser = _count_band(points, B0_MAX_SIMPLE_RETURN * _LOOSER, B1_MAX_Z * _LOOSER)
    stricter = _count_band(points, B0_MAX_SIMPLE_RETURN * _STRICTER, B1_MAX_Z * _STRICTER)
    return {
        "reported_count": frozen,
        "looser_count": looser,
        "stricter_count": stricter,
        "adopted": "frozen",
        "tape_recount": bool(points),
        "thresholds": frozen_thresholds(),
    }


def assess_evidence(
    rows: Sequence[Evidence],
    *,
    holdout_symbol: str | None = None,
) -> dict[str, object]:
    """Judge a quote-backed sample. The historical screen does not use this path."""

    score = score_rows(rows)
    holdout = _named_holdout(rows, score, holdout_symbol)
    decision = judge(
        mode="quote_tape",
        arms=_arm_list(score),
        stresses=stress_evidence(rows, holdout_symbol),
        single_name=holdout["single_name"] is True,
    )
    return {
        **decision,
        "mode": "quote_tape",
        "overnight_profit_claimed": False,
        "live_enabled": False,
        "score": score,
        "symbol_holdout": holdout,
    }


def assess_screen(screen: dict[str, object], coverage: dict[str, object]) -> dict[str, object]:
    """Turn the saved OHLCV screen into an assessment. No price is reread."""

    parsed = _parse_screen(screen)
    frames = _frames(coverage)
    if any(frame["has_bid_ask"] is True for frame in frames):
        raise ValueError("COVERAGE_QUOTES")
    overnight = _clock(frames, "5Min", "OVERNIGHT")
    arms = [
        _empty_arm(name, _as_int(item["decisions"], name), _path_map(item["paths"]))
        for name, item in parsed
    ]
    stresses = _historical_stresses()
    decision = judge(mode="rth_candidate_screen", arms=arms, stresses=stresses, single_name=False)
    ablation: list[dict[str, object]] = []
    signals = 0
    for name, item in parsed:
        ablation.extend(_ablation(name, item))
        signals += _as_int(item["decisions"], name)
    return {
        **decision,
        "mode": "rth_candidate_screen",
        "study": "RTH candidate screen",
        "overnight_profit_claimed": False,
        "live_enabled": False,
        "quotes": 0,
        "has_bid_ask": False,
        "overnight_bars_5m": overnight,
        "signals": signals,
        "quote_backed_fills": 0,
        "arms": arms,
        "ablation": ablation,
        "sectors": _sectors(),
        "event_days": None,
        "non_event_days": None,
        "event_note": "NEWS_UNKNOWN",
        "stress": stresses,
        "neighbors": neighbor_report(()),
        "symbol_holdout": {
            "symbol": None,
            "removed_fills": 0,
            "adopted": "full",
            "full_expectancy_usdc": None,
            "reduced_expectancy_usdc": None,
            "single_name": False,
        },
        "lockbox": prospective_plan(),
        "experiment": experiment_config(),
    }


def judge(
    *,
    mode: str,
    arms: Sequence[dict[str, object]],
    stresses: Sequence[dict[str, object]],
    single_name: bool,
) -> dict[str, object]:
    """NO_GO when the sample, the exit, or a listed stress fails. Otherwise shadow only."""

    reasons: list[str] = []
    if mode != "quote_tape":
        reasons.append("NO_EXECUTABLE_QUOTES")
    closed = sum(_as_int(arm["closed_trades"], "closed") for arm in arms)
    if closed < MIN_RESOLVED:
        reasons.append("SAMPLE_INSUFFICIENT")
    for arm in arms:
        name = str(arm["arm"])
        expectancy = arm["expectancy_usdc"]
        if _as_int(arm["closed_trades"], "closed") == 0 or expectancy is None:
            reasons.append(f"EXPECTANCY_UNAVAILABLE:{name}")
        elif Decimal(str(expectancy)) <= 0:
            reasons.append(f"EXPECTANCY_NOT_POSITIVE:{name}")
        if arm["slippage_known"] is not True:
            reasons.append(f"SLIPPAGE_UNRELIABLE:{name}")
        if arm["exit_acceptable"] is not True:
            reasons.append(f"EXIT_FAILURE:{name}")
    for item in stresses:
        kind = str(item["class"])
        label = str(item["name"])
        if kind == "safety" and item["books_profit"] is True:
            reasons.append(f"SAFETY_BOOKED_PROFIT:{label}")
        elif kind == "fragility" and item["keeps_positive_expectancy"] is not True:
            reasons.append(f"STRESS:{label}")
        elif kind == "disclosure" and item["adopted"] != "frozen":
            reasons.append(f"THRESHOLD_SELECTION:{label}")
    if single_name:
        reasons.append("SINGLE_NAME")
    ordered = list(dict.fromkeys(reasons))
    if ordered:
        verdict, research, shadow = "NO_GO", "RESEARCH_ONLY", False
    else:
        verdict, research, shadow = "SHADOW_ONLY", "SHADOW_ONLY", True
    if mode != "quote_tape" and verdict != "NO_GO":
        raise RuntimeError("screen mode cannot authorize shadow")
    phase = "PASS"
    if verdict not in {"NO_GO", "SHADOW_ONLY"}:
        phase = "FAIL"
    return {
        "strategy_verdict": verdict,
        "research_mode": research,
        "shadow_authorized": shadow,
        "reasons": ordered,
        "phase_gate": phase,
    }


def stress_evidence(
    rows: Sequence[Evidence],
    holdout_symbol: str | None = None,
) -> list[dict[str, object]]:
    """Replay the listed stresses. Safety cases must not book a profit."""

    items = [
        _fragility("fee_x2", score_rows(rows, fee_multiple=Decimal(2))),
        _fragility("spread_x2", score_rows(rows, spread_multiple=Decimal(2))),
        _fragility("penetration_x2", score_rows(rows, penetration_required=PENETRATION * 2)),
        _fragility("delay_1s", score_rows(rows, min_lag_ms=1000)),
        _fragility("delay_5s", score_rows(rows, min_lag_ms=5000)),
        _fragility("segment_holdout", _segments(rows)),
        _fragility("event_cluster_holdout", _cluster(rows)),
        _symbol_stress(rows, holdout_symbol),
        {
            "name": "params_pm15",
            "class": "disclosure",
            "adopted": "frozen",
            "tape_recount": False,
            "books_profit": False,
        },
        _safety(
            "overnight_zero_bid",
            score_rows([replace(row, exit_bid=Decimal(0)) for row in rows if row.arm == ARM_C]),
        ),
        _safety("gap_or_halt", score_rows([replace(row, gap_without_print=True) for row in rows])),
    ]
    return items


def _named_holdout(
    rows: Sequence[Evidence],
    score: dict[str, object],
    symbol: str | None,
) -> dict[str, object]:
    if symbol is None:
        return {
            "symbol": None,
            "removed_fills": 0,
            "adopted": "full",
            "full_expectancy_usdc": _first_expectancy(score),
            "reduced_expectancy_usdc": None,
            "single_name": False,
        }
    return symbol_holdout(rows, symbol)


def _symbol_stress(rows: Sequence[Evidence], symbol: str | None) -> dict[str, object]:
    if symbol is None:
        removed = 0
        reduced: str | None = None
    else:
        held = symbol_holdout(rows, symbol)
        removed = int(held["removed_fills"]) if isinstance(held["removed_fills"], int) else 0
        raw = held["reduced_expectancy_usdc"]
        reduced = None if raw is None else str(raw)
    return {
        "name": "drop_symbol",
        "class": "disclosure",
        "adopted": "frozen",
        "removed_fills": removed,
        "reduced_expectancy_usdc": reduced,
        "books_profit": False,
    }


def symbol_holdout(rows: Sequence[Evidence], symbol: str) -> dict[str, object]:
    """Report the sample without one name, and keep the full sample as the headline."""

    full = score_rows(rows)
    reduced_rows = [row for row in rows if row.symbol != symbol]
    reduced = score_rows(reduced_rows)
    full_positive = _positive(full)
    reduced_positive = _positive(reduced)
    removed = sum(1 for row in rows if row.symbol == symbol)
    return {
        "symbol": symbol,
        "in_preregistered_universe": symbol in TRADED,
        "removed_fills": removed,
        "adopted": "full",
        "full_expectancy_usdc": _first_expectancy(full),
        "reduced_expectancy_usdc": _first_expectancy(reduced),
        "single_name": removed > 0 and reduced_positive and not full_positive,
    }


def frozen_thresholds() -> dict[str, str]:
    """The ±15% band around the pre-registered numbers. The band is not a new fit."""

    return {
        "b0_frozen": format(B0_MAX_SIMPLE_RETURN, "f"),
        "b0_looser": format(B0_MAX_SIMPLE_RETURN * _LOOSER, "f"),
        "b0_stricter": format(B0_MAX_SIMPLE_RETURN * _STRICTER, "f"),
        "b1_frozen": format(B1_MAX_Z, "f"),
        "b1_looser": format(B1_MAX_Z * _LOOSER, "f"),
        "b1_stricter": format(B1_MAX_Z * _STRICTER, "f"),
        "target_frozen": format(PRIMARY_NET_TARGET, "f"),
        "target_down": format(PRIMARY_NET_TARGET * _LOOSER, "f"),
        "target_up": format(PRIMARY_NET_TARGET * _STRICTER, "f"),
        "stop_frozen": format(PLANNED_STOP, "f"),
        "stop_down": format(PLANNED_STOP * _LOOSER, "f"),
        "stop_up": format(PLANNED_STOP * _STRICTER, "f"),
        "penetration_frozen": format(PENETRATION, "f"),
        "penetration_x2": format(PENETRATION * 2, "f"),
    }


def experiment_config() -> dict[str, object]:
    return {
        "strategy_id": STRATEGY_ID,
        "model_version": MODEL_VERSION,
        "live_enabled": False,
        "matching": "next_event_conservative_penetration",
        "loose_touch_fills": False,
        "quote_required_for_pnl": True,
        "loss_filter": "forbidden",
        "arms_pooled": False,
        "min_closed_trades": MIN_RESOLVED,
        "final_evaluations_allowed": FINAL_EVALUATIONS,
        "thresholds": frozen_thresholds(),
        "splits": {
            "train": [TRAIN_START.isoformat(), TRAIN_END.isoformat()],
            "validation": [VAL_START.isoformat(), VAL_END.isoformat()],
            "lockbox_read": False,
        },
    }


def prospective_plan() -> dict[str, object]:
    """The 2026-05 window was used by V2.2. It is not an untouched S5 test."""

    return {
        "old_lockbox": {
            "start": LOCKBOX_START.isoformat(),
            "end": LOCKBOX_END.isoformat(),
            "v22_use": "stage14_executed_once",
            "seal_sha256": V22_LOCKBOX_SEAL,
            "s5_status": "not_an_untouched_s5_test",
            "s5_screen_read": False,
        },
        "consumed_by_s5_counts": {
            "train": f"{TRAIN_START.isoformat()}..{TRAIN_END.isoformat()}",
            "validation": f"{VAL_START.isoformat()}..{VAL_END.isoformat()}",
            "fit": "none",
        },
        "prospective_lockbox": {
            "start": "first_xnys_session_after_this_freeze",
            "collection": "append_only_forward",
            "rewrite": False,
            "final_evaluations_allowed": FINAL_EVALUATIONS,
            "refit_before_final": False,
            "opened_in_this_phase": False,
        },
    }


def require_research_mode(live_enabled: bool) -> None:
    if live_enabled:
        raise RuntimeError("S5 code lock drifted")


def _historical_stresses() -> list[dict[str, object]]:
    fragility = (
        "fee_x2",
        "spread_x2",
        "penetration_x2",
        "delay_1s",
        "delay_5s",
        "segment_holdout",
        "event_cluster_holdout",
        "drop_symbol",
    )
    rows: list[dict[str, object]] = [
        {
            "name": name,
            "class": "fragility",
            "closed_trades": 0,
            "expectancy_usdc": None,
            "keeps_positive_expectancy": False,
            "books_profit": False,
            "adopted": "frozen",
        }
        for name in fragility
    ]
    rows.append(
        {
            "name": "params_pm15",
            "class": "disclosure",
            "adopted": "frozen",
            "tape_recount": False,
            "books_profit": False,
            "keeps_positive_expectancy": False,
        }
    )
    for name in ("overnight_zero_bid", "gap_or_halt"):
        rows.append(
            {
                "name": name,
                "class": "safety",
                "closed_trades": 0,
                "expectancy_usdc": None,
                "books_profit": False,
                "keeps_positive_expectancy": False,
            }
        )
    return rows


def _fragility(name: str, score: dict[str, object]) -> dict[str, object]:
    return {
        "name": name,
        "class": "fragility",
        "keeps_positive_expectancy": _positive(score),
        "books_profit": _books_profit(score),
        "adopted": "frozen",
        "expectancy_usdc": _first_expectancy(score),
    }


def _safety(name: str, score: dict[str, object]) -> dict[str, object]:
    return {
        "name": name,
        "class": "safety",
        "books_profit": _books_profit(score),
        "keeps_positive_expectancy": False,
        "expectancy_usdc": _first_expectancy(score),
    }


def _segments(rows: Sequence[Evidence]) -> dict[str, object]:
    parts = [
        score_rows([row for row in rows if row.split == name]) for name in ("train", "validation")
    ]
    if not all(_positive(score) for score in parts):
        return {"arms": []}
    return score_rows(rows)


def _cluster(rows: Sequence[Evidence]) -> dict[str, object]:
    if not rows or any(row.event_id == "" for row in rows):
        return {"arms": []}
    counts = Counter(row.event_id for row in rows)
    largest = counts.most_common(1)[0][0]
    return score_rows([row for row in rows if row.event_id != largest])


def _positive(score: dict[str, object]) -> bool:
    arms = _arm_list(score)
    if not arms:
        return False
    for arm in arms:
        expectancy = arm["expectancy_usdc"]
        if _as_int(arm["closed_trades"], "closed") < MIN_RESOLVED or expectancy is None:
            return False
        if Decimal(str(expectancy)) <= 0:
            return False
    return True


def _books_profit(score: dict[str, object]) -> bool:
    for arm in _arm_list(score):
        expectancy = arm["expectancy_usdc"]
        closed = _as_int(arm["closed_trades"], "closed")
        if closed > 0 and expectancy is not None and Decimal(str(expectancy)) > 0:
            return True
    return False


def _first_expectancy(score: dict[str, object]) -> str | None:
    arms = _arm_list(score)
    if len(arms) != 1:
        return None
    raw = arms[0]["expectancy_usdc"]
    if raw is None:
        return None
    return str(raw)


def _arm_list(score: dict[str, object]) -> list[dict[str, object]]:
    raw = score["arms"]
    if not isinstance(raw, list):
        raise ValueError("arms")
    return [_mapping(item, "arm") for item in raw]


def _exit_ok(rows: Sequence[Evidence]) -> bool:
    opened = [row for row in rows if row.quote_backed and row.path != "UNFILLABLE"]
    if not opened:
        return False
    return all(
        not row.halted
        and not row.gap_without_print
        and row.exit_bid is not None
        and row.exit_bid > 0
        for row in opened
    )


def _count_band(points: Sequence[ScreenPoint], simple_max: Decimal, z_max: Decimal) -> int:
    return sum(
        1 for point in points if point.simple_return <= simple_max and point.residual_z <= z_max
    )


def _empty_arm(name: str, signals: int, paths: dict[str, int]) -> dict[str, object]:
    return {
        "arm": name,
        "signals": signals,
        "closed_trades": 0,
        "expectancy_usdc": None,
        "profit_factor": None,
        "max_drawdown_usdc": None,
        "cvar95_usdc": None,
        "bootstrap_bps_ci95": [None, None],
        "path_labels": paths,
        "sample_sufficient": False,
        "slippage_known": False,
        "exit_acceptable": False,
    }


def _path_map(value: object) -> dict[str, int]:
    mapped = _mapping(value, "paths")
    return {key: _as_int(item, key) for key, item in mapped.items()}


def _ablation(name: str, arm: dict[str, object]) -> list[dict[str, object]]:
    screens = _mapping(arm["screens"], "screens")
    admitted = _mapping(arm["admitted"], "admitted")
    rows = [_ablation_row(name, level, _as_int(screens[level], level)) for level in LEVELS]
    reached = sum(_as_int(admitted[level], level) for level in LEVELS)
    rows.append(_ablation_row(name, "B4", reached))
    rows.append(_ablation_row(name, "B5", _as_int(arm["decisions"], name)))
    return rows


def _ablation_row(arm: str, level: str, signals: int) -> dict[str, object]:
    return {
        "arm": arm,
        "level": level,
        "signals": signals,
        "closed_trades": 0,
        "tp_first": 0,
        "sl_first": 0,
        "timeout": 0,
        "unfillable": signals,
        "expectancy_usdc": None,
        "profit_factor": None,
        "max_drawdown_usdc": None,
        "cvar95_usdc": None,
        "bootstrap_bps_ci95": [None, None],
        "sample_sufficient": False,
    }


def _sectors() -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for symbol in TRADED:
        parent = sector_parent(symbol)
        if parent is None:
            raise RuntimeError(symbol)
        rows.append(
            {
                "symbol": symbol,
                "sector": parent,
                "closed_trades": 0,
                "expectancy_usdc": None,
                "screen_count": None,
            }
        )
    return rows


def _parse_screen(
    screen: dict[str, object],
) -> list[tuple[str, dict[str, object]]]:
    if screen.get("quotes") != 0:
        raise ValueError("SCREEN_QUOTES")
    if screen.get("lockbox_read") is not False:
        raise ValueError("LOCKBOX")
    if screen.get("mode") != "rth_post_ohlcv_screen":
        raise ValueError("mode")
    summary = _mapping(screen["summary"], "summary")
    if summary.get("routable") != 0:
        raise ValueError("ROUTABLE")
    arms = _mapping(summary["arms"], "arms")
    if set(arms) != set(ARMS):
        raise ValueError("arms")
    parsed: list[tuple[str, dict[str, object]]] = []
    total = 0
    for name in ARMS:
        item = _parse_arm(_mapping(arms[name], name))
        parsed.append((name, item))
        total += _as_int(item["decisions"], name)
    if total != _as_int(summary["decisions"], "decisions"):
        raise ValueError("COUNT")
    return parsed


def _parse_arm(arm: dict[str, object]) -> dict[str, object]:
    decisions = _as_int(arm["decisions"], "decisions")
    if _as_int(arm["routable"], "routable") != 0:
        raise ValueError("ROUTABLE")
    labels = _mapping(arm["path_labels"], "paths")
    paths = {key: _as_int(value, key) for key, value in labels.items()}
    if sum(paths.values()) != decisions or set(paths) != {"UNFILLABLE"}:
        raise ValueError("PATH_WITHOUT_QUOTES")
    raw_splits = _mapping(arm["splits"], "splits")
    splits = {key: _as_int(value, key) for key, value in raw_splits.items()}
    if "lockbox" in splits or sum(splits.values()) != decisions:
        raise ValueError("LOCKBOX")
    screens = _mapping(arm["screens"], "screens")
    admitted = _mapping(arm["admitted"], "admitted")
    if set(screens) != set(LEVELS) or set(admitted) != set(LEVELS):
        raise ValueError("levels")
    return {
        "decisions": decisions,
        "screens": {level: _as_int(screens[level], level) for level in LEVELS},
        "admitted": {level: _as_int(admitted[level], level) for level in LEVELS},
        "paths": paths,
        "splits": splits,
    }


def _frames(coverage: dict[str, object]) -> list[dict[str, object]]:
    raw = coverage["timeframes"]
    if not isinstance(raw, list) or not raw:
        raise ValueError("timeframes")
    return [_mapping(item, "timeframe") for item in raw]


def _clock(frames: Sequence[dict[str, object]], timeframe: str, label: str) -> int:
    for frame in frames:
        if frame.get("timeframe") != timeframe:
            continue
        clock = _mapping(frame["clock"], "clock")
        return _as_int(clock[label], label)
    raise ValueError(timeframe)


def _as_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(label)
    return value


def _mapping(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError(label)
    mapped: dict[str, object] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise ValueError(label)
        mapped[key] = item
    return mapped


def _decimal_text(value: object) -> Decimal | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("decimal")
    return Decimal(value)
