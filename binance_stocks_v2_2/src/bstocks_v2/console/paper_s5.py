"""S5 paper plan. The live order route stays closed."""

from datetime import date
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal

from bstocks_v2.console.archive import SessionArchive
from bstocks_v2.strategies.s5.spec import B0_MAX_SIMPLE_RETURN, PLANNED_STOP, PRIMARY_NET_TARGET

_CENT = Decimal("0.01")


def s5_paper_plan(
    archive: SessionArchive,
    symbol: str,
    close: Decimal,
    day: date,
) -> tuple[str, tuple[Decimal, Decimal, Decimal] | None]:
    """A drop versus the previous regular-session close can open a paper buy."""

    previous = archive.prior_rth_close(symbol, day)
    if previous is None or previous <= 0 or close <= 0:
        return "NO_PREV_CLOSE", None
    simple = (close - previous) / previous
    if simple > B0_MAX_SIMPLE_RETURN:
        return "Z", None
    limit = close.quantize(_CENT, rounding=ROUND_HALF_UP)
    stop = (limit * (Decimal(1) - PLANNED_STOP)).quantize(_CENT, rounding=ROUND_DOWN)
    take_profit = (limit * (Decimal(1) + PRIMARY_NET_TARGET)).quantize(_CENT, rounding=ROUND_DOWN)
    if not stop < limit < take_profit:
        return "GEOMETRY", None
    return "SIGNAL", (limit, stop, take_profit)
