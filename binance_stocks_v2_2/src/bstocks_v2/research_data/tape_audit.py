"""Check the 5-minute SIP tape the sealed backtest actually read.

The check uses the same window, feed, and session filter as the lockbox loader.
It does not place orders and does not rewrite the SIP store or the seal.
"""

from collections import Counter
from datetime import date, datetime, timedelta
from decimal import Decimal
from hashlib import sha256
from pathlib import Path

from bstocks_v2.limit_optimizer.spec import COMMISSION_RATE, HALF_SPREAD, PENETRATION_DELTA
from bstocks_v2.research_data.calendar import NEW_YORK, XnysCalendar
from bstocks_v2.research_data.paths import FRESH_DEFAULT
from bstocks_v2.stage14.bars import _cents, _number, _repair, symbol_list
from bstocks_v2.stage14.spec import LOCKBOX_END
from bstocks_v2.strategies.s1.spec import COMMISSION_RATE as S1_COMMISSION
from bstocks_v2.strategies.s1.spec import HALF_SPREAD as S1_SPREAD
from bstocks_v2.strategies.s1.spec import TRAIN_START

_FIVE = timedelta(minutes=5)
_COLUMNS = ["t", "o", "h", "l", "c", "v", "feed", "bar_end_utc", "session", "ny_date"]


def frozen_costs() -> dict[str, str]:
    """The cost inputs the sealed run used. Both layers must name the same numbers."""

    if S1_COMMISSION != COMMISSION_RATE or S1_SPREAD != HALF_SPREAD:
        raise RuntimeError("strategy costs drifted from the execution constants")
    return {
        "commission_rate": format(COMMISSION_RATE, "f"),
        "half_spread": format(HALF_SPREAD, "f"),
        "penetration_delta": format(PENETRATION_DELTA, "f"),
        "quote": "close ± half_spread",
        "fee_each_side": "commission_rate * fill_notional",
        "spread_each_side": "half_spread * quantity",
    }


def round_trip_cost(price: Decimal, take_profit: Decimal, quantity: Decimal) -> Decimal:
    """Entry fee, exit fee, and both half-spreads. This is the engine's cash formula."""

    entry_fee = COMMISSION_RATE * price * quantity
    exit_fee = COMMISSION_RATE * take_profit * quantity
    return entry_fee + exit_fee + HALF_SPREAD * quantity * 2


def session_gaps(expected: list[datetime], actual: list[datetime]) -> tuple[int, int]:
    """Missing and unexpected 5-minute starts. Order does not matter."""

    wanted = set(expected)
    seen = set(actual)
    return len(wanted - seen), len(seen - wanted)


def long_holes(expected: list[datetime], actual: list[datetime]) -> int:
    """Runs of two or more missing slots. One missing bar is a five-minute hole."""

    missing = set(expected) - set(actual)
    holes = 0
    run = 0
    previous: datetime | None = None
    for slot in expected:
        if slot not in missing:
            if run >= 2:
                holes += 1
            run = 0
            previous = None
            continue
        if previous is not None and slot - previous == _FIVE:
            run += 1
        else:
            if run >= 2:
                holes += 1
            run = 1
        previous = slot
    if run >= 2:
        holes += 1
    return holes


def audit_symbol(fresh: Path, symbol: str, calendar: XnysCalendar) -> dict[str, object]:
    """One symbol, same rows the lockbox loader keeps."""

    import pyarrow.dataset as ds

    folder = fresh / "parquet" / "5Min" / symbol
    files = sorted(folder.glob("*.parquet"))
    if not files:
        raise RuntimeError(f"{symbol} has no 5-minute file")
    dataset = ds.dataset([str(path) for path in files], format="parquet")
    table = dataset.to_table(
        columns=_COLUMNS,
        filter=(ds.field("ny_date") >= TRAIN_START)
        & (ds.field("ny_date") <= LOCKBOX_END)
        & (ds.field("session") == "rth")
        & (ds.field("feed") == "sip"),
    )
    payload = table.to_pydict()
    sessions = calendar.sessions_between(TRAIN_START, LOCKBOX_END)
    expected = {day: calendar.rth_bucket_starts(day) for day in sessions}
    by_day: dict[date, list[datetime]] = {day: [] for day in sessions}
    issues: Counter[str] = Counter()
    accepted: list[tuple[datetime, str]] = []
    seen: set[datetime] = set()
    repairs = 0
    rounded = 0
    for index, start in enumerate(payload["t"]):
        end = payload["bar_end_utc"][index]
        day = payload["ny_date"][index]
        reason = _row_issue(start, end, day, seen, calendar)
        if reason is not None:
            issues[reason] += 1
            continue
        assert isinstance(start, datetime)
        assert isinstance(day, date)
        seen.add(start)
        raw = (
            _number(payload["o"][index]),
            _number(payload["h"][index]),
            _number(payload["l"][index]),
            _number(payload["c"][index]),
        )
        cents = tuple(_cents(value) for value in raw)
        if cents != raw:
            rounded += 1
        repaired = _repair(*cents)
        if repaired != cents:
            repairs += 1
        slot = by_day.get(day)
        if slot is None:
            issues["outside_session"] += 1
            continue
        slot.append(start.astimezone(NEW_YORK))
        volume = _number(payload["v"][index])
        accepted.append((start, _line(symbol, start, end, repaired, volume)))
    digest = sha256()
    for _start, line in sorted(accepted):
        digest.update(line.encode("utf-8"))
    missing = 0
    extra = 0
    empty_days = 0
    holes = 0
    for day, slots in expected.items():
        got = by_day[day]
        if not got:
            empty_days += 1
        miss, surplus = session_gaps(slots, got)
        missing += miss
        extra += surplus
        holes += long_holes(slots, got)
    return {
        "symbol": symbol,
        "bars": len(seen),
        "missing": missing,
        "extra": extra,
        "empty_days": empty_days,
        "long_holes": holes,
        "repairs": repairs,
        "rounded": rounded,
        "issues": dict(issues),
        "sha256": digest.hexdigest(),
    }


def audit_tape(fresh: Path = FRESH_DEFAULT) -> dict[str, object]:
    """Scan the lockbox universe and fold the per-symbol fingerprints."""

    from bstocks_v2.research_data.calendar import load_xnys_calendar

    calendar = load_xnys_calendar()
    symbols = symbol_list()
    rows = []
    for index, symbol in enumerate(symbols, start=1):
        rows.append(audit_symbol(fresh, symbol, calendar))
        print(f"{index}/{len(symbols)} {symbol}", flush=True)
    folded = sha256()
    for row in rows:
        folded.update(str(row["sha256"]).encode("utf-8"))
    issues: Counter[str] = Counter()
    for row in rows:
        raw = row["issues"]
        if isinstance(raw, dict):
            issues.update({str(key): int(value) for key, value in raw.items()})
    return {
        "source": str(fresh / "parquet" / "5Min"),
        "feed": "sip",
        "adjustment": "raw",
        "symbols": len(rows),
        "window": [TRAIN_START.isoformat(), LOCKBOX_END.isoformat()],
        "bars": sum(_as_int(row, "bars") for row in rows),
        "missing": sum(_as_int(row, "missing") for row in rows),
        "extra": sum(_as_int(row, "extra") for row in rows),
        "empty_days": sum(_as_int(row, "empty_days") for row in rows),
        "long_holes": sum(_as_int(row, "long_holes") for row in rows),
        "repairs": sum(_as_int(row, "repairs") for row in rows),
        "rounded": sum(_as_int(row, "rounded") for row in rows),
        "issues": dict(issues),
        "costs": frozen_costs(),
        "sha256": folded.hexdigest(),
        "by_symbol": rows,
    }


def _as_int(row: dict[str, object], key: str) -> int:
    value = row[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise RuntimeError(key)
    return value


def _row_issue(
    start: object,
    end: object,
    day: object,
    seen: set[datetime],
    calendar: XnysCalendar,
) -> str | None:
    if not isinstance(start, datetime) or start.tzinfo is None:
        return "naive_start"
    if not isinstance(end, datetime) or end.tzinfo is None:
        return "naive_end"
    if not isinstance(day, date):
        return "bad_day"
    if end - start != _FIVE:
        return "duration"
    local = start.astimezone(NEW_YORK)
    if local.date() != day:
        return "ny_date"
    if local.second or local.microsecond or local.minute % 5:
        return "grid"
    if calendar.classify(start, end) != "RTH":
        return "not_rth"
    if start in seen:
        return "duplicate"
    return None


def _line(
    symbol: str,
    start: datetime,
    end: datetime,
    ohlc: tuple[Decimal, Decimal, Decimal, Decimal],
    volume: Decimal,
) -> str:
    opened, high, low, closed = ohlc
    return (
        f"{symbol}|{start.isoformat()}|{end.isoformat()}|"
        f"{opened}|{high}|{low}|{closed}|{volume}\n"
    )


if __name__ == "__main__":
    import json

    from bstocks_v2.research_data.paths import OUT_DEFAULT

    report = audit_tape()
    destination = OUT_DEFAULT / "reports" / "tape_foundation.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(report, indent=2), encoding="utf-8")
    public = {key: value for key, value in report.items() if key != "by_symbol"}
    print(json.dumps(public, indent=2))
    print(destination)
