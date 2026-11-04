"""Shared sector residual. One board feeds every stock filter."""

from decimal import Decimal

from bstocks_v2.strategies.s1.filters import Strength, ThroughOrigin
from bstocks_v2.strategies.s1.spec import PARENTS, RS_BARS


class FactorBoard:
    """Orthogonalize each sector once per bar, then reuse that residual."""

    def __init__(self, rs_bars: int = RS_BARS) -> None:
        if rs_bars < 1:
            raise ValueError("rs bars")
        self._slopes = {sector: ThroughOrigin() for sector in PARENTS}
        self._strength = {sector: Strength(rs_bars) for sector in PARENTS}

    def update(
        self,
        sector: str,
        spy: Decimal,
        sector_ret: Decimal,
        asof: str,
    ) -> Decimal | None:
        observed = self._slopes[sector].observe(spy, sector_ret, asof)
        if observed is None:
            return None
        _gamma, perp = observed
        self._strength[sector].push(perp, asof)
        return perp

    def strength(self, sector: str) -> Decimal | None:
        return self._strength[sector].value()

    def residual_dot(self, sector: str) -> Decimal | None:
        return self._slopes[sector].residual_dot()
