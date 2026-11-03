"""Crash the writer with kill -9, then prove the next process restores the same VWAP."""

import sys
import time
from datetime import datetime, timedelta
from decimal import Decimal

from bstocks_v2.market_ingestor.wal import (
    BAR_MS,
    NEW_YORK,
    IntradayBarStore,
    StoredBar,
    recover_session,
)

SYMBOL = "AAPL"
BAR_COUNT = 78


def sample_session() -> tuple[datetime, datetime, list[StoredBar], Decimal]:
    session = datetime(2026, 11, 2, 9, 30, tzinfo=NEW_YORK)
    open_ms = int(session.timestamp() * 1000)
    bars: list[StoredBar] = []
    for index in range(BAR_COUNT):
        vwap = Decimal("10.00") + (Decimal(index) / Decimal(100))
        close = Decimal("10") + Decimal(index % 7)
        bars.append(
            StoredBar(
                symbol=SYMBOL,
                session_date=session.date().isoformat(),
                open_time_ms=open_ms + index * BAR_MS,
                close_time_ms=open_ms + index * BAR_MS + BAR_MS - 1,
                open=Decimal("10"),
                high=Decimal("12"),
                low=Decimal("9"),
                close=close,
                volume=Decimal(100),
                vwap=vwap,
                vwap_source="wire",
            )
        )
    expected = sum((bar.vwap * bar.volume for bar in bars), start=Decimal(0)) / sum(
        (bar.volume for bar in bars),
        start=Decimal(0),
    )
    asof = session + timedelta(minutes=5 * BAR_COUNT)
    return session, asof, bars, expected


def main(argv: list[str] | None = None) -> None:
    args = list(sys.argv[1:] if argv is None else argv)
    mode, path = args[0], args[1]
    session, asof, bars, expected = sample_session()
    if mode == "write":
        store = IntradayBarStore(path)
        for bar in bars:
            store.upsert_bar(bar)
        print("READY", flush=True)
        time.sleep(120)
        return
    store = IntradayBarStore(path)
    started = time.perf_counter()
    state = recover_session(store, SYMBOL, session, asof)
    elapsed_ms = (time.perf_counter() - started) * 1000
    match = state.vwap_intact and state.rth_vwap == expected and len(state.bars) == BAR_COUNT
    print(
        f"elapsed_ms={elapsed_ms:.3f} match={match} bars={len(state.bars)} vwap={state.rth_vwap}"
    )
    if not match:
        raise SystemExit(2)
    if elapsed_ms >= 100:
        raise SystemExit(3)


if __name__ == "__main__":
    main()
