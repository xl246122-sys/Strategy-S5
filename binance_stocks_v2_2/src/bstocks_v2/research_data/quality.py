"""Bar quality flags. Non-SIP rows are not allowed into RVOL or VWAP."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal


@dataclass(frozen=True)
class PriceBar:
    symbol: str
    start: datetime
    end: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    vwap: Decimal | None
    feed: str


def bar_quality(bar: PriceBar, *, duplicate: bool) -> str:
    if bar.feed.casefold() != "sip":
        return "NON_SIP"
    if duplicate:
        return "DUPLICATE_TS"
    if min(bar.open, bar.high, bar.low, bar.close) <= 0 or bar.volume < 0:
        return "OHLC_INVALID"
    if bar.high < bar.low:
        return "OHLC_INVALID"
    if bar.open < bar.low or bar.open > bar.high or bar.close < bar.low or bar.close > bar.high:
        return "OHLC_INVALID"
    if bar.volume == 0:
        return "ZERO_VOLUME"
    return "OK"


def trade_price(bar: PriceBar) -> Decimal:
    if bar.vwap is not None:
        return bar.vwap
    return (bar.high + bar.low + bar.close) / Decimal(3)
