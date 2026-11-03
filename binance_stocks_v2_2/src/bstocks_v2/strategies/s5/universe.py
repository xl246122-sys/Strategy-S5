"""Pre-registered S5 names. Sector parents come from the fund map, not from returns."""

from bstocks_v2.strategies.s1.spec import SECTOR_PARENT, TRADED

UNIVERSE: frozenset[str] = frozenset(TRADED)


def in_universe(symbol: str) -> bool:
    return symbol in UNIVERSE


def sector_parent(symbol: str) -> str | None:
    return SECTOR_PARENT.get(symbol)
