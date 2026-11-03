"""S5 end-of-day research. Live trading is locked off. The mock book is not the global guardian."""

from bstocks_v2.strategies.s5.book import S5ResearchBook
from bstocks_v2.strategies.s5.lock import s5_live_enabled
from bstocks_v2.strategies.s5.policy import S5GuardianPolicy, research_policy
from bstocks_v2.strategies.s5.signals import ResearchRecord, evaluate

__all__ = [
    "ResearchRecord",
    "S5GuardianPolicy",
    "S5ResearchBook",
    "evaluate",
    "research_policy",
    "s5_live_enabled",
]
