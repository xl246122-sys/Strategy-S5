"""Map a clock label and an exchangeInfo row onto one order session.

A missing flag is not false and not true. The order is refused.
"""

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from bstocks_v2.contracts.decimals import parse_decimal
from bstocks_v2.contracts.models import OrderType, Side, TradingSession
from bstocks_v2.sessions.calendar import SessionLabel, TradingCalendar

_FILTERS = (
    "stepSize",
    "minQty",
    "minNotional",
    "multiplierUp",
    "multiplierDown",
)
_FLAGS = ("extendedSession", "overnightSupported", "fractionableEh")


@dataclass(frozen=True)
class OrderRequest:
    symbol: str
    now_et: datetime
    side: Side
    order_type: OrderType
    trading_session: TradingSession | None


@dataclass(frozen=True)
class EligibilityDecision:
    allowed: bool
    reason: str
    label: SessionLabel
    snapshot_hash: str


class SessionEligibility:
    def __init__(self, calendar: TradingCalendar) -> None:
        self._calendar = calendar

    def decide(self, row: object, request: OrderRequest) -> EligibilityDecision:
        label = self._calendar.label(request.now_et)
        parsed, problem = _parse_row(row)
        digest = _hash(parsed)
        if request.symbol.startswith("EQ_"):
            return _no(label, digest, "EQ_PREFIX")
        if problem is not None:
            return _no(label, digest, problem)
        if parsed["symbol"] != request.symbol:
            return _no(label, digest, "SYMBOL_MISMATCH")
        if label is SessionLabel.NON_SESSION:
            return _no(label, digest, "MARKET_CLOSED")
        if parsed["tradability"] != "BUY_SELL":
            return _no(label, digest, "NOT_TRADABLE")
        if request.order_type is OrderType.MARKET:
            return _market(request, label, digest)
        return _session_gate(parsed, request, label, digest)


def _market(request: OrderRequest, label: SessionLabel, digest: str) -> EligibilityDecision:
    """A market order is never an after-hours exit. RTH sell-market keeps an empty session."""

    if label is not SessionLabel.RTH:
        return _no(label, digest, "MARKET_AFTER_HOURS")
    if request.side is Side.BUY:
        return _no(label, digest, "BUY_MARKET_FORBIDDEN")
    if request.trading_session is not None:
        return _no(label, digest, "SESSION_MISMATCH")
    return EligibilityDecision(True, "OK", label, digest)


def _session_gate(
    parsed: dict[str, object],
    request: OrderRequest,
    label: SessionLabel,
    digest: str,
) -> EligibilityDecision:
    if label is SessionLabel.RTH:
        if request.trading_session is not TradingSession.RTH:
            return _no(label, digest, "SESSION_MISMATCH")
        return EligibilityDecision(True, "OK", label, digest)
    if label in {SessionLabel.PRE, SessionLabel.POST}:
        if request.trading_session is not TradingSession.EXTENDED:
            return _no(label, digest, "SESSION_MISMATCH")
        if parsed["extendedSession"] is not True:
            return _no(label, digest, _flag_reason(parsed["extendedSession"], "EXTENDED"))
        return EligibilityDecision(True, "OK", label, digest)
    if request.trading_session is not TradingSession.H24:
        return _no(label, digest, "SESSION_MISMATCH")
    if parsed["overnightSupported"] is not True:
        return _no(label, digest, _flag_reason(parsed["overnightSupported"], "OVERNIGHT"))
    return EligibilityDecision(True, "OK", label, digest)


def _flag_reason(value: object, name: str) -> str:
    if value is False:
        return f"{name}_UNSUPPORTED"
    return "UNKNOWN_ELIGIBILITY"


def _parse_row(row: object) -> tuple[dict[str, object], str | None]:
    blank = _blank()
    if not isinstance(row, dict):
        return blank, "UNKNOWN_ELIGIBILITY"
    symbol = row.get("symbol")
    tradability = row.get("tradability")
    parsed = _blank()
    parsed["symbol"] = symbol if isinstance(symbol, str) else ""
    parsed["tradability"] = tradability if isinstance(tradability, str) else ""
    problem: str | None = None
    if not isinstance(symbol, str) or not symbol:
        problem = "UNKNOWN_ELIGIBILITY"
    if "tradability" not in row or not isinstance(tradability, str):
        problem = "UNKNOWN_ELIGIBILITY"
    for key in _FLAGS:
        if key not in row:
            parsed[key] = None
            problem = problem or "UNKNOWN_ELIGIBILITY"
            continue
        value = row[key]
        if not isinstance(value, bool):
            parsed[key] = None
            problem = "INVALID_FILTER"
            continue
        parsed[key] = value
    for key in _FILTERS:
        if key not in row or row[key] is None:
            problem = problem or "UNKNOWN_ELIGIBILITY"
            continue
        try:
            number = parse_decimal(row[key])
        except (TypeError, ValueError, ArithmeticError):
            problem = "INVALID_FILTER"
            continue
        if key == "minNotional":
            if number < 0:
                problem = "INVALID_FILTER"
                continue
        elif number <= 0:
            problem = "INVALID_FILTER"
            continue
        parsed[key] = format(number, "f")
    return parsed, problem


def _blank() -> dict[str, object]:
    fields: dict[str, object] = {"symbol": "", "tradability": ""}
    for key in _FLAGS:
        fields[key] = None
    for key in _FILTERS:
        fields[key] = None
    return fields


def _hash(parsed: dict[str, object]) -> str:
    payload = json.dumps(parsed, sort_keys=True, separators=(",", ":"), default=_json)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _json(value: object) -> str:
    if isinstance(value, Decimal):
        return format(value, "f")
    raise TypeError("eligibility snapshot is not JSON")


def _no(label: SessionLabel, digest: str, reason: str) -> EligibilityDecision:
    return EligibilityDecision(False, reason, label, digest)
