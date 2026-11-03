"""One-minute bars for the S3 sequential arm, including the lockbox dates."""

from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from bstocks_v2.research_backtest.tape import ClosedBar
from bstocks_v2.research_data.calendar import NEW_YORK, XnysCalendar
from bstocks_v2.stage14.spec import LOCKBOX_END

_CENT = Decimal("0.01")
_COLUMNS = ["symbol", "t", "o", "h", "l", "c", "v", "feed", "bar_end_utc", "session", "ny_date"]


def load_parent_minutes(
    fresh: Path,
    parents: dict[str, set[datetime]],
    calendar: XnysCalendar,
) -> dict[tuple[str, datetime], tuple[ClosedBar, ...]]:
    found: dict[tuple[str, datetime], list[ClosedBar]] = {}
    for symbol, starts in parents.items():
        if not starts:
            continue
        found.update(_one_symbol(fresh, symbol, starts, calendar))
    return {key: tuple(rows) for key, rows in found.items()}


def _one_symbol(
    fresh: Path,
    symbol: str,
    starts: set[datetime],
    calendar: XnysCalendar,
) -> dict[tuple[str, datetime], list[ClosedBar]]:
    folder = fresh / "parquet" / "1Min" / symbol
    files = sorted(folder.glob("*.parquet")) if folder.exists() else []
    if not files:
        raise RuntimeError(f"{symbol} has no 1-minute file")
    session_days = sorted({start.astimezone(NEW_YORK).date() for start in starts})
    if any(day > LOCKBOX_END for day in session_days):
        raise RuntimeError("minute is past the lockbox end")
    import pyarrow.compute as pc
    import pyarrow.dataset as ds

    dataset = ds.dataset([str(path) for path in files], format="parquet")
    table = dataset.to_table(
        columns=_COLUMNS,
        filter=(pc.field("ny_date").isin(session_days))
        & (ds.field("session") == "rth")
        & (ds.field("feed") == "sip"),
    )
    payload = table.to_pydict()
    index = _parent_index(starts)
    grouped: dict[tuple[str, datetime], list[ClosedBar]] = {}
    seen: set[datetime] = set()
    for cursor, start in enumerate(payload["t"]):
        if not isinstance(start, datetime) or start.tzinfo is None:
            raise RuntimeError(f"{symbol} minute timestamp is not timezone-aware")
        parent = index.get(start)
        if parent is None:
            continue
        if start in seen:
            raise RuntimeError(f"{symbol} duplicate minute {start.isoformat()}")
        seen.add(start)
        end = payload["bar_end_utc"][cursor]
        day = payload["ny_date"][cursor]
        if not isinstance(end, datetime) or end.tzinfo is None or not isinstance(day, date):
            raise RuntimeError(f"{symbol} minute row is incomplete")
        if day > LOCKBOX_END or calendar.classify(start, end) != "RTH":
            raise RuntimeError(f"{symbol} minute is outside the research window")
        opened, high, low, closed = _repair(
            _cents(payload["o"][cursor]),
            _cents(payload["h"][cursor]),
            _cents(payload["l"][cursor]),
            _cents(payload["c"][cursor]),
        )
        grouped.setdefault((symbol, parent), []).append(
            ClosedBar(
                symbol=symbol,
                start=start,
                end=end,
                open=opened,
                high=high,
                low=low,
                close=closed,
                volume=_number(payload["v"][cursor]),
                timeframe="1m",
            )
        )
    for rows in grouped.values():
        rows.sort(key=lambda bar: bar.start)
    return grouped


def _parent_index(starts: set[datetime]) -> dict[datetime, datetime]:
    index: dict[datetime, datetime] = {}
    for parent in starts:
        for step in range(5):
            index[parent + timedelta(minutes=step)] = parent
    return index


def _cents(value: object) -> Decimal:
    return _number(value).quantize(_CENT, rounding=ROUND_HALF_UP)


def _number(value: object) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise ValueError("missing number")
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, str):
        return Decimal(value)
    if isinstance(value, float):
        return Decimal(format(value, ".10f"))
    raise ValueError(f"cannot read {type(value).__name__}")


def _repair(
    opened: Decimal,
    high: Decimal,
    low: Decimal,
    closed: Decimal,
) -> tuple[Decimal, Decimal, Decimal, Decimal]:
    if high < low:
        high, low = low, high
    if opened < low:
        opened = low
    if opened > high:
        opened = high
    if closed < low:
        closed = low
    if closed > high:
        closed = high
    return opened, high, low, closed
