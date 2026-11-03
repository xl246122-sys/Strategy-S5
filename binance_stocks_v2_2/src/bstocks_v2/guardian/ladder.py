"""Marketable limit price and the whole-share split used after 16:00 ET."""

from datetime import datetime
from decimal import ROUND_DOWN, Decimal

from bstocks_v2.constitution.guard import NEW_YORK
from bstocks_v2.contracts.errors import OrderRejected
from bstocks_v2.guardian.spec import CENT, MARKETABLE_BID_FACTOR


def marketable_price(bid: Decimal, floor: Decimal, cap: Decimal) -> Decimal:
    """`max(floor, trunc(bid * 0.997, 2dp))`, still inside the published cage."""

    _cents(floor)
    _cents(cap)
    _cents(bid)
    if floor > cap:
        raise OrderRejected("PRICE_CAGE")
    clipped = (bid * MARKETABLE_BID_FACTOR).quantize(CENT, rounding=ROUND_DOWN)
    price = max(floor, clipped)
    if price > cap:
        raise OrderRejected("PRICE_CAGE")
    return price


def split_shares(quantity: Decimal) -> tuple[Decimal, Decimal]:
    """Integer shares that can leave as an extended limit, and the residual fraction."""

    if quantity <= 0:
        raise OrderRejected("QUANTITY")
    whole = quantity.to_integral_value(rounding=ROUND_DOWN)
    return whole, quantity - whole


def session_phase(now_et: datetime, is_half_day: bool) -> str:
    if now_et.tzinfo is None:
        raise OrderRejected("NAIVE_DATETIME")
    local = now_et.astimezone(NEW_YORK)
    if local.weekday() >= 5:
        return "EXTENDED"
    start = local.replace(hour=9, minute=30, second=0, microsecond=0)
    hour, minute = (13, 0) if is_half_day else (16, 0)
    close = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if start <= local < close:
        return "RTH"
    return "EXTENDED"


def _cents(value: Decimal) -> None:
    exponent = value.as_tuple().exponent
    if not isinstance(exponent, int) or exponent < -2:
        raise OrderRejected("PRICE_PRECISION")
