"""Three-level subscription plan. One socket never carries more than 120 streams."""

MAX_STREAMS_PER_SOCKET = 120
MAX_L2_SYMBOLS = 300
MAX_L3_SYMBOLS = 20
L2_SUFFIXES = ("@kline_5m", "@tradingStatus", "@tradability")
WS_BASE = "wss://nbstream.binance.com/equity"


class PoolLimitError(ValueError):
    """The requested subscription exceeds a hard pool limit."""


PREFERRED_L2 = (
    "AAPL",
    "MSFT",
    "NVDA",
    "TSLA",
    "AMZN",
    "META",
    "GOOGL",
    "AVGO",
    "SPY",
    "QQQ",
)


def l1_streams() -> list[str]:
    return ["price", "calendar"]


def raw_ws_url() -> str:
    return f"{WS_BASE}/ws"


def select_l2_symbols(payload: object, limit: int = MAX_L2_SYMBOLS) -> list[str]:
    """Prefer liquid names, then other BUY_SELL names. Cap is 300."""

    if limit > MAX_L2_SYMBOLS:
        raise PoolLimitError("L2 scan pool is capped at 300 symbols")
    rows = payload.get("symbols") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return []
    tradable: list[str] = []
    others: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        symbol = row.get("symbol")
        if not isinstance(symbol, str) or not symbol:
            continue
        if row.get("tradability") == "BUY_SELL":
            tradable.append(symbol)
        else:
            others.append(symbol)
    tradable_set = set(tradable)
    ordered = [symbol for symbol in PREFERRED_L2 if symbol in tradable_set]
    ordered.extend(sorted(symbol for symbol in tradable_set if symbol not in ordered))
    if len(ordered) < limit:
        ordered.extend(sorted(set(others) - set(ordered)))
    return ordered[:limit]


def plan_l2(symbols: list[str]) -> list[list[str]]:
    if len(symbols) > MAX_L2_SYMBOLS:
        raise PoolLimitError("L2 scan pool is capped at 300 symbols")
    if len(set(symbols)) != len(symbols):
        raise PoolLimitError("duplicate L2 symbol")
    streams = [f"{symbol}{suffix}" for symbol in symbols for suffix in L2_SUFFIXES]
    return _pack(streams)


def l3_commands(current: set[str], desired: set[str], request_id: int) -> list[dict[str, object]]:
    if len(desired) > MAX_L3_SYMBOLS:
        raise PoolLimitError("L3 quote pool is capped at 20 symbols")
    commands: list[dict[str, object]] = []
    subscribe = [f"{symbol}@quote" for symbol in sorted(desired - current)]
    unsubscribe = [f"{symbol}@quote" for symbol in sorted(current - desired)]
    if subscribe:
        commands.append({"method": "SUBSCRIBE", "params": subscribe, "id": request_id})
    if unsubscribe:
        commands.append({"method": "UNSUBSCRIBE", "params": unsubscribe, "id": request_id + 1})
    return commands


def combined_url(streams: list[str]) -> str:
    if not streams:
        raise PoolLimitError("refusing an empty socket")
    if len(streams) > MAX_STREAMS_PER_SOCKET:
        raise PoolLimitError("socket would exceed 120 streams")
    return f"{WS_BASE}/stream?streams={'/'.join(streams)}"


def _pack(streams: list[str]) -> list[list[str]]:
    shards: list[list[str]] = []
    current: list[str] = []
    for stream in streams:
        if len(current) == MAX_STREAMS_PER_SOCKET:
            shards.append(current)
            current = []
        current.append(stream)
    if current:
        shards.append(current)
    return shards
