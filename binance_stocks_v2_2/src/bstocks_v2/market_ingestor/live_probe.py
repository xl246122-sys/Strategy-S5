"""Bounded read-only probe. It never places or cancels an order and never prints secrets."""

import asyncio
import json
import re
import time
from collections.abc import Callable

from bstocks_v2.config.credentials import alpaca_from_env, binance_from_env
from bstocks_v2.market_ingestor.alpaca_backfill import AlpacaSipBackfill
from bstocks_v2.market_ingestor.equity_rest import ApiError, EquityReadClient, payload_is_empty
from bstocks_v2.market_ingestor.http_util import HttpResult, UrllibTransport
from bstocks_v2.market_ingestor.messages import parse_ws_message
from bstocks_v2.market_ingestor.shards import (
    combined_url,
    l1_streams,
    l3_commands,
    plan_l2,
    raw_ws_url,
    select_l2_symbols,
)

_SECRET = re.compile(r"(signature=)[0-9a-fA-F]+")


def main() -> None:
    client = EquityReadClient(binance_from_env(), UrllibTransport(timeout=30))
    report: dict[str, object] = {"rest": {}, "ws": {}}
    rest = report["rest"]
    assert isinstance(rest, dict)
    _time_repeat(rest, "exchangeInfo", client.exchange_info, 5)
    _once(rest, "tokenizedAssets", client.tokenized_assets)
    _once(rest, "quoteAapl", lambda: client.quote("AAPL"))
    _once(rest, "quoteUnknown", lambda: client.quote("NOTAREALSYMBOLZZZ"))
    now_ms = int(time.time() * 1000)
    start_ms = now_ms - 7 * 24 * 60 * 60 * 1000
    _once(rest, "openOrders", client.open_orders)
    _once(rest, "orderHistoryFilled", lambda: client.order_history("FILLED", start_ms, now_ms))
    _once(rest, "tradeHistory", lambda: client.trade_history(None, start_ms, now_ms))
    _once(rest, "funding", client.funding_assets)
    try:
        listen_key = client.create_listen_key()
        rest["listenKey"] = {"created": True, "length": len(listen_key)}
    except ApiError as exc:
        rest["listenKey"] = {"created": False, "error": exc.code, "message": exc.message}
    except Exception as exc:  # noqa: BLE001
        rest["listenKey"] = {"created": False, "error": _safe(exc)}
    symbols: list[str] = []
    try:
        symbols = select_l2_symbols(client.exchange_info().payload)
    except Exception as exc:  # noqa: BLE001
        report["l2_symbols_error"] = _safe(exc)
    report["l2_symbol_count"] = len(symbols)
    try:
        alpaca = AlpacaSipBackfill(alpaca_from_env())
        minutes = alpaca.fetch_minutes("AAPL", "2026-10-30T13:30:00Z", "2026-10-30T13:40:00Z")
        report["alpaca"] = {"bars": len(minutes), "feed": "sip"}
    except Exception as exc:  # noqa: BLE001
        report["alpaca"] = {"error": _safe(exc)}
    report["ws"] = asyncio.run(_probe_sockets(symbols))
    print(json.dumps(report, default=str))


def _once(bucket: dict[str, object], name: str, call: Callable[[], object]) -> None:
    try:
        result = call()
        bucket[name] = _summarize(result)
    except ApiError as exc:
        bucket[name] = {"error": exc.code, "message": exc.message}
    except Exception as exc:  # noqa: BLE001
        bucket[name] = {"error": _safe(exc)}


def _time_repeat(
    bucket: dict[str, object],
    name: str,
    call: Callable[[], object],
    times: int,
) -> None:
    samples: list[object] = []
    last: dict[str, object] = {}
    for _ in range(times):
        _once(last, "item", call)
        item = last.get("item")
        if isinstance(item, dict):
            samples.append(item.get("ms"))
            last_status: object = item
        else:
            last_status = {}
    bucket[name] = {"samples_ms": samples, "last": last_status}


def _payload_kind(result: HttpResult) -> str:
    if payload_is_empty(result):
        return "empty"
    if isinstance(result.payload, list):
        return "list"
    return "object"


def _summarize(result: object) -> dict[str, object]:
    if not isinstance(result, HttpResult):
        return {"type": type(result).__name__}
    payload = result.payload
    summary: dict[str, object] = {
        "status": result.status,
        "ms": round(result.elapsed_ms, 2),
        "empty_body": payload_is_empty(result),
        "bytes": result.raw_length,
        "payload_kind": _payload_kind(result),
    }
    if isinstance(payload, dict):
        summary["keys"] = sorted(str(key) for key in payload.keys())[:12]
        if "code" in payload:
            summary["code"] = payload.get("code")
    if isinstance(payload, list):
        summary["count"] = len(payload)
        if payload and isinstance(payload[0], dict) and "asset" in payload[0]:
            summary["funding"] = _funding_brief(payload)
    return summary


def _funding_brief(payload: list[object]) -> dict[str, object]:
    assets: list[str] = []
    usdc = False
    eq = 0
    for item in payload:
        if not isinstance(item, dict):
            continue
        asset = item.get("asset")
        if not isinstance(asset, str):
            continue
        assets.append(asset)
        if asset.upper() == "USDC":
            usdc = True
        if asset.upper().startswith("EQ_"):
            eq += 1
    return {"assets": assets[:20], "usdc_present": usdc, "eq_count": eq}


def _safe(exc: BaseException) -> str:
    return _SECRET.sub(r"\1redacted", f"{type(exc).__name__}: {exc}")[:180]


async def _probe_sockets(symbols: list[str]) -> dict[str, object]:
    l1 = await _hold_and_read(combined_url(l1_streams()), read=True)
    l2: dict[str, object] = {"shards": 0, "opened": 0, "error": ""}
    if len(symbols) >= 40:
        shards = plan_l2(symbols[:300])
        jobs = [_hold_and_read(combined_url(shards[0]), read=True)]
        jobs.extend(_hold_and_read(combined_url(shard), read=False) for shard in shards[1:])
        results = await asyncio.gather(*jobs)
        l2 = {
            "shards": len(shards),
            "max_streams": max(len(shard) for shard in shards),
            "opened": sum(1 for item in results if item.get("opened")),
            "pong": sum(1 for item in results if item.get("pong")),
            "errors": [item.get("error") for item in results if item.get("error")],
        }
    l3 = await _probe_l3()
    return {"l1": l1, "l2": l2, "l3": l3}


async def _hold_and_read(url: str, *, read: bool) -> dict[str, object]:
    import websockets

    socket = None
    try:
        socket = await websockets.connect(url, open_timeout=15, close_timeout=2, max_size=1_048_576)
        out: dict[str, object] = {"opened": True, "messages": 0, "pong": False, "error": ""}
        if read:
            try:
                raw = await asyncio.wait_for(socket.recv(), timeout=8)
                text = raw if isinstance(raw, str) else raw.decode()
                event = parse_ws_message(text)
                out["messages"] = 1
                out["kind"] = event.kind
                out["market_phase"] = event.market_phase
                out["vwap_source"] = event.vwap_source
                out["fields"] = _field_names(text)
                out["phase_count"] = len(event.phases)
                out["phases"] = sorted({phase for _, phase in event.phases})
            except TimeoutError:
                out["messages"] = 0
        else:
            await asyncio.sleep(15)
        pong = await socket.ping()
        await asyncio.wait_for(pong, timeout=5)
        out["pong"] = True
        return out
    except Exception as exc:  # noqa: BLE001
        return {"opened": socket is not None, "pong": False, "error": _safe(exc), "messages": 0}
    finally:
        if socket is not None:
            await socket.close()


async def _probe_l3() -> dict[str, object]:
    import websockets

    socket = None
    try:
        socket = await websockets.connect(
            raw_ws_url(),
            open_timeout=15,
            close_timeout=2,
            max_size=1_048_576,
        )
        sent: list[str] = []
        replies: list[str] = []
        for command in l3_commands(set(), {"AAPL", "MSFT"}, 1):
            await socket.send(json.dumps(command))
            sent.append(str(command["method"]))
            raw = await asyncio.wait_for(socket.recv(), timeout=8)
            replies.append(_reply_id(raw))
        for command in l3_commands({"AAPL", "MSFT"}, {"AAPL"}, 3):
            await socket.send(json.dumps(command))
            sent.append(str(command["method"]))
            raw = await asyncio.wait_for(socket.recv(), timeout=8)
            replies.append(_reply_id(raw))
        return {"opened": True, "sent": sent, "replies": replies, "error": ""}
    except Exception as exc:  # noqa: BLE001
        return {"opened": socket is not None, "sent": [], "replies": [], "error": _safe(exc)}
    finally:
        if socket is not None:
            await socket.close()


def _field_names(raw: str) -> list[str]:
    payload = json.loads(raw)
    data = payload
    if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
        data = payload["data"]
    if not isinstance(data, dict):
        return []
    return sorted(str(key) for key in data)


def _reply_id(raw: object) -> str:
    if isinstance(raw, str):
        text = raw
    elif isinstance(raw, bytes):
        text = raw.decode()
    else:
        return "unparsed"
    payload = json.loads(text)
    if isinstance(payload, dict) and "id" in payload:
        return f"id={payload['id']}"
    return parse_ws_message(text).kind


if __name__ == "__main__":
    main()
