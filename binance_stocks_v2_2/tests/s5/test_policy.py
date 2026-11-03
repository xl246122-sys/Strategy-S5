import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from bstocks_v2.config.loader import load_runtime_config
from bstocks_v2.constitution.guard import entry_window_open, flatten_deadline
from bstocks_v2.contracts.errors import (
    ConstitutionBypassError,
    LiveTradingDisabledError,
    OrderRejected,
)
from bstocks_v2.contracts.models import Direction, FillHorizon, TradeIntent
from bstocks_v2.signal_arbitrator.spec import ADMITTED
from bstocks_v2.strategies.s5.lock import refuse_live_route, s5_live_enabled
from bstocks_v2.strategies.s5.policy import S5GuardianPolicy, research_policy

ET = ZoneInfo("America/New_York")
ROOT = Path(__file__).resolve().parents[2]
GUARD = ROOT / "src" / "bstocks_v2" / "guardian" / "machine.py"


def test_s5_lock_rejects_config_and_has_no_order_route(tmp_path: Path) -> None:
    assert s5_live_enabled() is False
    hostile = tmp_path / "s5.json"
    hostile.write_text(
        json.dumps({"s5_live_enabled": True, "live_enabled": True, "mode": "LIVE"}),
        encoding="utf-8",
    )
    with pytest.raises(ConstitutionBypassError):
        load_runtime_config(hostile)
    with pytest.raises(LiveTradingDisabledError):
        refuse_live_route("/sapi/v1/equity/order/place")


def test_policy_is_not_installed_on_the_global_flatten() -> None:
    policy = research_policy()
    assert policy.active is False
    assert policy.live_enabled is False
    assert policy.session_eligibility_verified is False
    assert policy.allow_cross_rth_close is True
    assert policy.allowed_order_types == ("LIMIT",)
    guardian = S5GuardianPolicy(policy)
    assert guardian.governs() is False
    with pytest.raises(OrderRejected) as caught:
        guardian.on_clock(datetime(2024, 11, 1, 16, 1, tzinfo=ET))
    assert caught.value.reason == "S5_POLICY_INACTIVE"
    source = GUARD.read_text(encoding="utf-8")
    assert "S5GuardianPolicy" not in source
    assert "strategies.s5" not in source
    assert "S5" not in ADMITTED
    assert "S5_EOD_REVERSAL" not in ADMITTED


def test_old_trade_intent_payload_still_loads() -> None:
    now = datetime(2026, 11, 2, 14, 0, tzinfo=UTC)
    intent = TradeIntent(
        intent_id="intent-1",
        created_at_utc=now,
        data_asof_utc=now,
        symbol="AAPL",
        strategy_id="S1",
        model_version="v0",
        regime="UNKNOWN",
        features_hash="abc",
        vwap_intact=True,
        signal_valid_until=now,
        direction=Direction.LONG,
        p_win_calibrated=Decimal("0.50"),
        p_sl_calibrated=Decimal("0.30"),
        p_timeout_calibrated=Decimal("0.20"),
        expected_return_bps=Decimal("20"),
        expected_net_bps=Decimal("8"),
        prediction_interval=(Decimal("1"), Decimal("3")),
        candidate_limit_prices=[Decimal("100.50")],
        p_fill_by_price_horizon=[
            FillHorizon(price=Decimal("100.50"), horizon_seconds=300, p_fill=Decimal("0.40"))
        ],
        entry_price_ceiling=Decimal("101.00"),
        stop_price=Decimal("99.00"),
        take_profit_price=Decimal("104.00"),
        max_holding_seconds=3600,
        risk_budget_usdc=Decimal("4"),
        min_viable_notional_usdc=Decimal("120"),
        expected_cost_usdc=Decimal("0.50"),
        requires_fractional=False,
        whole_share_fallback_qty=Decimal("1"),
        data_feed="sip",
        data_freshness_ms=80,
    )
    assert intent.news_event_state == "UNKNOWN"
    assert intent.session == ""
    assert intent.research_arm == ""
    stored = intent.model_dump()
    for key in (
        "session",
        "capture_ts",
        "exchange_ts",
        "feed",
        "quote_freshness_ms",
        "session_eligibility_snapshot_hash",
        "research_arm",
        "news_event_state",
    ):
        del stored[key]
    restored = TradeIntent.model_validate(stored)
    assert restored.news_event_state == "UNKNOWN"
    assert restored.strategy_id == "S1"


def test_global_1545_and_1555_clocks_are_unchanged() -> None:
    regular = datetime(2024, 11, 1, 15, 44, tzinfo=ET)
    cutoff = datetime(2024, 11, 1, 15, 45, tzinfo=ET)
    assert entry_window_open(regular, False) is True
    assert entry_window_open(cutoff, False) is False
    assert flatten_deadline(cutoff, False).hour == 15
    assert flatten_deadline(cutoff, False).minute == 55
    half_open = datetime(2024, 11, 29, 12, 44, tzinfo=ET)
    half_cut = datetime(2024, 11, 29, 12, 45, tzinfo=ET)
    assert entry_window_open(half_open, True) is True
    assert entry_window_open(half_cut, True) is False
    assert flatten_deadline(half_cut, True).hour == 12
    assert flatten_deadline(half_cut, True).minute == 55
