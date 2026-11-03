"""S5 position contract. The global Guardian does not call this module."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from bstocks_v2.contracts.errors import OrderRejected
from bstocks_v2.portfolio_risk.authority import PortfolioRisk
from bstocks_v2.strategies.s5.lock import s5_live_enabled

if TYPE_CHECKING:
    from bstocks_v2.strategies.s5.book import S5ResearchBook

STRATEGY_ID = "S5_EOD_REVERSAL"


@dataclass(frozen=True)
class PositionPolicy:
    strategy_id: str
    session_profile: str
    allow_cross_rth_close: bool
    live_enabled: bool
    max_holding_seconds: int
    exit_deadline_utc: datetime | None
    allowed_order_types: tuple[str, ...]
    session_eligibility_verified: bool
    active: bool


def research_policy() -> PositionPolicy:
    """The only S5 policy this phase can build. It is not installed on Guardian."""

    if s5_live_enabled():
        raise RuntimeError("S5 code lock drifted")
    return PositionPolicy(
        strategy_id=STRATEGY_ID,
        session_profile="RESEARCH_POST_OR_24H",
        allow_cross_rth_close=True,
        live_enabled=False,
        max_holding_seconds=6 * 60 * 60,
        exit_deadline_utc=None,
        allowed_order_types=("LIMIT",),
        session_eligibility_verified=False,
        active=False,
    )


class S5GuardianPolicy:
    """Not installed on the global Guardian. ``governs`` stays false."""

    def __init__(self, policy: PositionPolicy | None = None) -> None:
        self.policy = research_policy() if policy is None else policy

    def governs(self) -> bool:
        if s5_live_enabled() or self.policy.live_enabled or self.policy.active:
            raise RuntimeError("S5 code lock drifted")
        return False

    def on_clock(self, now_et: datetime) -> None:
        del now_et
        raise OrderRejected("S5_POLICY_INACTIVE")

    def open_research(self, root: Path, risk: PortfolioRisk) -> S5ResearchBook:
        """Mock book only. `governs` stays false, so the 15:55 flatten is not this object."""

        if s5_live_enabled() or self.policy.live_enabled or self.policy.active:
            raise RuntimeError("S5 code lock drifted")
        from bstocks_v2.strategies.s5.book import S5ResearchBook

        return S5ResearchBook.open(root, risk)
