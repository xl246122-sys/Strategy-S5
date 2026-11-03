from datetime import datetime
from zoneinfo import ZoneInfo

from bstocks_v2.contracts.models import OrderType, Side, TradingSession
from bstocks_v2.sessions.calendar import SessionLabel, TradingCalendar
from bstocks_v2.sessions.eligibility import OrderRequest, SessionEligibility

ET = ZoneInfo("America/New_York")
CALENDAR = TradingCalendar.load()
GATE = SessionEligibility(CALENDAR)


def at(year: int, month: int, day: int, hour: int, minute: int) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=ET)


def row(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "symbol": "AAPL",
        "tradability": "BUY_SELL",
        "extendedSession": True,
        "overnightSupported": False,
        "fractionableEh": False,
        "stepSize": "1",
        "minQty": "1",
        "minNotional": "1",
        "multiplierUp": "1.20",
        "multiplierDown": "0.80",
    }
    payload.update(overrides)
    return payload


def ask(
    moment: datetime,
    *,
    order_type: OrderType = OrderType.LIMIT,
    trading_session: TradingSession | None = TradingSession.RTH,
    side: Side = Side.BUY,
    symbol: str = "AAPL",
) -> OrderRequest:
    return OrderRequest(symbol, moment, side, order_type, trading_session)


def test_rth_limit_is_allowed_and_half_day_changes_the_close() -> None:
    decision = GATE.decide(row(), ask(at(2024, 11, 1, 10, 0)))
    assert decision.allowed is True
    assert decision.label is SessionLabel.RTH
    assert decision.reason == "OK"
    assert CALENDAR.is_half_day(at(2024, 11, 29, 12, 0).date()) is True
    noon = GATE.decide(row(), ask(at(2024, 11, 29, 12, 44)))
    assert noon.allowed is True
    assert noon.label is SessionLabel.RTH
    after = GATE.decide(
        row(),
        ask(at(2024, 11, 29, 13, 30), trading_session=TradingSession.RTH),
    )
    assert after.allowed is False
    assert after.label is SessionLabel.POST
    assert after.reason == "SESSION_MISMATCH"


def test_closed_days_and_weekend_overnight_follow_the_calendar() -> None:
    holiday = GATE.decide(row(), ask(at(2025, 1, 9, 10, 0)))
    assert holiday.allowed is False
    assert holiday.label is SessionLabel.NON_SESSION
    assert holiday.reason == "MARKET_CLOSED"
    saturday = GATE.decide(row(), ask(at(2024, 11, 2, 10, 0)))
    assert saturday.reason == "MARKET_CLOSED"
    friday_night = GATE.decide(
        row(overnightSupported=True),
        ask(at(2024, 11, 1, 21, 0), trading_session=TradingSession.H24),
    )
    assert friday_night.allowed is True
    assert friday_night.label is SessionLabel.OVERNIGHT
    assert (
        GATE.decide(row(), ask(at(2024, 11, 2, 2, 0))).label is SessionLabel.OVERNIGHT
    )
    sunday_morning = GATE.decide(row(), ask(at(2024, 11, 3, 2, 0)))
    assert sunday_morning.label is SessionLabel.NON_SESSION
    assert sunday_morning.reason == "MARKET_CLOSED"


def test_unknown_eligibility_fails_closed() -> None:
    missing = dict(row())
    del missing["overnightSupported"]
    del missing["extendedSession"]
    decision = GATE.decide(missing, ask(at(2024, 11, 1, 10, 0)))
    assert decision.allowed is False
    assert decision.reason == "UNKNOWN_ELIGIBILITY"
    assert GATE.decide({"symbol": "AAPL"}, ask(at(2024, 11, 1, 10, 0))).reason == (
        "UNKNOWN_ELIGIBILITY"
    )
    floated = row(stepSize=1.5)
    assert GATE.decide(floated, ask(at(2024, 11, 1, 10, 0))).reason == "INVALID_FILTER"


def test_post_market_and_unsupported_24h_are_refused() -> None:
    post = at(2024, 11, 1, 16, 5)
    market = GATE.decide(
        row(),
        ask(post, order_type=OrderType.MARKET, trading_session=None, side=Side.SELL),
    )
    assert market.allowed is False
    assert market.label is SessionLabel.POST
    assert market.reason == "MARKET_AFTER_HOURS"
    extended = GATE.decide(row(), ask(post, trading_session=TradingSession.EXTENDED))
    assert extended.allowed is True
    assert extended.reason == "OK"
    as_rth = GATE.decide(
        row(extendedSession=False),
        ask(post, trading_session=TradingSession.EXTENDED),
    )
    assert as_rth.reason == "EXTENDED_UNSUPPORTED"
    night = at(2024, 11, 1, 20, 1)
    pretend = GATE.decide(row(), ask(night, trading_session=TradingSession.EXTENDED))
    assert pretend.allowed is False
    assert pretend.label is SessionLabel.OVERNIGHT
    assert pretend.reason == "SESSION_MISMATCH"
    unsupported = GATE.decide(
        row(overnightSupported=False),
        ask(night, trading_session=TradingSession.H24),
    )
    assert unsupported.reason == "OVERNIGHT_UNSUPPORTED"
    missing_night = dict(row())
    del missing_night["overnightSupported"]
    assert (
        GATE.decide(missing_night, ask(night, trading_session=TradingSession.H24)).reason
        == "UNKNOWN_ELIGIBILITY"
    )


def test_premarket_limit_needs_extended_and_buy_market_stays_blocked() -> None:
    pre = at(2024, 11, 1, 8, 0)
    assert GATE.decide(row(), ask(pre, trading_session=TradingSession.RTH)).reason == (
        "SESSION_MISMATCH"
    )
    assert (
        GATE.decide(row(), ask(pre, trading_session=TradingSession.EXTENDED)).allowed is True
    )
    rth_market = GATE.decide(
        row(),
        ask(
            at(2024, 11, 1, 10, 0),
            order_type=OrderType.MARKET,
            trading_session=None,
            side=Side.BUY,
        ),
    )
    assert rth_market.reason == "BUY_MARKET_FORBIDDEN"
    sell = GATE.decide(
        row(),
        ask(
            at(2024, 11, 1, 10, 0),
            order_type=OrderType.MARKET,
            trading_session=None,
            side=Side.SELL,
        ),
    )
    assert sell.allowed is True
    assert sell.label is SessionLabel.RTH
