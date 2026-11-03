"""Path-probability baselines. Validation dates are scored and never fit."""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from bstocks_v2.contracts.decimals import parse_decimal
from bstocks_v2.strategies.s5.spec import (
    ECE_BINS,
    LAPLACE,
    LOGISTIC_L2,
    LOGISTIC_MARGIN,
    LOGISTIC_RATE,
    LOGISTIC_STEPS,
    MIN_CLASS,
    MIN_RESOLVED,
    PATHS,
    research_split,
)


@dataclass(frozen=True)
class Sample:
    session: date
    label: str
    features: tuple[Decimal, Decimal]

    def __post_init__(self) -> None:
        if self.label not in PATHS:
            raise ValueError("path label")
        object.__setattr__(
            self,
            "features",
            (parse_decimal(self.features[0]), parse_decimal(self.features[1])),
        )


Probabilities = tuple[Decimal, Decimal, Decimal, Decimal]


@dataclass(frozen=True)
class FrequencyModel:
    kind: str
    note: str
    probabilities: tuple[Decimal, Decimal, Decimal, Decimal]
    validation_brier: Decimal | None
    validation_ece: Decimal | None
    train_rows: int

    def predict(
        self,
        features: tuple[Decimal, Decimal],
    ) -> tuple[Decimal, Decimal, Decimal, Decimal]:
        del features
        return self.probabilities


@dataclass(frozen=True)
class LogisticModel:
    kind: str
    note: str
    weights: tuple[tuple[Decimal, Decimal, Decimal], ...]
    means: tuple[Decimal, Decimal]
    scales: tuple[Decimal, Decimal]
    validation_brier: Decimal | None
    validation_ece: Decimal | None
    train_rows: int

    def predict(
        self,
        features: tuple[Decimal, Decimal],
    ) -> tuple[Decimal, Decimal, Decimal, Decimal]:
        scaled = (
            (features[0] - self.means[0]) / self.scales[0],
            (features[1] - self.means[1]) / self.scales[1],
        )
        raw = [
            _sigmoid(weight[0] + weight[1] * scaled[0] + weight[2] * scaled[1])
            for weight in self.weights
        ]
        total = sum(raw, Decimal(0))
        return _normalize(raw if total > 0 else [Decimal(1), Decimal(1), Decimal(1), Decimal(1)])


PathModel = FrequencyModel | LogisticModel


def insufficient_model() -> FrequencyModel:
    quarter = Decimal("0.25")
    return FrequencyModel(
        kind="insufficient",
        note="executable path labels are below the pre-registered minimum",
        probabilities=(quarter, quarter, quarter, quarter),
        validation_brier=None,
        validation_ece=None,
        train_rows=0,
    )


def select_model(train: tuple[Sample, ...], validation: tuple[Sample, ...]) -> PathModel:
    """Fit on train only. Keep logistic only when validation Brier improves."""

    _require_split(train, "train")
    _require_split(validation, "validation")
    if not validation:
        raise ValueError("validation sample is empty")
    base = _frequency(train)
    chosen: PathModel = base
    note = base.note
    logistic = _logistic(train)
    if logistic is not None and base.kind == "frequency":
        base_score = brier_score(validation, base)
        logistic_score = brier_score(validation, logistic)
        if logistic_score <= base_score - LOGISTIC_MARGIN:
            chosen = logistic
            note = "logistic kept: validation Brier improved by the pre-registered margin"
        else:
            note = "frequency kept: logistic did not improve validation Brier enough"
    return _with_scores(chosen, validation, note)


def brier_score(samples: tuple[Sample, ...], model: PathModel) -> Decimal:
    if not samples:
        raise ValueError("empty score sample")
    total = Decimal(0)
    for sample in samples:
        probs = model.predict(sample.features)
        _check_probability(probs)
        for label, prob in zip(PATHS, probs, strict=True):
            target = Decimal(1) if sample.label == label else Decimal(0)
            total += (prob - target) * (prob - target)
    return total / Decimal(len(samples))


def expected_calibration_error(samples: tuple[Sample, ...], model: PathModel) -> Decimal:
    if not samples:
        raise ValueError("empty score sample")
    confidence_sum = [Decimal(0) for _ in range(ECE_BINS)]
    correct = [Decimal(0) for _ in range(ECE_BINS)]
    counts = [0 for _ in range(ECE_BINS)]
    for sample in samples:
        probs = model.predict(sample.features)
        _check_probability(probs)
        confidence = max(probs)
        predicted = PATHS[max(range(len(PATHS)), key=lambda index: (probs[index], -index))]
        slot = int(confidence * ECE_BINS)
        if slot >= ECE_BINS:
            slot = ECE_BINS - 1
        confidence_sum[slot] += confidence
        if predicted == sample.label:
            correct[slot] += 1
        counts[slot] += 1
    total = Decimal(len(samples))
    error = Decimal(0)
    for slot, count in enumerate(counts):
        if count == 0:
            continue
        size = Decimal(count)
        accuracy = correct[slot] / size
        mean_confidence = confidence_sum[slot] / size
        error += size / total * (accuracy - mean_confidence).copy_abs()
    return error


def _frequency(train: tuple[Sample, ...]) -> FrequencyModel:
    counts = _counts(train)
    resolved = counts["TP_FIRST"] + counts["SL_FIRST"] + counts["TIMEOUT"]
    kind = "frequency"
    note = "Laplace class frequencies fit on the train split only"
    if resolved < MIN_RESOLVED or counts["TP_FIRST"] < 1 or counts["SL_FIRST"] < 1:
        kind = "insufficient"
        note = "train split does not contain enough resolved TP and SL paths"
    if sum(counts.values()) == 0:
        probabilities = (Decimal("0.25"), Decimal("0.25"), Decimal("0.25"), Decimal("0.25"))
    else:
        probabilities = _normalize([Decimal(counts[label]) + LAPLACE for label in PATHS])
    return FrequencyModel(
        kind=kind,
        note=note,
        probabilities=probabilities,
        validation_brier=None,
        validation_ece=None,
        train_rows=sum(counts.values()),
    )


def _logistic(train: tuple[Sample, ...]) -> LogisticModel | None:
    counts = _counts(train)
    if any(counts[label] < MIN_CLASS for label in PATHS):
        return None
    raw = [sample.features for sample in train]
    means, scales = _moments(raw)
    scaled = [_scale(sample.features, means, scales) for sample in train]
    weights = []
    for label in PATHS:
        rows = [
            (point[0], point[1], Decimal(1) if sample.label == label else Decimal(0))
            for sample, point in zip(train, scaled, strict=True)
        ]
        weights.append(_fit_binary(rows))
    return LogisticModel(
        kind="logistic",
        note="one-vs-rest logistic, standardized on the train split",
        weights=tuple(weights),
        means=means,
        scales=scales,
        validation_brier=None,
        validation_ece=None,
        train_rows=len(train),
    )


def _fit_binary(rows: list[tuple[Decimal, Decimal, Decimal]]) -> tuple[Decimal, Decimal, Decimal]:
    intercept = slope_a = slope_b = Decimal(0)
    count = Decimal(len(rows))
    for _step in range(LOGISTIC_STEPS):
        grad_i = grad_a = grad_b = Decimal(0)
        for left, right, target in rows:
            probability = _sigmoid(intercept + slope_a * left + slope_b * right)
            error = probability - target
            grad_i += error
            grad_a += error * left
            grad_b += error * right
        intercept -= LOGISTIC_RATE * grad_i / count
        slope_a -= LOGISTIC_RATE * (grad_a / count + LOGISTIC_L2 * slope_a)
        slope_b -= LOGISTIC_RATE * (grad_b / count + LOGISTIC_L2 * slope_b)
    return intercept, slope_a, slope_b


def _moments(
    rows: list[tuple[Decimal, Decimal]],
) -> tuple[tuple[Decimal, Decimal], tuple[Decimal, Decimal]]:
    count = Decimal(len(rows))
    mean_a = sum((row[0] for row in rows), Decimal(0)) / count
    mean_b = sum((row[1] for row in rows), Decimal(0)) / count
    var_a = sum(((row[0] - mean_a) ** 2 for row in rows), Decimal(0)) / count
    var_b = sum(((row[1] - mean_b) ** 2 for row in rows), Decimal(0)) / count
    scale_a = var_a.sqrt() if var_a > 0 else Decimal(1)
    scale_b = var_b.sqrt() if var_b > 0 else Decimal(1)
    return (mean_a, mean_b), (scale_a, scale_b)


def _scale(
    features: tuple[Decimal, Decimal],
    means: tuple[Decimal, Decimal],
    scales: tuple[Decimal, Decimal],
) -> tuple[Decimal, Decimal]:
    return ((features[0] - means[0]) / scales[0], (features[1] - means[1]) / scales[1])


def _with_scores(model: PathModel, validation: tuple[Sample, ...], note: str) -> PathModel:
    scored_brier = brier_score(validation, model)
    scored_ece = expected_calibration_error(validation, model)
    if isinstance(model, FrequencyModel):
        return FrequencyModel(
            model.kind,
            note,
            model.probabilities,
            scored_brier,
            scored_ece,
            model.train_rows,
        )
    if isinstance(model, LogisticModel):
        return LogisticModel(
            model.kind,
            note,
            model.weights,
            model.means,
            model.scales,
            scored_brier,
            scored_ece,
            model.train_rows,
        )
    raise TypeError("unknown path model")


def _counts(samples: tuple[Sample, ...]) -> dict[str, int]:
    counts = dict.fromkeys(PATHS, 0)
    for sample in samples:
        counts[sample.label] += 1
    return counts


def _require_split(samples: tuple[Sample, ...], split: str) -> None:
    if not samples:
        raise ValueError(f"{split} sample is empty")
    for sample in samples:
        found = research_split(sample.session)
        if found != split:
            raise ValueError(f"refusing {found} sample while fitting {split}")


def _normalize(values: list[Decimal]) -> tuple[Decimal, Decimal, Decimal, Decimal]:
    total = sum(values, Decimal(0))
    if total <= 0:
        raise ValueError("probabilities are empty")
    first = [item / total for item in values[:3]]
    last = Decimal(1) - sum(first, Decimal(0))
    return (first[0], first[1], first[2], last)


def _check_probability(probs: tuple[Decimal, Decimal, Decimal, Decimal]) -> None:
    if len(probs) != 4:
        raise ValueError("probability width")
    if any(item < 0 for item in probs):
        raise ValueError("negative probability")
    if abs(sum(probs, Decimal(0)) - 1) > Decimal("0.000000001"):
        raise ValueError("probabilities must sum to 1")


def _sigmoid(value: Decimal) -> Decimal:
    if value > 20:
        return Decimal(1) - Decimal("1e-9")
    if value < -20:
        return Decimal("1e-9")
    return Decimal(1) / (Decimal(1) + (-value).exp())
