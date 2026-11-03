import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from bstocks_v2.config.loader import load_runtime_config
from bstocks_v2.constitution.guard import (
    ConstitutionGuard,
    entry_window_open,
    flatten_deadline,
    live_trade_enabled,
)
from bstocks_v2.contracts.errors import (
    ConstitutionBypassError,
    EndpointForbiddenError,
    LiveTradingDisabledError,
    OrderRejected,
    ShortSellingForbiddenError,
)
from bstocks_v2.contracts.models import OrderDraft, OrderType, Side, TradingSession

ET = ZoneInfo("America/New_York")
MONDAY_OPEN = datetime(2026, 11, 2, 10, 0, tzinfo=ET)
CLIENT = "b" * 32


def buy(**overrides: object) -> OrderDraft:
    fields: dict[str, object] = {
        "symbol": "AAPL",
        "side": Side.BUY,
        "order_type": OrderType.LIMIT,
        "client_order_id": CLIENT,
        "quantity": Decimal("2"),
        "price": Decimal("100.00"),
        "trading_session": TradingSession.RTH,
        "quote_asset": "USDC",
        "wallet_type": "CARD",
        "tokenize": False,
        "is_opening": True,
        "gross_take_profit_usdc": Decimal("10"),
        "round_trip_friction_usdc": Decimal("1"),
    }
    fields.update(overrides)
    return OrderDraft.model_validate(fields)


def check(draft: OrderDraft, **overrides: object) -> None:
    args: dict[str, object] = {
        "now_et": MONDAY_OPEN,
        "is_half_day": False,
        "open_positions": 0,
        "pending_buys": 0,
        "equity": Decimal("1600"),
        "hwm": Decimal("1600"),
        "entries_halted": False,
    }
    args.update(overrides)
    ConstitutionGuard().check_order(draft, **args)  # type: ignore[arg-type]


def test_live_flag_stays_false_and_place_is_blocked(tmp_path: Path) -> None:
    path = tmp_path / "hostile.json"
    path.write_text(
        json.dumps({"live_trade_enabled": True, "max_open_positions": 50}), encoding="utf-8"
    )
    with pytest.raises(ConstitutionBypassError):
        load_runtime_config(path)
    assert live_trade_enabled() is False
    guard = ConstitutionGuard()
    with pytest.raises(LiveTradingDisabledError):
        guard.check_endpoint("POST", "/sapi/v1/equity/order/place")
    with pytest.raises(LiveTradingDisabledError):
        guard.check_endpoint("POST", "/sapi/v1/equity/order/cancel")


def test_endpoint_whitelist() -> None:
    guard = ConstitutionGuard()
    guard.check_endpoint("GET", "https://api.binance.com/sapi/v1/equity/market/exchangeInfo")
    guard.check_endpoint("POST", "/sapi/v1/equity/listenKey")
    guard.check_endpoint("POST", "/sapi/v1/asset/get-funding-asset")
    guard.check_endpoint("GET", "https://data.alpaca.markets/v2/stocks/bars")
    with pytest.raises(EndpointForbiddenError):
        guard.check_endpoint("POST", "/sapi/v1/equity/tokenized/mint")
    with pytest.raises(EndpointForbiddenError):
        guard.check_endpoint("POST", "/sapi/v1/equity/tokenized/redeem")
    with pytest.raises(EndpointForbiddenError):
        guard.check_endpoint("GET", "/fapi/v1/ticker/price")
    with pytest.raises(EndpointForbiddenError):
        guard.check_endpoint("POST", "/api/v3/order")
    with pytest.raises(EndpointForbiddenError):
        guard.check_endpoint("POST", "https://paper-api.alpaca.markets/v2/orders")


def test_valid_opening_buy_passes() -> None:
    check(buy())


def test_price_with_three_decimals_is_rejected() -> None:
    with pytest.raises(OrderRejected) as caught:
        ConstitutionGuard().check_price(Decimal("10.999"))
    assert caught.value.reason == "PRICE_PRECISION"
    with pytest.raises(ValidationError):
        buy(price=Decimal("10.999"))
    with pytest.raises((ValidationError, OrderRejected)):
        buy(price=10.25)


def test_bad_client_order_id_and_field_matrix() -> None:
    with pytest.raises(OrderRejected) as caught:
        check(buy(client_order_id="x" * 31))
    assert caught.value.reason == "CLIENT_ORDER_ID"
    with pytest.raises(OrderRejected) as caught:
        check(buy(client_order_id="bad id" + ("y" * 30)))
    assert caught.value.reason == "CLIENT_ORDER_ID"
    with pytest.raises(OrderRejected) as caught:
        check(buy(order_type=OrderType.MARKET, price=None, trading_session=None))
    assert caught.value.reason == "BUY_MARKET_FORBIDDEN"
    with pytest.raises(OrderRejected) as caught:
        check(buy(notional=Decimal("200")))
    assert caught.value.reason == "FIELD_MATRIX"
    with pytest.raises(OrderRejected) as caught:
        check(buy(tokenize=True))
    assert caught.value.reason == "TOKENIZE"
    sell = OrderDraft(
        symbol="AAPL",
        side=Side.SELL,
        order_type=OrderType.MARKET,
        client_order_id=CLIENT,
        quantity=Decimal("1"),
        price=Decimal("100.00"),
        confirmed_long_qty=Decimal("1"),
        is_opening=False,
    )
    with pytest.raises(OrderRejected) as caught:
        check(sell)
    assert caught.value.reason == "FIELD_MATRIX"


def test_short_sell_is_forbidden() -> None:
    sell = OrderDraft(
        symbol="AAPL",
        side=Side.SELL,
        order_type=OrderType.MARKET,
        client_order_id=CLIENT,
        quantity=Decimal("1"),
        confirmed_long_qty=Decimal("0"),
        is_opening=False,
    )
    with pytest.raises(ShortSellingForbiddenError):
        check(sell)


def test_session_notional_slots_hwm_and_friction() -> None:
    with pytest.raises(OrderRejected) as caught:
        check(buy(), now_et=datetime(2026, 11, 1, 10, 0, tzinfo=ET))
    assert caught.value.reason == "NON_RTH"
    with pytest.raises(OrderRejected) as caught:
        check(buy(), now_et=datetime(2026, 11, 2, 15, 45, tzinfo=ET))
    assert caught.value.reason == "NON_RTH"
    with pytest.raises(OrderRejected) as caught:
        check(buy(), now_et=datetime(2026, 11, 2, 9, 29, 59, tzinfo=ET))
    assert caught.value.reason == "NON_RTH"
    assert entry_window_open(datetime(2026, 11, 2, 12, 44, 59, tzinfo=ET), True)
    assert not entry_window_open(datetime(2026, 11, 2, 12, 45, tzinfo=ET), True)
    deadline = flatten_deadline(datetime(2026, 11, 2, 9, 0, tzinfo=ET), False)
    assert (deadline.hour, deadline.minute, deadline.second) == (15, 55, 0)
    half = flatten_deadline(datetime(2026, 11, 2, 9, 0, tzinfo=ET), True)
    assert (half.hour, half.minute, half.second) == (12, 55, 0)
    with pytest.raises(OrderRejected) as caught:
        check(buy(quantity=Decimal("1"), price=Decimal("100.00")))
    assert caught.value.reason == "NOTIONAL_TOO_SMALL"
    with pytest.raises(OrderRejected) as caught:
        check(buy(), open_positions=4, pending_buys=1)
    assert caught.value.reason == "MAX_POSITIONS"
    with pytest.raises(OrderRejected) as caught:
        check(buy(), equity=Decimal("1472"), hwm=Decimal("1600"))
    assert caught.value.reason == "HWM_LOCK"
    check(buy(), equity=Decimal("1472.01"), hwm=Decimal("1600"))
    with pytest.raises(OrderRejected) as caught:
        check(buy(round_trip_friction_usdc=Decimal("2.01")))
    assert caught.value.reason == "FRICTION"
    check(buy(round_trip_friction_usdc=Decimal("2")))
