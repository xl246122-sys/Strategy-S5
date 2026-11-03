"""Stage-8 ORB band. The passive, retest, and aggressive utility search lives in stage 11."""

from dataclasses import dataclass
from decimal import Decimal

from bstocks_v2.contracts.errors import OrderRejected
from bstocks_v2.contracts.models import TradeIntent
from bstocks_v2.strategies.s3.spec import TTL_SECONDS


@dataclass(frozen=True)
class LimitPlan:
    limit: Decimal
    ceiling: Decimal
    ttl_seconds: int
    refused_chase: bool


def bind_limit(intent: TradeIntent) -> LimitPlan:
    if not intent.candidate_limit_prices:
        raise OrderRejected("LIMIT_MISSING")
    limit = intent.candidate_limit_prices[0]
    ceiling = intent.entry_price_ceiling
    if limit > ceiling:
        raise OrderRejected("CHASE")
    if not intent.stop_price < limit <= ceiling < intent.take_profit_price:
        raise OrderRejected("GEOMETRY")
    return LimitPlan(limit, ceiling, TTL_SECONDS, False)


def replace_limit(plan: LimitPlan, new_price: Decimal) -> LimitPlan:
    """A second price is ignored. The working limit stays on the original ORH bid."""

    if new_price == plan.limit:
        return plan
    return LimitPlan(plan.limit, plan.ceiling, plan.ttl_seconds, True)
