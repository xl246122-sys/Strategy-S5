"""Typed contracts. Amounts, prices, and quantities are Decimal, never float."""

from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from bstocks_v2.contracts.decimals import Money, Price, Quantity

MIN_VIABLE_NOTIONAL = Decimal("120")
PROBABILITY_TOLERANCE = Decimal("0.000000001")


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(StrEnum):
    LIMIT = "LIMIT"
    MARKET = "MARKET"


class OrderStatus(StrEnum):
    NEW = "NEW"
    ACCEPTED = "ACCEPTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    EXPIRED = "EXPIRED"
    REJECTED = "REJECTED"


class ExecutionEvent(StrEnum):
    ORDER_UPDATE = "ORDER_UPDATE"
    ORDER_TERMINAL = "ORDER_TERMINAL"


class TradingSession(StrEnum):
    RTH = "RTH"
    EXTENDED = "EXTENDED"
    H24 = "24H"


class Direction(StrEnum):
    LONG = "LONG"


class FeeSource(StrEnum):
    PROVISIONAL_WITHHELD = "provisional_withheld"
    PROVISIONAL_RATE = "provisional_rate"
    WS_TERMINAL_INITIAL = "ws_terminal_initial"
    REST_DETAIL = "rest_detail"


class FillHorizon(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    price: Price
    horizon_seconds: int = Field(ge=0)
    p_fill: Quantity


class TradeIntent(BaseModel):
    """One candidate long entry. Rejection reasons keep it out of the order path."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    intent_id: str
    created_at_utc: datetime
    data_asof_utc: datetime
    symbol: str
    strategy_id: str
    model_version: str
    regime: str
    features_hash: str
    vwap_intact: bool
    signal_valid_until: datetime
    direction: Direction
    p_win_calibrated: Quantity
    p_sl_calibrated: Quantity
    p_timeout_calibrated: Quantity
    expected_return_bps: Quantity
    expected_net_bps: Quantity
    prediction_interval: tuple[Quantity, Quantity]
    candidate_limit_prices: list[Price]
    p_fill_by_price_horizon: list[FillHorizon]
    entry_price_ceiling: Price
    stop_price: Price
    take_profit_price: Price
    max_holding_seconds: int = Field(gt=0)
    risk_budget_usdc: Money
    min_viable_notional_usdc: Money
    expected_cost_usdc: Money
    requires_fractional: bool
    whole_share_fallback_qty: Quantity
    rejection_reasons: list[str] = Field(default_factory=list)
    data_feed: str
    data_freshness_ms: int = Field(ge=0)

    @model_validator(mode="after")
    def _check_long_geometry(self) -> "TradeIntent":
        if self.symbol.startswith("EQ_"):
            raise ValueError("symbol must already have the EQ_ prefix removed")
        total = self.p_win_calibrated + self.p_sl_calibrated + self.p_timeout_calibrated
        if abs(total - Decimal("1")) > PROBABILITY_TOLERANCE:
            raise ValueError("calibrated probabilities must sum to 1")
        if not self.stop_price < self.entry_price_ceiling < self.take_profit_price:
            raise ValueError("stop_price < entry_price_ceiling < take_profit_price")
        if self.min_viable_notional_usdc < MIN_VIABLE_NOTIONAL:
            raise ValueError("min_viable_notional_usdc cannot be set below 120")
        return self


class CanonicalOrder(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    order_id: str
    client_order_id: str | None
    symbol: str
    wire_quote: str
    side: Side
    order_type: OrderType
    status: OrderStatus
    execution_event: ExecutionEvent | None
    is_terminal: bool
    session: TradingSession | None
    quantity: Quantity
    limit_price: Price | None
    filled_qty: Quantity
    filled_notional: Money
    total_cost_raw: Money | None
    realized_commission: Money
    provisional_reserved_fee: Money | None
    fee_is_final: bool
    fee_source: FeeSource


class CanonicalPosition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    symbol: str
    quantity: Quantity
    average_price: Price
    wallet_type: str = "CARD"
    quote_asset: str = "USDC"

    @model_validator(mode="after")
    def _long_only_card(self) -> "CanonicalPosition":
        if self.quantity < 0:
            raise ValueError("short positions are forbidden")
        if self.wallet_type != "CARD" or self.quote_asset != "USDC":
            raise ValueError("positions are CARD / USDC only")
        return self


class AccountSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    equity_usdc: Money
    hwm_equity_usdc: Money
    available_usdc: Money
    locked_buy_principal_usdc: Money
    halt_new_entries: bool
    positions: list[CanonicalPosition]


class OrderDraft(BaseModel):
    """Fields a caller wants to send. The guard rejects anything the matrix forbids."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    symbol: str
    side: Side
    order_type: OrderType
    client_order_id: str
    quantity: Quantity | None = None
    price: Price | None = None
    notional: Money | None = None
    trading_session: TradingSession | None = None
    quote_asset: str | None = None
    wallet_type: str | None = None
    tokenize: bool | None = None
    is_opening: bool = True
    confirmed_long_qty: Quantity = Field(default=Decimal("0"))
    gross_take_profit_usdc: Money | None = None
    round_trip_friction_usdc: Money | None = None
