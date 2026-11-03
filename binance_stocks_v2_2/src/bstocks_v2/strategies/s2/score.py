"""Pre-registered comparison of the rule book and the probability gate."""

from dataclasses import dataclass
from decimal import Decimal

MIN_CLOSED = 30
EDGE = Decimal(2)
DRAWDOWN_RATIO = Decimal("1.2")


@dataclass(frozen=True)
class SplitScore:
    closed_trades: int
    mean_net_bps: Decimal | None
    se_net_bps: Decimal | None
    profit_factor: Decimal | None
    max_drawdown_usdc: Decimal
    net_pnl_usdc: Decimal


def summarize(pnls: list[Decimal], net_bps: list[Decimal]) -> SplitScore:
    """`pnls` must already be ordered by exit time. Drawdown follows that order."""

    if len(pnls) != len(net_bps):
        raise ValueError("pnl and bps lengths differ")
    mean, se = _mean_se(net_bps)
    return SplitScore(
        len(pnls),
        mean,
        se,
        _profit_factor(pnls),
        _max_drawdown(pnls),
        sum(pnls, start=Decimal(0)),
    )


def prefer_gate(rule: SplitScore, gate: SplitScore) -> bool:
    """The gate wins only when the score window clears this bar. Train does not vote."""

    if rule.closed_trades < MIN_CLOSED or gate.closed_trades < MIN_CLOSED:
        return False
    if (
        rule.mean_net_bps is None
        or gate.mean_net_bps is None
        or rule.se_net_bps is None
        or gate.se_net_bps is None
        or rule.profit_factor is None
        or gate.profit_factor is None
    ):
        return False
    gap = gate.mean_net_bps - rule.mean_net_bps
    noise = (gate.se_net_bps**2 + rule.se_net_bps**2).sqrt()
    if gap <= EDGE * noise:
        return False
    if gate.profit_factor < rule.profit_factor:
        return False
    if rule.max_drawdown_usdc == 0:
        return gate.max_drawdown_usdc == 0
    return gate.max_drawdown_usdc <= rule.max_drawdown_usdc * DRAWDOWN_RATIO


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
