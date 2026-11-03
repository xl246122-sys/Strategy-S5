"""Split factors for a research view. Raw prints stay raw."""

import csv
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path

_JUMP_RATIO = Decimal("1.2214027581601699")
_SPLIT_TYPES = frozenset({"forward_splits", "reverse_splits", "forward_split", "reverse_split"})


@dataclass(frozen=True)
class SplitEvent:
    symbol: str
    ex_date: date
    new_rate: Decimal
    old_rate: Decimal


def load_splits(path: Path) -> list[SplitEvent]:
    events: list[SplitEvent] = []
    with path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            kind = row.get("type", "")
            if kind not in _SPLIT_TYPES:
                continue
            symbol = row.get("symbol", "").strip()
            ex_text = row.get("ex_date", "").strip()
            new_text = row.get("new_rate", "").strip()
            old_text = row.get("old_rate", "").strip()
            if not symbol or not ex_text or not new_text or not old_text:
                continue
            new_rate = Decimal(new_text)
            old_rate = Decimal(old_text)
            if new_rate <= 0 or old_rate <= 0:
                continue
            events.append(
                SplitEvent(
                    symbol=symbol,
                    ex_date=date.fromisoformat(ex_text[:10]),
                    new_rate=new_rate,
                    old_rate=old_rate,
                )
            )
    return events


def backward_price_factors(splits: list[SplitEvent], sessions: list[date]) -> dict[date, Decimal]:
    """Price multiplier that puts older raw prints on the latest share basis."""

    by_day: dict[date, list[SplitEvent]] = {}
    for split in splits:
        by_day.setdefault(split.ex_date, []).append(split)
    factor = Decimal(1)
    out: dict[date, Decimal] = {}
    for session in reversed(sessions):
        out[session] = factor
        for split in by_day.get(session, []):
            factor *= split.old_rate / split.new_rate
    return out


def split_adjusted_volume(raw_volume: Decimal, price_factor: Decimal) -> Decimal:
    if price_factor <= 0:
        raise ValueError("price factor must be positive")
    return raw_volume / price_factor


def is_large_jump(previous_close: Decimal, close: Decimal) -> bool:
    if previous_close <= 0 or close <= 0:
        return True
    ratio = close / previous_close
    return ratio >= _JUMP_RATIO or ratio <= (Decimal(1) / _JUMP_RATIO)


def jump_flag(previous_close: Decimal, close: Decimal, split_on_day: bool) -> str | None:
    if not is_large_jump(previous_close, close):
        return None
    if split_on_day:
        return "SPLIT_RAW"
    return "UNMATCHED_JUMP"
