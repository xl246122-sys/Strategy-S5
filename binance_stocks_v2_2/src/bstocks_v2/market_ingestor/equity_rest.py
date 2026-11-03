"""Read-only Binance Stocks REST. Order placement is rejected by the constitution guard."""

import hashlib
import hmac
from dataclasses import dataclass
from time import time
from urllib.parse import urlencode

from bstocks_v2.config.credentials import ApiPair
from bstocks_v2.constitution.guard import ConstitutionGuard
from bstocks_v2.contracts.errors import OrderRejected
from bstocks_v2.market_ingestor.http_util import EmptyBody, HttpResult, Transport, decode_body

REST_HOST = "https://api.binance.com"
TERMINAL_ORDER_STATUSES = frozenset(
    {"FILLED", "PARTIALLY_FILLED", "CANCELED", "EXPIRED", "REJECTED"}
)
_LISTEN_KEY_REBUILD = frozenset({-1125, -3038})
_SIGNATURE_RETRY = frozenset({-1022, -1102, -2014, -2015})


@dataclass(frozen=True)
class ApiError(Exception):
    code: int
    message: str

    def __str__(self) -> str:
        return f"{self.code} {self.message}"


class EquityReadClient:
    def __init__(
        self,
        credentials: ApiPair,
        transport: Transport,
        guard: ConstitutionGuard | None = None,
    ) -> None:
        self._credentials = credentials
        self._transport = transport
        self._guard = guard or ConstitutionGuard()

    def exchange_info(self) -> HttpResult:
        return self._public_get("/sapi/v1/equity/market/exchangeInfo")

    def tokenized_assets(self) -> HttpResult:
        return self._public_get("/sapi/v1/equity/market/tokenized-assets")

    def quote(self, symbol: str) -> HttpResult:
        return self._public_get("/sapi/v1/equity/market/quote", {"symbol": symbol})

    def open_orders(self) -> HttpResult:
        return self._signed("GET", "/sapi/v1/equity/order/open-orders")

    def order_detail(
        self,
        *,
        order_id: str | None = None,
        client_order_id: str | None = None,
    ) -> HttpResult:
        params: dict[str, str] = {}
        if order_id:
            params["orderId"] = order_id
        if client_order_id:
            params["clientOrderId"] = client_order_id
        return self._signed("GET", "/sapi/v1/equity/order/detail", params)

    def order_history(
        self,
        order_status: str,
        start_time_ms: int | None = None,
        end_time_ms: int | None = None,
    ) -> HttpResult:
        if order_status not in TERMINAL_ORDER_STATUSES:
            raise OrderRejected("ORDER_HISTORY_STATUS")
        params = {"orderStatus": order_status}
        _window(params, start_time_ms, end_time_ms)
        return self._signed("GET", "/sapi/v1/equity/order/history", params)

    def trade_history(
        self,
        symbol: str | None = None,
        start_time_ms: int | None = None,
        end_time_ms: int | None = None,
    ) -> HttpResult:
        params = {"symbol": symbol} if symbol else {}
        _window(params, start_time_ms, end_time_ms)
        return self._signed("GET", "/sapi/v1/equity/trade/history", params)

    def funding_assets(self) -> HttpResult:
        return self._signed("POST", "/sapi/v1/asset/get-funding-asset")

    def create_listen_key(self) -> str:
        result = self._signed("POST", "/sapi/v1/equity/listenKey")
        return _listen_key(result)

    def _public_get(self, path: str, params: dict[str, str] | None = None) -> HttpResult:
        self._guard.check_endpoint("GET", path)
        url = REST_HOST + path
        if params:
            url = f"{url}?{urlencode(params)}"
        try:
            return self._send("GET", url, path)
        except ApiError as exc:
            if exc.code not in _SIGNATURE_RETRY:
                raise
            return self._signed("GET", path, params)

    def _signed(self, method: str, path: str, params: dict[str, str] | None = None) -> HttpResult:
        self._guard.check_endpoint(method, path)
        query = dict(params or {})
        query["timestamp"] = str(int(time() * 1000))
        query["recvWindow"] = "5000"
        encoded = urlencode(query)
        signature = hmac.new(
            self._credentials.secret.encode("utf-8"),
            encoded.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        url = f"{REST_HOST}{path}?{encoded}&signature={signature}"
        return self._send(method, url, path)

    def _send(self, method: str, url: str, path: str) -> HttpResult:
        status, body, elapsed = self._transport.exchange(
            method,
            url,
            {"X-MBX-APIKEY": self._credentials.key},
        )
        result = decode_body(status, body, elapsed)
        payload = result.payload
        if isinstance(payload, dict) and "code" in payload and "msg" in payload:
            raw_code = payload["code"]
            if type(raw_code) is int and raw_code < 0:
                raise ApiError(raw_code, str(payload["msg"]))
        return result


def _window(params: dict[str, str], start_time_ms: int | None, end_time_ms: int | None) -> None:
    if start_time_ms is not None:
        params["startTime"] = str(start_time_ms)
    if end_time_ms is not None:
        params["endTime"] = str(end_time_ms)


def needs_listen_key_rebuild(error: ApiError) -> bool:
    return error.code in _LISTEN_KEY_REBUILD


def _listen_key(result: HttpResult) -> str:
    if isinstance(result.payload, dict):
        value = result.payload.get("listenKey")
        if isinstance(value, str) and value:
            return value
    raise ApiError(-1, "listenKey missing")


def payload_is_empty(result: HttpResult) -> bool:
    return isinstance(result.payload, EmptyBody)


def as_object(result: HttpResult) -> dict[str, object]:
    if isinstance(result.payload, dict):
        return result.payload
    return {}
