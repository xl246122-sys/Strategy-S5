"""Purge and embargo around the lockbox. Two XNYS sessions on each side."""

from dataclasses import dataclass
from datetime import date

from bstocks_v2.research_data.calendar import XnysCalendar
from bstocks_v2.stage14.spec import EMBARGO_SESSIONS, LOCKBOX_END
from bstocks_v2.strategies.s1.spec import LOCKBOX_START, VAL_END, VAL_START


@dataclass(frozen=True)
class Bands:
    validation: frozenset[date]
    lockbox: frozenset[date]
    embargo: frozenset[date]

    def name_of(self, day: date) -> str | None:
        if day in self.validation:
            return "validation"
        if day in self.lockbox:
            return "lockbox"
        if day in self.embargo:
            return "embargo"
        return None


def isolation_bands(calendar: XnysCalendar) -> Bands:
    validation = calendar.sessions_between(VAL_START, VAL_END)
    lockbox = calendar.sessions_between(LOCKBOX_START, LOCKBOX_END)
    width = EMBARGO_SESSIONS
    if len(validation) <= width or len(lockbox) <= width * 2:
        raise RuntimeError("embargo does not fit inside the windows")
    embargo = frozenset(validation[-width:] + lockbox[:width] + lockbox[-width:])
    scored_validation = frozenset(validation[:-width])
    scored_lockbox = frozenset(lockbox[width:-width])
    overlap = scored_validation & scored_lockbox
    overlap = overlap or (scored_validation & embargo) or (scored_lockbox & embargo)
    if overlap:
        raise RuntimeError("embargo overlaps a scored window")
    return Bands(scored_validation, scored_lockbox, embargo)
