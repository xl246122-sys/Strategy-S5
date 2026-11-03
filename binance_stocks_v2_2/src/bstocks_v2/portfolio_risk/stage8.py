"""Stage-8 slot reservation. The full account authority remains stage 13."""

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from bstocks_v2.constitution.guard import ConstitutionGuard
from bstocks_v2.contracts.errors import OrderRejected
from bstocks_v2.contracts.models import TradeIntent
from bstocks_v2.research_backtest.models import SymbolFilter
from bstocks_v2.research_backtest.sizing import SizedOrder, SizingRejection, plan_buy
from bstocks_v2.strategies.s3.spec import COMMISSION_RATE, HALF_SPREAD


@dataclass
class RiskBook:
    equity: Decimal
    hwm: Decimal
    open_positions: int = 0
    pending_buys: int = 0
    entries_halted: bool = False
    open_symbols: set[str] = field(default_factory=set)
    pending_symbols: set[str] = field(default_factory=set)


@dataclass(frozen=True)
class RiskDecision:
    accepted: bool
    reason: str
    sized: SizedOrder | None
    open_positions: int
    pending_buys: int


def reserve(
    book: RiskBook,
    intent: TradeIntent,
    *,
    client_order_id: str,
    limit: Decimal,
    now_et: datetime,
    is_half_day: bool,
    guard: ConstitutionGuard,
    filters: SymbolFilter | None = None,
) -> RiskDecision:
    if intent.symbol in book.open_symbols or intent.symbol in book.pending_symbols:
        return _no(book, "ALREADY_IN_POSITION")
    sized = plan_buy(
        symbol=intent.symbol,
        client_order_id=client_order_id,
        limit=limit,
        stop=intent.stop_price,
        take_profit=intent.take_profit_price,
        risk_budget=intent.risk_budget_usdc,
        equity=book.equity,
        max_position_fraction=Decimal("0.20"),
        whole_share_fallback=intent.whole_share_fallback_qty,
        filters=filters if filters is not None else SymbolFilter(),
        commission_rate=COMMISSION_RATE,
        half_spread=HALF_SPREAD,
    )
    if isinstance(sized, SizingRejection):
        return _no(book, sized.reasons[0])
    try:
        guard.check_order(
            sized.draft,
            now_et=now_et,
            is_half_day=is_half_day,
            open_positions=book.open_positions,
            pending_buys=book.pending_buys,
            equity=book.equity,
            hwm=book.hwm,
            entries_halted=book.entries_halted,
        )
    except OrderRejected as exc:
        return _no(book, exc.reason)
    book.pending_buys += 1
    book.pending_symbols.add(intent.symbol)
    return RiskDecision(True, "SLOT_RESERVED", sized, book.open_positions, book.pending_buys)


def release_pending(book: RiskBook, symbol: str) -> None:
    if symbol not in book.pending_symbols:
        raise RuntimeError("pending symbol is missing")
    book.pending_symbols.remove(symbol)
    book.pending_buys -= 1


def note_fill(book: RiskBook, symbol: str) -> None:
    release_pending(book, symbol)
    book.open_symbols.add(symbol)
    book.open_positions += 1


def note_flat(book: RiskBook, symbol: str) -> None:
    if symbol not in book.open_symbols:
        raise RuntimeError("open symbol is missing")
    book.open_symbols.remove(symbol)
    book.open_positions -= 1


def _no(book: RiskBook, reason: str) -> RiskDecision:
    return RiskDecision(False, reason, None, book.open_positions, book.pending_buys)
