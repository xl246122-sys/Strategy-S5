"""Sector residual board for the candidate pool. Research parents stay as they were."""

from bstocks_v2.strategies.s1.book import FactorBoard
from bstocks_v2.strategies.s1.filters import Strength, ThroughOrigin
from bstocks_v2.strategies.s1.spec import RS_BARS

_EXTRA = ("XLC", "XLP")


class ConsoleFactors(FactorBoard):
    def __init__(self, rs_bars: int = RS_BARS) -> None:
        super().__init__(rs_bars)
        for sector in _EXTRA:
            self._slopes[sector] = ThroughOrigin()
            self._strength[sector] = Strength(rs_bars)
