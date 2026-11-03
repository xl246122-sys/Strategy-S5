"""Session clock for the paper console.

The frozen research file stops at 2026-10-30. Later dates come from the
installed XNYS calendar and are not written back into that file.
"""

from datetime import date, datetime, time, timedelta
from typing import Any

from bstocks_v2.research_data.calendar import (
    EXTENDED_END,
    EXTENDED_START,
    NEW_YORK,
    OPEN,
    XnysCalendar,
    load_xnys_calendar,
)


class LiveCalendar(XnysCalendar):
    def __init__(self) -> None:
        super().__init__(load_xnys_calendar().closes())
        self._extra: dict[date, time] = {}
        self._xnys: Any | None
        try:
            import exchange_calendars as calendars  # type: ignore[import-untyped]

            self._xnys = calendars.get_calendar("XNYS")
            self.source = "frozen+exchange_calendars"
        except Exception:
            self._xnys = None
            self.source = "frozen"

    def close_on(self, day: date) -> time | None:
        known = self._closes.get(day)
        if known is not None:
            return known
        cached = self._extra.get(day)
        if cached is not None:
            return cached
        calendar = self._xnys
        if calendar is None:
            return None
        if not calendar.is_session(day.isoformat()):
            return None
        stamp = calendar.session_close(day.isoformat())
        local = stamp.to_pydatetime().astimezone(NEW_YORK)
        clock = time(local.hour, local.minute)
        self._extra[day] = clock
        return clock

    def is_half_day(self, day: date) -> bool:
        close = self.close_on(day)
        return close is not None and close < time(16, 0)

    def classify(self, start: datetime, end: datetime) -> str:
        if start.tzinfo is None or end.tzinfo is None:
            raise ValueError("bar timestamps must be timezone-aware")
        if end <= start:
            raise ValueError("bar end must be after bar start")
        opened = self._label(start.astimezone(NEW_YORK))
        closed = self._label((end - timedelta(microseconds=1)).astimezone(NEW_YORK))
        if opened != closed:
            return "MIXED"
        return opened

    def _label(self, local: datetime) -> str:
        clock = local.timetz().replace(tzinfo=None)
        close = self.close_on(local.date())
        if close is None:
            return "NON_SESSION"
        if OPEN <= clock < close:
            return "RTH"
        if EXTENDED_START <= clock < OPEN or close <= clock < EXTENDED_END:
            return "EXTENDED"
        return "OVERNIGHT"
