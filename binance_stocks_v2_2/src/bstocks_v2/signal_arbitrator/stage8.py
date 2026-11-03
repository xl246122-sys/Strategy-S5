"""Stage-8 pass-through. Multi-strategy ranking stays in stage 10."""

from dataclasses import dataclass

from bstocks_v2.contracts.models import Direction, TradeIntent


@dataclass(frozen=True)
class Arbitration:
    accepted: bool
    reason: str


def arbitrate(intent: TradeIntent) -> Arbitration:
    """One S3 intent in, one decision out. A second strategy is not ranked here."""

    if intent.strategy_id != "S3":
        return Arbitration(False, "STRATEGY_NOT_S3")
    if intent.direction is not Direction.LONG:
        return Arbitration(False, "DIRECTION")
    if intent.rejection_reasons:
        return Arbitration(False, intent.rejection_reasons[0])
    return Arbitration(True, "PASS")
