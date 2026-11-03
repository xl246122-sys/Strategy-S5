"""Stage 14 rules, embargo, DSR, and the one-shot seal. No market scan."""

from datetime import date
from decimal import Decimal
from pathlib import Path

from bstocks_v2.research_data.calendar import load_xnys_calendar
from bstocks_v2.stage14.audit import safety_audit
from bstocks_v2.stage14.bands import isolation_bands
from bstocks_v2.stage14.seal import sealed, write_seal
from bstocks_v2.stage14.spec import DSR_MIN, EMBARGO_SESSIONS, LOCKBOX_END, OPEN_P0_P1
from bstocks_v2.stage14.stats import deflated_sharpe, summarize
from bstocks_v2.stage14.trials import validation_sharpes
from bstocks_v2.stage14.verdict import judge
from bstocks_v2.strategies.s1.spec import LOCKBOX_START


def test_embargo_is_two_sessions_on_each_side() -> None:
    bands = isolation_bands(load_xnys_calendar())
    assert bands.validation.isdisjoint(bands.lockbox)
    assert bands.validation.isdisjoint(bands.embargo)
    assert bands.lockbox.isdisjoint(bands.embargo)
    assert max(bands.validation) < LOCKBOX_START
    assert min(bands.lockbox) > LOCKBOX_START
    assert max(bands.lockbox) < LOCKBOX_END
    left = sorted(day for day in bands.embargo if day < LOCKBOX_START)
    opening = sorted(day for day in bands.embargo if LOCKBOX_START <= day < date(2026, 6, 1))
    closing = sorted(day for day in bands.embargo if day > date(2026, 10, 1))
    assert len(left) == EMBARGO_SESSIONS
    assert len(opening) == EMBARGO_SESSIONS
    assert len(closing) == EMBARGO_SESSIONS


def test_summary_and_dsr_fail_closed_on_a_short_sample() -> None:
    body = summarize([Decimal("-1"), Decimal("2")], [Decimal("100"), Decimal("100")])
    assert body["closed_trades"] == 2
    assert body["expectancy_usdc"] == "0.5"
    short = deflated_sharpe([Decimal("1"), Decimal("2"), Decimal("3")], [Decimal("0.1")], 13)
    assert short["significant"] is False
    assert short["reason"] == "SAMPLE_OR_BENCHMARK"


def test_positive_expectancy_still_needs_every_bar() -> None:
    passed = judge(
        mean_net_bps=Decimal("1"),
        dsr=DSR_MIN,
        neighbor_means=(Decimal("0.5"), Decimal("0.25")),
        selected=True,
        live_locked=False,
        p0_p1_clear=True,
    )
    assert passed.deployment == "GO"
    blocked = judge(
        mean_net_bps=Decimal("1"),
        dsr=DSR_MIN,
        neighbor_means=(Decimal("0.5"), Decimal("-0.1")),
        selected=True,
        live_locked=False,
        p0_p1_clear=True,
    )
    assert blocked.deployment == "NO_GO"
    assert "NEIGHBORHOOD" in blocked.reasons
    missing = judge(
        mean_net_bps=Decimal("1"),
        dsr=DSR_MIN,
        neighbor_means=(Decimal("0.5"), None),
        selected=True,
        live_locked=False,
        p0_p1_clear=True,
    )
    assert missing.deployment == "NO_GO"
    locked = judge(
        mean_net_bps=Decimal("5"),
        dsr=Decimal("0.99"),
        neighbor_means=(Decimal("1"),),
        selected=True,
        live_locked=True,
        p0_p1_clear=True,
    )
    assert locked.deployment == "NO_GO"
    assert locked.statistical == "PASS"
    unselected = judge(
        mean_net_bps=Decimal("5"),
        dsr=Decimal("0.99"),
        neighbor_means=(Decimal("1"),),
        selected=False,
        live_locked=False,
        p0_p1_clear=True,
    )
    assert unselected.statistical == "NOT_SELECTED"
    assert unselected.deployment == "NO_GO"
    open_items = judge(
        mean_net_bps=Decimal("5"),
        dsr=Decimal("0.99"),
        neighbor_means=(Decimal("1"),),
        selected=True,
        live_locked=False,
        p0_p1_clear=False,
    )
    assert open_items.deployment == "NO_GO"
    assert "P0_P1_OPEN" in open_items.reasons


def test_known_p0_items_are_still_open() -> None:
    assert OPEN_P0_P1
    assert "stage3_key_exposure_not_rotated" in OPEN_P0_P1


def test_prior_trial_sharpes_do_not_touch_the_lockbox() -> None:
    defined, printed = validation_sharpes()
    assert len(printed) == 13
    assert defined
    assert all(name in printed for name in ("S1:ols", "S2:gate", "S4:guarded"))


def test_seal_refuses_a_second_write(tmp_path: Path) -> None:
    result = tmp_path / "stage14_lockbox.json"
    seal = tmp_path / "stage14_lockbox_seal.json"
    result.write_text("{}", encoding="utf-8")
    digest = write_seal(seal, result, "{}")
    assert sealed(seal)
    assert len(digest) == 64
    try:
        write_seal(seal, result, "{}")
    except RuntimeError as exc:
        assert "seal" in str(exc)
    else:
        raise AssertionError("second seal was accepted")


def test_safety_audit_keeps_live_trading_off() -> None:
    report = safety_audit()
    assert report["passed"] is True
    runtime = report["runtime"]
    assert isinstance(runtime, dict)
    assert runtime["live_trade_enabled"] is False
    assert runtime["s4_live_enabled"] is False
    assert report["secret_hits"] == []
