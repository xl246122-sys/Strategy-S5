"""Local map from exchange order id to the clientOrderId the WebSocket does not send."""

import re

_CLIENT_ORDER_ID = re.compile(r"^[a-zA-Z0-9\-_]{32,36}$")


class OrderIdBinder:
    def __init__(self) -> None:
        self._by_order: dict[str, str] = {}
        self._by_client: dict[str, str] = {}

    def bind(self, client_order_id: str, order_id: str) -> None:
        if _CLIENT_ORDER_ID.fullmatch(client_order_id) is None:
            raise ValueError("clientOrderId must match ^[a-zA-Z0-9-_]{32,36}$")
        if not order_id:
            raise ValueError("order_id is required")
        self._by_order[order_id] = client_order_id
        self._by_client[client_order_id] = order_id

    def client_id_for(self, order_id: str) -> str | None:
        return self._by_order.get(order_id)

    def order_id_for(self, client_order_id: str) -> str | None:
        return self._by_client.get(client_order_id)
