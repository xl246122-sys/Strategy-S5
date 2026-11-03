"""S5 end-of-day research. Live trading is locked off and the guardian hook is inactive."""

from bstocks_v2.strategies.s5.lock import s5_live_enabled
from bstocks_v2.strategies.s5.policy import S5GuardianPolicy, research_policy
from bstocks_v2.strategies.s5.signals import ResearchRecord, evaluate

__all__ = [
    "ResearchRecord",
    "S5GuardianPolicy",
    "evaluate",
    "research_policy",
    "s5_live_enabled",
]
