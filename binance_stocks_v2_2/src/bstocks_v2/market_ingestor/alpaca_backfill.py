"""Read-only Alpaca SIP minute bars, aggregated to 5-minute buckets for WAL gaps."""

import json
from dataclasses import dataclass
from decimal import Decimal
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from bstocks_v2.config.credentials import ApiPair
from bstocks_v2.constitution.guard import ConstitutionGuard
from bstocks_v2.market_ingestor.wal import BAR_MS, StoredBar

DATA_URL = "https://data.alpaca.markets/v2/stocks/bars"


@dataclass(frozen=True)
class MinuteBar:
    symbol: str
    open_time_ms: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    vwap: Decimal | None


class AlpacaSipBackfill:
    def __init__(self, credentials: ApiPair, guard: ConstitutionGuard | None = None) -> None:
        self._credentials = credentials
        self._guard = guard or ConstitutionGuard()

    def fetch_minutes(self, symbol: str, start_iso: str, end_iso: str) -> list[MinuteBar]:
        self._guard.check_endpoint("GET", DATA_URL)
        query = urlencode(
            {
                "symbols": symbol,
                "timeframe": "1Min",
                "start": start_iso,
                "end": end_iso,
                "feed": "sip",
                "adjustment": "raw",
                "limit": "10000",
            }
        )
        request = Request(
            f"{DATA_URL}?{query}",
            headers={
                "APCA-API-KEY-ID": self._credentials.key,
                "APCA-API-SECRET-KEY": self._credentials.secret,
            },
        )
        with urlopen(request, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"), parse_float=Decimal)
        rows = payload.get("bars", {}).get(symbol, []) if isinstance(payload, dict) else []
        bars: list[MinuteBar] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            bars.append(
                MinuteBar(
                    symbol=symbol,
                    open_time_ms=_ms(str(row["t"])),
                    open=Decimal(str(row["o"])),
                    high=Decimal(str(row["h"])),
                    low=Decimal(str(row["l"])),
                    close=Decimal(str(row["c"])),
                    volume=Decimal(str(row["v"])),
                    vwap=Decimal(str(row["vw"])) if row.get("vw") is not None else None,
                )
            )
        return bars


def aggregate_minutes(
    symbol: str,
    session_date: str,
    minutes: list[MinuteBar],
    bucket_open_ms: int,
) -> StoredBar | None:
    group = [bar for bar in minutes if bucket_open_ms <= bar.open_time_ms < bucket_open_ms + BAR_MS]
    if not group:
        return None
    volume = sum((bar.volume for bar in group), start=Decimal(0))
    weighted = sum(((bar.vwap or bar.close) * bar.volume for bar in group), start=Decimal(0))
    vwap = weighted / volume if volume > 0 else group[-1].close
    return StoredBar(
        symbol=symbol,
        session_date=session_date,
        open_time_ms=bucket_open_ms,
        close_time_ms=bucket_open_ms + BAR_MS - 1,
        open=group[0].open,
        high=max(bar.high for bar in group),
        low=min(bar.low for bar in group),
        close=group[-1].close,
        volume=volume,
        vwap=vwap,
        vwap_source="alpaca_sip",
    )


def _ms(timestamp: str) -> int:
    from datetime import datetime

    parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    return int(parsed.timestamp() * 1000)
