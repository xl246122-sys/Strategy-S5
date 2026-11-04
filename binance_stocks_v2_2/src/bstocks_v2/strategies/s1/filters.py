"""Causal coefficient filters. There is no smoother in this module."""

from collections import deque
from decimal import Decimal

from bstocks_v2.strategies.s1.linalg import (
    Mat,
    Vec,
    add,
    diagonal,
    dot,
    identity,
    invert,
    madd,
    matmul,
    matvec,
    mscale,
    msub,
    outer,
    scale,
    solve,
    symmetrize,
    transpose,
)
from bstocks_v2.strategies.s1.spec import (
    KALMAN_OBS_VAR,
    KALMAN_STATE_VAR,
    LAMBDA,
    MIN_UPDATES,
    RLS_PRIOR,
    WINDOW,
)

_ZERO = Decimal(0)


class Fit:
    """Coefficients after this bar. `solved` is false during warmup or a singular window."""

    __slots__ = (
        "alpha",
        "beta_market",
        "beta_sector",
        "variance_alpha",
        "updates",
        "solved",
    )

    def __init__(
        self,
        alpha: Decimal,
        beta_market: Decimal,
        beta_sector: Decimal,
        variance_alpha: Decimal | None,
        updates: int,
        solved: bool,
    ) -> None:
        self.alpha = alpha
        self.beta_market = beta_market
        self.beta_sector = beta_sector
        self.variance_alpha = variance_alpha
        self.updates = updates
        self.solved = solved

    @property
    def z_alpha(self) -> Decimal | None:
        return self.z_after(MIN_UPDATES)

    def z_after(self, min_updates: int) -> Decimal | None:
        variance = self.variance_alpha
        if not self.solved or variance is None or variance <= 0 or self.updates < min_updates:
            return None
        return self.alpha / variance.sqrt()


def _fit(theta: Vec | None, variance: Decimal | None, updates: int) -> Fit:
    if theta is None:
        return Fit(_ZERO, _ZERO, _ZERO, None, updates, False)
    return Fit(theta[0], theta[1], theta[2], variance, updates, True)


class ThroughOrigin:
    """Rolling through-origin slope of sector return on SPY. The window includes the current bar."""

    def __init__(self, window: int = WINDOW) -> None:
        if window < 2:
            raise ValueError("window")
        self.window = window
        self._rows: deque[tuple[Decimal, Decimal]] = deque(maxlen=window)
        self._asof: str | None = None
        self._perp: Decimal | None = None
        self._gamma: Decimal | None = None

    def observe(self, spy: Decimal, sector: Decimal, asof: str) -> tuple[Decimal, Decimal] | None:
        if self._asof == asof:
            if self._gamma is None or self._perp is None:
                return None
            return self._gamma, self._perp
        self._rows.append((spy, sector))
        self._asof = asof
        self._gamma = None
        self._perp = None
        if len(self._rows) < self.window:
            return None
        energy = sum((spy_ret * spy_ret for spy_ret, _sector in self._rows), start=_ZERO)
        if energy <= 0:
            return None
        cross = sum((spy_ret * sector_ret for spy_ret, sector_ret in self._rows), start=_ZERO)
        gamma = cross / energy
        perp = sector - gamma * spy
        self._gamma = gamma
        self._perp = perp
        return gamma, perp

    def residual_dot(self) -> Decimal | None:
        """Dot product of the current window's SPY returns and residuals. Orthogonal means zero."""

        if self._gamma is None or len(self._rows) < self.window:
            return None
        gamma = self._gamma
        return sum(
            (spy_ret * (sector_ret - gamma * spy_ret) for spy_ret, sector_ret in self._rows),
            start=_ZERO,
        )


class Strength:
    """Trailing sum of orthogonal sector returns. The current bar is included."""

    def __init__(self, length: int) -> None:
        if length < 1:
            raise ValueError("length")
        self.length = length
        self._rows: deque[Decimal] = deque(maxlen=length)
        self._asof: str | None = None

    def push(self, value: Decimal, asof: str) -> Decimal | None:
        if self._asof == asof:
            return self.value()
        self._rows.append(value)
        self._asof = asof
        return self.value()

    def value(self) -> Decimal | None:
        if len(self._rows) < self.length:
            return None
        return sum(self._rows, start=_ZERO)


class CausalKalman:
    """Random-walk regression updated by predict then update. No backward pass."""

    def __init__(self) -> None:
        self._theta: Vec = (_ZERO, _ZERO, _ZERO)
        self._p: Mat = identity()
        self._q: Mat = diagonal(KALMAN_STATE_VAR)
        self._r = KALMAN_OBS_VAR
        self.updates = 0
        self._variance: Decimal | None = None
        self._solved = False

    def update(self, regressors: Vec, target: Decimal) -> Fit:
        predicted = madd(self._p, self._q)
        projected = matvec(predicted, regressors)
        innovation_var = dot(regressors, projected) + self._r
        self.updates += 1
        if innovation_var <= 0:
            self._solved = False
            self._variance = None
            return _fit(None, None, self.updates)
        gain = scale(projected, Decimal(1) / innovation_var)
        innovation = target - dot(regressors, self._theta)
        self._theta = add(self._theta, scale(gain, innovation))
        eye_minus = msub(identity(), outer(gain, regressors))
        joseph = madd(
            matmul(matmul(eye_minus, predicted), transpose(eye_minus)),
            mscale(outer(gain, gain), self._r),
        )
        self._p = symmetrize(joseph)
        variance = self._p[0][0]
        if variance <= 0:
            self._solved = False
            self._variance = None
            return _fit(None, None, self.updates)
        self._solved = True
        self._variance = variance
        return _fit(self._theta, variance, self.updates)


class ForgettingRLS:
    """Recursive least squares. The forgetting factor is fixed, not estimated online."""

    def __init__(self) -> None:
        if not Decimal("0.96") <= LAMBDA <= Decimal("0.99"):
            raise ValueError("forgetting factor is outside [0.96, 0.99]")
        self._theta: Vec = (_ZERO, _ZERO, _ZERO)
        self._p: Mat = diagonal(RLS_PRIOR)
        self._lambda = LAMBDA
        self._sigma2 = Decimal(1)
        self.updates = 0
        self._variance: Decimal | None = None

    def update(self, regressors: Vec, target: Decimal) -> Fit:
        projected = matvec(self._p, regressors)
        denominator = self._lambda + dot(regressors, projected)
        self.updates += 1
        if denominator <= 0:
            return _fit(None, None, self.updates)
        gain = scale(projected, Decimal(1) / denominator)
        error = target - dot(regressors, self._theta)
        self._theta = add(self._theta, scale(gain, error))
        weight = Decimal(1) - self._lambda
        self._sigma2 = self._lambda * self._sigma2 + weight * error * error
        self._p = symmetrize(
            mscale(msub(self._p, outer(gain, projected)), Decimal(1) / self._lambda)
        )
        variance = self._sigma2 * self._p[0][0]
        if variance <= 0:
            self._variance = None
            return _fit(None, None, self.updates)
        self._variance = variance
        return _fit(self._theta, variance, self.updates)


class RollingOLS:
    """Fixed-window OLS. The window is the last N valid bars, including the current one."""

    def __init__(self, window: int = WINDOW) -> None:
        if window <= 3:
            raise ValueError("window")
        self.window = window
        self._rows: deque[tuple[Vec, Decimal]] = deque(maxlen=window)
        self.updates = 0

    def update(self, regressors: Vec, target: Decimal) -> Fit:
        self._rows.append((regressors, target))
        self.updates += 1
        if len(self._rows) < self.window:
            return _fit(None, None, self.updates)
        gram: Mat = (
            (_ZERO, _ZERO, _ZERO),
            (_ZERO, _ZERO, _ZERO),
            (_ZERO, _ZERO, _ZERO),
        )
        moment: Vec = (_ZERO, _ZERO, _ZERO)
        for regressor, observed in self._rows:
            gram = madd(gram, outer(regressor, regressor))
            moment = add(moment, scale(regressor, observed))
        theta = solve(gram, moment)
        inverse = invert(gram)
        if theta is None or inverse is None:
            return _fit(None, None, self.updates)
        residual = _ZERO
        for regressor, observed in self._rows:
            error = observed - dot(regressor, theta)
            residual += error * error
        degrees = Decimal(len(self._rows) - 3)
        variance = (residual / degrees) * inverse[0][0]
        if variance <= 0:
            return Fit(theta[0], theta[1], theta[2], None, self.updates, True)
        return _fit(theta, variance, self.updates)


def make_filter(model: str) -> CausalKalman | ForgettingRLS | RollingOLS:
    if model == "kalman":
        return CausalKalman()
    if model == "rls":
        return ForgettingRLS()
    if model == "ols":
        return RollingOLS()
    raise ValueError(model)

