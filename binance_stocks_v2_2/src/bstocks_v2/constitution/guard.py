"""Hardcoded trading rules. A config file cannot turn these off."""

import re
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from bstocks_v2.contracts.decimals import parse_decimal
from bstocks_v2.contracts.errors import (
    ConstitutionBypassError,
    EndpointForbiddenError,
    LiveTradingDisabledError,
    OrderRejected,
    ShortSellingForbiddenError,
)
from bstocks_v2.contracts.models import OrderDraft, OrderType, Side

NEW_YORK = ZoneInfo("America/New_York")
CLIENT_ORDER_ID = re.compile(r"^[a-zA-Z0-9\-_]{32,36}$")
MIN_VIABLE_NOTIONAL = Decimal("120")
MAX_OPEN_POSITIONS = 5
HWM_RETAINED = Decimal("0.92")
MAX_FRICTION_RATIO = Decimal("0.20")
SESSION_POLICY_GLOBAL = "GLOBAL"
SESSION_POLICY_S5_RESEARCH = "S5_RESEARCH"
_SESSION_POLICIES = frozenset({SESSION_POLICY_GLOBAL, SESSION_POLICY_S5_RESEARCH})

_IMMUTABLE: dict[str, object] = {
    "live_trade_enabled": False,
    "live_enabled": False,
    "s4_live_enabled": False,
    "s5_live_enabled": False,
    "max_open_positions": MAX_OPEN_POSITIONS,
    "hwm_drawdown": Decimal("0.08"),
    "min_viable_notional_usdc": MIN_VIABLE_NOTIONAL,
    "max_friction_ratio": MAX_FRICTION_RATIO,
    "quote_asset": "USDC",
    "wallet_type": "CARD",
    "tokenize": False,
}
_BLOCKED_FRAGMENTS = (
    "/tokenized/mint",
    "/tokenized/redeem",
    "/fapi/",
    "/dapi/",
    "/sapi/v1/margin",
    "/api/v3/",
)
_WRITE_METHODS = {"POST", "PUT", "DELETE", "PATCH"}


def live_trade_enabled() -> bool:
    """Literal false. This is not read from disk."""

    return False


class ConstitutionGuard:
    def reject_config_override(self, incoming: dict[str, object]) -> None:
        for key, expected in _IMMUTABLE.items():
            if key not in incoming:
                continue
            if not _same(incoming[key], expected):
                raise ConstitutionBypassError(key)

    def check_endpoint(self, method: str, path: str) -> None:
        verb = method.upper()
        host, clean = _split_url(path)
        if any(fragment in clean for fragment in _BLOCKED_FRAGMENTS):
            raise EndpointForbiddenError(clean)
        if host == "data.alpaca.markets":
            if verb != "GET":
                raise EndpointForbiddenError(clean)
            return
        if host not in {
            "",
            "api.binance.com",
            "api1.binance.com",
            "api2.binance.com",
            "api3.binance.com",
            "api4.binance.com",
        }:
            raise EndpointForbiddenError(clean)
        if clean == "/sapi/v1/asset/get-funding-asset":
            if verb not in {"GET", "POST"}:
                raise EndpointForbiddenError(clean)
            return
        if not clean.startswith("/sapi/v1/equity/"):
            raise EndpointForbiddenError(clean)
        if verb in _WRITE_METHODS:
            if verb == "POST" and clean == "/sapi/v1/equity/listenKey":
                return
            if not live_trade_enabled():
                raise LiveTradingDisabledError(clean)

    def check_price(self, value: object) -> Decimal:
        try:
            price = parse_decimal(value)
        except (TypeError, ValueError, ArithmeticError) as exc:
            raise OrderRejected("PRICE_PRECISION") from exc
        exponent = price.as_tuple().exponent
        if not isinstance(exponent, int) or exponent < -2:
            raise OrderRejected("PRICE_PRECISION")
        return price

    def check_order(
        self,
        draft: OrderDraft,
        *,
        now_et: datetime,
        is_half_day: bool,
        open_positions: int,
        pending_buys: int,
        equity: Decimal,
        hwm: Decimal,
        entries_halted: bool,
        session_policy: str = SESSION_POLICY_GLOBAL,
    ) -> None:
        if session_policy not in _SESSION_POLICIES:
            raise OrderRejected("ENTRY_POLICY")
        if session_policy == SESSION_POLICY_S5_RESEARCH:
            if live_trade_enabled():
                raise OrderRejected("S5_LIVE_FORBIDDEN")
            if not draft.research_only:
                raise OrderRejected("ENTRY_POLICY")
        self._field_matrix(draft)
        if CLIENT_ORDER_ID.fullmatch(draft.client_order_id) is None:
            raise OrderRejected("CLIENT_ORDER_ID")
        if draft.price is not None:
            self.check_price(draft.price)
        if draft.side is Side.SELL and (
            draft.quantity is None or draft.quantity > draft.confirmed_long_qty
        ):
            raise ShortSellingForbiddenError("SHORT")
        if draft.is_opening and draft.side is Side.BUY:
            self._opening_buy_limits(
                draft,
                now_et=now_et,
                is_half_day=is_half_day,
                open_positions=open_positions,
                pending_buys=pending_buys,
                equity=equity,
                hwm=hwm,
                entries_halted=entries_halted,
                session_policy=session_policy,
            )

    def _field_matrix(self, draft: OrderDraft) -> None:
        if draft.side is Side.BUY:
            if draft.quote_asset != "USDC":
                raise OrderRejected("QUOTE_ASSET")
            if draft.wallet_type != "CARD":
                raise OrderRejected("WALLET_TYPE")
            if draft.tokenize is not False:
                raise OrderRejected("TOKENIZE")
            if draft.order_type is OrderType.MARKET:
                raise OrderRejected("BUY_MARKET_FORBIDDEN")
        if draft.order_type is OrderType.LIMIT:
            if draft.price is None or draft.quantity is None or draft.trading_session is None:
                raise OrderRejected("FIELD_MATRIX")
            if draft.notional is not None:
                raise OrderRejected("FIELD_MATRIX")
            return
        if draft.side is Side.SELL and draft.order_type is OrderType.MARKET:
            if (
                draft.quantity is None
                or draft.price is not None
                or draft.notional is not None
                or draft.trading_session is not None
            ):
                raise OrderRejected("FIELD_MATRIX")
            return
        raise OrderRejected("FIELD_MATRIX")

    def _opening_buy_limits(
        self,
        draft: OrderDraft,
        *,
        now_et: datetime,
        is_half_day: bool,
        open_positions: int,
        pending_buys: int,
        equity: Decimal,
        hwm: Decimal,
        entries_halted: bool,
        session_policy: str,
    ) -> None:
        if session_policy == SESSION_POLICY_GLOBAL and not entry_window_open(now_et, is_half_day):
            raise OrderRejected("NON_RTH")
        if open_positions + pending_buys >= MAX_OPEN_POSITIONS:
            raise OrderRejected("MAX_POSITIONS")
        if entries_halted or equity <= hwm * HWM_RETAINED:
            raise OrderRejected("HWM_LOCK")
        if draft.price is None or draft.quantity is None:
            raise OrderRejected("FIELD_MATRIX")
        if draft.price * draft.quantity < MIN_VIABLE_NOTIONAL:
            raise OrderRejected("NOTIONAL_TOO_SMALL")
        gross = draft.gross_take_profit_usdc
        friction = draft.round_trip_friction_usdc
        if gross is not None and friction is not None:
            if gross <= 0 or friction / gross > MAX_FRICTION_RATIO:
                raise OrderRejected("FRICTION")


def entry_window_open(now_et: datetime, is_half_day: bool) -> bool:
    if now_et.tzinfo is None:
        raise OrderRejected("NAIVE_DATETIME")
    local = now_et.astimezone(NEW_YORK)
    if local.weekday() >= 5:
        return False
    start = local.replace(hour=9, minute=30, second=0, microsecond=0)
    hour, minute = (12, 45) if is_half_day else (15, 45)
    cutoff = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return start <= local < cutoff


def flatten_deadline(now_et: datetime, is_half_day: bool) -> datetime:
    local = now_et.astimezone(NEW_YORK)
    hour, minute = (12, 55) if is_half_day else (15, 55)
    return local.replace(hour=hour, minute=minute, second=0, microsecond=0)


def _split_url(path: str) -> tuple[str, str]:
    raw = path.split("?", 1)[0]
    host = ""
    clean = raw
    if "://" in raw:
        after = raw.split("://", 1)[1]
        slash = after.find("/")
        host = after if slash < 0 else after[:slash]
        clean = "/" if slash < 0 else after[slash:]
    return host.lower(), clean.rstrip("/") or "/"


def _same(actual: object, expected: object) -> bool:
    if isinstance(expected, bool):
        return actual is expected
    if isinstance(expected, Decimal):
        try:
            return parse_decimal(actual) == expected
        except (TypeError, ValueError, ArithmeticError):
            return False
    return actual == expected
