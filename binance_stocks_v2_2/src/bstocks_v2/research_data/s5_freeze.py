"""S5-P7 freeze. The sealed tape stays NO_GO, and this module cannot enable trading."""

from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from bstocks_v2.constitution.guard import (
    HWM_RETAINED,
    MAX_OPEN_POSITIONS,
    MIN_VIABLE_NOTIONAL,
    live_trade_enabled,
)
from bstocks_v2.contracts.errors import LiveTradingDisabledError
from bstocks_v2.research_data.manifest import assert_output_isolated, sha256_file
from bstocks_v2.research_data.paths import FRESH_DEFAULT, OUT_DEFAULT
from bstocks_v2.research_data.s5_backtest import COVERAGE_SHA, SCREEN_SHA, probe_schema
from bstocks_v2.research_data.s5_shadow import ASSESSMENT_SHA, LEDGER_SHA
from bstocks_v2.signal_arbitrator.spec import ADMITTED
from bstocks_v2.strategies.s4.lock import s4_live_enabled
from bstocks_v2.strategies.s5.assess import V22_LOCKBOX_SEAL
from bstocks_v2.strategies.s5.decision import ArmFacts, ArmRuling, rule_arms
from bstocks_v2.strategies.s5.lock import MODE, refuse_live_route, s5_live_enabled
from bstocks_v2.strategies.s5.observe import parameter_freeze
from bstocks_v2.strategies.s5.policy import S5GuardianPolicy
from bstocks_v2.strategies.s5.spec import ARMS

MAIN_SHA = "e284d2ed2e6bd3c8c2d7be28fda5edc8e7044bee"
FREEZE_SHA = "00f5a0c250b1c0fac2688fb64881b5ccf2a56f13a6d47b914300d1735c59f963"
EXPERIMENT_SHA = "92f61c63c8d9c169bec91d0094a3b004cec73790db371710672e14594b6f314d"
MANIFEST05_SHA = "be991553ab032eaf186b204503b8303db41ded2165d8380ae898c598e27573d4"
SUMMARY_SHA = "b688c050ce253d2f2f5bbdbeca51a5991e55c2bf854070ae2d7c0fcd8dbaf7cf"
OBSERVATIONS_SHA = "a5338d955b09046ec0b16f3a9625b7955c763aae07dc722e474e6078745f932f"
SQLITE_SHA = "f4f9afd2e6cf56daeee61f52e92b6ad7123822b7fd81c4eb04f9530fa1484391"
MANIFEST06_SHA = "9d1f61e2a7527980431e6dd59da8975a26e700d7ce6163626490a7d2c7e30e21"
_PLACE = "/sapi/v1/equity/order/place"
_PINS = {
    "coverage.json": COVERAGE_SHA,
    "screen_summary.json": SCREEN_SHA,
    "s5_05_assessment.json": ASSESSMENT_SHA,
    "s5_05_experiment.json": EXPERIMENT_SHA,
    "s5_05_ledger.json": LEDGER_SHA,
    "s5_05_manifest.json": MANIFEST05_SHA,
    "s5_06_summary.json": SUMMARY_SHA,
    "s5_06_observations.json": OBSERVATIONS_SHA,
    "s5_06_shadow.sqlite": SQLITE_SHA,
    "s5_06_manifest.json": MANIFEST06_SHA,
}
_ALLOWED_EXACT = frozenset(
    {
        ".gitignore",
        "binance_stocks_v2_2/src/bstocks_v2/constitution/guard.py",
        "binance_stocks_v2_2/src/bstocks_v2/contracts/models.py",
        "binance_stocks_v2_2/src/bstocks_v2/market_ingestor/messages.py",
        "binance_stocks_v2_2/src/bstocks_v2/portfolio_risk/authority.py",
        "binance_stocks_v2_2/tests/test_console.py",
    }
)
_ALLOWED_PREFIXES = (
    ".cursor/rules/",
    "K-Line-DataBase/",
    "binance_stocks_v2_2/native/",
    "binance_stocks_v2_2/src/bstocks_v2/console/",
    "binance_stocks_v2_2/src/bstocks_v2/strategies/s5/",
    "binance_stocks_v2_2/src/bstocks_v2/sessions/",
    "binance_stocks_v2_2/src/bstocks_v2/research_data/s5_",
    "binance_stocks_v2_2/tests/s5/",
    "docs/s5/",
    "release/",
)


@dataclass(frozen=True)
class Locks:
    live_trade_enabled: bool
    s4_live_enabled: bool
    s5_live_enabled: bool
    s5_mode: str
    admitted: frozenset[str]
    governs: bool
    guardian_references_s5: bool
    hwm_retained: str
    max_open_positions: int
    min_notional: str


def refuse_enablement(*, live_enabled: bool, unlock_hwm: bool, phase15: bool) -> None:
    """This phase has no live switch, no HWM unlock, and no Phase 15 bypass."""

    if live_enabled or unlock_hwm or phase15:
        raise RuntimeError("S5-P7 cannot enable live trading")


def verify_pins(store: Path) -> dict[str, str]:
    """Every sealed research file must still match the hash recorded at its gate."""

    found: dict[str, str] = {}
    for name, expected in _PINS.items():
        path = store / name
        if not path.is_file():
            raise ValueError(f"hash mismatch for {name}")
        digest = sha256_file(path)
        if digest != expected:
            raise ValueError(f"hash mismatch for {name}")
        found[name] = digest
    return found


def read_g14(reports: Path) -> dict[str, object]:
    """Check the stage-14 seal. A passing code checklist is not a deployment pass."""

    seal = _load(reports / "stage14_lockbox_seal.json")
    recorded = seal.get("sha256")
    if recorded != V22_LOCKBOX_SEAL:
        raise RuntimeError("G14 seal drifted")
    body_path = reports / "stage14_lockbox.json"
    text = body_path.read_text(encoding="utf-8")
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    if digest != recorded:
        raise RuntimeError("G14 body drifted")
    raw: object = json.loads(text)
    if not isinstance(raw, dict):
        raise ValueError("lockbox")
    body = _string_key_dict(raw, "lockbox")
    if body.get("live_trade_enabled") is not False or body.get("s4_live_enabled") is not False:
        raise RuntimeError("G14 live flag drifted")
    if body.get("p0_p1_clear") is not False:
        raise RuntimeError("G14 clearance drifted")
    decisions = body.get("decisions")
    if not isinstance(decisions, dict):
        raise ValueError("decisions")
    rows: list[dict[str, object]] = []
    for name in ("S1", "S2", "S3", "S4"):
        item = decisions.get(name)
        if not isinstance(item, dict) or item.get("deployment") != "NO_GO":
            raise RuntimeError("G14 deployment drifted")
        rows.append(
            {
                "strategy": name,
                "deployment": "NO_GO",
                "statistical": item.get("statistical"),
            }
        )
    safety = body.get("safety_audit")
    if not isinstance(safety, dict) or safety.get("passed") is not True:
        raise RuntimeError("G14 safety audit drifted")
    return {
        "verified": True,
        "deployment": "NO_GO",
        "p0_p1_clear": False,
        "open_p0_p1": body.get("open_p0_p1"),
        "safety_audit_passed": True,
        "safety_audit_is_deployment": False,
        "strategies": rows,
        "seal_sha256": digest,
    }


def code_locks() -> Locks:
    """Read the live switches from code. A config file cannot flip them here."""

    policy = S5GuardianPolicy()
    machine = Path(__file__).resolve().parents[1] / "guardian" / "machine.py"
    text = machine.read_text(encoding="utf-8")
    try:
        refuse_live_route(_PLACE)
    except LiveTradingDisabledError:
        refused = True
    else:
        refused = False
    if not refused or policy.governs():
        raise RuntimeError("S5 route drifted")
    return Locks(
        live_trade_enabled(),
        s4_live_enabled(),
        s5_live_enabled(),
        MODE,
        frozenset(ADMITTED),
        policy.governs(),
        "S5GuardianPolicy" in text or "strategies.s5" in text,
        format(HWM_RETAINED, "f"),
        MAX_OPEN_POSITIONS,
        format(MIN_VIABLE_NOTIONAL, "f"),
    )


def change_list(root: Path) -> dict[str, object]:
    """Paths changed since the frozen main commit. Protected paths fail closed."""

    if _git(root, "rev-parse", "main") != MAIN_SHA:
        raise RuntimeError("main baseline drifted")
    committed = [line for line in _git(root, "diff", "--name-only", "main").splitlines() if line]
    extra: list[str] = []
    for raw_line in _git(root, "status", "--porcelain").splitlines():
        trimmed = raw_line.strip()
        if not trimmed or " " not in trimmed:
            continue
        path = trimmed.split(None, 1)[1]
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        extra.append(path.strip())
    paths = list(dict.fromkeys([*committed, *extra]))
    unexpected = [path for path in paths if not _allowed(path.replace("\\", "/"))]
    return {
        "baseline_main": MAIN_SHA,
        "head": _git(root, "rev-parse", "HEAD"),
        "dirty": _git(root, "status", "--porcelain") != "",
        "paths": paths,
        "unexpected": unexpected,
    }


def assemble(
    rulings: tuple[ArmRuling, ...],
    *,
    g14_deployment: str,
    g14_verified: bool,
    locks: Locks,
    unexpected_paths: tuple[str, ...],
) -> dict[str, object]:
    """Build the decision. Live authorization stays closed in every branch."""

    refuse_enablement(live_enabled=False, unlock_hwm=False, phase15=False)
    if not g14_verified:
        raise RuntimeError("G14 unverified")
    if unexpected_paths:
        raise RuntimeError("whitelist")
    _require_locks(locks)
    if tuple(item.arm for item in rulings) != ARMS:
        raise ValueError("ARMS_NOT_POOLED")
    verdicts = tuple(item.verdict for item in rulings)
    if "NO_GO" in verdicts:
        overall = "NO_GO"
    elif "SHADOW_MORE" in verdicts:
        overall = "SHADOW_MORE"
    else:
        overall = "GO_FOR_REVIEW"
    rfc: dict[str, object] | None = None
    if overall == "GO_FOR_REVIEW" and g14_deployment == "GO":
        rfc_reason = "LISTED_NOT_APPLIED"
        rfc = {
            "apply_now": False,
            "live_enabled": False,
            "hwm_unlock": False,
            "phase15_bypass": False,
            "kill_switch": "s5_live_enabled remains false",
            "window": "unset",
        }
    elif overall == "GO_FOR_REVIEW":
        rfc_reason = "G14_NO_GO"
    elif overall == "SHADOW_MORE":
        rfc_reason = "SHADOW_INCOMPLETE"
    else:
        rfc_reason = "STRATEGY_NO_GO"
    statistical = "PASS" if all(item.statistical == "PASS" for item in rulings) else "FAIL"
    shadow = "PASS" if all(item.shadow == "PASS" for item in rulings) else "INCOMPLETE"
    return {
        "phase": "S5-P7",
        "phase_gate": "PASS",
        "strategy_verdict": overall,
        "research_mode": "RESEARCH_ONLY",
        "layers": {
            "research_engineering": "DONE",
            "statistical": statistical,
            "shadow": shadow,
            "live_authorization": "NOT_GRANTED",
        },
        "arms": [item.as_dict() for item in rulings],
        "live_authorized": False,
        "collection_authorized": False,
        "shadow_authorized": False,
        "hwm_unlocked": False,
        "phase15_opened": False,
        "parameters_refit": False,
        "final_evaluation_consumed": False,
        "forward_window_opened": False,
        "live_enablement_rfc": rfc,
        "live_enablement_reason": rfc_reason,
        "g14_deployment": g14_deployment,
        "dsr_estimated": all(item.dsr is not None for item in rulings),
    }


def run(
    source: Path = FRESH_DEFAULT,
    store: Path = OUT_DEFAULT / "s5",
    reports: Path = OUT_DEFAULT / "reports",
) -> dict[str, object]:
    """Write the freeze from the sealed files. A quote column stops the run."""

    refuse_enablement(live_enabled=False, unlock_hwm=False, phase15=False)
    locks = code_locks()
    _require_locks(locks)
    if parameter_freeze() != FREEZE_SHA:
        raise RuntimeError("parameter freeze drifted")
    before = verify_pins(store)
    g14 = read_g14(reports)
    assessment = _load(store / "s5_05_assessment.json")
    shadow = _load(store / "s5_06_summary.json")
    _require_sealed(assessment, shadow)
    probe = probe_schema(source)
    if probe.get("has_bid_ask") is True or probe.get("rows_loaded") != 0:
        raise RuntimeError("QUOTE_TAPE_UNREVIEWED")
    facts = _facts(assessment, shadow, probe)
    rulings = rule_arms(facts, quote_tape=False)
    root = _repo_root(Path(__file__).resolve())
    changes = change_list(root)
    unexpected = changes["unexpected"]
    if not isinstance(unexpected, list) or unexpected:
        raise RuntimeError("whitelist")
    body = assemble(
        rulings,
        g14_deployment="NO_GO",
        g14_verified=g14["verified"] is True,
        locks=locks,
        unexpected_paths=(),
    )
    if body["strategy_verdict"] != "NO_GO" or body["research_mode"] != "RESEARCH_ONLY":
        raise RuntimeError("official tape left NO_GO")
    if body["dsr_estimated"] is not False:
        raise RuntimeError("dsr drifted")
    if body["live_authorized"] is not False or body["collection_authorized"] is not False:
        raise RuntimeError("authorization drifted")
    if body["live_enablement_rfc"] is not None or body["phase15_opened"] is not False:
        raise RuntimeError("rfc drifted")
    if verify_pins(store) != before:
        raise RuntimeError("sealed files changed during the freeze")
    payload: dict[str, object] = {
        **body,
        "g14": g14,
        "locks": _lock_dict(locks),
        "whitelist": changes,
        "schema": _schema(),
        "shadow_evidence": _shadow_evidence(shadow),
        "tail": {"cvar95_usdc": None, "status": "UNMEASURED"},
        "inputs": before,
        "probe": probe,
        "freeze": FREEZE_SHA,
        "investor_options": _options(),
        "rollback": _rollback(),
    }
    _publish(store, source, payload)
    if verify_pins(store) != before:
        raise RuntimeError("sealed files changed during the freeze")
    return payload


def _facts(
    assessment: dict[str, object],
    shadow: dict[str, object],
    probe: dict[str, object],
) -> tuple[ArmFacts, ...]:
    live = _count(shadow, "live_session_count")
    by_arm = {str(row["arm"]): row for row in _rows(shadow, "arms")}
    facts: list[ArmFacts] = []
    for row in _rows(assessment, "arms"):
        name = str(row["arm"])
        other = by_arm[name]
        facts.append(
            ArmFacts(
                name,
                _count(row, "closed_trades"),
                _expectancy(row.get("expectancy_usdc")),
                row.get("slippage_known") is True,
                row.get("exit_acceptable") is True,
                None,
                (),
                live,
                _count(other, "exchange_fills"),
                probe.get("has_bid_ask") is True,
            )
        )
    return tuple(facts)


def _require_sealed(assessment: dict[str, object], shadow: dict[str, object]) -> None:
    verdict_ok = assessment.get("strategy_verdict") == "NO_GO"
    shadow_closed = assessment.get("shadow_authorized") is False
    if not verdict_ok or not shadow_closed:
        raise RuntimeError("frozen verdict drifted")
    if assessment.get("mode") != "rth_candidate_screen":
        raise RuntimeError("mode drifted")
    if assessment.get("overnight_profit_claimed") is not False:
        raise RuntimeError("overnight claim drifted")
    if assessment.get("quote_backed_fills") != 0 or assessment.get("live_enabled") is not False:
        raise RuntimeError("assessment book drifted")
    shadow_verdict = shadow.get("strategy_verdict") == "NO_GO"
    collection_closed = shadow.get("collection_authorized") is False
    if not shadow_verdict or not collection_closed:
        raise RuntimeError("shadow flag drifted")
    incomplete = shadow.get("observation_status") == "INCOMPLETE"
    no_tape = shadow.get("attribution") == "NO_LIVE_TAPE"
    if not incomplete or not no_tape:
        raise RuntimeError("shadow tape drifted")
    if shadow.get("meets_engineering_floor") is not False or shadow.get("freeze") != FREEZE_SHA:
        raise RuntimeError("shadow floor drifted")
    if shadow.get("process_started") is not False or shadow.get("shadow_exchange_fills") != 0:
        raise RuntimeError("shadow process drifted")
    for row in _rows(assessment, "arms"):
        if row.get("expectancy_usdc") is not None or row.get("cvar95_usdc") is not None:
            raise RuntimeError("sealed expectancy drifted")
        if _count(row, "closed_trades") != 0:
            raise RuntimeError("sealed fills drifted")


def _require_locks(locks: Locks) -> None:
    if locks.live_trade_enabled or locks.s4_live_enabled or locks.s5_live_enabled:
        raise RuntimeError("live lock drifted")
    if locks.s5_mode != "RESEARCH_ONLY":
        raise RuntimeError("mode drifted")
    if locks.admitted != frozenset({"S1", "S2", "S3", "S4"}):
        raise RuntimeError("admission drifted")
    if locks.governs or locks.guardian_references_s5:
        raise RuntimeError("guardian policy drifted")
    if locks.hwm_retained != "0.92" or locks.max_open_positions != 5 or locks.min_notional != "120":
        raise RuntimeError("risk limits drifted")


def _publish(store: Path, source: Path, payload: dict[str, object]) -> None:
    target = store / "s5_07_verdict.json"
    assert_output_isolated(target, source)
    if target.name in {"intraday.sqlite", "guardian.sqlite", "portfolio.sqlite", "orders.sqlite"}:
        raise ValueError("production filename")
    store.mkdir(parents=True, exist_ok=True)
    _dump(target, payload)
    whitelist = {
        "phase": "S5-P7",
        "baseline_main": MAIN_SHA,
        "whitelist": payload["whitelist"],
        "schema": payload["schema"],
        "rollback": payload["rollback"],
    }
    _dump(store / "s5_07_whitelist.json", whitelist)
    manifest = {
        "created_utc": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "mode": "research_freeze",
        "strategy_verdict": payload["strategy_verdict"],
        "live_authorized": False,
        "order_place": "refused",
        "source_opened_for_write": False,
        "files": [
            _file(target, "verdict"),
            _file(store / "s5_07_whitelist.json", "whitelist"),
        ],
    }
    _dump(store / "s5_07_manifest.json", manifest)


def _shadow_evidence(shadow: dict[str, object]) -> dict[str, object]:
    return {
        "observation_status": shadow.get("observation_status"),
        "live_session_count": shadow.get("live_session_count"),
        "engineering_floor": shadow.get("engineering_floor"),
        "exchange_fills": shadow.get("shadow_exchange_fills"),
        "attribution": shadow.get("attribution"),
        "collection_authorized": False,
    }


def _schema() -> dict[str, object]:
    return {
        "production_migration": "none",
        "production_databases": [
            "intraday.sqlite",
            "guardian.sqlite",
            "portfolio.sqlite",
            "orders.sqlite",
        ],
        "research_tables": [
            {"database": "s5 recorder sqlite", "table": "s5_shadow_event"},
            {
                "database": "s5 research book",
                "tables": ["s5_flag", "s5_alert", "s5_refusal", "s5_order", "s5_position"],
            },
            {"database": "s5_06_shadow.sqlite", "table": "s5_06_observation"},
        ],
    }


def _options() -> list[dict[str, object]]:
    return [
        {
            "id": "ACCEPT_RESEARCH_ONLY",
            "granted_by_this_phase": False,
            "effect": "Keep S5 in research. V2.2 can continue its own later phase.",
        },
        {
            "id": "AUTHORIZE_READONLY_COLLECTION",
            "granted_by_this_phase": False,
            "effect": "Needs a separate written decision before collection can change.",
        },
        {
            "id": "AUTHORIZE_LIVE",
            "granted_by_this_phase": False,
            "effect": "Refused here. Phase 15 and a separate written authorization remain.",
        },
    ]


def _rollback() -> list[str]:
    return [
        "Delete decision.py, s5_freeze.py, test_decision.py, and s5_07_go_nogo.md.",
        "Delete s5_07_verdict.json, s5_07_whitelist.json, and s5_07_manifest.json.",
        "Leave s5_05, s5_06, coverage.json, screen_summary.json, SIP, and the stage-14 seal.",
    ]


def _lock_dict(locks: Locks) -> dict[str, object]:
    return {
        "live_trade_enabled": locks.live_trade_enabled,
        "s4_live_enabled": locks.s4_live_enabled,
        "s5_live_enabled": locks.s5_live_enabled,
        "s5_mode": locks.s5_mode,
        "admitted": sorted(locks.admitted),
        "governs": locks.governs,
        "guardian_references_s5": locks.guardian_references_s5,
        "hwm_retained": locks.hwm_retained,
        "max_open_positions": locks.max_open_positions,
        "min_notional": locks.min_notional,
    }


def _allowed(path: str) -> bool:
    if path in _ALLOWED_EXACT:
        return True
    return any(path.startswith(prefix) for prefix in _ALLOWED_PREFIXES)


def _expectancy(value: object) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, str):
        raise ValueError("expectancy")
    return Decimal(value)


def _count(payload: dict[str, object], key: str) -> int:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(key)
    return value


def _rows(payload: dict[str, object], key: str) -> list[dict[str, object]]:
    raw = payload.get(key)
    if not isinstance(raw, list):
        raise ValueError(key)
    return [_string_key_dict(item, key) for item in raw]


def _string_key_dict(raw: object, label: str) -> dict[str, object]:
    if not isinstance(raw, dict):
        raise ValueError(label)
    loaded: dict[str, object] = {}
    for key, value in raw.items():
        if not isinstance(key, str):
            raise ValueError(label)
        loaded[key] = value
    return loaded


def _load(path: Path) -> dict[str, object]:
    raw: object = json.loads(path.read_text(encoding="utf-8"))
    return _string_key_dict(raw, path.name)


def _repo_root(start: Path) -> Path:
    for candidate in (start, *start.parents):
        if (candidate / ".git").exists():
            return candidate
    raise RuntimeError("git root missing")


def _git(root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _dump(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _file(path: Path, role: str) -> dict[str, object]:
    return {
        "role": role,
        "path": str(path),
        "sha256": sha256_file(path),
        "bytes": path.stat().st_size,
    }


def main() -> None:
    payload = run()
    print(payload["strategy_verdict"], payload["phase_gate"], payload["research_mode"])


if __name__ == "__main__":
    main()
