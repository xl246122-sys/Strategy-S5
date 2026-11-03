"""Per-desk US-listed tape. Progress is the strategy gate, not a separate signal."""

from datetime import datetime, timedelta
from decimal import Decimal

from bstocks_v2.console.pool import SECTOR_ETFS
from bstocks_v2.console.pool import SYMBOLS as POOL_SYMBOLS
from bstocks_v2.research_data.calendar import NEW_YORK

_CENT = Decimal("0.01")
_STEPS = (0, 25, 50, 75, 100)
_POOL = frozenset(POOL_SYMBOLS)
SHOW_ETFS = len(SECTOR_ETFS)
SHOW_STOCKS = 20
SHOW = SHOW_ETFS + SHOW_STOCKS

# The value is how many of the four gates already passed: 0, 25, 50, 75, 100.
_GATES: dict[str, dict[str, int]] = {
    "S1": {
        "NOT_IN_S1_UNIVERSE": 0,
        "FACTOR_WARMUP": 0,
        "NOT_ELIGIBLE": 0,
        "REGIME_ASOF": 0,
        "REGIME": 0,
        "VWAP": 0,
        "ATR": 0,
        "BELOW_VWAP": 25,
        "EXTENDED": 25,
        "WARMUP": 50,
        "VARIANCE": 50,
        "ALPHA": 50,
        "Z": 75,
        "STRENGTH_WARMUP": 75,
        "SECTOR": 75,
        "GEOMETRY": 75,
        "NO_SECTOR": 0,
        "AWAITING_VENUE": 75,
        "SIGNAL": 100,
    },
    "S2": {
        "NOT_IN_S2_UNIVERSE": 0,
        "NOT_ELIGIBLE": 0,
        "REGIME_ASOF": 0,
        "REGIME": 0,
        "NOT_INTACT": 0,
        "WARMUP": 0,
        "SLOPE": 25,
        "BELOW_VWAP": 25,
        "EXTENDED": 25,
        "NO_CROSS": 50,
        "NO_DIP": 50,
        "VOLUME": 75,
        "STRUCTURE": 75,
        "COOLDOWN": 75,
        "GEOMETRY": 75,
        "ENTRY_WINDOW": 75,
        "AWAITING_VENUE": 75,
        "SIGNAL": 100,
    },
    "S3": {
        "NO_RANGE": 0,
        "DAY_INVALID": 0,
        "WINDOW_OPEN": 0,
        "NOT_IN_S3_UNIVERSE": 0,
        "NOT_ELIGIBLE": 0,
        "REGIME_ASOF": 0,
        "S3_DISABLED": 0,
        "VWAP_GAP": 0,
        "LOCK_BAR": 25,
        "INSIDE_RANGE": 25,
        "ALREADY_BROKE": 50,
        "NO_ATR": 75,
        "NO_RVOL": 75,
        "RVOL_LOW": 75,
        "BELOW_VWAP": 75,
        "GEOMETRY": 75,
        "ENTRY_WINDOW": 75,
        "AWAITING_VENUE": 75,
        "SIGNAL": 100,
    },
    "S4": {
        "NOT_IN_S4_UNIVERSE": 0,
        "NOT_ELIGIBLE": 0,
        "REGIME_ASOF": 0,
        "NOT_INTACT": 0,
        "WARMUP": 0,
        "ABOVE_VWAP": 25,
        "NO_STRETCH": 50,
        "NO_CROSS": 75,
        "NO_BOUNCE": 75,
        "GEOMETRY": 75,
        "ENTRY_WINDOW": 75,
        "AWAITING_VENUE": 75,
        "SIGNAL": 100,
    },
    "S5": {"S5_ROUTE_CLOSED": 0},
}


def tier(strategy: str, reason: str) -> int:
    """Map the reason this strategy just returned onto one of the four steps."""

    value = _GATES[strategy].get(reason, 0)
    if strategy == "S5":
        value = min(value, 75)
    if value not in _STEPS:
        raise RuntimeError("progress step drifted")
    return value


def reached(previous: int, progress: int) -> bool:
    return progress == 100 and previous < 100


def listed(symbol: str) -> bool:
    return symbol in _POOL


def money(value: Decimal) -> str:
    return format(value.quantize(_CENT), "f")


def remember(
    samples: list[tuple[datetime, Decimal]],
    start: datetime,
    close: Decimal,
) -> None:
    if samples and samples[-1][0] == start:
        samples[-1] = (start, close)
    else:
        samples.append((start, close))
    if len(samples) > 18:
        del samples[:-18]


def track(points: list[tuple[datetime, Decimal]], when: datetime, price: Decimal) -> None:
    """Keep one print about every 15 seconds inside the latest 30-minute window."""

    if when.tzinfo is None:
        raise ValueError("print time needs a timezone")
    cutoff = when - timedelta(minutes=40)
    if points and points[0][0] < cutoff:
        points[:] = [item for item in points if item[0] >= cutoff]
    if points and when < points[-1][0]:
        return
    if points:
        elapsed = (when - points[-1][0]).total_seconds()
        if price == points[-1][1] and elapsed < 15:
            return
        if elapsed < 0.5 and price != points[-1][1]:
            points[-1] = (when, price)
            return
    points.append((when, price))
    if len(points) > 160:
        del points[:-160]


def choose_wave(live: list[str], bars: list[str]) -> list[str]:
    if len(live) >= 2:
        return live
    return bars


def wave_points(samples: list[tuple[datetime, Decimal]]) -> list[str]:
    """Closes inside one 30-minute bar. A new bar waits until it has two prints."""

    groups: dict[datetime, list[Decimal]] = {}
    order: list[datetime] = []
    for start, close in samples:
        key = _bucket(start)
        if key not in groups:
            order.append(key)
            groups[key] = []
        groups[key].append(close)
    if not order:
        return []
    chosen = order[-1]
    if len(groups[chosen]) < 2 and len(order) >= 2 and len(groups[order[-2]]) >= 2:
        chosen = order[-2]
    return [money(price) for price in groups[chosen]]


def sort_names(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    return sorted(rows, key=_name_key)


def visible(rows: list[dict[str, object]], limit: int = SHOW) -> list[dict[str, object]]:
    """Sector ETFs first, then the closest individual stocks."""

    by_symbol = {str(row["symbol"]): row for row in rows}
    pinned = [by_symbol[symbol] for symbol in SECTOR_ETFS if symbol in by_symbol]
    held = set(SECTOR_ETFS)
    stocks = [row for row in rows if str(row["symbol"]) not in held and row.get("kind") == "个股"]
    room = max(0, limit - len(pinned))
    return sort_names(pinned) + sort_names(stocks)[:room]


def _name_key(row: dict[str, object]) -> tuple[int, Decimal, str]:
    progress = row["progress"]
    if not isinstance(progress, int):
        raise RuntimeError("progress")
    raw = row.get("edge", "0")
    try:
        edge = Decimal(str(raw))
    except (ArithmeticError, ValueError):
        edge = Decimal(0)
    return (-progress, -edge, str(row["symbol"]))


def _bucket(start: datetime) -> datetime:
    local = start.astimezone(NEW_YORK).replace(second=0, microsecond=0)
    minute = (local.minute // 30) * 30
    return local.replace(minute=minute)
