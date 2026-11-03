"""Clock labels for RTH, premarket, postmarket, and overnight.

The stored XNYS calendar still decides which dates exist and when RTH ends.
A missing date is closed. Overnight is only the 20:00–04:00 span attached to
a real session, not a rolling UTC hour.
"""

from datetime import date, datetime, time, timedelta
from enum import StrEnum

from bstocks_v2.research_data.calendar import NEW_YORK, XnysCalendar, load_xnys_calendar

PRE_START = time(4, 0)
RTH_START = time(9, 30)
POST_END = time(20, 0)
FULL_CLOSE = time(16, 0)


class SessionLabel(StrEnum):
    PRE = "PRE"
    RTH = "RTH"
    POST = "POST"
    OVERNIGHT = "OVERNIGHT"
    NON_SESSION = "NON_SESSION"


class TradingCalendar:
    def __init__(self, calendar: XnysCalendar) -> None:
        self._calendar = calendar

    @classmethod
    def load(cls) -> "TradingCalendar":
        return cls(load_xnys_calendar())

    def is_session(self, day: date) -> bool:
        return self._calendar.is_session(day)

    def is_half_day(self, day: date) -> bool:
        if not self.is_session(day):
            return False
        return self._calendar.close_time(day) < FULL_CLOSE

    def close_time(self, day: date) -> time:
        return self._calendar.close_time(day)

    def label(self, moment: datetime) -> SessionLabel:
        if moment.tzinfo is None:
            raise ValueError("bar timestamps must be timezone-aware")
        local = moment.astimezone(NEW_YORK)
        clock = local.timetz().replace(tzinfo=None)
        if clock < PRE_START:
            prior = local.date() - timedelta(days=1)
            if self.is_session(prior):
                return SessionLabel.OVERNIGHT
            return SessionLabel.NON_SESSION
        if not self.is_session(local.date()):
            return SessionLabel.NON_SESSION
        close = self.close_time(local.date())
        if clock < RTH_START:
            return SessionLabel.PRE
        if clock < close:
            return SessionLabel.RTH
        if clock < POST_END:
            return SessionLabel.POST
        return SessionLabel.OVERNIGHT
