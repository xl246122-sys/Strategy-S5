"""Warm feature books from the local SIP store. Historical bars never become orders."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from bstocks_v2.console.hub import Hub
from bstocks_v2.console.pool import SYMBOLS
from bstocks_v2.console.swing import DailyBar
from bstocks_v2.feature_store.bars import FeatureBar
from bstocks_v2.feature_store.errors import LookaheadError
from bstocks_v2.research_data.paths import FRESH_DEFAULT

_SESSIONS = 60
_FEATURE_SESSIONS = 2


def warm_hub(hub: Hub) -> str:
    """Load recent regular-session bars into features and the daily book."""

    root = _sip_root()
    if root is None:
        hub.warm_status = "无本地 SIP，中线从实盘开始累积"
        return hub.warm_status
    try:
        import pyarrow.parquet as parquet
    except ImportError:
        hub.warm_status = "未安装 pyarrow，中线从实盘开始累积"
        return hub.warm_status
    warmed = 0
    symbols = ["SPY", *SYMBOLS]
    seen: set[str] = set()
    for symbol in symbols:
        if symbol in seen:
            continue
        seen.add(symbol)
        dailies, recent = _read_symbol(parquet, root, symbol)
        if not dailies and not recent:
            continue
        with hub._lock:
            if dailies:
                hub.swing.seed(dailies)
            for bar in recent:
                _absorb(hub, bar)
                warmed += 1
    hub.warm_status = f"已用 SIP 暖 {warmed} 根最近 K 线，历史日线 {len(seen)} 个标的，未下单"
    return hub.warm_status


def _sip_root() -> Path | None:
    folder = FRESH_DEFAULT / "parquet" / "5Min"
    if folder.is_dir():
        return folder
    return None


def _read_symbol(
    parquet: object,
    root: Path,
    symbol: str,
) -> tuple[list[DailyBar], list[FeatureBar]]:
    folder = root / symbol
    if not folder.is_dir():
        return [], []
    files = sorted(folder.glob(f"{symbol}_5Min_*.parquet"))[-3:]
    rows: list[tuple[date, datetime, Decimal, Decimal, Decimal, Decimal, Decimal]] = []
    for path in files:
        table = parquet.read_table(  # type: ignore[attr-defined]
            path,
            columns=["t", "o", "h", "l", "c", "v", "session", "ny_date"],
        )
        payload = table.to_pydict()
        for index, session in enumerate(payload["session"]):
            if session != "RTH":
                continue
            day = payload["ny_date"][index]
            if not isinstance(day, date):
                continue
            start = payload["t"][index]
            if not isinstance(start, datetime):
                continue
            if start.tzinfo is None:
                start = start.replace(tzinfo=UTC)
            rows.append(
                (
                    day,
                    start,
                    _decimal(payload["o"][index]),
                    _decimal(payload["h"][index]),
                    _decimal(payload["l"][index]),
                    _decimal(payload["c"][index]),
                    _decimal(payload["v"][index]),
                )
            )
    if not rows:
        return [], []
    rows.sort(key=lambda item: item[1])
    days = sorted({item[0] for item in rows})
    keep_days = set(days[-_SESSIONS:])
    feature_days = set(days[-_FEATURE_SESSIONS:])
    grouped: dict[date, list[tuple[datetime, Decimal, Decimal, Decimal, Decimal, Decimal]]] = {}
    recent: list[FeatureBar] = []
    for day, start, open_, high, low, close, volume in rows:
        if day not in keep_days:
            continue
        grouped.setdefault(day, []).append((start, open_, high, low, close, volume))
        if day in feature_days:
            recent.append(
                FeatureBar(
                    symbol=symbol,
                    start=start,
                    end=start + timedelta(minutes=5),
                    open=open_,
                    high=high,
                    low=low,
                    close=close,
                    volume=volume,
                    bar_vwap=close,
                    closed=True,
                    feed="sip",
                )
            )
    dailies = [_daily(symbol, day, bars) for day, bars in sorted(grouped.items())]
    return [item for item in dailies if item is not None], recent


def _daily(
    symbol: str,
    day: date,
    bars: list[tuple[datetime, Decimal, Decimal, Decimal, Decimal, Decimal]],
) -> DailyBar | None:
    ordered = sorted(bars, key=lambda item: item[0])
    for index in range(1, len(ordered)):
        if ordered[index][0] != ordered[index - 1][0] + timedelta(minutes=5):
            return None
    volume = sum((item[5] for item in ordered), start=Decimal(0))
    return DailyBar(
        symbol=symbol,
        day=day,
        open=ordered[0][1],
        high=max(item[2] for item in ordered),
        low=min(item[3] for item in ordered),
        close=ordered[-1][4],
        volume=volume,
        complete=True,
    )


def _absorb(hub: Hub, bar: FeatureBar) -> None:
    for desk in hub.desks:
        try:
            desk.features.update(bar)
        except (LookaheadError, ValueError):
            return
    hub.m30.absorb(bar)


def _decimal(value: object) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(format(value, ".10f"))
