"""Point-in-time market cap from a share count that was already public.

The share count comes from a periodic SEC filing. It applies on and after the
filing date, never before. Splits after the measurement date rescale the count
into the same share unit as that day's raw close. A later filing cannot change
an earlier close.
"""

from datetime import date, timedelta
from decimal import Decimal

from bstocks_v2.research_data.adjust import SplitEvent

PERIODIC_FORMS = frozenset(
    {
        "10-K",
        "10-K/A",
        "10-Q",
        "10-Q/A",
        "20-F",
        "20-F/A",
        "40-F",
        "40-F/A",
        "6-K",
        "6-K/A",
    }
)
_TAGS = (
    ("dei", "EntityCommonStockSharesOutstanding"),
    ("us-gaap", "CommonStockSharesOutstanding"),
    ("us-gaap", "SharesOutstanding"),
    ("ifrs-full", "NumberOfSharesOutstanding"),
)
_MIN_SHARES = Decimal(100_000)
_MAX_FILING_AGE = timedelta(days=548)


def share_prints(payload: dict[str, object]) -> list[tuple[date, date, Decimal]]:
    """`(filed, period_end, shares)` from the first populated share tag."""

    facts = payload.get("facts")
    if not isinstance(facts, dict):
        return []
    for taxonomy, tag in _TAGS:
        node = facts.get(taxonomy)
        if not isinstance(node, dict):
            continue
        block = node.get(tag)
        if not isinstance(block, dict):
            continue
        units = block.get("units")
        if not isinstance(units, dict):
            continue
        rows = units.get("shares")
        if not isinstance(rows, list):
            continue
        parsed = _parse_rows(rows)
        if parsed:
            return parsed
    return []


def shares_on_day(
    prints: list[tuple[date, date, Decimal]],
    day: date,
    splits: list[SplitEvent],
) -> Decimal | None:
    """Latest share count whose filing date is on or before `day`."""

    chosen: tuple[date, date, Decimal] | None = None
    for filed, period_end, shares in prints:
        if filed > day or day - filed > _MAX_FILING_AGE:
            continue
        if chosen is None or filed > chosen[0] or (filed == chosen[0] and period_end > chosen[1]):
            chosen = (filed, period_end, shares)
    if chosen is None:
        return None
    _filed, period_end, shares = chosen
    return shares * _split_factor(splits, period_end, day)


def cap_series(
    closes: list[tuple[date, Decimal]],
    prints: list[tuple[date, date, Decimal]],
    splits: list[SplitEvent],
) -> list[tuple[date, Decimal]]:
    """One cap per close. `asof` is the close date, so the next session can see it."""

    series: list[tuple[date, Decimal]] = []
    for day, close in closes:
        if close <= 0:
            continue
        shares = shares_on_day(prints, day, splits)
        if shares is None:
            continue
        series.append((day, close * shares))
    return series


def _parse_rows(rows: list[object]) -> list[tuple[date, date, Decimal]]:
    parsed: list[tuple[date, date, Decimal]] = []
    seen: set[tuple[date, date, Decimal]] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        form = row.get("form")
        if form not in PERIODIC_FORMS:
            continue
        filed_text = row.get("filed")
        end_text = row.get("end")
        value = row.get("val")
        if not isinstance(filed_text, str) or not isinstance(end_text, str):
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            continue
        shares = Decimal(str(value))
        if shares < _MIN_SHARES:
            continue
        item = (date.fromisoformat(filed_text[:10]), date.fromisoformat(end_text[:10]), shares)
        if item in seen:
            continue
        seen.add(item)
        parsed.append(item)
    return parsed


def _split_factor(splits: list[SplitEvent], period_end: date, day: date) -> Decimal:
    factor = Decimal(1)
    for split in splits:
        if period_end < split.ex_date <= day:
            factor *= split.new_rate / split.old_rate
    return factor
