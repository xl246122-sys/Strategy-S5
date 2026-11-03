"""Stable 35-character client ids. A retry recomputes the same id."""

import hashlib

from bstocks_v2.constitution.guard import CLIENT_ORDER_ID


def client_order_id(symbol: str, role: str) -> str:
    if not symbol or symbol.startswith("EQ_") or not role:
        raise ValueError("symbol")
    digest = hashlib.sha256(f"v22-stage12|{symbol}|{role}".encode()).hexdigest()
    client_id = f"g12{digest[:32]}"
    if CLIENT_ORDER_ID.fullmatch(client_id) is None:
        raise ValueError("CLIENT_ORDER_ID")
    return client_id
