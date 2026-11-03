"""S5-P4 mock execution. None of these fills is an exchange print."""

from collections.abc import Iterator
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from bstocks_v2.constitution.guard import (
    SESSION_POLICY_GLOBAL,
    SESSION_POLICY_S5_RESEARCH,
    ConstitutionGuard,
)
from bstocks_v2.contracts.errors import (
    FeeTrapError,
    LiveTradingDisabledError,
    OrderRejected,
)
from bstocks_v2.contracts.models import OrderDraft, OrderType, Side, TradingSession
from bstocks_v2.guardian.machine import BuyPlan, Guardian
from bstocks_v2.guardian.spec import FLAT_RECONCILED
from bstocks_v2.portfolio_risk.authority import EntryRequest, PortfolioRisk
from bstocks_v2.research_backtest.tape import ClosedBar
from bstocks_v2.strategies.s5.book import EntryOutcome, S5ResearchBook
from bstocks_v2.strategies.s5.execution import ResearchQuote, checked_price, research_client_id
from bstocks_v2.strategies.s5.policy import S5GuardianPolicy, research_policy
from bstocks_v2.strategies.s5.spec import ARM_A, ARM_B, ARM_C

ET = ZoneInfo("America/New_York")
BookPair = tuple[PortfolioRisk, S5ResearchBook]
ROOT = Path(__file__).resolve().parents[2]
GUARD = ROOT / "src" / "bstocks_v2" / "guardian" / "machine.py"


def _at(day: str, hour: int, minute: int) -> datetime:
    year, month, dom = (int(part) for part in day.split("-"))
    return datetime(year, month, dom, hour, minute, tzinfo=ET)


def _quote(
    *,
    bid: str | None = "100",
    ask: str | None = "100.10",
    size: str | None = "1000",
    down: str = "1",
    up: str = "500",
) -> ResearchQuote:
    return ResearchQuote(
        None if bid is None else Decimal(bid),
        None if ask is None else Decimal(ask),
        None if size is None else Decimal(size),
        None if size is None else Decimal(size),
        Decimal(down),
        Decimal(up),
    )


def _row(symbol: str, *, extended: bool = True, overnight: bool = False) -> dict[str, object]:
    return {
        "symbol": symbol,
        "tradability": "BUY_SELL",
        "extendedSession": extended,
        "overnightSupported": overnight,
        "fractionableEh": False,
        "stepSize": "1",
        "minQty": "1",
        "minNotional": "1",
        "multiplierUp": "500",
        "multiplierDown": "1",
    }


def _bar_ohlc(
    symbol: str,
    end: datetime,
    *,
    open_: str,
    high: str,
    low: str,
    close: str,
    volume: str = "1000",
) -> ClosedBar:
    return ClosedBar(
        symbol,
        end - timedelta(minutes=5),
        end,
        Decimal(open_),
        Decimal(high),
        Decimal(low),
        Decimal(close),
        Decimal(volume),
        "5m",
    )


def _open(tmp_path: Path) -> BookPair:
    risk = PortfolioRisk.open(tmp_path / "risk")
    book = S5GuardianPolicy(research_policy()).open_research(tmp_path / "s5", risk)
    return risk, book


def _adopt(guardian: Guardian) -> None:
    guardian.adopt_buy(
        BuyPlan("AAPL", Decimal("1"), Decimal("100"), Decimal("90"), Decimal("120"))
    )


def _tick(
    book: S5ResearchBook,
    when: datetime,
    symbol: str = "AIQ",
    *,
    overnight: bool = False,
    quote: ResearchQuote | None = None,
    status: str = "TRADING",
    bar: ClosedBar | None = None,
) -> None:
    book.on_clock(
        when,
        _row(symbol, overnight=overnight),
        _quote() if quote is None else quote,
        trading_status=status,
        bar=bar,
    )


def _fill_bar(symbol: str, end: datetime) -> ClosedBar:
    return _bar_ohlc(symbol, end, open_="100", high="100", low="99.98", close="100")


def _enter(
    book: S5ResearchBook,
    symbol: str,
    arm: str,
    when: datetime,
    *,
    tif: str = "DAY",
    bar: ClosedBar | None = None,
    overnight: bool = False,
    news: str = "CLEAR",
    regime: str = "TREND_UP",
    quote: ResearchQuote | None = None,
    row: dict[str, object] | None = None,
) -> EntryOutcome:
    return book.enter(
        symbol,
        arm,
        when,
        _row(symbol, overnight=overnight) if row is None else row,
        _quote() if quote is None else quote,
        news=news,
        regime=regime,
        tif=tif,
        bar=_fill_bar(symbol, when) if bar is None else bar,
    )


@pytest.fixture
def pair(tmp_path: Path) -> Iterator[BookPair]:
    risk, book = _open(tmp_path)
    yield risk, book
    book.close()
    risk.close()


def test_fault_global_window_still_rejects_the_last_minutes(tmp_path: Path) -> None:
    risk = PortfolioRisk.open(tmp_path / "risk")
    try:
        late = _at("2024-11-01", 15, 50)
        direct = risk.approve(
            EntryRequest(
                "AIQ",
                research_client_id("AIQ", "buy", 0),
                Decimal("100"),
                Decimal("2"),
                Decimal("98.50"),
                Decimal("101"),
                late,
            )
        )
        assert direct.accepted is False and direct.reason == "NON_RTH"
        half = _at("2024-11-29", 12, 50)
        half_direct = risk.approve(
            EntryRequest(
                "BOTZ",
                research_client_id("BOTZ", "buy", 0),
                Decimal("100"),
                Decimal("2"),
                Decimal("98.50"),
                Decimal("101"),
                half,
                is_half_day=True,
            )
        )
        assert half_direct.accepted is False and half_direct.reason == "NON_RTH"
        post = _at("2024-11-01", 16, 10)
        post_direct = risk.approve(
            EntryRequest(
                "CIBR",
                research_client_id("CIBR", "buy", 0),
                Decimal("100"),
                Decimal("2"),
                Decimal("98.50"),
                Decimal("101"),
                post,
                trading_session=TradingSession.EXTENDED,
            )
        )
        assert post_direct.accepted is False and post_direct.reason == "NON_RTH"
        guard = ConstitutionGuard()
        with pytest.raises(OrderRejected) as naked:
            guard.check_order(
                _draft("AIQ", late),
                now_et=late,
                is_half_day=False,
                open_positions=0,
                pending_buys=0,
                equity=Decimal("1600"),
                hwm=Decimal("1600"),
                entries_halted=False,
                session_policy=SESSION_POLICY_S5_RESEARCH,
            )
        assert naked.value.reason == "ENTRY_POLICY"
        with pytest.raises(OrderRejected) as friction:
            guard.check_order(
                _draft("AIQ", late, research=True, gross="0.10", friction="1"),
                now_et=late,
                is_half_day=False,
                open_positions=0,
                pending_buys=0,
                equity=Decimal("1600"),
                hwm=Decimal("1600"),
                entries_halted=False,
                session_policy=SESSION_POLICY_S5_RESEARCH,
            )
        assert friction.value.reason == "FRICTION"
        with pytest.raises(OrderRejected) as blocked:
            guard.check_order(
                _draft("AIQ", late),
                now_et=late,
                is_half_day=False,
                open_positions=0,
                pending_buys=0,
                equity=Decimal("1600"),
                hwm=Decimal("1600"),
                entries_halted=False,
                session_policy="WIDEN",
            )
        assert blocked.value.reason == "ENTRY_POLICY"
        assert SESSION_POLICY_GLOBAL == "GLOBAL"
    finally:
        risk.close()
    risk = PortfolioRisk.open(tmp_path / "risk2")
    book = S5GuardianPolicy(research_policy()).open_research(tmp_path / "s5", risk)
    try:
        late = _at("2024-11-01", 15, 50)
        opened = _enter(book, "AIQ", ARM_A, late, bar=_fill_bar("AIQ", late))
        assert opened.accepted is True
        assert opened.trading_session == "RTH"
        half_at = _at("2024-11-29", 12, 50)
        half_open = _enter(book, "BOTZ", ARM_A, half_at, bar=_fill_bar("BOTZ", half_at))
        assert half_open.accepted is True
        posted_at = _at("2024-11-01", 16, 10)
        posted = _enter(book, "CIBR", ARM_B, posted_at, bar=_fill_bar("CIBR", posted_at))
        assert posted.accepted is True and posted.trading_session == "EXTENDED"
    finally:
        book.close()
        risk.close()


def test_fault_1555_flattens_s1_and_keeps_the_s5_limit(tmp_path: Path) -> None:
    guardian = Guardian.open(tmp_path / "guardian.sqlite")
    risk, book = _open(tmp_path)
    try:
        _adopt(guardian)
        guardian.on_buy_report("AAPL", status="FILLED", filled_qty=Decimal("1"))
        opened = _enter(book, "AIQ", ARM_A, _at("2024-11-01", 15, 40))
        assert opened.accepted is True and opened.state == "TP_PENDING"
        moment = _at("2024-11-01", 15, 55)
        guardian.on_clock(
            moment,
            is_half_day=False,
            bid=Decimal("100"),
            trading_status="TRADING",
            multiplier_down=Decimal("1"),
            multiplier_up=Decimal("500"),
        )
        book.on_clock(moment, _row("AIQ"), _quote(), trading_status="TRADING")
        assert guardian.get("AAPL").state == FLAT_RECONCILED
        assert book.get("AIQ").state == "TP_PENDING"
        assert book.market_orders() == []
        assert book.halted is False
        assert "S5GuardianPolicy" not in GUARD.read_text(encoding="utf-8")
        assert "strategies.s5" not in GUARD.read_text(encoding="utf-8")
    finally:
        book.close()
        risk.close()
        guardian.close()


def test_fault_half_day_1255_flattens_s1_only(tmp_path: Path) -> None:
    guardian = Guardian.open(tmp_path / "guardian.sqlite")
    risk, book = _open(tmp_path)
    try:
        _adopt(guardian)
        guardian.on_buy_report("AAPL", status="FILLED", filled_qty=Decimal("1"))
        opened = _enter(book, "AIQ", ARM_A, _at("2024-11-29", 12, 40))
        assert opened.accepted is True
        moment = _at("2024-11-29", 12, 55)
        guardian.on_clock(
            moment,
            is_half_day=True,
            bid=Decimal("100"),
            trading_status="TRADING",
            multiplier_down=Decimal("1"),
            multiplier_up=Decimal("500"),
        )
        book.on_clock(moment, _row("AIQ"), _quote(), trading_status="TRADING")
        assert guardian.get("AAPL").state == FLAT_RECONCILED
        assert book.get("AIQ").state == "TP_PENDING"
        assert book.market_orders() == []
    finally:
        book.close()
        risk.close()
        guardian.close()


def test_fault_day_and_gtc_do_not_fill_across_1600(pair: BookPair) -> None:
    _risk, book = pair
    when = _at("2024-11-01", 15, 50)
    day = _enter(book, "AIQ", ARM_A, when, tif="DAY", bar=_flat("AIQ", when))
    gtc = _enter(book, "BOTZ", ARM_A, when, tif="GTC", bar=_flat("BOTZ", when))
    assert day.accepted and gtc.accepted
    assert day.filled_qty == 0 and gtc.filled_qty == 0
    book.on_clock(_at("2024-11-01", 16, 0), _row("AIQ"), _quote(), trading_status="TRADING")
    assert book.get("AIQ").state == "FLAT_RECONCILED"
    assert book.get("BOTZ").state == "FLAT_RECONCILED"
    assert book.get("AIQ").last_reason == "TIF_UNVERIFIED"
    assert book.get("BOTZ").last_reason == "TIF_UNVERIFIED"
    assert all(order.filled_qty == 0 for order in book.orders())
    assert {order.status for order in book.orders()} == {"EXPIRED_UNVERIFIED"}
    assert _risk.snapshot().slots == 0


def test_fault_mixed_bar_is_not_a_fill(pair: BookPair) -> None:
    _risk, book = pair
    when = _at("2024-11-01", 15, 50)
    opened = _enter(book, "AIQ", ARM_A, when, bar=_flat("AIQ", when))
    assert opened.filled_qty == 0
    end = _at("2024-11-01", 16, 0)
    mixed = _bar_ohlc("AIQ", end, open_="100", high="100", low="100", close="100")
    _tick(book, end, bar=mixed)
    assert book.get("AIQ").filled_qty == 0
    assert book.get("AIQ").last_reason == "MIXED_BAR"
    assert book.market_orders() == []


def test_fault_1959_extended_is_not_treated_as_24h(pair: BookPair) -> None:
    _risk, book = pair
    opened = _enter(book, "AIQ", ARM_B, _at("2024-11-01", 16, 10))
    assert opened.trading_session == "EXTENDED"
    _tick(book, _at("2024-11-01", 19, 59))
    sessions = {
        order.trading_session
        for order in book.orders()
        if order.side == "SELL" and order.status == "NEW"
    }
    assert sessions == {"EXTENDED"}
    assert all(order.trading_session != "24H" for order in book.orders())
    assert book.market_orders() == []


def test_fault_2000_without_overnight_halts(pair: BookPair) -> None:
    _risk, book = pair
    opened = _enter(book, "AIQ", ARM_C, _at("2024-11-01", 16, 10), overnight=True)
    assert opened.accepted is True
    _tick(book, _at("2024-11-01", 20, 1), overnight=False)
    assert book.get("AIQ").state == "EXIT_REQUIRED"
    assert book.halted is True
    assert all(order.trading_session != "24H" for order in book.orders())
    assert book.market_orders() == []
    assert any(code == "OVERNIGHT_UNSUPPORTED" for _level, code, _symbol in book.alerts())
    blocked = _enter(book, "BOTZ", ARM_A, _at("2024-11-04", 15, 40))
    assert blocked.accepted is False and blocked.reason == "HALT_NEW_ENTRIES"


def test_fault_2000_with_overnight_switches_to_a_24h_limit(
    pair: BookPair,
) -> None:
    _risk, book = pair
    _enter(book, "AIQ", ARM_C, _at("2024-11-01", 16, 10), overnight=True)
    _tick(book, _at("2024-11-01", 20, 1), overnight=True)
    expired = [order for order in book.orders() if order.status == "EXPIRED_UNVERIFIED"]
    live = [order for order in book.orders() if order.status == "NEW"]
    assert expired
    assert all(order.filled_qty == 0 and order.trading_session == "EXTENDED" for order in expired)
    assert len(live) == 1 and live[0].trading_session == "24H" and live[0].order_type == "LIMIT"
    assert book.get("AIQ").state == "TP_PENDING"
    assert book.halted is False
    assert book.market_orders() == []


def test_fault_touch_without_penetration_stays_unfilled(
    pair: BookPair,
) -> None:
    _risk, book = pair
    when = _at("2024-11-01", 15, 40)
    touched = _bar_ohlc("AIQ", when, open_="100", high="100", low="100", close="100")
    opened = _enter(book, "AIQ", ARM_A, when, bar=touched)
    assert opened.accepted is True
    assert opened.filled_qty == 0
    assert opened.match_reason == "TOUCH_NO_PENETRATION"
    assert book.get("AIQ").state == "BUY_PENDING"


def test_fault_whole_share_fallback_and_price_precision(
    pair: BookPair,
) -> None:
    _risk, book = pair
    opened = _enter(book, "AIQ", ARM_A, _at("2024-11-01", 15, 40))
    assert opened.quantity == Decimal("2")
    assert opened.filled_qty == Decimal("2")
    assert "486442" in opened.adjustments
    with pytest.raises(OrderRejected) as bad:
        checked_price(Decimal("100.001"))
    assert bad.value.reason == "PRICE_PRECISION"
    caged = _enter(
        book,
        "BOTZ",
        ARM_A,
        _at("2024-11-01", 15, 40),
        quote=_quote(up="50"),
    )
    assert caged.accepted is False and caged.reason == "PRICE_CAGE"
    assert research_client_id("AIQ", "buy", 0) == research_client_id("AIQ", "buy", 0)


def test_fault_news_regime_spread_and_unknown_eligibility(
    pair: BookPair,
) -> None:
    risk, book = pair
    when = _at("2024-11-01", 15, 40)
    assert _enter(book, "AIQ", ARM_A, when, news="UNKNOWN").reason == "NEWS_UNKNOWN"
    assert _enter(book, "AIQ", ARM_A, when, regime="HIGH_VOL_PANIC").reason == "REGIME_BLOCKED"
    assert _enter(book, "AIQ", ARM_A, when, regime="UNKNOWN").reason == "REGIME_BLOCKED"
    assert _enter(book, "AIQ", ARM_A, when, regime="").reason == "REGIME_NOT_ATTACHED"
    assert _enter(book, "AIQ", ARM_A, when, quote=_quote(bid=None)).reason == "SPREAD_UNKNOWN"
    assert _enter(book, "AIQ", ARM_A, when, quote=_quote(ask="101")).reason == "SPREAD_TOO_WIDE"
    assert _enter(book, "AIQ", ARM_A, when, quote=_quote(size=None)).reason == "DEPTH_UNKNOWN"
    assert _enter(book, "AAPL", ARM_A, when).reason == "NOT_IN_UNIVERSE"
    thin = dict(_row("AIQ"))
    del thin["overnightSupported"]
    assert _enter(book, "AIQ", ARM_A, when, row=thin).reason == "UNKNOWN_ELIGIBILITY"
    assert risk.snapshot().slots == 0


def test_fault_exchange_reject_is_not_a_fill(pair: BookPair) -> None:
    risk, book = pair
    when = _at("2024-11-01", 15, 40)
    opened = _enter(book, "AIQ", ARM_A, when, bar=_flat("AIQ", when))
    assert opened.client_order_id is not None
    book.apply_status(opened.client_order_id, "REJECTED")
    assert book.get("AIQ").state == "FLAT_RECONCILED"
    assert book.get("AIQ").filled_qty == 0
    assert risk.snapshot().slots == 0
    assert book.market_orders() == []


def test_fault_halt_and_missing_bid_require_exit(pair: BookPair) -> None:
    _risk, book = pair
    _enter(book, "AIQ", ARM_A, _at("2024-11-01", 15, 40))
    book.on_clock(_at("2024-11-01", 15, 50), _row("AIQ"), _quote(), trading_status="TRADING_HALTED")
    assert book.get("AIQ").state == "EXIT_REQUIRED"
    assert book.get("AIQ").alert == "TRADING_HALTED"
    assert book.halted is True
    assert book.market_orders() == []
    assert any(level == "CRITICAL" for level, _code, _symbol in book.alerts())


def test_fault_no_bid_while_open_requires_exit(tmp_path: Path) -> None:
    risk, book = _open(tmp_path)
    try:
        _enter(book, "AIQ", ARM_A, _at("2024-11-01", 15, 40))
        _tick(book, _at("2024-11-01", 15, 50), quote=_quote(bid=None))
        assert book.get("AIQ").state == "EXIT_REQUIRED"
        assert book.get("AIQ").alert == "NO_BID"
        assert book.market_orders() == []
        assert book.halted is True
    finally:
        book.close()
        risk.close()


def test_fault_partial_fill_does_not_book_tc_minus_fn(
    pair: BookPair,
) -> None:
    risk, book = pair
    when = _at("2024-11-01", 15, 40)
    partial = _bar_ohlc("AIQ", when, open_="100", high="100", low="99.98", close="100", volume="1")
    opened = _enter(book, "AIQ", ARM_A, when, bar=partial)
    assert opened.filled_qty == Decimal("1")
    assert opened.state == "PARTIAL_POSITION"
    assert opened.client_order_id is not None
    book.note_cost(opened.client_order_id, Decimal("150"))
    with pytest.raises(FeeTrapError):
        book.declare_fee(opened.client_order_id, Decimal("1"))
    assert book.realized_fee(opened.client_order_id) is None
    view = risk.snapshot()
    assert view.orders[0].rest_fee is None
    assert view.orders[0].status == "PARTIALLY_FILLED"
    assert view.orders[0].total_cost_raw is not None
    assert view.orders[0].total_cost_raw - view.orders[0].filled_notional == Decimal("50")


def test_fault_486444_flat_without_a_second_sell(pair: BookPair) -> None:
    risk, book = pair
    when = _at("2024-11-01", 15, 40)
    filled = _enter(book, "AIQ", ARM_A, when)
    unconfirmed = _enter(book, "BOTZ", ARM_A, when)
    assert filled.sell_client_order_id is not None
    assert unconfirmed.sell_client_order_id is not None
    book.apply_status(filled.sell_client_order_id, "FILLED", filled_qty=Decimal("2"), code=486444)
    state = book.request_stop("AIQ", when, _row("AIQ"), _quote())
    assert state == "FLAT_RECONCILED"
    assert book.get("AIQ").sold_qty == Decimal("2")
    aiq_sells = [order for order in book.orders() if order.symbol == "AIQ" and order.side == "SELL"]
    assert len(aiq_sells) == 1
    again = book.request_stop("AIQ", when, _row("AIQ"), _quote())
    assert again == "FLAT_RECONCILED"
    book.apply_status(unconfirmed.sell_client_order_id, "NEW", code=486444)
    pending = book.request_stop("BOTZ", when, _row("BOTZ"), _quote())
    assert pending == "EXIT_REQUIRED"
    assert book.get("BOTZ").sold_qty == 0
    assert book.get("BOTZ").alert == "CANCEL_486444_UNCONFIRMED"
    sells = [order for order in book.orders() if order.symbol == "BOTZ" and order.side == "SELL"]
    assert len(sells) == 1 and sells[0].status == "NEW"
    left = [item.symbol for item in risk.snapshot().positions]
    assert "AIQ" not in left


def test_fault_486446_does_not_double_sell(pair: BookPair) -> None:
    _risk, book = pair
    when = _at("2024-11-01", 15, 40)
    opened = _enter(book, "AIQ", ARM_A, when)
    assert opened.sell_client_order_id is not None
    book.apply_status(opened.sell_client_order_id, "NEW", shares_frozen=True, code=486446)
    first = book.request_stop("AIQ", when, _row("AIQ"), _quote())
    second = book.request_stop("AIQ", when, _row("AIQ"), _quote())
    assert first == second == "CANCEL_PENDING_FOR_SL"
    assert book.get("AIQ").sell_client_order_id == opened.sell_client_order_id
    assert book.get("AIQ").sold_qty == 0
    sells = [order for order in book.orders() if order.side == "SELL"]
    assert len(sells) == 1


def test_fault_short_and_market_sell_are_refused(pair: BookPair) -> None:
    _risk, book = pair
    when = _at("2024-11-01", 15, 40)
    opened = _enter(book, "AIQ", ARM_A, when)
    before = len(book.orders())
    with pytest.raises(OrderRejected) as short:
        book.try_sell("AIQ", Decimal("3"), when)
    assert short.value.reason == "SHORT"
    assert len(book.orders()) == before
    assert book.request_market_sell("AIQ", when, _row("AIQ")) == "S5_LIMIT_ONLY"
    post = _at("2024-11-01", 16, 10)
    assert book.request_market_sell("AIQ", post, _row("AIQ")) == "MARKET_AFTER_HOURS"
    assert book.market_orders() == []
    assert book.get("AIQ").sold_qty == 0
    assert opened.filled_qty == Decimal("2")
    with pytest.raises(LiveTradingDisabledError):
        book.live_place()
    with pytest.raises(LiveTradingDisabledError):
        book.live_cancel()


def test_fault_stop_retry_keeps_one_exit_id(pair: BookPair) -> None:
    _risk, book = pair
    when = _at("2024-11-01", 15, 40)
    _enter(book, "AIQ", ARM_A, when)
    later = _at("2024-11-01", 15, 50)
    touch = _bar_ohlc("AIQ", later, open_="98.50", high="98.50", low="98.50", close="98.50")
    first = book.request_stop("AIQ", later, _row("AIQ"), _quote(), bar=touch)
    exit_id = book.get("AIQ").sell_client_order_id
    second = book.request_stop("AIQ", later, _row("AIQ"), _quote(), bar=touch)
    assert first == second == "EXIT_REQUIRED"
    assert book.get("AIQ").sell_client_order_id == exit_id
    live = [order for order in book.orders() if order.side == "SELL" and order.status == "NEW"]
    assert len(live) == 1 and live[0].client_order_id == exit_id
    assert book.get("AIQ").sold_qty == 0


def test_fault_same_bar_stop_beats_the_target(pair: BookPair) -> None:
    _risk, book = pair
    _enter(book, "AIQ", ARM_A, _at("2024-11-01", 15, 40))
    later = _at("2024-11-01", 15, 50)
    wide = _bar_ohlc("AIQ", later, open_="100", high="102", low="98", close="100")
    book.on_clock(later, _row("AIQ"), _quote(), trading_status="TRADING", bar=wide)
    assert book.get("AIQ").state == "FLAT_RECONCILED"
    filled = [order for order in book.orders() if order.status == "FILLED" and order.side == "SELL"]
    assert len(filled) == 1
    assert filled[0].price == book.get("AIQ").stop_price
    assert filled[0].price != book.get("AIQ").take_profit_price


def test_fault_gap_through_the_stop_is_not_a_market_fill(
    pair: BookPair,
) -> None:
    risk, book = pair
    _enter(book, "AIQ", ARM_A, _at("2024-11-01", 15, 40))
    later = _at("2024-11-01", 15, 50)
    gap = _bar_ohlc("AIQ", later, open_="90", high="91", low="90", close="91")
    book.on_clock(later, _row("AIQ"), _quote(), trading_status="TRADING", bar=gap)
    assert book.get("AIQ").state == "EXIT_REQUIRED"
    assert book.get("AIQ").alert == "STOP_UNFILLED"
    assert book.get("AIQ").sold_qty == 0
    assert book.market_orders() == []
    assert risk.snapshot().positions[0].quantity == Decimal("2")


def test_fault_restart_restores_exit_required(tmp_path: Path) -> None:
    risk, book = _open(tmp_path)
    _enter(book, "AIQ", ARM_A, _at("2024-11-01", 15, 40))
    _tick(book, _at("2024-11-01", 15, 50), quote=_quote(bid=None))
    assert book.halted is True
    book.close()
    risk.close()
    risk = PortfolioRisk.open(tmp_path / "risk")
    book = S5ResearchBook.open(tmp_path / "s5", risk)
    try:
        assert book.halted is True
        assert book.get("AIQ").state == "EXIT_REQUIRED"
        assert book.get("AIQ").alert == "NO_BID"
        blocked = _enter(book, "BOTZ", ARM_A, _at("2024-11-04", 15, 40))
        assert blocked.reason == "HALT_NEW_ENTRIES"
        with pytest.raises(LiveTradingDisabledError):
            book.live_place()
    finally:
        book.close()
        risk.close()


def test_fault_weekend_halts_without_a_market_sell(pair: BookPair) -> None:
    _risk, book = pair
    _enter(book, "AIQ", ARM_A, _at("2024-11-01", 15, 40))
    book.on_clock(_at("2024-11-02", 10, 0), _row("AIQ"), _quote(), trading_status="TRADING")
    assert book.get("AIQ").state == "EXIT_REQUIRED"
    assert book.get("AIQ").alert == "MARKET_CLOSED"
    assert book.halted is True
    assert book.market_orders() == []
    assert book.get("AIQ").sold_qty == 0


def test_fault_slots_hwm_and_pdt_still_bind(tmp_path: Path) -> None:
    names = ("AIQ", "BOTZ", "CIBR", "FDN", "HACK")
    risk, book = _open(tmp_path)
    try:
        when = _at("2024-11-01", 15, 40)
        for symbol in names:
            opened = _enter(book, symbol, ARM_A, when)
            assert opened.accepted is True, opened.reason
        sixth = _enter(book, "IAI", ARM_A, when)
        assert sixth.accepted is False and sixth.reason == "MAX_POSITIONS"
        assert risk.snapshot().slots == 5
    finally:
        book.close()
        risk.close()
    risk, book = _open(tmp_path / "hwm")
    try:
        when = _at("2024-11-01", 15, 40)
        assert _enter(book, "AIQ", ARM_A, when).accepted is True
        locked = risk.mark({"AIQ": Decimal("30")}, when)
        assert locked.halt_new_entries is True
        assert locked.halt_reason == "HWM_LOCK"
        blocked = _enter(book, "BOTZ", ARM_A, when)
        assert blocked.accepted is False
        assert blocked.reason == "HALT_NEW_ENTRIES"
    finally:
        book.close()
        risk.close()
    risk, book = _open(tmp_path / "pdt")
    try:
        when = _at("2024-11-01", 15, 40)
        risk.on_broker_code(486440, when)
        blocked = _enter(book, "AIQ", ARM_A, when)
        assert blocked.accepted is False and blocked.reason == "486440"
    finally:
        book.close()
        risk.close()


def _flat(symbol: str, end: datetime) -> ClosedBar:
    return _bar_ohlc(symbol, end, open_="100", high="100", low="100", close="100")


def _draft(
    symbol: str,
    now: datetime,
    *,
    research: bool = False,
    gross: str = "2",
    friction: str = "0.24",
) -> OrderDraft:
    del now
    return OrderDraft(
        symbol=symbol,
        side=Side.BUY,
        order_type=OrderType.LIMIT,
        client_order_id=research_client_id(symbol, "probe", 3),
        quantity=Decimal("2"),
        price=Decimal("100"),
        trading_session=TradingSession.RTH,
        quote_asset="USDC",
        wallet_type="CARD",
        tokenize=False,
        is_opening=True,
        confirmed_long_qty=Decimal(0),
        gross_take_profit_usdc=Decimal(gross),
        round_trip_friction_usdc=Decimal(friction),
        research_only=research,
    )
