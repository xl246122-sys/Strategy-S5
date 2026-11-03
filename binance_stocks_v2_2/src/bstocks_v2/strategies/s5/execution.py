"""Research limit adapter. A match here is not evidence that an exchange would fill."""

import hashlib
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_DOWN, Decimal

from bstocks_v2.constitution.guard import CLIENT_ORDER_ID, ConstitutionGuard
from bstocks_v2.contracts.decimals import parse_decimal
from bstocks_v2.contracts.errors import OrderRejected
from bstocks_v2.contracts.models import OrderType, Side, TradingSession
from bstocks_v2.limit_optimizer.book import floor_cents
from bstocks_v2.research_backtest.fills import buy_limit, floor_step, sell_limit
from bstocks_v2.research_backtest.models import FillMode
from bstocks_v2.research_backtest.tape import ClosedBar
from bstocks_v2.sessions.calendar import SessionLabel, TradingCalendar
from bstocks_v2.sessions.eligibility import OrderRequest, SessionEligibility
from bstocks_v2.strategies.s5.spec import (
    MAX_SPREAD_BPS,
    MIN_DEPTH,
    MIN_NOTIONAL,
    PENETRATION,
    PLANNED_STOP,
    PRIMARY_NET_TARGET,
    RISK_BUDGET,
)

CENT = Decimal("0.01")
TIF_DAY = "DAY"
TIF_GTC = "GTC"
TIFS = frozenset({TIF_DAY, TIF_GTC})
TERMINAL_STATUS = frozenset({"FILLED", "CANCELED", "EXPIRED", "REJECTED", "EXPIRED_UNVERIFIED"})


@dataclass(frozen=True)
class ResearchQuote:
    bid: Decimal | None
    ask: Decimal | None
    bid_size: Decimal | None
    ask_size: Decimal | None
    multiplier_down: Decimal
    multiplier_up: Decimal


@dataclass(frozen=True)
class FillView:
    status: str
    qty: Decimal
    price: Decimal | None
    reason: str


@dataclass(frozen=True)
class SizedLimit:
    price: Decimal
    quantity: Decimal
    stop_price: Decimal
    take_profit_price: Decimal
    adjustments: tuple[str, ...]


def research_client_id(symbol: str, role: str, generation: int) -> str:
    """Same symbol, role, and generation always rebuild the same id."""

    if not symbol or symbol.startswith("EQ_") or not role or generation < 0:
        raise ValueError("symbol")
    digest = hashlib.sha256(f"v22-s5|{symbol}|{role}|{generation}".encode()).hexdigest()
    client_id = f"s05{digest[:32]}"
    if CLIENT_ORDER_ID.fullmatch(client_id) is None:
        raise ValueError("CLIENT_ORDER_ID")
    return client_id


def checked_price(value: Decimal) -> Decimal:
    return ConstitutionGuard().check_price(value)


def required_session(label: SessionLabel) -> TradingSession | None:
    if label is SessionLabel.RTH:
        return TradingSession.RTH
    if label in {SessionLabel.PRE, SessionLabel.POST}:
        return TradingSession.EXTENDED
    if label is SessionLabel.OVERNIGHT:
        return TradingSession.H24
    return None


def tif_cross(tif: str, placed: str, now_label: str) -> str | None:
    """DAY and GTC both stop at a session boundary.

    This phase has no exchange print that says either instruction stays working.
    A cross-session print is therefore not a fill.
    """

    if tif not in TIFS:
        return "TIF_UNKNOWN"
    if placed == now_label:
        return None
    return "TIF_UNVERIFIED"


def quote_block(quote: ResearchQuote) -> str | None:
    if quote.bid is None or quote.ask is None:
        return "SPREAD_UNKNOWN"
    try:
        bid = checked_price(quote.bid)
        ask = checked_price(quote.ask)
        checked_price(quote.multiplier_down)
        checked_price(quote.multiplier_up)
    except OrderRejected as exc:
        return exc.reason
    if bid <= 0 or ask <= 0 or bid > ask:
        return "SPREAD_UNKNOWN"
    if quote.multiplier_down <= 0 or quote.multiplier_up < quote.multiplier_down:
        return "PRICE_CAGE"
    mid = (bid + ask) / 2
    if mid <= 0:
        return "SPREAD_UNKNOWN"
    bps = (ask - bid) / mid * Decimal(10000)
    if bps > MAX_SPREAD_BPS:
        return "SPREAD_TOO_WIDE"
    if quote.bid_size is None or quote.ask_size is None:
        return "DEPTH_UNKNOWN"
    if quote.bid_size < MIN_DEPTH or quote.ask_size < MIN_DEPTH:
        return "DEPTH_TOO_THIN"
    return None


def size_limit(bid: Decimal, equity: Decimal, down: Decimal, up: Decimal) -> SizedLimit | str:
    price = floor_cents(bid)
    if price < down or price > up:
        return "PRICE_CAGE"
    stop = (price * (Decimal(1) - PLANNED_STOP)).quantize(CENT, rounding=ROUND_DOWN)
    target = (price * (Decimal(1) + PRIMARY_NET_TARGET)).quantize(CENT, rounding=ROUND_DOWN)
    if not stop < price < target:
        return "GEOMETRY"
    distance = price - stop
    if distance <= 0 or equity <= 0:
        return "GEOMETRY"
    raw = min(RISK_BUDGET / distance, equity * Decimal("0.20") / price)
    qty = floor_step(raw, Decimal(1))
    adjustments: tuple[str, ...] = ("486442",) if qty != raw else ()
    if qty < 1:
        return "WHOLE_SHARE_EXCEEDS_RISK_OR_CAP"
    if qty * price < MIN_NOTIONAL:
        return "NOTIONAL_TOO_SMALL_FOR_FEE_EFFICIENCY"
    return SizedLimit(price, qty, stop, target, adjustments)


def eligibility_reason(
    calendar: TradingCalendar,
    eligibility: SessionEligibility,
    row: object,
    *,
    symbol: str,
    now_et: datetime,
    side: Side,
    order_type: OrderType,
    trading_session: TradingSession | None,
) -> tuple[str, str]:
    decision = eligibility.decide(
        row,
        OrderRequest(
            symbol=symbol,
            now_et=now_et,
            side=side,
            order_type=order_type,
            trading_session=trading_session,
        ),
    )
    label = calendar.label(now_et)
    if decision.label is not label:
        return "SESSION_MISMATCH", decision.snapshot_hash
    if not decision.allowed:
        return decision.reason, decision.snapshot_hash
    return "OK", decision.snapshot_hash


def match_limit(
    calendar: TradingCalendar,
    *,
    side: Side,
    limit: Decimal,
    bar: ClosedBar | None,
    placed_label: str,
    now_et: datetime,
    displayed: Decimal | None,
    remaining: Decimal,
) -> FillView:
    """Conservative queue. Touching the limit, without clearing `PENETRATION`, is unfilled."""

    if remaining <= 0:
        return FillView("UNFILLED", Decimal(0), None, "FLAT")
    if bar is None:
        return FillView("UNFILLED", Decimal(0), None, "NO_BAR")
    if bar.end > now_et:
        return FillView("UNFILLED", Decimal(0), None, "LOOKAHEAD")
    start_label = calendar.label(bar.start).value
    end_label = calendar.label(bar.end).value
    if start_label != end_label:
        return FillView("UNFILLED", Decimal(0), None, "MIXED_BAR")
    if end_label != placed_label:
        return FillView("UNFILLED", Decimal(0), None, "SESSION_BAR")
    if side is Side.BUY:
        raw = buy_limit(FillMode.CONSERVATIVE, bar, limit, PENETRATION, remaining, Decimal(1))
    else:
        raw = sell_limit(FillMode.CONSERVATIVE, bar, limit, PENETRATION, remaining, Decimal(1))
    qty = raw.qty
    if displayed is not None:
        shown = floor_step(parse_decimal(displayed), Decimal(1))
        if shown < qty:
            qty = shown
    if qty <= 0:
        return FillView("UNFILLED", Decimal(0), None, "TOUCH_NO_PENETRATION")
    if qty < remaining:
        return FillView("PARTIALLY_FILLED", qty, limit, "QUEUE")
    return FillView("FILLED", remaining, limit, "PENETRATED")
