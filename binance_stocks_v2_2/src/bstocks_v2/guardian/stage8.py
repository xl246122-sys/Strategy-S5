"""Stage-8 exit marks. Stop, take-profit, and the 15:55 flatten do not reach an exchange."""

from datetime import datetime

from bstocks_v2.constitution.guard import flatten_deadline
from bstocks_v2.contracts.errors import OrderRejected

_EXITS = {"STOP", "TAKE_PROFIT", "FLATTEN"}


def mark_exit(
    state: str,
    reason: str,
    *,
    now_et: datetime,
    is_half_day: bool,
) -> str:
    if state != "OPEN":
        raise RuntimeError("exit requires an open position")
    if reason not in _EXITS:
        raise OrderRejected("EXIT_REASON")
    if reason == "FLATTEN" and now_et < flatten_deadline(now_et, is_half_day):
        raise OrderRejected("FLATTEN_EARLY")
    return "FLAT_RECONCILED"
