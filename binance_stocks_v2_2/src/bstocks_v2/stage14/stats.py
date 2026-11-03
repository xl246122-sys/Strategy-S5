"""Per-trade summaries, bootstrap intervals, and the deflated Sharpe ratio."""

import math
import random
from decimal import ROUND_CEILING, Decimal

from bstocks_v2.stage14.spec import ACCOUNT, BOOTSTRAP_DRAWS, BOOTSTRAP_SEED, DSR_MIN

_EULER = Decimal("0.5772156649015328606")
_E = Decimal("2.7182818284590452354")


def text(value: Decimal | None) -> str | None:
    if value is None:
        return None
    if value.is_infinite():
        return "inf"
    return format(value, "f")


def trade_bps(net: Decimal, notional: Decimal) -> Decimal:
    if notional <= 0:
        raise RuntimeError("closed trade has no notional")
    return net / notional * Decimal(10000)


def summarize(pnls: list[Decimal], notionals: list[Decimal]) -> dict[str, object]:
    """Closed trades, already ordered by exit time."""

    if len(pnls) != len(notionals):
        raise ValueError("pnl and notional lengths differ")
    bps = [trade_bps(net, notional) for net, notional in zip(pnls, notionals, strict=True)]
    count = len(pnls)
    total = sum(pnls, start=Decimal(0))
    mean_usd, mean_bps, se_bps = _means(pnls, bps)
    wins = [item for item in pnls if item > 0]
    losses = [item for item in pnls if item < 0]
    gross_win = sum(wins, start=Decimal(0))
    gross_loss = sum((-item for item in losses), start=Decimal(0))
    if gross_win == 0 and gross_loss == 0:
        factor: Decimal | None = None
    elif gross_loss == 0:
        factor = Decimal("Infinity")
    else:
        factor = gross_win / gross_loss
    payoff: Decimal | None = None
    if wins and losses:
        payoff = (gross_win / Decimal(len(wins))) / (gross_loss / Decimal(len(losses)))
    win_rate = (Decimal(len(wins)) / Decimal(count)) if count else None
    drawdown, min_equity, halt_line = _path(pnls)
    low_bps, high_bps = _bootstrap(bps)
    return {
        "closed_trades": count,
        "net_pnl_usdc": text(total),
        "net_return_pct": text(total / ACCOUNT * Decimal(100)) if count else text(Decimal(0)),
        "expectancy_usdc": text(mean_usd),
        "expectancy_bps": text(mean_bps),
        "se_bps": text(se_bps),
        "profit_factor": text(factor),
        "win_rate": text(win_rate),
        "payoff_ratio": text(payoff),
        "max_drawdown_usdc": text(drawdown),
        "min_equity_usdc": text(min_equity),
        "equity_crossed_hwm_line": min_equity <= halt_line,
        "cvar95_usdc": text(_cvar(pnls)),
        "bootstrap_bps_ci95": [text(low_bps), text(high_bps)],
        "sharpe": text(_sharpe(bps)),
    }


def deflated_sharpe(
    bps: list[Decimal],
    trial_sharpes: list[Decimal],
    trials: int,
) -> dict[str, object]:
    """Bailey and Lopez de Prado (2014), on the closed-trade net bps series.

    ``trials`` is the number of configurations already compared before this run.
    The variance of those trial Sharpes is the selection benchmark. A sample
    shorter than four trades has no kurtosis estimate and is not significant.
    """

    observed = _sharpe(bps)
    variance = _sample_variance(trial_sharpes)
    empty: dict[str, object] = {
        "dsr": None,
        "significant": False,
        "threshold": text(DSR_MIN),
        "observed_sharpe": text(observed),
        "benchmark_sharpe": None,
        "trials": trials,
        "trials_with_sharpe": len(trial_sharpes),
        "reason": None,
    }
    if observed is None or variance is None or trials < 2 or len(bps) < 4:
        empty["reason"] = "SAMPLE_OR_BENCHMARK"
        return empty
    benchmark = _sr_star(variance, trials)
    skew = _skew(bps)
    kurt = _kurtosis(bps)
    if skew is None or kurt is None:
        empty["reason"] = "MOMENTS"
        empty["benchmark_sharpe"] = text(benchmark)
        return empty
    inside = (
        Decimal(1)
        - skew * observed
        + ((kurt - Decimal(1)) / Decimal(4)) * observed * observed
    )
    if inside <= 0:
        empty["reason"] = "VARIANCE_TERM"
        empty["benchmark_sharpe"] = text(benchmark)
        return empty
    width = (inside.sqrt())
    z = (observed - benchmark) * (Decimal(len(bps) - 1).sqrt()) / width
    dsr = _norm_cdf(z)
    return {
        "dsr": text(dsr),
        "significant": dsr >= DSR_MIN,
        "threshold": text(DSR_MIN),
        "observed_sharpe": text(observed),
        "benchmark_sharpe": text(benchmark),
        "trials": trials,
        "trials_with_sharpe": len(trial_sharpes),
        "reason": None,
    }


def _means(
    pnls: list[Decimal],
    bps: list[Decimal],
) -> tuple[Decimal | None, Decimal | None, Decimal | None]:
    count = len(pnls)
    if count == 0:
        return None, None, None
    mean_usd = sum(pnls, start=Decimal(0)) / Decimal(count)
    mean_bps = sum(bps, start=Decimal(0)) / Decimal(count)
    if count < 2:
        return mean_usd, mean_bps, None
    var = sum(((item - mean_bps) ** 2 for item in bps), start=Decimal(0)) / Decimal(count - 1)
    return mean_usd, mean_bps, (var / Decimal(count)).sqrt()


def _sharpe(bps: list[Decimal]) -> Decimal | None:
    count = len(bps)
    if count < 2:
        return None
    mean = sum(bps, start=Decimal(0)) / Decimal(count)
    var = sum(((item - mean) ** 2 for item in bps), start=Decimal(0)) / Decimal(count - 1)
    if var <= 0:
        return None
    return mean / var.sqrt()


def _sample_variance(values: list[Decimal]) -> Decimal | None:
    count = len(values)
    if count < 2:
        return None
    mean = sum(values, start=Decimal(0)) / Decimal(count)
    return sum(((item - mean) ** 2 for item in values), start=Decimal(0)) / Decimal(count - 1)


def _skew(values: list[Decimal]) -> Decimal | None:
    count = len(values)
    if count < 3:
        return None
    mean = sum(values, start=Decimal(0)) / Decimal(count)
    m2 = sum(((item - mean) ** 2 for item in values), start=Decimal(0)) / Decimal(count)
    if m2 <= 0:
        return None
    m3 = sum(((item - mean) ** 3 for item in values), start=Decimal(0)) / Decimal(count)
    return m3 / (m2.sqrt() ** 3)


def _kurtosis(values: list[Decimal]) -> Decimal | None:
    """Raw Pearson kurtosis. A normal sample is near 3, not 0."""

    count = len(values)
    if count < 4:
        return None
    mean = sum(values, start=Decimal(0)) / Decimal(count)
    m2 = sum(((item - mean) ** 2 for item in values), start=Decimal(0)) / Decimal(count)
    if m2 <= 0:
        return None
    m4 = sum(((item - mean) ** 4 for item in values), start=Decimal(0)) / Decimal(count)
    return m4 / (m2 * m2)


def _path(pnls: list[Decimal]) -> tuple[Decimal, Decimal, Decimal]:
    equity = ACCOUNT
    peak = ACCOUNT
    worst = Decimal(0)
    lowest = ACCOUNT
    line = ACCOUNT * Decimal("0.92")
    for pnl in pnls:
        equity += pnl
        if equity > peak:
            peak = equity
        drop = peak - equity
        if drop > worst:
            worst = drop
        if equity < lowest:
            lowest = equity
    return worst, lowest, line


def _cvar(pnls: list[Decimal]) -> Decimal | None:
    count = len(pnls)
    if count == 0:
        return None
    width = (Decimal(count) * Decimal("0.05")).to_integral_value(rounding=ROUND_CEILING)
    take = int(width) if width >= 1 else 1
    worst = sorted(pnls)[:take]
    return sum(worst, start=Decimal(0)) / Decimal(len(worst))


def _bootstrap(bps: list[Decimal]) -> tuple[Decimal | None, Decimal | None]:
    count = len(bps)
    if count == 0:
        return None, None
    rng = random.Random(BOOTSTRAP_SEED)
    means: list[Decimal] = []
    for _ in range(BOOTSTRAP_DRAWS):
        draw = [bps[rng.randrange(count)] for _ in range(count)]
        means.append(sum(draw, start=Decimal(0)) / Decimal(count))
    means.sort()
    last = Decimal(len(means) - 1)
    low = means[int(Decimal("0.025") * last)]
    high = means[int(Decimal("0.975") * last)]
    return low, high


def _sr_star(variance: Decimal, trials: int) -> Decimal:
    if variance < 0 or trials < 2:
        raise ValueError("benchmark")
    first = _norm_ppf(Decimal(1) - (Decimal(1) / Decimal(trials)))
    second = _norm_ppf(Decimal(1) - (Decimal(1) / (Decimal(trials) * _E)))
    return variance.sqrt() * ((Decimal(1) - _EULER) * first + _EULER * second)


def _norm_cdf(value: Decimal) -> Decimal:
    z = float(value)
    probability = 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))
    return Decimal(format(probability, ".16f"))


def _norm_ppf(probability: Decimal) -> Decimal:
    """Acklam's rational approximation. The input is a probability, not a price."""

    p = float(probability)
    if not 0.0 < p < 1.0:
        raise ValueError("ppf domain")
    a = (
        -3.969683028665376e01,
        2.209460984245205e02,
        -2.759285104469687e02,
        1.383577518672690e02,
        -3.066479806614716e01,
        2.506628277459239e00,
    )
    b = (
        -5.447609879822406e01,
        1.615858368580409e02,
        -1.556989798598866e02,
        6.680131188771972e01,
        -1.328068155288572e01,
    )
    c = (
        -7.784894002430293e-03,
        -3.223964580411365e-01,
        -2.400758277161838e00,
        -2.549732539343734e00,
        4.374664141464968e00,
        2.938163982698783e00,
    )
    d = (
        7.784695709041462e-03,
        3.224671290700398e-01,
        2.445134137142996e00,
        3.754408661907416e00,
    )
    plow = 0.02425
    phigh = 1 - plow
    if p < plow:
        q = math.sqrt(-2 * math.log(p))
        num = (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5])
        den = ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
        z = num / den
    elif p > phigh:
        q = math.sqrt(-2 * math.log(1 - p))
        num = (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5])
        den = ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
        z = -(num / den)
    else:
        q = p - 0.5
        r = q * q
        num = (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q
        den = (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1)
        z = num / den
    return Decimal(format(z, ".16f"))
