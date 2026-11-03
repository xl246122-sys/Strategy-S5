"""Pair closed Binance equity 5-minute bars with Alpaca SIP bars.

Equity REST has no historical klines. SIP also has no prints after 20:00 ET.
The capture waits for the next 04:00–20:00 ET window, records closed
``@kline_5m`` events, and only then requests SIP. It does not place orders.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from functools import lru_cache
from pathlib import Path
from urllib.error import HTTPError

from bstocks_v2.config.credentials import alpaca_from_env
from bstocks_v2.market_ingestor.alpaca_backfill import (
    AlpacaSipBackfill,
    MinuteBar,
    aggregate_minutes,
)
from bstocks_v2.market_ingestor.messages import ParsedEvent, parse_ws_message
from bstocks_v2.market_ingestor.shards import combined_url
from bstocks_v2.regime.classifier import BENCHMARKS
from bstocks_v2.research_data.calendar import NEW_YORK, load_xnys_calendar
from bstocks_v2.research_data.domain_shift import ComparedBar, ShiftSummary, align, summarize
from bstocks_v2.strategies.s1.spec import TRADED

SIP_START = time(4, 0)
SIP_END = time(20, 0)
DEADLINE = time(10, 30)
MINIMUM = 30
ENV_PATH = Path("/home/ubuntu/.config/bstocks_v2/stage2.env")
SYMBOLS = tuple(sorted(set(BENCHMARKS) | set(TRADED)))
# Sessions after the frozen research calendar. Taken from XNYS, not fitted to prices.
_EXTRA_SESSIONS = frozenset(
    date.fromisoformat(item)
    for item in (
        "2026-11-02",
        "2026-11-03",
        "2026-11-04",
        "2026-11-05",
        "2026-11-06",
        "2026-11-09",
        "2026-11-10",
        "2026-11-11",
        "2026-11-12",
        "2026-11-13",
        "2026-11-16",
        "2026-11-17",
        "2026-11-18",
        "2026-11-19",
        "2026-11-20",
        "2026-11-23",
        "2026-11-24",
        "2026-11-25",
        "2026-11-27",
        "2026-11-30",
    )
)
_TOKEN = re.compile(r"[A-Za-z0-9_\-]{20,}")


@lru_cache(maxsize=1)
def _session_days() -> frozenset[date]:
    return frozenset(load_xnys_calendar().closes()) | _EXTRA_SESSIONS


def is_xnys_session(day: date) -> bool:
    return day in _session_days()


def load_env_file(path: Path) -> None:
    """Load KEY=value lines. Values stay in the process environment."""

    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :]
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ[key.strip()] = value.strip().strip('"').strip("'")


def redact(text: str) -> str:
    return _TOKEN.sub("redacted", text)[:180]


def next_listen(now: datetime, is_session: Callable[[date], bool]) -> datetime:
    """Return now when SIP can already print, otherwise the next 04:00 ET."""

    local = now.astimezone(NEW_YORK)
    clock = local.timetz().replace(tzinfo=None)
    if is_session(local.date()) and SIP_START <= clock < SIP_END:
        return now
    if is_session(local.date()) and clock < SIP_START:
        return datetime.combine(local.date(), SIP_START, tzinfo=NEW_YORK)
    cursor = local.date() + timedelta(days=1)
    for _ in range(14):
        if is_session(cursor):
            return datetime.combine(cursor, SIP_START, tzinfo=NEW_YORK)
        cursor += timedelta(days=1)
    raise RuntimeError("no upcoming XNYS session")


def in_sip_window(start: datetime, is_session: Callable[[date], bool]) -> bool:
    local = start.astimezone(NEW_YORK)
    clock = local.timetz().replace(tzinfo=None)
    return is_session(local.date()) and SIP_START <= clock < SIP_END


def binance_bar(event: ParsedEvent) -> ComparedBar | None:
    if event.kind != "kline" or event.closed is not True:
        return None
    if not event.symbol or event.bar_open_ms is None:
        return None
    if event.close is None or event.volume is None or event.vwap is None:
        return None
    opened = datetime.fromtimestamp(event.bar_open_ms / 1000, UTC)
    return ComparedBar(event.symbol.upper(), opened, event.close, event.volume, event.vwap)


def sip_bar(symbol: str, open_ms: int, minutes: list[MinuteBar]) -> ComparedBar | None:
    stored = aggregate_minutes(symbol, _session_date(open_ms), minutes, open_ms)
    if stored is None:
        return None
    opened = datetime.fromtimestamp(open_ms / 1000, UTC)
    return ComparedBar(symbol.upper(), opened, stored.close, stored.volume, stored.vwap)


def deadline_for(listen_at: datetime) -> datetime:
    local = listen_at.astimezone(NEW_YORK)
    return datetime.combine(local.date(), DEADLINE, tzinfo=NEW_YORK)


def render(summary: ShiftSummary, meta: dict[str, object]) -> str:
    lines = [
        "# 跨数据源偏差预算",
        "",
        f"- 状态：`{summary.status}`",
        f"- 对齐根数：{summary.overlap}",
        f"- 收盘价中位偏差 bps：{summary.close_median_bps}",
        f"- 收盘价 95% 绝对偏差 bps：{summary.close_p95_bps}",
        f"- 成交量中位比：{summary.volume_median_ratio}",
        f"- VWAP 中位偏差 bps：{summary.vwap_median_bps}",
        f"- 币安已收盘根数：{meta.get('binance_closed')}",
        f"- SIP 有成交的根数：{meta.get('sip_rows')}",
        f"- 第一根（美东）：{meta.get('first_open_et')}",
        f"- 最后一根（美东）：{meta.get('last_open_et')}",
        "",
        "比较键是同一标的、同一根 5 分钟线的开始时间。",
        "币安股票行情没有历史 K 线，20:00 ET 之后的 SIP 也没有成交。",
        "研究窗口里的锁箱没有重跑。",
        "",
    ]
    return "\n".join(lines)


def write_report(folder: Path, summary: ShiftSummary, meta: dict[str, object]) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    payload = {
        "status": summary.status,
        "overlap": summary.overlap,
        "close_median_bps": summary.close_median_bps,
        "close_p95_bps": summary.close_p95_bps,
        "volume_median_ratio": summary.volume_median_ratio,
        "vwap_median_bps": summary.vwap_median_bps,
        **meta,
    }
    text = json.dumps(payload, indent=2, default=str)
    (folder / "domain_shift_live.json").write_text(text + "\n", encoding="utf-8")
    (folder / "domain_shift_live.md").write_text(render(summary, meta), encoding="utf-8")
    if summary.status == "ok":
        (folder / "domain_shift.json").write_text(text + "\n", encoding="utf-8")
        (folder / "domain_shift.md").write_text(render(summary, meta), encoding="utf-8")


def _session_date(open_ms: int) -> str:
    opened = datetime.fromtimestamp(open_ms / 1000, UTC).astimezone(NEW_YORK)
    return opened.date().isoformat()


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _fetch(client: AlpacaSipBackfill, symbol: str, open_ms: int) -> list[MinuteBar] | None:
    opened = datetime.fromtimestamp(open_ms / 1000, UTC)
    try:
        return client.fetch_minutes(symbol, _iso(opened), _iso(opened + timedelta(minutes=5)))
    except HTTPError as exc:
        if exc.code == 403:
            return None
        raise


def note_kline(
    latest: dict[str, ParsedEvent],
    closed: dict[tuple[str, int], ParsedEvent],
    event: ParsedEvent,
) -> None:
    """A new open time means the previous bar finished, even if its close flag was missed."""

    if event.kind != "kline" or not event.symbol or event.bar_open_ms is None:
        return
    symbol = event.symbol.upper()
    previous = latest.get(symbol)
    previous_open = previous.bar_open_ms if previous is not None else None
    if previous is not None and previous_open is not None and previous_open != event.bar_open_ms:
        closed[(symbol, previous_open)] = replace(previous, closed=True)
    if event.closed is True:
        closed[(symbol, event.bar_open_ms)] = event
    latest[symbol] = event


async def _listen(
    symbols: tuple[str, ...],
    seconds: float,
    latest: dict[str, ParsedEvent],
    closed: dict[tuple[str, int], ParsedEvent],
) -> None:
    import websockets

    url = combined_url([f"{symbol}@kline_5m" for symbol in symbols])
    async with websockets.connect(url, open_timeout=20, max_size=1_048_576) as socket:
        loop = asyncio.get_running_loop()
        end = loop.time() + seconds
        while loop.time() < end:
            try:
                raw = await asyncio.wait_for(socket.recv(), timeout=8)
            except TimeoutError:
                break
            text = raw if isinstance(raw, str) else raw.decode()
            note_kline(latest, closed, parse_ws_message(text))


def _et(moment: datetime) -> str:
    return moment.astimezone(NEW_YORK).isoformat(timespec="minutes")


async def capture(out_dir: Path, env_file: Path) -> ShiftSummary:
    load_env_file(env_file)
    listen_at = next_listen(datetime.now(UTC), is_xnys_session)
    stop_at = deadline_for(listen_at)
    print(f"overlap_waiting {_iso(listen_at)}", flush=True)
    while datetime.now(UTC) < listen_at.astimezone(UTC):
        remaining = (listen_at.astimezone(UTC) - datetime.now(UTC)).total_seconds()
        await asyncio.sleep(min(300, max(remaining, 0)))
    client = AlpacaSipBackfill(alpaca_from_env())
    binance: dict[tuple[str, datetime], ComparedBar] = {}
    sip: dict[tuple[str, datetime], ComparedBar] = {}
    pending: dict[tuple[int, str], str] = {}
    finished: set[tuple[int, str]] = set()
    latest: dict[str, ParsedEvent] = {}
    closed: dict[tuple[str, int], ParsedEvent] = {}
    while datetime.now(UTC) < stop_at.astimezone(UTC) and len(sip) < MINIMUM:
        try:
            await _listen(SYMBOLS, 30, latest, closed)
        except Exception as exc:  # noqa: BLE001
            print(f"overlap_socket {redact(type(exc).__name__ + ': ' + str(exc))}", flush=True)
            await asyncio.sleep(5)
            continue
        for (symbol, open_ms), event in closed.items():
            bar = binance_bar(event)
            if bar is None or not in_sip_window(bar.start, is_xnys_session):
                continue
            binance[(bar.symbol, bar.start)] = bar
            if (open_ms, symbol) not in finished:
                pending[open_ms, symbol] = symbol
        ready = [
            (open_ms, symbol)
            for open_ms, symbol in list(pending)
            if datetime.fromtimestamp(open_ms / 1000, UTC) + timedelta(minutes=5, seconds=20)
            <= datetime.now(UTC)
        ]
        for open_ms, symbol in ready:
            try:
                minutes = _fetch(client, symbol, open_ms)
            except Exception as exc:  # noqa: BLE001
                print(f"overlap_sip {redact(type(exc).__name__ + ': ' + str(exc))}", flush=True)
                continue
            if minutes is None:
                continue
            pending.pop((open_ms, symbol), None)
            finished.add((open_ms, symbol))
            paired = sip_bar(symbol, open_ms, minutes)
            if paired is not None:
                sip[(paired.symbol, paired.start)] = paired
        print(
            f"overlap_progress binance={len(binance)} sip={len(sip)}",
            flush=True,
        )
        if len(sip) >= MINIMUM:
            break
        await asyncio.sleep(10)
    pairs = align(list(sip.values()), list(binance.values()))
    summary = summarize(pairs, MINIMUM)
    opens = sorted(start for _, start in sip)
    meta: dict[str, object] = {
        "binance_closed": len(binance),
        "sip_rows": len(sip),
        "first_open_et": _et(opens[0]) if opens else None,
        "last_open_et": _et(opens[-1]) if opens else None,
        "symbols": len(SYMBOLS),
    }
    write_report(out_dir, summary, meta)
    print(f"overlap_status {summary.status} {summary.overlap}", flush=True)
    return summary


def main() -> None:
    folder = Path(os.environ.get("OVERLAP_OUT", "/home/ubuntu/data_v2_2/reports"))
    env_file = Path(os.environ.get("OVERLAP_ENV", str(ENV_PATH)))
    asyncio.run(capture(folder, env_file))


if __name__ == "__main__":
    main()
