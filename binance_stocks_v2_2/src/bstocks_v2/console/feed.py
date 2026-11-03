"""Binance equity streams, with Alpaca used only to warm or fill gaps."""

import asyncio
import json
import threading
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from bstocks_v2.console.hub import Hub
from bstocks_v2.console.pool import BENCHMARKS, SYMBOLS
from bstocks_v2.console.scan import SCAN_SECONDS, WEIGHT_PAUSE, pull_official
from bstocks_v2.constitution.guard import ConstitutionGuard
from bstocks_v2.contracts.decimals import parse_decimal
from bstocks_v2.feature_store.bars import FeatureBar
from bstocks_v2.market_ingestor.alpaca_backfill import DATA_URL
from bstocks_v2.market_ingestor.messages import parse_ws_message
from bstocks_v2.market_ingestor.shards import combined_url

MARKET = tuple(sorted(set(SYMBOLS) | set(BENCHMARKS)))
_SHARD = 100


def stream_shards() -> list[list[str]]:
    streams = [f"{symbol}@kline_5m" for symbol in MARKET]
    streams.extend(f"{symbol}@quote" for symbol in SYMBOLS)
    return [streams[index : index + _SHARD] for index in range(0, len(streams), _SHARD)]


class MarketFeed:
    def __init__(self, hub: Hub) -> None:
        self.hub = hub
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._guard = ConstitutionGuard()

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="paper-feed", daemon=True)
        self._thread.start()
        self.hub.set_worker("alive")

    def stop(self) -> None:
        self._stop.set()
        self.hub.set_worker("stopped")

    def _run(self) -> None:
        self._tradability()
        threading.Thread(target=self._warm, name="paper-warm", daemon=True).start()
        threading.Thread(target=self._scan, name="paper-scan", daemon=True).start()
        asyncio.run(self._listen())

    def _scan(self) -> None:
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                batch = pull_official(SYMBOLS)
            except Exception:
                self.hub.set_scan("官方报价扫描失败，5 秒后重试")
                batch = None
            if batch is not None:
                count = self.hub.apply_quotes(batch.quotes)
                if batch.error == "weight":
                    self.hub.set_scan(f"官方报价 {count}/{len(SYMBOLS)}，权重过高，放慢")
                elif batch.error:
                    self.hub.set_scan("官方报价扫描失败，5 秒后重试")
                else:
                    self.hub.set_scan(f"官方报价 {count}/{len(SYMBOLS)}，每 {SCAN_SECONDS} 秒")
            pause = SCAN_SECONDS
            over_weight = (
                batch is not None and batch.weight is not None and batch.weight >= WEIGHT_PAUSE
            )
            if batch is not None and (batch.error == "weight" or over_weight):
                pause = 30
            remain = pause - (time.monotonic() - started)
            if remain > 0 and self._stop.wait(remain):
                return

    def _tradability(self) -> None:
        path = "/sapi/v1/equity/market/exchangeInfo"
        self._guard.check_endpoint("GET", path)
        request = Request("https://api.binance.com" + path)
        try:
            with urlopen(request, timeout=20) as response:
                payload = json.loads(response.read().decode("utf-8"), parse_float=Decimal)
        except HTTPError as exc:
            self.hub.set_feed(f"exchangeInfo HTTP {exc.code}，资格未知，不开新仓")
            return
        except Exception:
            self.hub.set_feed("exchangeInfo 失败，资格未知，不开新仓")
            return
        rows = payload.get("symbols") if isinstance(payload, dict) else None
        found: dict[str, str] = {}
        if isinstance(rows, list):
            for row in rows:
                if not isinstance(row, dict):
                    continue
                symbol = row.get("symbol")
                state = row.get("tradability")
                if isinstance(symbol, str) and isinstance(state, str):
                    found[symbol.upper()] = state
        self.hub.set_tradable(found)
        self.hub.set_feed("exchangeInfo 已读")

    def _warm(self) -> None:
        self.hub.set_alpaca("历史回放已关闭，只积累实盘 K 线")

    async def _listen(self) -> None:
        shards = stream_shards()
        await asyncio.gather(
            self._socket(["price"]),
            *(self._socket(shard) for shard in shards),
        )

    async def _socket(self, streams: list[str]) -> None:
        import websockets

        url = combined_url(streams)
        if not url.startswith("wss://nbstream.binance.com/equity/"):
            self.hub.set_feed("行情地址被拒绝")
            return
        while not self._stop.is_set():
            try:
                async with websockets.connect(url, open_timeout=20, max_size=1_048_576) as socket:
                    self.hub.set_feed(f"币安行情已连接 {len(stream_shards())} 路")
                    while not self._stop.is_set():
                        raw = await asyncio.wait_for(socket.recv(), timeout=30)
                        text = raw if isinstance(raw, str) else raw.decode()
                        self.hub.accept(parse_ws_message(text))
            except TimeoutError:
                continue
            except Exception:
                self.hub.set_feed("行情中断，正在重连")
                await asyncio.sleep(2)


def _alpaca_5m(
    guard: ConstitutionGuard,
    key: str,
    secret: str,
    symbol: str,
    start: datetime,
    end: datetime,
) -> list[FeatureBar]:
    guard.check_endpoint("GET", DATA_URL)
    query = (
        f"symbols={symbol}&timeframe=5Min&start={start.strftime('%Y-%m-%dT%H:%M:%SZ')}"
        f"&end={end.strftime('%Y-%m-%dT%H:%M:%SZ')}&feed=sip&adjustment=raw&limit=10000"
    )
    request = Request(
        f"{DATA_URL}?{query}",
        headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret},
    )
    with urlopen(request, timeout=30) as response:
        payload = json.loads(response.read().decode("utf-8"), parse_float=Decimal)
    rows = payload.get("bars", {}).get(symbol, []) if isinstance(payload, dict) else []
    bars: list[FeatureBar] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        opened = _stamp(str(row["t"]))
        bars.append(
            FeatureBar(
                symbol=symbol,
                start=opened,
                end=opened + timedelta(minutes=5),
                open=parse_decimal(row["o"]),
                high=parse_decimal(row["h"]),
                low=parse_decimal(row["l"]),
                close=parse_decimal(row["c"]),
                volume=parse_decimal(row["v"]),
                bar_vwap=parse_decimal(row["vw"]) if row.get("vw") is not None else None,
                closed=True,
                feed="alpaca",
            )
        )
    return bars


def _stamp(value: str) -> datetime:
    text = value.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        raise ValueError("alpaca bar has no timezone")
    return parsed.astimezone(UTC)
