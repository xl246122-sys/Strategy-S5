"""News and quote gates. Missing evidence is unknown, never 'no bad news'."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from bstocks_v2.contracts.decimals import parse_decimal
from bstocks_v2.feature_store.errors import LookaheadError
from bstocks_v2.strategies.s5.spec import MAX_SPREAD_BPS, MIN_DEPTH


class NewsState(StrEnum):
    CLEAR = "CLEAR"
    HARD_NEGATIVE = "HARD_NEGATIVE"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class NewsSnapshot:
    state: NewsState
    visible_at: datetime
    source: str

    def __post_init__(self) -> None:
        if self.visible_at.tzinfo is None:
            raise ValueError("news timestamps must be timezone-aware")
        if not isinstance(self.state, NewsState):
            raise ValueError("news state")
        if not self.source:
            raise ValueError("news source")


@dataclass(frozen=True)
class QuoteSnapshot:
    bid: Decimal
    ask: Decimal
    bid_size: Decimal
    ask_size: Decimal
    exchange_ts: datetime
    prior_spread: Decimal | None = None

    def __post_init__(self) -> None:
        if self.exchange_ts.tzinfo is None:
            raise ValueError("quote timestamps must be timezone-aware")
        object.__setattr__(self, "bid", parse_decimal(self.bid))
        object.__setattr__(self, "ask", parse_decimal(self.ask))
        object.__setattr__(self, "bid_size", parse_decimal(self.bid_size))
        object.__setattr__(self, "ask_size", parse_decimal(self.ask_size))
        if self.prior_spread is not None:
            object.__setattr__(self, "prior_spread", parse_decimal(self.prior_spread))


def classify_news(news: NewsSnapshot | None, decision_ts: datetime) -> str:
    """Absence stays NEWS_UNKNOWN. A later headline cannot be moved backward."""

    if news is None:
        return "NEWS_UNKNOWN"
    if news.visible_at > decision_ts:
        raise LookaheadError("NEWS_AFTER_DECISION")
    if news.state == NewsState.CLEAR:
        return "NEWS_CLEAR"
    if news.state == NewsState.HARD_NEGATIVE:
        return "NEWS_HARD_NEGATIVE"
    return "NEWS_UNKNOWN"


def classify_quote(quote: QuoteSnapshot | None, decision_ts: datetime) -> tuple[str, ...]:
    """No bid/ask is unknown depth, not a zero spread."""

    if quote is None:
        return ("SPREAD_UNKNOWN", "DEPTH_UNKNOWN")
    if quote.exchange_ts > decision_ts:
        raise LookaheadError("QUOTE_AFTER_DECISION")
    reasons: list[str] = []
    spread = quote.ask - quote.bid
    if quote.bid <= 0 or quote.ask <= 0 or spread < 0:
        reasons.append("SPREAD_UNKNOWN")
    else:
        mid = (quote.ask + quote.bid) / 2
        bps = spread / mid * Decimal(10000)
        if bps > MAX_SPREAD_BPS:
            reasons.append("SPREAD_TOO_WIDE")
    if quote.bid_size + quote.ask_size < MIN_DEPTH:
        reasons.append("DEPTH_TOO_THIN")
    return tuple(reasons)


def liquidity_recovered(quote: QuoteSnapshot | None) -> bool:
    """Recovery needs an earlier same-session spread that has since tightened."""

    if quote is None or quote.prior_spread is None:
        return False
    spread = quote.ask - quote.bid
    return quote.prior_spread > spread > 0
