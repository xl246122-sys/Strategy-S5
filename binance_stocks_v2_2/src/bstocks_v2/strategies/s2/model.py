"""MAP multinomial logistic model. Sector and volatility offsets share a Gaussian prior."""

from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext

from bstocks_v2.strategies.s2.simplex import softmax, temper
from bstocks_v2.strategies.s2.spec import (
    CAL_END,
    CLASSES,
    FEATURE_COUNT,
    NEWTON_STEPS,
    NEWTON_TOL,
    SCORE_START,
    TRAIN_END,
    TRAIN_START,
    VAL_END,
)

_CONTEXT = Context(prec=28, rounding=ROUND_HALF_EVEN)
_PIVOT = Decimal("1e-18")
_FREE_VOLS = 2


class FitError(RuntimeError):
    """The design could not be solved. The gate stays shut."""


@dataclass(frozen=True)
class Sample:
    features: tuple[Decimal, Decimal, Decimal, Decimal, Decimal]
    sector_index: int
    vol_index: int
    label: int
    weight: Decimal


@dataclass(frozen=True)
class Fitted:
    mean: tuple[Decimal, ...]
    scale: tuple[Decimal, ...]
    theta: tuple[Decimal, ...]
    width: int
    parents: int
    vol_edges: tuple[Decimal, Decimal]

    def logits(
        self,
        features: tuple[Decimal, Decimal, Decimal, Decimal, Decimal],
        sector_index: int,
        rel_vol: Decimal,
    ) -> tuple[Decimal, Decimal, Decimal]:
        scaled = tuple(
            _scale(features[i], self.mean[i], self.scale[i]) for i in range(FEATURE_COUNT)
        )
        row = design(scaled, sector_index, vol_bucket(rel_vol, self.vol_edges), self.parents)
        first = _dot(self.theta[: self.width], row)
        second = _dot(self.theta[self.width :], row)
        return first, second, Decimal(0)

    def probabilities(
        self,
        features: tuple[Decimal, Decimal, Decimal, Decimal, Decimal],
        sector_index: int,
        rel_vol: Decimal,
        temperature: Decimal,
    ) -> tuple[Decimal, Decimal, Decimal]:
        return temper(self.logits(features, sector_index, rel_vol), temperature)


def split_role(day: date) -> str:
    if day < TRAIN_START or day > VAL_END:
        raise RuntimeError("outside the open window")
    if day <= TRAIN_END:
        return "train"
    if day <= CAL_END:
        return "cal"
    if day < SCORE_START:
        raise RuntimeError("gap between calibration and score")
    return "score"


def usable(role: str, outcome: str, rel_vol: Decimal | None, *, temperature: bool) -> bool:
    wanted = "cal" if temperature else "train"
    return role == wanted and rel_vol is not None and outcome in CLASSES


def vol_edges(values: list[Decimal]) -> tuple[Decimal, Decimal]:
    if len(values) < 2:
        raise FitError("not enough volatility observations")
    ordered = sorted(values)
    return _quantile(ordered, Decimal(1) / Decimal(3)), _quantile(ordered, Decimal(2) / Decimal(3))


def vol_bucket(value: Decimal, edges: tuple[Decimal, Decimal]) -> int:
    low, high = edges
    if value < low:
        return 0
    if value < high:
        return 1
    return 2


def deduped(rows: list[tuple[datetime, str, Sample]], cooldown: timedelta) -> list[Sample]:
    """A later overlap on the same symbol gets weight 0 and does not move the kept clock."""

    ordered = sorted(rows, key=lambda item: (item[1], item[0]))
    last: dict[str, datetime] = {}
    kept: list[Sample] = []
    for when, symbol, sample in ordered:
        previous = last.get(symbol)
        weight = sample.weight
        if previous is not None and when - previous < cooldown:
            weight = Decimal(0)
        elif weight > 0:
            last[symbol] = when
        kept.append(replace(sample, weight=weight))
    return kept


def design(
    features: tuple[Decimal, ...],
    sector_index: int,
    vol_index: int,
    parents: int,
) -> tuple[Decimal, ...]:
    if len(features) != FEATURE_COUNT or parents < 2:
        raise FitError("design")
    if sector_index < 0 or sector_index >= parents or vol_index < 0 or vol_index > 2:
        raise FitError("stratum")
    values = [Decimal(1), *features]
    for index in range(parents - 1):
        values.append(Decimal(1) if index == sector_index else Decimal(0))
    for index in range(_FREE_VOLS):
        values.append(Decimal(1) if index == vol_index else Decimal(0))
    return tuple(values)


def fit(
    samples: list[Sample],
    *,
    parents: int,
    vol_edges_pair: tuple[Decimal, Decimal],
    prior_feature: Decimal,
    prior_sector: Decimal,
    prior_vol: Decimal,
) -> Fitted:
    """Newton MAP. Coefficients use only the rows passed in, and weight 0 rows do not."""

    if prior_feature <= 0 or prior_sector <= 0 or prior_vol <= 0:
        raise FitError("prior")
    active = [sample for sample in samples if sample.weight > 0]
    if len(active) < 2:
        raise FitError("not enough weighted samples")
    mean, scale = _moments(active)
    width = 1 + FEATURE_COUNT + (parents - 1) + _FREE_VOLS
    prior = _priors(width, parents, prior_feature, prior_sector, prior_vol)
    encoded = [
        (
            design(
                _standardize(sample.features, mean, scale),
                sample.sector_index,
                sample.vol_index,
                parents,
            ),
            sample.label,
            sample.weight,
        )
        for sample in samples
    ]
    theta = [Decimal(0)] * (2 * width)
    with localcontext(_CONTEXT):
        current = _nll(encoded, theta, prior, width)
        for _ in range(NEWTON_STEPS):
            grad, hess = _derivatives(encoded, theta, prior, width)
            if max(abs(item) for item in grad) < NEWTON_TOL:
                break
            try:
                step = solve(hess, grad)
            except FitError:
                break
            accepted = _backtrack(encoded, theta, step, prior, width, current)
            if accepted is None:
                break
            theta, current, move = accepted
            if move < NEWTON_TOL:
                break
    return Fitted(mean, scale, tuple(theta), width, parents, vol_edges_pair)


def solve(matrix: list[list[Decimal]], rhs: list[Decimal]) -> list[Decimal]:
    """Gaussian elimination with partial pivoting. The inputs are copied."""

    size = len(rhs)
    if size == 0 or any(len(row) != size for row in matrix):
        raise FitError("shape")
    board = [row[:] + [rhs[index]] for index, row in enumerate(matrix)]
    for col in range(size):
        pivot = max(range(col, size), key=lambda row: abs(board[row][col]))
        if abs(board[pivot][col]) < _PIVOT:
            raise FitError("singular")
        if pivot != col:
            board[col], board[pivot] = board[pivot], board[col]
        scale = board[col][col]
        for row in range(col + 1, size):
            factor = board[row][col] / scale
            if factor == 0:
                continue
            for cursor in range(col, size + 1):
                board[row][cursor] -= factor * board[col][cursor]
    answer = [Decimal(0)] * size
    for row in range(size - 1, -1, -1):
        total = board[row][size]
        for col in range(row + 1, size):
            total -= board[row][col] * answer[col]
        answer[row] = total / board[row][row]
    return answer


def _backtrack(
    encoded: list[tuple[tuple[Decimal, ...], int, Decimal]],
    theta: list[Decimal],
    step: list[Decimal],
    prior: tuple[Decimal, ...],
    width: int,
    current: Decimal,
) -> tuple[list[Decimal], Decimal, Decimal] | None:
    scale = Decimal(1)
    for _ in range(10):
        trial = [theta[index] - scale * step[index] for index in range(len(theta))]
        trial_nll = _nll(encoded, trial, prior, width)
        if trial_nll < current:
            move = max(abs(scale * step[index]) for index in range(len(theta)))
            return trial, trial_nll, move
        scale /= Decimal(2)
    return None


def _derivatives(
    encoded: list[tuple[tuple[Decimal, ...], int, Decimal]],
    theta: list[Decimal],
    prior: tuple[Decimal, ...],
    width: int,
) -> tuple[list[Decimal], list[list[Decimal]]]:
    size = len(theta)
    grad = [Decimal(0)] * size
    hess = [[Decimal(0)] * size for _ in range(size)]
    for row, label, weight in encoded:
        if weight <= 0:
            continue
        probs = softmax(_etas(theta, row, width))
        for cls in range(2):
            residual = probs[cls] - (Decimal(1) if label == cls else Decimal(0))
            base = cls * width
            for index, value in enumerate(row):
                grad[base + index] += weight * residual * value
        for left in range(2):
            for right in range(2):
                shared = probs[left] * (
                    (Decimal(1) if left == right else Decimal(0)) - probs[right]
                )
                left_base = left * width
                right_base = right * width
                for left_index, left_value in enumerate(row):
                    for right_index, right_value in enumerate(row):
                        hess[left_base + left_index][right_base + right_index] += (
                            weight * shared * left_value * right_value
                        )
    for index in range(size):
        penalty = prior[index % width]
        grad[index] += theta[index] / penalty
        hess[index][index] += Decimal(1) / penalty
    return grad, hess


def _nll(
    encoded: list[tuple[tuple[Decimal, ...], int, Decimal]],
    theta: list[Decimal],
    prior: tuple[Decimal, ...],
    width: int,
) -> Decimal:
    total = Decimal(0)
    for row, label, weight in encoded:
        if weight <= 0:
            continue
        prob = softmax(_etas(theta, row, width))[label]
        if prob <= 0:
            total += weight * Decimal(1000)
        else:
            total -= weight * prob.ln()
    for index, value in enumerate(theta):
        total += value * value / (Decimal(2) * prior[index % width])
    return total


def _etas(
    theta: list[Decimal], row: tuple[Decimal, ...], width: int
) -> tuple[Decimal, Decimal, Decimal]:
    return _dot(theta[:width], row), _dot(theta[width:], row), Decimal(0)


def _moments(
    samples: list[Sample],
) -> tuple[tuple[Decimal, ...], tuple[Decimal, ...]]:
    count = len(samples)
    means: list[Decimal] = []
    scales: list[Decimal] = []
    width = Decimal(count)
    for index in range(FEATURE_COUNT):
        column = [sample.features[index] for sample in samples]
        center = sum(column, start=Decimal(0)) / width
        if count < 2:
            scale = Decimal(0)
        else:
            variance = sum(((item - center) ** 2 for item in column), start=Decimal(0)) / Decimal(
                count - 1
            )
            scale = Decimal(0) if variance == 0 else variance.sqrt()
        means.append(center)
        scales.append(scale)
    return tuple(means), tuple(scales)


def _standardize(
    features: tuple[Decimal, Decimal, Decimal, Decimal, Decimal],
    mean: tuple[Decimal, ...],
    scale: tuple[Decimal, ...],
) -> tuple[Decimal, ...]:
    return tuple(
        _scale(features[index], mean[index], scale[index]) for index in range(FEATURE_COUNT)
    )


def _scale(value: Decimal, center: Decimal, width: Decimal) -> Decimal:
    if width == 0:
        return Decimal(0)
    return (value - center) / width


def _priors(
    width: int,
    parents: int,
    feature: Decimal,
    sector: Decimal,
    vol: Decimal,
) -> tuple[Decimal, ...]:
    values = [feature] * (1 + FEATURE_COUNT)
    values.extend([sector] * (parents - 1))
    values.extend([vol] * _FREE_VOLS)
    if len(values) != width:
        raise FitError("prior length")
    return tuple(values)


def _quantile(ordered: list[Decimal], q: Decimal) -> Decimal:
    if len(ordered) == 1:
        return ordered[0]
    pos = Decimal(len(ordered) - 1) * q
    lower = int(pos)
    upper = min(lower + 1, len(ordered) - 1)
    frac = pos - Decimal(lower)
    return ordered[lower] * (Decimal(1) - frac) + ordered[upper] * frac


def _dot(left: tuple[Decimal, ...] | list[Decimal], right: tuple[Decimal, ...]) -> Decimal:
    if len(left) != len(right):
        raise FitError("dot")
    return sum((left[index] * right[index] for index in range(len(right))), start=Decimal(0))
