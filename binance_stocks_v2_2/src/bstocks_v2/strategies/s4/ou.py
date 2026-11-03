"""Dimensionless VWAP residual and a same-window AR(1). Later points are not an input."""

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from bstocks_v2.strategies.s4.spec import (
    ADF_C_5,
    BAR_MINUTES,
    HALF_LIFE_MAX_MINUTES,
    HALF_LIFE_QUANTUM,
    T_PHI_CRIT,
    WINDOW,
)


@dataclass(frozen=True)
class OuFit:
    """One rolling fit. `t_phi` is None when the residual sum of squares is zero."""

    pairs: int
    intercept: Decimal
    phi: Decimal
    se_phi: Decimal
    t_phi: Decimal | None
    t_unit: Decimal | None
    half_life_minutes: Decimal | None


def dimensionless(close: Decimal, vwap: Decimal, atr: Decimal) -> Decimal:
    """z = (P - VWAP) / ATR. Doubling the gap and the ATR leaves z unchanged."""

    if atr <= 0:
        raise ValueError("atr")
    return (close - vwap) / atr


def adf_critical(pairs: int) -> Decimal:
    """MacKinnon 5% critical value for an AR(1) with an intercept. Reject when t is below it."""

    if pairs < 2:
        raise ValueError("pairs")
    width = Decimal(pairs)
    b0, b1, b2, b3 = ADF_C_5
    return b0 + b1 / width + b2 / (width**2) + b3 / (width**3)


def half_life_minutes(phi: Decimal) -> Decimal:
    """tau = -ln(2) / ln(phi) * bar minutes. phi has to sit in (0, 1)."""

    if not Decimal(0) < phi < Decimal(1):
        raise ValueError("phi")
    raw = -Decimal(2).ln() / phi.ln() * Decimal(BAR_MINUTES)
    return raw.quantize(HALF_LIFE_QUANTUM, rounding=ROUND_HALF_UP)


def fit_ar1(values: tuple[Decimal, ...]) -> OuFit | None:
    """OLS of z_t on 1 and z_{t-1}. None means the lagged residual has no variance."""

    if len(values) != WINDOW:
        raise ValueError("AR(1) window")
    lagged = values[:-1]
    current = values[1:]
    count = Decimal(len(lagged))
    x_mean = sum(lagged, start=Decimal(0)) / count
    y_mean = sum(current, start=Decimal(0)) / count
    sxx = sum(((item - x_mean) ** 2 for item in lagged), start=Decimal(0))
    if sxx == 0:
        return None
    sxy = sum(
        ((x - x_mean) * (y - y_mean) for x, y in zip(lagged, current, strict=True)),
        start=Decimal(0),
    )
    phi = sxy / sxx
    intercept = y_mean - phi * x_mean
    sse = sum(
        ((y - intercept - phi * x) ** 2 for x, y in zip(lagged, current, strict=True)),
        start=Decimal(0),
    )
    df = count - Decimal(2)
    if df <= 0:
        raise RuntimeError("AR(1) degrees of freedom")
    if sse == 0:
        se = Decimal(0)
        t_phi = None
        t_unit = None
    else:
        se = (sse / df / sxx).sqrt()
        t_phi = phi / se
        t_unit = (phi - Decimal(1)) / se
    life = half_life_minutes(phi) if Decimal(0) < phi < Decimal(1) else None
    return OuFit(
        pairs=len(lagged),
        intercept=intercept,
        phi=phi,
        se_phi=se,
        t_phi=t_phi,
        t_unit=t_unit,
        half_life_minutes=life,
    )


def qualifies(fit: OuFit) -> str:
    """OK, or the first failed OU clause. A phi at or above 1 is the falling-knife guard's job."""

    if fit.phi >= 1:
        raise RuntimeError("diverging phi belongs to the falling-knife guard")
    if fit.phi <= 0:
        return "OU_PHI_NONPOSITIVE"
    if fit.t_unit is None or fit.t_phi is None:
        if fit.se_phi != 0:
            raise RuntimeError("missing t statistic")
    elif fit.t_unit >= adf_critical(fit.pairs):
        return "OU_NOT_STATIONARY"
    elif fit.t_phi <= T_PHI_CRIT:
        return "OU_NOT_SIGNIFICANT"
    if fit.half_life_minutes is None or fit.half_life_minutes > HALF_LIFE_MAX_MINUTES:
        return "OU_HALF_LIFE"
    return "OK"
