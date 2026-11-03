"""Read SIP bars only through the validation end. The lockbox date is refused before a scan."""

from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from bstocks_v2.feature_store.bars import FeatureBar
from bstocks_v2.research_data.adjust import SplitEvent, backward_price_factors, load_splits
from bstocks_v2.research_data.calendar import NEW_YORK, XnysCalendar
from bstocks_v2.research_data.paths import FRESH_DEFAULT
from bstocks_v2.strategies.s1.spec import LOCKBOX_START, TRAIN_START, VAL_END

_CENT = Decimal("0.01")
_COLUMNS = [
    "symbol",
    "t",
    "o",
    "h",
    "l",
    "c",
    "v",
    "vw",
    "feed",
    "bar_end_utc",
    "session",
    "ny_date",
]


def require_split_end(end: date) -> None:
    if end >= LOCKBOX_START:
        raise RuntimeError("lockbox is closed")


def load_membership(
    path: Path,
) -> tuple[set[tuple[str, str]], int]:
    """Eligible (symbol, session) pairs inside the train/validation window, and the stock count."""

    require_split_end(VAL_END)
    import pyarrow.dataset as ds

    dataset = ds.dataset(str(path), format="parquet")
    table = dataset.to_table(
        columns=["symbol", "asset_type", "research_eligible", "session"],
        filter=(ds.field("session") >= TRAIN_START.isoformat())
        & (ds.field("session") <= VAL_END.isoformat()),
    )
    payload = table.to_pydict()
    eligible: set[tuple[str, str]] = set()
    stocks = 0
    for symbol, asset, flag, session in zip(
        payload["symbol"],
        payload["asset_type"],
        payload["research_eligible"],
        payload["session"],
        strict=True,
    ):
        if str(flag) != "true":
            continue
        if str(asset) != "etf":
            stocks += 1
            continue
        eligible.add((str(symbol), str(session)))
    return eligible, stocks


def load_bars(
    fresh: Path,
    symbols: tuple[str, ...],
    calendar: XnysCalendar,
    splits: list[SplitEvent],
) -> list[FeatureBar]:
    require_split_end(VAL_END)
    sessions = calendar.sessions_between(TRAIN_START, VAL_END)
    loaded: list[FeatureBar] = []
    for symbol in symbols:
        factors = backward_price_factors(
            [item for item in splits if item.symbol == symbol],
            sessions,
        )
        loaded.extend(_one_symbol(fresh, symbol, calendar, factors))
    latest = max(bar.start.astimezone(NEW_YORK).date() for bar in loaded)
    if latest >= LOCKBOX_START:
        raise RuntimeError("lockbox bar was loaded")
    return loaded


def load_splits_until(fresh: Path = FRESH_DEFAULT) -> tuple[list[SplitEvent], int]:
    """Keep splits on or before the validation end. Later ex-dates are counted and dropped."""

    path = fresh / "corporate_actions" / "corporate_actions_20241101_20261030.csv"
    if not path.exists():
        return [], 0
    events = load_splits(path)
    kept = [item for item in events if item.ex_date <= VAL_END]
    return kept, len(events) - len(kept)


def _one_symbol(
    fresh: Path,
    symbol: str,
    calendar: XnysCalendar,
    factors: dict[date, Decimal],
) -> list[FeatureBar]:
    folder = fresh / "parquet" / "5Min" / symbol
    files = sorted(folder.glob("*.parquet")) if folder.exists() else []
    if not files:
        raise RuntimeError(f"{symbol} has no 5-minute file")
    import pyarrow.dataset as ds

    dataset = ds.dataset([str(path) for path in files], format="parquet")
    table = dataset.to_table(
        columns=_COLUMNS,
        filter=(ds.field("ny_date") >= TRAIN_START)
        & (ds.field("ny_date") <= VAL_END)
        & (ds.field("session") == "rth")
        & (ds.field("feed") == "sip"),
    )
    if table.num_rows == 0:
        raise RuntimeError(f"{symbol} has no RTH bars through {VAL_END.isoformat()}")
    payload = table.to_pydict()
    bars: list[FeatureBar] = []
    seen: set[datetime] = set()
    for index, start in enumerate(payload["t"]):
        if not isinstance(start, datetime):
            raise RuntimeError(f"{symbol} timestamp is not a datetime")
        end = payload["bar_end_utc"][index]
        if not isinstance(end, datetime):
            raise RuntimeError(f"{symbol} bar end is not a datetime")
        if start.tzinfo is None or end.tzinfo is None:
            raise RuntimeError(f"{symbol} timestamp is naive")
        day = payload["ny_date"][index]
        if not isinstance(day, date) or day >= LOCKBOX_START:
            raise RuntimeError(f"{symbol} row is outside the open window")
        if calendar.classify(start, end) != "RTH":
            raise RuntimeError(f"{symbol} rth label disagrees with the calendar")
        if start in seen:
            raise RuntimeError(f"{symbol} duplicate bar {start.isoformat()}")
        seen.add(start)
        opened, high, low, closed = _repair(
            _cents(payload["o"][index]),
            _cents(payload["h"][index]),
            _cents(payload["l"][index]),
            _cents(payload["c"][index]),
        )
        bars.append(
            FeatureBar(
                symbol=symbol,
                start=start,
                end=end,
                open=opened,
                high=high,
                low=low,
                close=closed,
                volume=_number(payload["v"][index]),
                bar_vwap=_optional(payload["vw"][index]),
                closed=True,
                feed="sip",
                price_factor=factors.get(day, Decimal(1)),
            )
        )
    return bars


def _cents(value: object) -> Decimal:
    number = _number(value)
    return number.quantize(_CENT, rounding=ROUND_HALF_UP)


def _optional(value: object) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, float) and value != value:
        return None
    number = _number(value)
    if number <= 0:
        return None
    return number


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
