"""Replay configuration and the audit row written for every intent."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from bstocks_v2.contracts.decimals import parse_decimal
from bstocks_v2.contracts.errors import ConstitutionBypassError

MAX_POSITION_FRACTION = Decimal("0.20")


class FillMode(StrEnum):
    LOOSE = "loose"
    CONSERVATIVE = "conservative"


def _decimal(value: object, name: str) -> Decimal:
    try:
        return parse_decimal(value)
    except (TypeError, ValueError, ArithmeticError) as exc:
        raise ValueError(name) from exc


@dataclass(frozen=True)
class EngineConfig:
    """Research knobs. Position cap, live trading, and the 120 USDC floor are not knobs."""

    mode: FillMode
    penetration: str = "fixed"
    fixed_delta: Decimal = Decimal("0.02")
    atr_fraction: Decimal = Decimal("0.05")
    spread_multiple: Decimal = Decimal("1")
    half_spread: Decimal = Decimal("0.01")
    commission_rate: Decimal = Decimal("0.0005")
    latency: timedelta = timedelta(0)
    initial_equity: Decimal = Decimal("1600")
    hwm_equity: Decimal = Decimal("1600")
    max_position_fraction: Decimal = MAX_POSITION_FRACTION
    occupied_slots: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "mode", FillMode(self.mode))
        if self.penetration not in {"fixed", "atr", "spread"}:
            raise ValueError("penetration must be fixed, atr, or spread")
        fixed = _decimal(self.fixed_delta, "fixed_delta")
        atr_fraction = _decimal(self.atr_fraction, "atr_fraction")
        spread_multiple = _decimal(self.spread_multiple, "spread_multiple")
        half_spread = _decimal(self.half_spread, "half_spread")
        commission_rate = _decimal(self.commission_rate, "commission_rate")
        initial = _decimal(self.initial_equity, "initial_equity")
        hwm = _decimal(self.hwm_equity, "hwm_equity")
        fraction = _decimal(self.max_position_fraction, "max_position_fraction")
        if fixed <= 0 or atr_fraction <= 0 or spread_multiple <= 0:
            raise ValueError("penetration width must be positive")
        if half_spread < 0 or commission_rate < 0 or initial <= 0 or hwm <= 0:
            raise ValueError("costs and equity must be non-negative")
        if fraction > MAX_POSITION_FRACTION:
            raise ConstitutionBypassError("max_position_fraction")
        if fraction <= 0:
            raise ValueError("max_position_fraction")
        if self.latency < timedelta(0):
            raise ValueError("latency")
        if self.occupied_slots < 0:
            raise ValueError("occupied_slots")
        object.__setattr__(self, "fixed_delta", fixed)
        object.__setattr__(self, "atr_fraction", atr_fraction)
        object.__setattr__(self, "spread_multiple", spread_multiple)
        object.__setattr__(self, "half_spread", half_spread)
        object.__setattr__(self, "commission_rate", commission_rate)
        object.__setattr__(self, "initial_equity", initial)
        object.__setattr__(self, "hwm_equity", hwm)
        object.__setattr__(self, "max_position_fraction", fraction)


@dataclass(frozen=True)
class SymbolFilter:
    """Offline stand-in for one symbol's exchangeInfo filters."""

    step_size: Decimal = Decimal("1")
    min_qty: Decimal = Decimal("1")
    min_notional: Decimal = Decimal("1")
    fractional_limit_allowed: bool = False

    def __post_init__(self) -> None:
        step = _decimal(self.step_size, "step_size")
        min_qty = _decimal(self.min_qty, "min_qty")
        min_notional = _decimal(self.min_notional, "min_notional")
        if step <= 0 or min_qty <= 0 or min_notional < 0:
            raise ValueError("symbol filter")
        object.__setattr__(self, "step_size", step)
        object.__setattr__(self, "min_qty", min_qty)
        object.__setattr__(self, "min_notional", min_notional)


@dataclass(frozen=True)
class ReplayEvent:
    time: datetime
    kind: str
    symbol: str
    detail: str


@dataclass(frozen=True)
class IntentRecord:
    """One row in the candidate log. MFE is filled in only after the replay stops."""

    intent_id: str
    symbol: str
    mode: FillMode
    status: str
    rejection_reasons: tuple[str, ...]
    adjustments: tuple[str, ...]
    cancel_reason: str | None
    client_order_id: str | None
    wallet_type: str
    tokenize: bool
    limit_price: Decimal | None
    delta: Decimal | None
    ordered_qty: Decimal
    filled_qty: Decimal
    open_qty: Decimal
    entry_price: Decimal | None
    exit_price: Decimal | None
    exit_reason: str
    ambiguous_bar_sl_assumed: bool
    entry_commission: Decimal
    exit_commission: Decimal
    spread_cost: Decimal
    gross_pnl: Decimal
    net_pnl: Decimal
    mfe_usdc: Decimal
    mae_usdc: Decimal
    opportunity_cost_usdc: Decimal
    signal_bar_end: datetime
    entry_bar_end: datetime | None
    exit_bar_end: datetime | None


@dataclass(frozen=True)
class ReplayResult:
    mode: FillMode
    records: tuple[IntentRecord, ...]
    events: tuple[ReplayEvent, ...]
    starting_cash: Decimal
    ending_cash: Decimal
    net_pnl: Decimal
    halt_new_entries: bool
