from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from bstocks_v2.contracts.models import Direction, FillHorizon, TradeIntent


def test_trade_intent_accepts_decimal_and_rejects_float() -> None:
    now = datetime(2026, 11, 2, 14, 0, tzinfo=UTC)
    intent = TradeIntent(
        intent_id="intent-1",
        created_at_utc=now,
        data_asof_utc=now,
        symbol="AAPL",
        strategy_id="s1",
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
        rejection_reasons=[],
        data_feed="sip",
        data_freshness_ms=80,
    )
    assert intent.symbol == "AAPL"
    data = intent.model_dump()
    data["candidate_limit_prices"] = [10.5]
    with pytest.raises(ValidationError):
        TradeIntent.model_validate(data)
    data = intent.model_dump()
    data["symbol"] = "EQ_AAPL"
    with pytest.raises(ValidationError):
        TradeIntent.model_validate(data)
    data = intent.model_dump()
    data["min_viable_notional_usdc"] = Decimal("119")
    with pytest.raises(ValidationError):
        TradeIntent.model_validate(data)
