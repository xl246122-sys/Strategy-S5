"""Pre-registered comparison of the bounce baseline and the OU / knife arm."""

from dataclasses import dataclass
from decimal import Decimal

from bstocks_v2.strategies.s4.spec import DRAWDOWN_RATIO, EDGE, MIN_CLOSED


@dataclass(frozen=True)
class SplitScore:
    closed_trades: int
    mean_net_bps: Decimal | None
    se_net_bps: Decimal | None
    profit_factor: Decimal | None
    max_drawdown_usdc: Decimal
    net_pnl_usdc: Decimal
    miss_rate: Decimal | None


def summarize(
    pnls: list[Decimal],
    net_bps: list[Decimal],
    *,
    miss_rate: Decimal | None,
) -> SplitScore:
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
        miss_rate,
    )


def comparison(baseline: SplitScore, guarded: SplitScore) -> str:
    """The guarded arm wins only on the validation scores. Train does not vote."""

    if not _eligible(baseline) or not _eligible(guarded):
        return "insufficient"
    assert baseline.mean_net_bps is not None
    assert guarded.mean_net_bps is not None
    assert baseline.se_net_bps is not None
    assert guarded.se_net_bps is not None
    assert baseline.profit_factor is not None
    assert guarded.profit_factor is not None
    assert baseline.miss_rate is not None
    assert guarded.miss_rate is not None
    gap = guarded.mean_net_bps - baseline.mean_net_bps
    noise = (guarded.se_net_bps**2 + baseline.se_net_bps**2).sqrt()
    if gap <= EDGE * noise:
        return "baseline"
    if guarded.profit_factor < baseline.profit_factor:
        return "baseline"
    if baseline.max_drawdown_usdc == 0:
        if guarded.max_drawdown_usdc != 0:
            return "baseline"
    elif guarded.max_drawdown_usdc > baseline.max_drawdown_usdc * DRAWDOWN_RATIO:
        return "baseline"
    if guarded.miss_rate > baseline.miss_rate:
        return "baseline"
    return "guarded"


def live_deployable() -> bool:
    """S4 stays offline. A positive research comparison does not open a live route."""

    return False


def _eligible(score: SplitScore) -> bool:
    return (
        score.closed_trades >= MIN_CLOSED
        and score.mean_net_bps is not None
        and score.se_net_bps is not None
        and score.profit_factor is not None
        and score.miss_rate is not None
    )


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
