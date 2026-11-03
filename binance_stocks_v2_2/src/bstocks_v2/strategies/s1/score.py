"""Validation rule for the three S1 filters. Train numbers do not enter this choice."""

from collections.abc import Mapping
from decimal import Decimal

from bstocks_v2.strategies.s1.spec import MODELS

MIN_CLOSED = 30
EDGE = Decimal("2")
DRAWDOWN_RATIO = Decimal("1.2")
_COMPLEXITY = {"ols": 0, "rls": 1, "kalman": 2}


class ModelScore:
    """Closed-trade summary for one filter on one split."""

    __slots__ = (
        "name",
        "closed_trades",
        "mean_net_bps",
        "se_net_bps",
        "profit_factor",
        "max_drawdown_usdc",
        "net_pnl_usdc",
    )

    def __init__(
        self,
        name: str,
        closed_trades: int,
        mean_net_bps: Decimal | None,
        se_net_bps: Decimal | None,
        profit_factor: Decimal | None,
        max_drawdown_usdc: Decimal,
        net_pnl_usdc: Decimal,
    ) -> None:
        if name not in MODELS:
            raise ValueError(name)
        self.name = name
        self.closed_trades = closed_trades
        self.mean_net_bps = mean_net_bps
        self.se_net_bps = se_net_bps
        self.profit_factor = profit_factor
        self.max_drawdown_usdc = max_drawdown_usdc
        self.net_pnl_usdc = net_pnl_usdc


def summarize(name: str, pnls: list[Decimal], net_bps: list[Decimal]) -> ModelScore:
    """`pnls` must already be ordered by exit time. Drawdown follows that order."""

    if len(pnls) != len(net_bps):
        raise ValueError("pnl and bps lengths differ")
    mean, se = _mean_se(net_bps)
    return ModelScore(
        name,
        len(pnls),
        mean,
        se,
        _profit_factor(pnls),
        _max_drawdown(pnls),
        sum(pnls, start=Decimal(0)),
    )


def recommend(scores: Mapping[str, ModelScore]) -> str:
    """Pick Kalman only when it clears the pre-registered bar against the stronger simple model."""

    kalman = scores["kalman"]
    rls = scores["rls"]
    ols = scores["ols"]
    if significantly_better(kalman, _stronger(rls, ols)):
        return "kalman"
    if significantly_better(rls, ols):
        return "rls"
    return "ols"


def significantly_better(challenger: ModelScore, incumbent: ModelScore) -> bool:
    if challenger.closed_trades < MIN_CLOSED or incumbent.closed_trades < MIN_CLOSED:
        return False
    if (
        challenger.mean_net_bps is None
        or incumbent.mean_net_bps is None
        or challenger.se_net_bps is None
        or incumbent.se_net_bps is None
        or challenger.profit_factor is None
        or incumbent.profit_factor is None
    ):
        return False
    gap = challenger.mean_net_bps - incumbent.mean_net_bps
    noise = (challenger.se_net_bps**2 + incumbent.se_net_bps**2).sqrt()
    if gap <= EDGE * noise:
        return False
    if challenger.profit_factor < incumbent.profit_factor:
        return False
    if incumbent.max_drawdown_usdc == 0:
        return challenger.max_drawdown_usdc == 0
    return challenger.max_drawdown_usdc <= incumbent.max_drawdown_usdc * DRAWDOWN_RATIO


def _stronger(left: ModelScore, right: ModelScore) -> ModelScore:
    return left if _rank(left) >= _rank(right) else right


def _rank(score: ModelScore) -> tuple[Decimal, int]:
    mean = score.mean_net_bps if score.mean_net_bps is not None else Decimal("-1e18")
    return (mean, -_COMPLEXITY[score.name])


def _mean_se(values: list[Decimal]) -> tuple[Decimal | None, Decimal | None]:
    count = len(values)
    if count == 0:
        return None, None
    mean = sum(values, start=Decimal(0)) / Decimal(count)
    if count < 2:
        return mean, None
    variance = sum(((item - mean) ** 2 for item in values), start=Decimal(0)) / Decimal(count - 1)
    return mean, (variance / Decimal(count)).sqrt()


def _profit_factor(pnls: list[Decimal]) -> Decimal | None:
    wins = sum((item for item in pnls if item > 0), start=Decimal(0))
    losses = sum((-item for item in pnls if item < 0), start=Decimal(0))
    if wins == 0 and losses == 0:
        return None
    if losses == 0:
        return Decimal("Infinity")
    return wins / losses


def _max_drawdown(pnls: list[Decimal]) -> Decimal:
    equity = Decimal(0)
    peak = Decimal(0)
    worst = Decimal(0)
    for pnl in pnls:
        equity += pnl
        if equity > peak:
            peak = equity
        drop = peak - equity
        if drop > worst:
            worst = drop
    return worst
