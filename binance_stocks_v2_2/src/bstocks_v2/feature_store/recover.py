"""Rebuild a feature book from intraday_bars_wal after a mid-session crash."""

from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal

from bstocks_v2.feature_store.bars import FeatureBar
from bstocks_v2.feature_store.engine import CausalFeatureBook
from bstocks_v2.market_ingestor.wal import IntradayBarStore, StoredBar
from bstocks_v2.research_data.calendar import NEW_YORK, XnysCalendar


def bar_to_stored(bar: FeatureBar) -> StoredBar:
    if bar.bar_vwap is None:
        stored_vwap = (bar.high + bar.low + bar.close) / Decimal(3)
        source = "hlc3"
    else:
        stored_vwap = bar.bar_vwap
        source = "bar"
    return StoredBar(
        symbol=bar.symbol,
        session_date=bar.start.astimezone(NEW_YORK).date().isoformat(),
        open_time_ms=_millis(bar.start),
        close_time_ms=_millis(bar.end),
        open=bar.open,
        high=bar.high,
        low=bar.low,
        close=bar.close,
        volume=bar.volume,
        vwap=stored_vwap,
        vwap_source=source,
    )


def recover_book(
    store: IntradayBarStore,
    calendar: XnysCalendar,
    factors: Mapping[tuple[str, str], Decimal] | None = None,
) -> CausalFeatureBook:
    """Replay every stored bar. The caller keeps writing bars that were not in the WAL yet."""

    book = CausalFeatureBook(calendar)
    table = factors or {}
    for symbol in store.symbols():
        for stored in store.load_symbol(symbol):
            factor = table.get((stored.symbol, stored.session_date), Decimal(1))
            book.update(_from_stored(stored, factor))
    return book


def _from_stored(bar: StoredBar, factor: Decimal) -> FeatureBar:
    return FeatureBar(
        symbol=bar.symbol,
        start=datetime.fromtimestamp(bar.open_time_ms / 1000, tz=UTC),
        end=datetime.fromtimestamp(bar.close_time_ms / 1000, tz=UTC),
        open=bar.open,
        high=bar.high,
        low=bar.low,
        close=bar.close,
        volume=bar.volume,
        bar_vwap=bar.vwap if bar.vwap_source == "bar" else None,
        closed=True,
        feed="sip",
        price_factor=factor,
    )


def _millis(value: datetime) -> int:
    return int(value.timestamp() * 1000)
