"""U(L, T) in Decimal. A miss charges this limit's own edge and does not read a future fill."""

from decimal import Decimal

from bstocks_v2.contracts.decimals import parse_decimal
from bstocks_v2.limit_optimizer.spec import (
    COMMISSION_RATE,
    CVAR_ALPHA,
    HALF_SPREAD,
    LAMBDA_CVAR,
    MAX_FRICTION_RATIO,
)


def round_trip_friction_ratio(limit: Decimal, take_profit: Decimal) -> Decimal:
    """Buy commission, sell commission at the target, and the full spread, over target gross."""

    gross = take_profit - limit
    if gross <= 0:
        raise ValueError("take profit must sit above the limit")
    friction = COMMISSION_RATE * (limit + take_profit) + HALF_SPREAD * 2
    return friction / gross


def friction_exceeds_cap(limit: Decimal, take_profit: Decimal) -> bool:
    """Strictly above 20%. A ratio that lands on 20% is still allowed."""

    return round_trip_friction_ratio(limit, take_profit) > MAX_FRICTION_RATIO


def conditional_net(
    *,
    limit: Decimal,
    stop: Decimal,
    take_profit: Decimal,
    qty: Decimal,
    p_win: Decimal,
    p_sl: Decimal,
    p_timeout: Decimal,
) -> Decimal:
    """Expected USDC if this limit fills. A timeout scratches at the limit."""

    win_p, stop_p, timeout_p = _unit_probabilities(p_win, p_sl, p_timeout)
    entry_fee = COMMISSION_RATE * limit * qty
    spread = HALF_SPREAD * 2 * qty
    win = (take_profit - limit) * qty - entry_fee - COMMISSION_RATE * take_profit * qty - spread
    lose = (stop - limit) * qty - entry_fee - COMMISSION_RATE * stop * qty - spread
    scratch = -entry_fee - COMMISSION_RATE * limit * qty - spread
    return win_p * win + stop_p * lose + timeout_p * scratch


def cvar_loss(
    *,
    limit: Decimal,
    stop: Decimal,
    take_profit: Decimal,
    qty: Decimal,
    p_fill: Decimal,
    p_win: Decimal,
    p_sl: Decimal,
    p_timeout: Decimal,
) -> Decimal:
    """Expected loss in the worst 5% of the mixture. A miss and a win both contribute zero loss."""

    del take_profit
    win_p, stop_p, timeout_p = _unit_probabilities(p_win, p_sl, p_timeout)
    fill = parse_decimal(p_fill)
    entry_fee = COMMISSION_RATE * limit * qty
    spread = HALF_SPREAD * 2 * qty
    loss_stop = (limit - stop) * qty + entry_fee + COMMISSION_RATE * stop * qty + spread
    loss_timeout = entry_fee + COMMISSION_RATE * limit * qty + spread
    outcomes = (
        (loss_stop, fill * stop_p),
        (loss_timeout, fill * timeout_p),
        (Decimal(0), fill * win_p + (Decimal(1) - fill)),
    )
    return _cvar(outcomes, CVAR_ALPHA)


def utility_of(
    *,
    limit: Decimal,
    stop: Decimal,
    take_profit: Decimal,
    qty: Decimal,
    p_fill: Decimal,
    p_win: Decimal,
    p_sl: Decimal,
    p_timeout: Decimal,
) -> tuple[Decimal, Decimal, Decimal, Decimal]:
    """Return utility, conditional net, missed-trade cost, and CVaR.

    Missed cost is (1 - P) times the positive conditional net of this same limit.
    When that net is positive, P * net - missed cost equals (2P - 1) * net.
    """

    net = conditional_net(
        limit=limit,
        stop=stop,
        take_profit=take_profit,
        qty=qty,
        p_win=p_win,
        p_sl=p_sl,
        p_timeout=p_timeout,
    )
    fill = parse_decimal(p_fill)
    forgone = net if net > 0 else Decimal(0)
    missed = (Decimal(1) - fill) * forgone
    tail = cvar_loss(
        limit=limit,
        stop=stop,
        take_profit=take_profit,
        qty=qty,
        p_fill=fill,
        p_win=p_win,
        p_sl=p_sl,
        p_timeout=p_timeout,
    )
    utility = fill * net - missed - LAMBDA_CVAR * tail
    return utility, net, missed, tail


def _unit_probabilities(
    p_win: Decimal,
    p_sl: Decimal,
    p_timeout: Decimal,
) -> tuple[Decimal, Decimal, Decimal]:
    mass = p_win + p_sl + p_timeout
    if mass <= 0:
        raise ValueError("probabilities")
    return p_win / mass, p_sl / mass, p_timeout / mass


def _cvar(outcomes: tuple[tuple[Decimal, Decimal], ...], alpha: Decimal) -> Decimal:
    tail = Decimal(1) - alpha
    if tail <= 0:
        raise ValueError("alpha")
    ordered = tuple(sorted(outcomes, key=lambda item: (-item[0], -item[1])))
    remaining = tail
    weighted = Decimal(0)
    for loss, prob in ordered:
        if prob < 0:
            raise ValueError("probability")
        if remaining == 0:
            break
        take = prob if prob < remaining else remaining
        weighted += take * loss
        remaining -= take
    if remaining != 0:
        raise ValueError("probability mass")
    return weighted / tail
