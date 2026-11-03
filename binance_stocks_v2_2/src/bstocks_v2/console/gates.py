"""Paper-console gates for names outside the frozen research universe."""

from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal

from bstocks_v2.strategies.s3.spec import CEILING_ATR, STOP_ATR, TARGET_MULTIPLE

_CENT = Decimal("0.01")


def ready_reason(
    pattern_reason: str,
    *,
    regime_ok: bool,
    window_open: bool,
    eligible: bool,
) -> str:
    if pattern_reason != "READY":
        return pattern_reason
    if not regime_ok:
        return "REGIME"
    if not window_open:
        return "ENTRY_WINDOW"
    if not eligible:
        return "AWAITING_VENUE"
    return "SIGNAL"


def opening_prices(
    orh: Decimal,
    orl: Decimal,
    atr: Decimal,
) -> tuple[Decimal, Decimal, Decimal] | None:
    limit = orh.quantize(_CENT, rounding=ROUND_HALF_UP)
    stop = min(orl, limit - STOP_ATR * atr).quantize(_CENT, rounding=ROUND_HALF_UP)
    if not stop < limit:
        return None
    take_profit = (limit + TARGET_MULTIPLE * (limit - stop)).quantize(_CENT, rounding=ROUND_DOWN)
    ceiling = (orh + CEILING_ATR * atr).quantize(_CENT, rounding=ROUND_HALF_UP)
    if ceiling >= take_profit or not stop < limit < take_profit:
        return None
    return limit, stop, take_profit
