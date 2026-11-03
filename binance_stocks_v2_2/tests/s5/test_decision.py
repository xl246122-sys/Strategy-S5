from decimal import Decimal
from pathlib import Path

import pytest

from bstocks_v2.research_data.paths import OUT_DEFAULT
from bstocks_v2.research_data.s5_backtest import COVERAGE_SHA, SCREEN_SHA
from bstocks_v2.research_data.s5_freeze import (
    ASSESSMENT_SHA,
    FREEZE_SHA,
    LEDGER_SHA,
    MAIN_SHA,
    Locks,
    assemble,
    change_list,
    code_locks,
    read_g14,
    refuse_enablement,
    verify_pins,
)
from bstocks_v2.stage14.spec import DSR_MIN
from bstocks_v2.strategies.s5.decision import ArmFacts, pooled_expectancy, rule_arm, rule_arms
from bstocks_v2.strategies.s5.observe import parameter_freeze
from bstocks_v2.strategies.s5.spec import ARMS


def _repo_root(start: Path) -> Path:
    for candidate in (start, *start.parents):
        if (candidate / ".git").exists():
            return candidate
    raise RuntimeError("git root missing")


ROOT = _repo_root(Path(__file__).resolve())


def test_the_sealed_shape_is_no_go_on_every_arm() -> None:
    rulings = rule_arms(tuple(_empty(arm) for arm in ARMS), quote_tape=False)
    body = assemble(
        rulings,
        g14_deployment="NO_GO",
        g14_verified=True,
        locks=_locks(),
        unexpected_paths=(),
    )
    assert [item.verdict for item in rulings] == ["NO_GO", "NO_GO", "NO_GO"]
    assert body["strategy_verdict"] == "NO_GO"
    assert body["research_mode"] == "RESEARCH_ONLY"
    layers = body["layers"]
    assert isinstance(layers, dict)
    assert layers["research_engineering"] == "DONE"
    assert layers["statistical"] == "FAIL"
    assert layers["shadow"] == "INCOMPLETE"
    assert layers["live_authorization"] == "NOT_GRANTED"
    assert body["live_authorized"] is False
    assert body["collection_authorized"] is False
    assert body["live_enablement_rfc"] is None
    assert body["dsr_estimated"] is False
    assert "NO_EXECUTABLE_QUOTES" in rulings[0].reasons
    assert "SAMPLE_OR_EXPECTANCY" in rulings[0].reasons


def test_a_finished_shadow_does_not_repair_a_failed_sample() -> None:
    facts = _empty("A_RTH_TO_POST")
    ready_shadow = ArmFacts(
        facts.arm,
        facts.closed_trades,
        facts.expectancy_usdc,
        facts.slippage_known,
        facts.exit_acceptable,
        facts.dsr,
        facts.neighbor_means,
        20,
        0,
        True,
    )
    ruling = rule_arm(ready_shadow, quote_tape=False)
    assert ruling.shadow == "PASS"
    assert ruling.statistical == "FAIL"
    assert ruling.verdict == "NO_GO"


def test_a_statistical_pass_with_a_short_shadow_stays_shadow_more() -> None:
    rulings = rule_arms(tuple(_ready(arm, sessions=19) for arm in ARMS), quote_tape=True)
    body = assemble(
        rulings,
        g14_deployment="NO_GO",
        g14_verified=True,
        locks=_locks(),
        unexpected_paths=(),
    )
    assert {item.verdict for item in rulings} == {"SHADOW_MORE"}
    assert body["collection_authorized"] is False
    assert body["live_authorized"] is False
    assert body["live_enablement_rfc"] is None
    assert body["research_mode"] == "RESEARCH_ONLY"


def test_review_does_not_grant_live_or_skip_g14() -> None:
    rulings = rule_arms(tuple(_ready(arm, sessions=20) for arm in ARMS), quote_tape=True)
    blocked = assemble(
        rulings,
        g14_deployment="NO_GO",
        g14_verified=True,
        locks=_locks(),
        unexpected_paths=(),
    )
    listed = assemble(
        rulings,
        g14_deployment="GO",
        g14_verified=True,
        locks=_locks(),
        unexpected_paths=(),
    )
    assert blocked["strategy_verdict"] == "GO_FOR_REVIEW"
    assert blocked["live_authorized"] is False
    assert blocked["live_enablement_rfc"] is None
    assert blocked["live_enablement_reason"] == "G14_NO_GO"
    rfc = listed["live_enablement_rfc"]
    assert isinstance(rfc, dict)
    assert rfc["apply_now"] is False
    assert rfc["live_enabled"] is False
    assert rfc["hwm_unlock"] is False
    assert rfc["phase15_bypass"] is False
    assert listed["live_authorized"] is False
    assert listed["phase15_opened"] is False


def test_arms_stay_separate_and_a_zero_expectancy_fails() -> None:
    mixed = (
        _empty(ARMS[0]),
        _ready(ARMS[1], sessions=0),
        _ready(ARMS[2], sessions=20),
    )
    rulings = rule_arms(mixed, quote_tape=True)
    assert [item.verdict for item in rulings] == ["NO_GO", "SHADOW_MORE", "GO_FOR_REVIEW"]
    with pytest.raises(ValueError, match="ARMS_NOT_POOLED"):
        pooled_expectancy(mixed)
    flat = rule_arm(_ready(ARMS[0], sessions=20, expectancy=Decimal(0)), quote_tape=True)
    assert flat.verdict == "NO_GO"
    assert "EXPECTANCY_NOT_POSITIVE" in flat.reasons


def test_the_dsr_boundary_and_a_missing_neighbor_fail_closed() -> None:
    below = rule_arm(_ready(ARMS[0], sessions=20, dsr=DSR_MIN - Decimal("0.01")), quote_tape=True)
    exact = rule_arm(_ready(ARMS[0], sessions=20, dsr=DSR_MIN), quote_tape=True)
    missing = rule_arm(_ready(ARMS[0], sessions=20, neighbors=()), quote_tape=True)
    assert below.verdict == "NO_GO"
    assert "DSR" in below.reasons
    assert exact.verdict == "GO_FOR_REVIEW"
    assert missing.verdict == "NO_GO"
    assert "NEIGHBORHOOD" in missing.reasons


def test_enablement_and_a_bad_lock_are_refused() -> None:
    with pytest.raises(RuntimeError, match="cannot enable"):
        refuse_enablement(live_enabled=True, unlock_hwm=False, phase15=False)
    with pytest.raises(RuntimeError, match="cannot enable"):
        refuse_enablement(live_enabled=False, unlock_hwm=True, phase15=False)
    with pytest.raises(RuntimeError, match="cannot enable"):
        refuse_enablement(live_enabled=False, unlock_hwm=False, phase15=True)
    rulings = rule_arms(tuple(_empty(arm) for arm in ARMS), quote_tape=False)
    drifted = _locks()
    with pytest.raises(RuntimeError, match="live lock"):
        assemble(
            rulings,
            g14_deployment="NO_GO",
            g14_verified=True,
            locks=Locks(
                True,
                drifted.s4_live_enabled,
                drifted.s5_live_enabled,
                drifted.s5_mode,
                drifted.admitted,
                drifted.governs,
                drifted.guardian_references_s5,
                drifted.hwm_retained,
                drifted.max_open_positions,
                drifted.min_notional,
            ),
            unexpected_paths=(),
        )


def test_a_changed_pin_is_refused(tmp_path: Path) -> None:
    (tmp_path / "s5_05_assessment.json").write_text("{}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="hash"):
        verify_pins(tmp_path)


def test_the_freeze_module_has_no_unlock_and_no_live_assignment() -> None:
    source = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "bstocks_v2"
        / "research_data"
        / "s5_freeze.py"
    )
    body = source.read_text(encoding="utf-8")
    assert ".unlock(" not in body
    assert "live_enabled = True" not in body
    assert "collection_authorized = True" not in body
    assert "unlink" not in body
    assert "rmtree" not in body


def test_real_locks_pins_and_g14_still_match() -> None:
    locks = code_locks()
    assert locks.live_trade_enabled is False
    assert locks.s4_live_enabled is False
    assert locks.s5_live_enabled is False
    assert locks.admitted == frozenset({"S1", "S2", "S3", "S4"})
    assert locks.governs is False
    pins = verify_pins(OUT_DEFAULT / "s5")
    assert pins["s5_05_assessment.json"] == ASSESSMENT_SHA
    assert pins["s5_05_ledger.json"] == LEDGER_SHA
    assert pins["coverage.json"] == COVERAGE_SHA
    assert pins["screen_summary.json"] == SCREEN_SHA
    assert parameter_freeze() == FREEZE_SHA
    g14 = read_g14(OUT_DEFAULT / "reports")
    assert g14["verified"] is True
    assert g14["deployment"] == "NO_GO"
    assert g14["p0_p1_clear"] is False
    assert g14["safety_audit_passed"] is True
    assert g14["safety_audit_is_deployment"] is False
    listed = change_list(ROOT)
    assert listed["baseline_main"] == MAIN_SHA
    assert listed["unexpected"] == []
    paths = listed["paths"]
    assert isinstance(paths, list)
    joined = "\n".join(str(path) for path in paths)
    for folder in ("/strategies/s1/", "/strategies/s2/", "/strategies/s3/", "/strategies/s4/"):
        assert folder not in joined
    for folder in ("/guardian/", "/limit_optimizer/", "/signal_arbitrator/", "/stage14/"):
        assert folder not in joined


def _empty(arm: str) -> ArmFacts:
    return ArmFacts(arm, 0, None, False, False, None, (), 0, 0, False)


def _ready(
    arm: str,
    *,
    sessions: int,
    expectancy: Decimal = Decimal("1"),
    dsr: Decimal = DSR_MIN,
    neighbors: tuple[Decimal | None, ...] = (Decimal("1"), Decimal("1")),
) -> ArmFacts:
    return ArmFacts(arm, 30, expectancy, True, True, dsr, neighbors, sessions, 0, True)


def _locks() -> Locks:
    return Locks(
        False,
        False,
        False,
        "RESEARCH_ONLY",
        frozenset({"S1", "S2", "S3", "S4"}),
        False,
        False,
        "0.92",
        5,
        "120",
    )
