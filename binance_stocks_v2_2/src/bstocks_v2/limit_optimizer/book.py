"""Three resting bids. Prices truncate down to the cent and cannot stand above the ceiling."""

from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal

from bstocks_v2.contracts.decimals import parse_decimal
from bstocks_v2.contracts.models import TradeIntent
from bstocks_v2.limit_optimizer.spec import PASSIVE_PULLBACK_TICKS, TICK, TIER_ORDER


@dataclass(frozen=True)
class QuoteBook:
    """Book at the decision. Finer than a cent is allowed; the candidate floors it."""

    bid: Decimal
    ask: Decimal
    reference: Decimal
    multiplier_down: Decimal
    multiplier_up: Decimal
    structure: Decimal | None = None
    vwap: Decimal | None = None
    orb_high: Decimal | None = None

    def __post_init__(self) -> None:
        bid = parse_decimal(self.bid)
        ask = parse_decimal(self.ask)
        reference = parse_decimal(self.reference)
        down = parse_decimal(self.multiplier_down)
        up = parse_decimal(self.multiplier_up)
        if bid <= 0 or ask <= 0 or reference <= 0:
            raise ValueError("quotes must be positive")
        if bid > ask:
            raise ValueError("bid above ask")
        if down <= 0 or up <= 0 or down > up:
            raise ValueError("price cage")
        object.__setattr__(self, "bid", bid)
        object.__setattr__(self, "ask", ask)
        object.__setattr__(self, "reference", reference)
        object.__setattr__(self, "multiplier_down", down)
        object.__setattr__(self, "multiplier_up", up)
        object.__setattr__(self, "structure", _optional(self.structure))
        object.__setattr__(self, "vwap", _optional(self.vwap))
        object.__setattr__(self, "orb_high", _optional(self.orb_high))


@dataclass(frozen=True)
class PriceCandidate:
    tier: str
    price: Decimal | None
    reason: str | None
    detail: str


def floor_cents(price: Decimal) -> Decimal:
    """Exchange prices have a one-cent tick. Extra digits are dropped, never rounded up."""

    return parse_decimal(price).quantize(TICK, rounding=ROUND_DOWN)


def build_candidates(intent: TradeIntent, book: QuoteBook) -> tuple[PriceCandidate, ...]:
    """Passive, then retest, then aggressive. A missing anchor stays in the audit as its own row."""

    raw = {
        "passive": book.bid - TICK * PASSIVE_PULLBACK_TICKS,
        "retest": _retest_anchor(book),
        "aggressive": min(book.ask, intent.entry_price_ceiling),
    }
    rows: list[PriceCandidate] = []
    for tier in TIER_ORDER:
        anchor = raw[tier]
        if anchor is None:
            rows.append(PriceCandidate(tier, None, "NO_RETEST_ANCHOR", tier))
            continue
        price = floor_cents(anchor)
        reason = _admit(price, intent, book)
        if reason is None:
            rows.append(PriceCandidate(tier, price, None, tier))
        else:
            rows.append(PriceCandidate(tier, price, reason, f"{tier};price={price}"))
    return tuple(rows)


def _retest_anchor(book: QuoteBook) -> Decimal | None:
    """One pullback level. A named structure wins, then VWAP, then the opening-range high."""

    if book.structure is not None:
        return book.structure
    if book.vwap is not None:
        return book.vwap
    return book.orb_high


def _admit(price: Decimal, intent: TradeIntent, book: QuoteBook) -> str | None:
    if not intent.stop_price < price <= intent.entry_price_ceiling < intent.take_profit_price:
        return "GEOMETRY"
    low = book.reference * book.multiplier_down
    high = book.reference * book.multiplier_up
    if price < low or price > high:
        return "PRICE_CAGE"
    return None


def _optional(value: Decimal | None) -> Decimal | None:
    if value is None:
        return None
    parsed = parse_decimal(value)
    if parsed <= 0:
        raise ValueError("anchor must be positive")
    return parsed
