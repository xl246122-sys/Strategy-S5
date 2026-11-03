from decimal import Decimal

import pytest

from bstocks_v2.contracts.errors import FeeTrapError, NormalizerError
from bstocks_v2.contracts.models import ExecutionEvent, FeeSource, OrderStatus, Side, TradingSession
from bstocks_v2.normalizer.binder import OrderIdBinder
from bstocks_v2.normalizer.payload import (
    EquityPayloadNormalizer,
    equity_after_buy_report,
    session_from_wire,
    session_to_wire,
)

CLIENT = "a" * 32
ORDER_ID = "b0b6dd9d-1111-2222-3333-444444444444"
TRAP = Decimal("928.7")


def partial_payload() -> dict[str, object]:
    return {
        "b": "EQ_AAPL",
        "S": "buy",
        "o": "limit",
        "s": "partially_filled",
        "x": "ORDER_UPDATE",
        "Q": "10",
        "p": "185.5",
        "fq": "5",
        "FN": "927.5",
        "tc": "1856.2",
        "q": "USD",
        "i": ORDER_ID,
        "n": "Regular",
    }


def test_partial_fill_does_not_book_tc_minus_fn_or_trip_hwm() -> None:
    binder = OrderIdBinder()
    binder.bind(CLIENT, ORDER_ID)
    order = EquityPayloadNormalizer().normalize_order_report(partial_payload(), binder)
    assert order.symbol == "AAPL"
    assert order.side is Side.BUY
    assert order.status is OrderStatus.PARTIALLY_FILLED
    assert order.is_terminal is False
    assert order.execution_event is ExecutionEvent.ORDER_UPDATE
    assert order.client_order_id == CLIENT
    assert order.session is TradingSession.RTH
    assert order.realized_commission == Decimal("0")
    assert order.realized_commission != TRAP
    assert order.provisional_reserved_fee == Decimal("1.2")
    assert order.fee_is_final is False
    assert order.fee_source is FeeSource.PROVISIONAL_WITHHELD
    equity, halt = equity_after_buy_report(
        Decimal("1600"), Decimal("1600"), order.realized_commission
    )
    assert equity == Decimal("1600")
    assert halt is False
    trap_equity, trap_halt = equity_after_buy_report(Decimal("1600"), Decimal("1600"), TRAP)
    assert trap_equity == Decimal("671.3")
    assert trap_halt is True


def test_rate_accrual_stays_far_from_the_trap() -> None:
    order = EquityPayloadNormalizer().normalize_order_report(
        partial_payload(),
        estimated_fee_rate=Decimal("0.001"),
    )
    assert order.realized_commission == Decimal("0.9275")
    assert order.realized_commission != TRAP
    equity, halt = equity_after_buy_report(
        Decimal("1600"), Decimal("1600"), order.realized_commission
    )
    assert halt is False
    assert equity > Decimal("1600") * Decimal("0.92")


def test_full_fill_uses_tc_minus_fn_only_as_initial_until_rest_fee() -> None:
    payload = partial_payload()
    payload["s"] = "filled"
    payload["x"] = "ORDER_TERMINAL"
    payload["fq"] = "10"
    payload["FN"] = "1855"
    payload["tc"] = "1856.2"
    initial = EquityPayloadNormalizer().normalize_order_report(payload)
    assert initial.realized_commission == Decimal("1.2")
    assert initial.fee_is_final is False
    assert initial.is_terminal is True
    final = EquityPayloadNormalizer().normalize_order_report(payload, rest_fee=Decimal("0.99"))
    assert final.realized_commission == Decimal("0.99")
    assert final.fee_source is FeeSource.REST_DETAIL
    assert final.fee_is_final is True


def test_filled_status_with_remaining_qty_still_refuses_the_trap() -> None:
    payload = partial_payload()
    payload["s"] = "filled"
    payload["fq"] = "9"
    order = EquityPayloadNormalizer().normalize_order_report(payload)
    assert order.realized_commission != TRAP


def test_wire_session_round_trip() -> None:
    assert session_from_wire("24 Hours Trading") is TradingSession.H24
    assert session_to_wire(TradingSession.H24) == "24 Hours Trading"
    assert session_from_wire("Extended Hours") is TradingSession.EXTENDED
    assert session_to_wire(TradingSession.RTH) == "Regular"


def test_order_report_rejects_embedded_client_order_id() -> None:
    payload = partial_payload()
    payload["clientOrderId"] = CLIENT
    with pytest.raises(NormalizerError):
        EquityPayloadNormalizer().normalize_order_report(payload)


def test_fee_trap_guard_is_raised_if_realized_equals_unfilled_trap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def explode(**_kwargs: object) -> tuple[Decimal, None, FeeSource, bool]:
        return TRAP, None, FeeSource.PROVISIONAL_WITHHELD, False

    monkeypatch.setattr("bstocks_v2.normalizer.payload._commission", explode)
    with pytest.raises(FeeTrapError):
        EquityPayloadNormalizer().normalize_order_report(partial_payload())
