"""Read-only S5 shadow ledger.

This module does not open a socket, spawn a worker, or send an order.
A penetrated price is stored as an estimate. It is not an exchange fill.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
from collections import Counter
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from bstocks_v2.contracts.errors import LiveTradingDisabledError
from bstocks_v2.research_data.manifest import assert_output_isolated, sha256_file
from bstocks_v2.research_data.paths import FRESH_DEFAULT, OUT_DEFAULT
from bstocks_v2.research_data.s5_backtest import probe_schema
from bstocks_v2.sessions.calendar import TradingCalendar
from bstocks_v2.strategies.s5.lock import refuse_live_route, s5_live_enabled
from bstocks_v2.strategies.s5.observe import (
    LIVE_SESSION_FLOOR,
    Observation,
    ShadowTape,
    estimate_interval,
    live_session_dates,
    observe,
    parameter_freeze,
)
from bstocks_v2.strategies.s5.policy import S5GuardianPolicy, research_policy
from bstocks_v2.strategies.s5.spec import ARMS, MIN_RESOLVED

ASSESSMENT_SHA = "8d549b7d43d1c378b03479ffad8e9a3a53f9d46c38d5f938a5feb175bde08a39"
LEDGER_SHA = "dee12d90476e8ee6cd63754935f687c5e021cb8f5986800f4bafec81f7e9f231"
PRODUCTION_NAMES = frozenset(
    {
        "intraday.sqlite",
        "guardian.sqlite",
        "portfolio.sqlite",
        "orders.sqlite",
    }
)
_PLACE = "/sapi/v1/equity/order/place"
_CANCEL = "/sapi/v1/equity/order/cancel"


class ShadowTracker:
    """The order methods exist so a caller hits the research lock."""

    def place(self) -> None:
        refuse_live_route(_PLACE)

    def cancel(self) -> None:
        refuse_live_route(_CANCEL)


def decide_launch(*, shadow_authorized: bool, host_measured: bool) -> dict[str, object]:
    """No argument starts a process. An unattended spawn stays closed."""

    reasons = ["NO_UNATTENDED_SPAWN"]
    if shadow_authorized is not True:
        reasons.append("G_S5_5_SHADOW_NOT_AUTHORIZED")
    if host_measured is not True:
        reasons.append("HOST_UNMEASURED")
    return {"start_process": False, "pid": None, "reasons": reasons}


def assert_shadow_target(output: Path, source: Path, production: Sequence[Path]) -> None:
    """The journal stays out of the SIP store and out of a named production file."""

    assert_output_isolated(output, source)
    resolved = output.resolve()
    if resolved.name in PRODUCTION_NAMES:
        raise ValueError("shadow store uses a production filename")
    for item in production:
        other = item.resolve()
        if resolved == other or other in resolved.parents or resolved in other.parents:
            raise ValueError("shadow store collides with a production path")


def write_bundle(
    store: Path,
    source: Path,
    tapes: Sequence[ShadowTape],
    assessment: dict[str, object],
    probe: dict[str, object],
    production: Sequence[Path] = (),
) -> dict[str, object]:
    """Replay a local tape. The returned payload is the only artifact."""

    _require_locked()
    journal = store / "s5_06_shadow.sqlite"
    assert_shadow_target(journal, source, production)
    if probe.get("rows_loaded") not in {0, None}:
        raise RuntimeError("shadow replay loaded a price file")
    calendar = TradingCalendar.load()
    freeze = parameter_freeze()
    kept: list[Observation] = []
    seen: set[str] = set()
    duplicates = 0
    for tape in tapes:
        row = observe(tape, calendar, freeze=freeze)
        if row.books_profit or row.exchange_fill_id is not None:
            raise RuntimeError("shadow estimate booked a fill")
        if row.dedup_key in seen:
            duplicates += 1
            continue
        seen.add(row.dedup_key)
        kept.append(row)
    launch = decide_launch(
        shadow_authorized=_flag(assessment, "shadow_authorized"),
        host_measured=False,
    )
    if launch["start_process"] is not False or launch["pid"] is not None:
        raise RuntimeError("shadow worker spawn drifted")
    offline = _count(assessment, "quote_backed_fills")
    body = _summary(kept, duplicates, launch, probe, assessment, offline, freeze)
    store.mkdir(parents=True, exist_ok=True)
    _write_sqlite(journal, kept)
    _dump(store / "s5_06_observations.json", [row.as_dict() for row in kept])
    _dump(store / "s5_06_summary.json", body)
    manifest = {
        "created_utc": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "mode": "readonly_shadow_replay",
        "process_started": False,
        "order_place": "refused",
        "order_cancel": "refused",
        "source_opened_for_write": False,
        "files": [
            _file(store / "s5_06_summary.json", "summary"),
            _file(store / "s5_06_observations.json", "observations"),
            _file(journal, "sqlite"),
        ],
    }
    _dump(store / "s5_06_manifest.json", manifest)
    return body


def run(
    source: Path = FRESH_DEFAULT,
    store: Path = OUT_DEFAULT / "s5",
) -> dict[str, object]:
    """Replay nothing live. The frozen P5 file must still refuse shadow."""

    assessment_path = store / "s5_05_assessment.json"
    ledger_path = store / "s5_05_ledger.json"
    if sha256_file(assessment_path) != ASSESSMENT_SHA:
        raise ValueError("assessment hash")
    if sha256_file(ledger_path) != LEDGER_SHA:
        raise ValueError("ledger hash")
    assessment = _load(assessment_path)
    if assessment.get("strategy_verdict") != "NO_GO":
        raise RuntimeError("frozen verdict drifted")
    if assessment.get("shadow_authorized") is not False:
        raise RuntimeError("frozen shadow flag drifted")
    probe = probe_schema(source)
    if probe.get("has_bid_ask") is True:
        raise RuntimeError("QUOTE_TAPE_UNREVIEWED")
    if probe.get("rows_loaded") != 0:
        raise RuntimeError("schema probe loaded rows")
    return write_bundle(store, source, (), assessment, probe)


def _require_locked() -> None:
    if s5_live_enabled() or research_policy().live_enabled or research_policy().active:
        raise RuntimeError("S5 code lock drifted")
    if S5GuardianPolicy().governs():
        raise RuntimeError("S5 guardian policy drifted")
    tracker = ShadowTracker()
    if _refused(tracker.place) != _PLACE or _refused(tracker.cancel) != _CANCEL:
        raise RuntimeError("order route opened")


def _refused(method: Callable[[], None]) -> str:
    try:
        method()
    except LiveTradingDisabledError as exc:
        return exc.reason
    raise RuntimeError("order route opened")


def _summary(
    rows: Sequence[Observation],
    duplicates: int,
    launch: dict[str, object],
    probe: dict[str, object],
    assessment: dict[str, object],
    offline_fills: int,
    freeze: str,
) -> dict[str, object]:
    live = live_session_dates(rows)
    exchange = sum(1 for row in rows if row.exchange_fill_id is not None)
    if exchange != 0:
        raise RuntimeError("shadow exchange fill drifted")
    lags = [row.quote_lag_ms for row in rows if row.quote_lag_ms is not None]
    if len(live) == 0:
        attribution = "NO_LIVE_TAPE"
    else:
        attribution = "EXCHANGE_FILLS_ABSENT"
    return {
        "phase": "S5-P6",
        "phase_gate": "PASS" if launch["start_process"] is False else "FAIL",
        "observation_status": "COMPLETE" if len(live) >= LIVE_SESSION_FLOOR else "INCOMPLETE",
        "strategy_verdict": "NO_GO",
        "assessment_verdict": assessment.get("strategy_verdict"),
        "assessment_shadow_authorized": assessment.get("shadow_authorized"),
        "collection_authorized": False,
        "process_started": False,
        "pid": None,
        "freeze": freeze,
        "parameters_refit": False,
        "final_evaluation_consumed": False,
        "forward_window_opened": False,
        "launch": launch,
        "probe": probe,
        "offline_quote_backed_fills": offline_fills,
        "shadow_exchange_fills": 0,
        "exchange_fill_rate": None,
        "exchange_fill_rate_reason": "NO_EXCHANGE_FILLS",
        "attribution": attribution,
        "duplicates": duplicates,
        "observations": len(rows),
        "books_profit": sum(1 for row in rows if row.books_profit),
        "live_sessions": list(live),
        "live_session_count": len(live),
        "engineering_floor": LIVE_SESSION_FLOOR,
        "meets_engineering_floor": len(live) >= LIVE_SESSION_FLOOR,
        "significance_floor": MIN_RESOLVED,
        "latency_ms": {
            "n": len(lags),
            "max": None if not lags else max(lags),
            "stale": sum(1 for row in rows if row.quality == "STALE"),
        },
        "arms": [_arm(arm, rows) for arm in ARMS],
        "reasons": _reasons(rows, live, assessment),
        "deployment": _deployment(),
        "git": _git_state(Path(__file__).resolve()),
        "generated_at_utc": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def _arm(arm: str, rows: Sequence[Observation]) -> dict[str, object]:
    chosen = [row for row in rows if row.arm == arm]
    quoted = [
        row
        for row in chosen
        if row.quality == "OK"
        and row.bid is not None
        and row.ask is not None
        and row.reason != "ARM_SESSION"
    ]
    estimated = [row for row in quoted if row.fill_rate_kind == "estimate"]
    trials = len(quoted)
    successes = len(estimated)
    if trials == 0:
        rate: str | None = None
        kind = "unobserved"
    else:
        rate = _rate(successes, trials)
        kind = "estimate"
    return {
        "arm": arm,
        "signals": len(chosen),
        "quoted": trials,
        "estimated_entries": successes,
        "partial_estimates": sum(1 for row in estimated if row.status == "PARTIAL_ESTIMATE"),
        "exit_failed": sum(1 for row in chosen if row.path == "EXIT_FAILED"),
        "exchange_fills": 0,
        "estimated_fill_rate": rate,
        "estimated_fill_rate_kind": kind,
        "estimated_fill_interval": estimate_interval(successes, trials),
        "exchange_fill_rate": None,
    }


def _rate(successes: int, trials: int) -> str:
    if trials <= 0:
        raise RuntimeError("empty rate")
    value = (Decimal(successes) / Decimal(trials)).quantize(Decimal("0.000001"))
    return format(value, "f")


def _reasons(
    rows: Sequence[Observation],
    live: Sequence[str],
    assessment: dict[str, object],
) -> list[str]:
    reasons = ["NO_UNATTENDED_SPAWN", "NO_EXCHANGE_FILLS"]
    if assessment.get("strategy_verdict") == "NO_GO":
        reasons.append("G_S5_5_STILL_NO_GO")
    if len(live) < LIVE_SESSION_FLOOR:
        reasons.append("LIVE_SESSIONS_BELOW_FLOOR")
    counts = Counter(row.reason for row in rows)
    for name, count in sorted(counts.items()):
        if count:
            reasons.append(f"{name}:{count}")
    return reasons


def _deployment() -> dict[str, object]:
    return {
        "executed": False,
        "start": [
            "Do not start a worker while the frozen assessment has shadow_authorized false.",
            "python -m bstocks_v2.research_data.s5_shadow writes only under data_v2_2/s5.",
            "That command does not open a socket and does not call order place or cancel.",
        ],
        "stop": ["No pid was created. Do not signal an unrelated Python process."],
        "rollback": [
            "Delete s5_06_summary.json, s5_06_observations.json, s5_06_manifest.json,",
            "and s5_06_shadow.sqlite.",
            "Leave the s5_05 files, coverage.json, screen_summary.json, and the SIP store.",
        ],
    }


def _write_sqlite(path: Path, rows: Sequence[Observation]) -> None:
    connection = sqlite3.connect(path)
    try:
        tables = {
            str(item[0])
            for item in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        if "intraday_bars_wal" in tables:
            raise ValueError("refusing to write a shadow journal into the intraday WAL")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS s5_06_observation (
                dedup_key TEXT PRIMARY KEY,
                symbol TEXT NOT NULL,
                arm TEXT NOT NULL,
                status TEXT NOT NULL,
                fill_rate_kind TEXT NOT NULL,
                books_profit INTEGER NOT NULL CHECK (books_profit = 0),
                exchange_fill_id TEXT CHECK (exchange_fill_id IS NULL),
                payload TEXT NOT NULL
            )
            """
        )
        for row in rows:
            connection.execute(
                """
                INSERT INTO s5_06_observation (
                    dedup_key, symbol, arm, status, fill_rate_kind,
                    books_profit, exchange_fill_id, payload
                ) VALUES (?, ?, ?, ?, ?, 0, NULL, ?)
                """,
                (
                    row.dedup_key,
                    row.symbol,
                    row.arm,
                    row.status,
                    row.fill_rate_kind,
                    json.dumps(row.as_dict(), sort_keys=True),
                ),
            )
        connection.commit()
    finally:
        connection.close()


def _flag(payload: dict[str, object], key: str) -> bool:
    value = payload.get(key)
    if not isinstance(value, bool):
        raise ValueError(key)
    return value


def _count(payload: dict[str, object], key: str) -> int:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(key)
    return value


def _load(path: Path) -> dict[str, object]:
    raw: object = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(path.name)
    loaded: dict[str, object] = {}
    for key, value in raw.items():
        if not isinstance(key, str):
            raise ValueError(path.name)
        loaded[key] = value
    return loaded


def _dump(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _file(path: Path, role: str) -> dict[str, object]:
    return {
        "role": role,
        "path": str(path),
        "sha256": sha256_file(path),
        "bytes": path.stat().st_size,
    }


def _git_state(start: Path) -> dict[str, object]:
    root = start
    for candidate in (start, *start.parents):
        if (candidate / ".git").exists():
            root = candidate
            break
    sha = _git(root, "rev-parse", "HEAD")
    status = _git(root, "status", "--porcelain")
    return {"root": str(root), "sha": sha, "dirty": status != ""}


def _git(root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def main() -> None:
    payload = run()
    print(payload["observation_status"], payload["phase_gate"], payload["attribution"])


if __name__ == "__main__":
    main()
