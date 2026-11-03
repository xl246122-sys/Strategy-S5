"""HTTP helpers. An empty 200 body is a valid halted-symbol response, not a JSON error."""

import json
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol
from urllib import error, request


class EmptyBody:
    """HTTP 200 with ``b''``. Callers must not pass this to ``json.loads``."""


@dataclass(frozen=True)
class HttpResult:
    status: int
    elapsed_ms: float
    payload: dict[str, object] | list[object] | EmptyBody
    raw_length: int


class Transport(Protocol):
    def exchange(self, method: str, url: str, headers: dict[str, str]) -> tuple[int, bytes, float]:
        """Return status, body, and elapsed milliseconds."""


class UrllibTransport:
    def __init__(self, timeout: float = 15.0) -> None:
        self.timeout = timeout

    def exchange(self, method: str, url: str, headers: dict[str, str]) -> tuple[int, bytes, float]:
        import time

        req = request.Request(url, method=method, headers=headers)
        started = time.perf_counter()
        try:
            with request.urlopen(req, timeout=self.timeout) as response:
                body = response.read()
                status = int(response.status)
        except error.HTTPError as exc:
            body = exc.read()
            status = int(exc.code)
        elapsed_ms = (time.perf_counter() - started) * 1000
        return status, body, elapsed_ms


def decode_body(status: int, body: bytes, elapsed_ms: float) -> HttpResult:
    if status == 200 and body == b"":
        return HttpResult(status, elapsed_ms, EmptyBody(), 0)
    if not body:
        return HttpResult(status, elapsed_ms, {}, 0)
    try:
        payload = json.loads(body.decode("utf-8"), parse_float=Decimal)
    except json.JSONDecodeError as exc:
        raise ValueError("response was not JSON and was not an empty 200") from exc
    if not isinstance(payload, (dict, list)):
        raise ValueError("JSON response must be an object or a list")
    return HttpResult(status, elapsed_ms, payload, len(body))
