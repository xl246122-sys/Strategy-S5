"""Turn one arbitrated intent into a cent-priced CARD limit, or a structured refusal."""

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from bstocks_v2.constitution.guard import CLIENT_ORDER_ID, ConstitutionGuard
from bstocks_v2.contracts.decimals import parse_decimal
from bstocks_v2.contracts.errors import OrderRejected
from bstocks_v2.contracts.models import (
    MIN_VIABLE_NOTIONAL,
    OrderDraft,
    OrderType,
    Side,
    TradeIntent,
    TradingSession,
)
from bstocks_v2.limit_optimizer.book import PriceCandidate, QuoteBook, build_candidates
from bstocks_v2.limit_optimizer.fill_model import FillModel
from bstocks_v2.limit_optimizer.spec import (
    BAR_SECONDS,
    DEFAULT_HORIZON_BARS,
    HORIZON_BARS,
    MAX_POSITION_FRACTION,
    TIER_ORDER,
)
from bstocks_v2.limit_optimizer.utility import (
    friction_exceeds_cap,
    round_trip_friction_ratio,
    utility_of,
)
from bstocks_v2.research_backtest.fills import floor_step
from bstocks_v2.research_backtest.models import SymbolFilter


@dataclass(frozen=True)
class EntryClock:
    """Session and account the guard sees. Sizing uses this equity; the 20% cap is not a knob."""

    now_et: datetime
    is_half_day: bool = False
    open_positions: int = 0
    pending_buys: int = 0
    equity: Decimal = Decimal("1600")
    hwm: Decimal = Decimal("1600")
    entries_halted: bool = False

    def __post_init__(self) -> None:
        if self.now_et.tzinfo is None:
            raise ValueError("NAIVE_DATETIME")
        equity = parse_decimal(self.equity)
        hwm = parse_decimal(self.hwm)
        if equity <= 0 or hwm <= 0:
            raise ValueError("equity")
        if self.open_positions < 0 or self.pending_buys < 0:
            raise ValueError("slots")
        object.__setattr__(self, "equity", equity)
        object.__setattr__(self, "hwm", hwm)


@dataclass(frozen=True)
class CandidateScore:
    tier: str
    price: Decimal | None
    quantity: Decimal | None
    p_fill: Decimal | None
    expected_net: Decimal | None
    missed_cost: Decimal | None
    cvar: Decimal | None
    utility: Decimal | None
    reason: str | None
    detail: str


@dataclass(frozen=True)
class PreparedOrder:
    intent_id: str
    tier: str
    client_order_id: str
    ttl_seconds: int
    adjustments: tuple[str, ...]
    utility: Decimal
    p_fill: Decimal
    expected_net: Decimal
    missed_cost: Decimal
    cvar: Decimal
    draft: OrderDraft


@dataclass(frozen=True)
class OptimizeResult:
    order: PreparedOrder | None
    scores: tuple[CandidateScore, ...]
    reason: str | None


@dataclass(frozen=True)
class _Sized:
    score: CandidateScore
    order: PreparedOrder | None


class LimitOptimizer:
    """Score the three bids. The live place path is not on this object."""

    def __init__(
        self,
        model: FillModel,
        *,
        horizon_bars: int = DEFAULT_HORIZON_BARS,
        whole_share_safe_mode: bool = True,
    ) -> None:
        if horizon_bars not in HORIZON_BARS:
            raise ValueError("horizon_bars must be 1, 2, or 3")
        self._model = model
        self._horizon = horizon_bars
        self._whole = whole_share_safe_mode
        self._guard = ConstitutionGuard()

    def optimize(
        self,
        intent: TradeIntent,
        book: QuoteBook,
        filters: SymbolFilter,
        clock: EntryClock,
    ) -> OptimizeResult:
        if intent.rejection_reasons:
            return OptimizeResult(None, (), intent.rejection_reasons[0])
        if intent.risk_budget_usdc <= 0:
            return OptimizeResult(None, (), "RISK_BUDGET")
        ttl = self._horizon * BAR_SECONDS
        if intent.data_asof_utc > clock.now_et:
            return OptimizeResult(None, (), "LOOKAHEAD_ASOF")
        if intent.signal_valid_until < clock.now_et + timedelta(seconds=ttl):
            return OptimizeResult(None, (), "TTL_SHORTER_THAN_HORIZON")
        built = build_candidates(intent, book)
        sized = tuple(self._score(intent, book, filters, clock, row, ttl) for row in built)
        scores = tuple(item.score for item in sized)
        viable = [item for item in sized if item.order is not None]
        if not viable:
            return OptimizeResult(None, scores, _shared_reason(scores))
        best = viable[0]
        for item in viable[1:]:
            if _prefer(item, best):
                best = item
        order = best.order
        assert order is not None
        return OptimizeResult(order, scores, None)

    def _score(
        self,
        intent: TradeIntent,
        book: QuoteBook,
        filters: SymbolFilter,
        clock: EntryClock,
        row: PriceCandidate,
        ttl: int,
    ) -> _Sized:
        if row.reason is not None or row.price is None:
            return _blank(row.tier, row.price, row.reason or "GEOMETRY", row.detail)
        try:
            probability = self._model.probability(ask=book.ask, limit=row.price, tier=row.tier)
        except (TypeError, ValueError, ArithmeticError):
            return _blank(row.tier, row.price, "FILL_PROBABILITY", row.tier)
        fill = parse_decimal(probability)
        if fill < 0 or fill > 1:
            return _blank(row.tier, row.price, "FILL_PROBABILITY", row.tier)
        sized = _quantity(intent, row.price, clock.equity, filters, self._whole)
        if isinstance(sized, _QtyReject):
            return _blank(row.tier, row.price, sized.reason, sized.detail)
        qty, adjustments = sized
        ratio = round_trip_friction_ratio(row.price, intent.take_profit_price)
        if friction_exceeds_cap(row.price, intent.take_profit_price):
            detail = f"friction_ratio>0.20;ratio={ratio}"
            return _blank(row.tier, row.price, "NOTIONAL_TOO_SMALL_FOR_FEE_EFFICIENCY", detail)
        utility, net, missed, tail = utility_of(
            limit=row.price,
            stop=intent.stop_price,
            take_profit=intent.take_profit_price,
            qty=qty,
            p_fill=fill,
            p_win=intent.p_win_calibrated,
            p_sl=intent.p_sl_calibrated,
            p_timeout=intent.p_timeout_calibrated,
        )
        gross = (intent.take_profit_price - row.price) * qty
        friction = ratio * gross
        client_id = client_order_id_for(intent.intent_id, row.tier)
        try:
            draft = OrderDraft(
                symbol=intent.symbol,
                side=Side.BUY,
                order_type=OrderType.LIMIT,
                client_order_id=client_id,
                quantity=qty,
                price=row.price,
                trading_session=TradingSession.RTH,
                quote_asset="USDC",
                wallet_type="CARD",
                tokenize=False,
                is_opening=True,
                confirmed_long_qty=Decimal(0),
                gross_take_profit_usdc=gross,
                round_trip_friction_usdc=friction,
            )
            self._guard.check_order(
                draft,
                now_et=clock.now_et,
                is_half_day=clock.is_half_day,
                open_positions=clock.open_positions,
                pending_buys=clock.pending_buys,
                equity=clock.equity,
                hwm=clock.hwm,
                entries_halted=clock.entries_halted,
            )
        except OrderRejected as exc:
            reason = _guard_reason(exc.reason)
            return _blank(row.tier, row.price, reason, exc.reason)
        score = CandidateScore(
            tier=row.tier,
            price=row.price,
            quantity=qty,
            p_fill=fill,
            expected_net=net,
            missed_cost=missed,
            cvar=tail,
            utility=utility,
            reason=None,
            detail=row.tier,
        )
        order = PreparedOrder(
            intent_id=intent.intent_id,
            tier=row.tier,
            client_order_id=client_id,
            ttl_seconds=ttl,
            adjustments=adjustments,
            utility=utility,
            p_fill=fill,
            expected_net=net,
            missed_cost=missed,
            cvar=tail,
            draft=draft,
        )
        return _Sized(score, order)


def client_order_id_for(intent_id: str, tier: str) -> str:
    """35 characters from the intent and the tier. A retry recomputes the same id."""

    if tier not in TIER_ORDER:
        raise ValueError("tier")
    digest = hashlib.sha256(f"v22-stage11|{intent_id}|{tier}".encode()).hexdigest()
    client_id = f"v22{digest[:32]}"
    if CLIENT_ORDER_ID.fullmatch(client_id) is None:
        raise ValueError("CLIENT_ORDER_ID")
    return client_id


def prefer_score(left: CandidateScore, right: CandidateScore) -> bool:
    """True when left should replace right. Higher utility, then the lower price, then passive."""

    if left.utility is None or right.utility is None or left.price is None or right.price is None:
        return False
    if left.utility != right.utility:
        return left.utility > right.utility
    if left.price != right.price:
        return left.price < right.price
    return TIER_ORDER.index(left.tier) < TIER_ORDER.index(right.tier)


@dataclass(frozen=True)
class _QtyReject:
    reason: str
    detail: str


def _quantity(
    intent: TradeIntent,
    limit: Decimal,
    equity: Decimal,
    filters: SymbolFilter,
    whole_share_safe: bool,
) -> tuple[Decimal, tuple[str, ...]] | _QtyReject:
    distance = limit - intent.stop_price
    if distance <= 0:
        return _QtyReject("GEOMETRY", "distance")
    room = equity * MAX_POSITION_FRACTION
    raw = min(intent.risk_budget_usdc / distance, room / limit)
    whole = whole_share_safe or not filters.fractional_limit_allowed
    adjustments: list[str] = []
    if whole:
        qty = floor_step(raw, Decimal(1))
        if filters.step_size > 1:
            qty = floor_step(qty, filters.step_size)
        if qty != raw:
            adjustments.append("486442")
        minimum = filters.min_qty if filters.min_qty > 1 else Decimal(1)
        if qty < minimum:
            return _QtyReject("WHOLE_SHARE_EXCEEDS_RISK_OR_CAP", f"qty={qty}")
    else:
        qty = floor_step(raw, filters.step_size)
        if qty < filters.min_qty:
            return _QtyReject("MIN_QTY", f"qty={qty}")
    notional = qty * limit
    floor_notional = intent.min_viable_notional_usdc
    if floor_notional < MIN_VIABLE_NOTIONAL:
        floor_notional = MIN_VIABLE_NOTIONAL
    if filters.min_notional > floor_notional:
        floor_notional = filters.min_notional
    if notional < floor_notional:
        return _QtyReject(
            "NOTIONAL_TOO_SMALL_FOR_FEE_EFFICIENCY",
            f"notional<120;notional={notional}",
        )
    return qty, tuple(adjustments)


def _blank(tier: str, price: Decimal | None, reason: str, detail: str) -> _Sized:
    score = CandidateScore(tier, price, None, None, None, None, None, None, reason, detail)
    return _Sized(score, None)


def _prefer(left: _Sized, right: _Sized) -> bool:
    return prefer_score(left.score, right.score)


def _shared_reason(scores: tuple[CandidateScore, ...]) -> str:
    reasons = {row.reason for row in scores if row.reason is not None}
    if len(reasons) == 1:
        return next(iter(reasons))
    return "NO_VIABLE_LIMIT"


def _guard_reason(reason: str) -> str:
    if reason in {"NOTIONAL_TOO_SMALL", "FRICTION"}:
        return "NOTIONAL_TOO_SMALL_FOR_FEE_EFFICIENCY"
    return reason
