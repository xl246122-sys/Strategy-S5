"""Parse raw ``/ws`` events and combined ``/stream`` envelopes."""

import json
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from bstocks_v2.contracts.decimals import parse_decimal

EventKind = Literal["kline", "quote", "price", "calendar", "tradability", "tradingStatus", "other"]


@dataclass(frozen=True)
class ParsedEvent:
    kind: EventKind
    stream: str | None
    symbol: str | None
    event_time_ms: int | None
    market_phase: str | None
    bar_open_ms: int | None
    bar_close_ms: int | None
    closed: bool | None
    open: Decimal | None
    high: Decimal | None
    low: Decimal | None
    close: Decimal | None
    volume: Decimal | None
    vwap: Decimal | None
    vwap_source: str | None
    quote_time_ms: int | None
    quote_time_source: str | None
    bid: Decimal | None
    ask: Decimal | None
    raw_state: str | None
    phases: tuple[tuple[str, str], ...] = ()


def parse_ws_message(raw: str) -> ParsedEvent:
    payload = json.loads(raw, parse_float=Decimal)
    if not isinstance(payload, dict):
        raise ValueError("websocket payload must be an object")
    stream = payload.get("stream")
    if isinstance(stream, str) and isinstance(payload.get("data"), dict):
        data: dict[str, object] = payload["data"]
        outer = _optional_int(payload.get("E"))
        return _event(data, stream, outer)
    return _event(payload, None, None)


def _event(data: dict[str, object], stream: str | None, outer_event_ms: int | None) -> ParsedEvent:
    kind = str(data.get("e") or _kind_from_stream(stream) or "other")
    event_time = _optional_int(data.get("E")) or outer_event_ms
    symbol = _symbol(data, stream)
    if kind == "kline":
        return _kline(data, stream, symbol, event_time)
    if kind == "quote":
        return _quote(data, stream, symbol, event_time)
    if kind == "price":
        phases = _price_phases(data.get("rates"))
        distinct = {phase for _, phase in phases}
        top_phase = data.get("mp")
        if isinstance(top_phase, str):
            phase_text = top_phase
        elif len(distinct) == 1:
            phase_text = next(iter(distinct))
        else:
            phase_text = None
        return _blank(
            kind="price",
            stream=stream,
            symbol=symbol,
            event_time_ms=event_time,
            market_phase=phase_text,
            phases=phases,
        )
    if kind in {"calendar", "tradability", "tradingStatus"}:
        state = data.get("tradability") or data.get("tradingStatus") or data.get("status")
        state_kind: EventKind = "calendar"
        if kind == "tradability":
            state_kind = "tradability"
        elif kind == "tradingStatus":
            state_kind = "tradingStatus"
        return _blank(
            kind=state_kind,
            stream=stream,
            symbol=symbol,
            event_time_ms=event_time,
            raw_state=str(state) if state is not None else None,
        )
    return _blank(kind="other", stream=stream, symbol=symbol, event_time_ms=event_time)


def _kline(
    data: dict[str, object],
    stream: str | None,
    symbol: str | None,
    event_time: int | None,
) -> ParsedEvent:
    nested = data.get("k")
    if not isinstance(nested, dict):
        raise ValueError("kline payload has no k object")
    high = parse_decimal(nested["h"])
    low = parse_decimal(nested["l"])
    close = parse_decimal(nested["c"])
    volume = parse_decimal(nested.get("v", "0"))
    wire_vwap = nested.get("vw")
    if wire_vwap is None:
        turnover = nested.get("q")
        if turnover is not None and volume > 0:
            vwap = parse_decimal(turnover) / volume
            source = "turnover"
        else:
            vwap = (high + low + close) / Decimal(3)
            source = "typical"
    else:
        vwap = parse_decimal(wire_vwap)
        source = "wire"
    bar_symbol = nested.get("s")
    return ParsedEvent(
        kind="kline",
        stream=stream,
        symbol=str(bar_symbol) if isinstance(bar_symbol, str) else symbol,
        event_time_ms=event_time,
        market_phase=None,
        bar_open_ms=_optional_int(nested.get("t")),
        bar_close_ms=_optional_int(nested.get("ct")),
        closed=bool(nested.get("x")) if isinstance(nested.get("x"), bool) else None,
        open=parse_decimal(nested["o"]),
        high=high,
        low=low,
        close=close,
        volume=volume,
        vwap=vwap,
        vwap_source=source,
        quote_time_ms=None,
        quote_time_source=None,
        bid=None,
        ask=None,
        raw_state=None,
    )


def _quote(
    data: dict[str, object],
    stream: str | None,
    symbol: str | None,
    event_time: int | None,
) -> ParsedEvent:
    quote_time = _optional_int(data.get("T"))
    source = "quote"
    if quote_time is None:
        quote_time = event_time
        source = "event_time"
    return ParsedEvent(
        kind="quote",
        stream=stream,
        symbol=symbol,
        event_time_ms=event_time,
        market_phase=None,
        bar_open_ms=None,
        bar_close_ms=None,
        closed=None,
        open=None,
        high=None,
        low=None,
        close=None,
        volume=None,
        vwap=None,
        vwap_source=None,
        quote_time_ms=quote_time,
        quote_time_source=source,
        bid=parse_decimal(data["bp"]) if "bp" in data else None,
        ask=parse_decimal(data["ap"]) if "ap" in data else None,
        raw_state=None,
    )


def _blank(
    *,
    kind: EventKind,
    stream: str | None,
    symbol: str | None,
    event_time_ms: int | None,
    market_phase: str | None = None,
    raw_state: str | None = None,
    phases: tuple[tuple[str, str], ...] = (),
) -> ParsedEvent:
    return ParsedEvent(
        kind=kind,
        stream=stream,
        symbol=symbol,
        event_time_ms=event_time_ms,
        market_phase=market_phase,
        bar_open_ms=None,
        bar_close_ms=None,
        closed=None,
        open=None,
        high=None,
        low=None,
        close=None,
        volume=None,
        vwap=None,
        vwap_source=None,
        quote_time_ms=None,
        quote_time_source=None,
        bid=None,
        ask=None,
        raw_state=raw_state,
        phases=phases,
    )


def _price_phases(rates: object) -> tuple[tuple[str, str], ...]:
    if not isinstance(rates, list):
        return ()
    phases: list[tuple[str, str]] = []
    for item in rates:
        if not isinstance(item, dict):
            continue
        symbol = item.get("s")
        phase = item.get("mp")
        if isinstance(symbol, str) and isinstance(phase, str):
            phases.append((symbol.removeprefix("EQ_"), phase))
    return tuple(phases)


def _symbol(data: dict[str, object], stream: str | None) -> str | None:
    raw = data.get("s") or data.get("symbol")
    if isinstance(raw, str) and raw:
        return raw.removeprefix("EQ_")
    if stream and "@" in stream:
        return stream.split("@", 1)[0]
    return None


def _kind_from_stream(stream: str | None) -> str | None:
    if stream is None:
        return None
    if stream in {"price", "calendar"}:
        return stream
    if "@" not in stream:
        return None
    suffix = stream.split("@", 1)[1]
    return {"kline_5m": "kline", "quote": "quote"}.get(suffix, suffix)


def _optional_int(value: object) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, Decimal) and value == int(value):
        return int(value)
    return None
