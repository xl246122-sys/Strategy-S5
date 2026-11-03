"""XNYS session labels. The schedule is the exchange_calendars XNYS calendar."""

import json
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

NEW_YORK = ZoneInfo("America/New_York")
OPEN = time(9, 30)
EXTENDED_START = time(4, 0)
EXTENDED_END = time(20, 0)
RESEARCH_START = date(2024, 11, 1)
RESEARCH_END = date(2026, 10, 30)


class XnysCalendar:
    def __init__(self, closes: dict[date, time]) -> None:
        self._closes = dict(closes)

    def closes(self) -> dict[date, time]:
        return dict(self._closes)

    def is_session(self, day: date) -> bool:
        return day in self._closes

    def close_time(self, day: date) -> time:
        try:
            return self._closes[day]
        except KeyError as exc:
            raise ValueError(f"{day.isoformat()} is not an XNYS session") from exc

    def sessions_between(self, start: date, end: date) -> list[date]:
        return sorted(day for day in self._closes if start <= day <= end)

    def classify(self, start: datetime, end: datetime) -> str:
        """Label the half-open interval [start, end). Mixed bars are not RTH."""

        if start.tzinfo is None or end.tzinfo is None:
            raise ValueError("bar timestamps must be timezone-aware")
        if end <= start:
            raise ValueError("bar end must be after bar start")
        opened = self._bucket(start.astimezone(NEW_YORK))
        closed = self._bucket((end - timedelta(microseconds=1)).astimezone(NEW_YORK))
        if opened != closed:
            return "MIXED"
        return opened

    def rth_bucket_starts(self, day: date) -> list[datetime]:
        close = self.close_time(day)
        cursor = datetime.combine(day, OPEN, tzinfo=NEW_YORK)
        stop = datetime.combine(day, close, tzinfo=NEW_YORK)
        starts: list[datetime] = []
        while cursor < stop:
            starts.append(cursor)
            cursor += timedelta(minutes=5)
        return starts

    def _bucket(self, local: datetime) -> str:
        clock = local.timetz().replace(tzinfo=None)
        close = self._closes.get(local.date())
        if close is None:
            return "NON_SESSION"
        if OPEN <= clock < close:
            return "RTH"
        if EXTENDED_START <= clock < OPEN or close <= clock < EXTENDED_END:
            return "EXTENDED"
        return "OVERNIGHT"


def load_xnys_calendar() -> XnysCalendar:
    payload = json.loads(Path(__file__).with_name("xnys_sessions.json").read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError("XNYS calendar is not a list")
    closes: dict[date, time] = {}
    for item in payload:
        if not isinstance(item, dict):
            raise ValueError("XNYS calendar row is not an object")
        day_text = item.get("date")
        close_text = item.get("close")
        if not isinstance(day_text, str) or not isinstance(close_text, str):
            raise ValueError("XNYS calendar row is missing date or close")
        hour_text, minute_text = close_text.split(":")
        closes[date.fromisoformat(day_text)] = time(int(hour_text), int(minute_text))
    return XnysCalendar(closes)


def utc(year: int, month: int, day: int, hour: int, minute: int) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=UTC)
