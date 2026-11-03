"""Simplex probabilities, temperature, and the calibration scores."""

from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext

from bstocks_v2.strategies.s2.spec import CLASSES

_CONTEXT = Context(prec=28, rounding=ROUND_HALF_EVEN)
_LOG_FLOOR = Decimal(-1000)


@dataclass(frozen=True)
class BinRow:
    lower: Decimal
    upper: Decimal
    count: int
    mean_probability: Decimal | None
    observed_rate: Decimal | None


class LCG:
    """Park-Miller generator. Bootstrap draws do not go through binary floating point."""

    def __init__(self, seed: int) -> None:
        self._state = seed % 2147483647
        if self._state <= 0:
            self._state = 1

    def below(self, bound: int) -> int:
        if bound <= 0:
            raise RuntimeError("bound")
        self._state = (self._state * 48271) % 2147483647
        return self._state % bound


def softmax(logits: tuple[Decimal, Decimal, Decimal]) -> tuple[Decimal, Decimal, Decimal]:
    """Three-class softmax. The reference class is adjusted so the sum is exactly 1."""

    with localcontext(_CONTEXT):
        clipped = tuple(_clip(item) for item in logits)
        peak = max(clipped)
        weights = tuple((item - peak).exp() for item in clipped)
        total = sum(weights, start=Decimal(0))
        if total <= 0:
            raise RuntimeError("softmax")
        first = weights[0] / total
        second = weights[1] / total
        third = Decimal(1) - first - second
        if third < 0:
            scale = first + second
            first = first / scale
            second = Decimal(1) - first
            third = Decimal(0)
        return first, second, third


def temper(
    logits: tuple[Decimal, Decimal, Decimal], temperature: Decimal
) -> tuple[Decimal, Decimal, Decimal]:
    if temperature <= 0:
        raise RuntimeError("temperature")
    scaled = (
        logits[0] / temperature,
        logits[1] / temperature,
        logits[2] / temperature,
    )
    return softmax(scaled)


def choose_temperature(
    logits: list[tuple[Decimal, Decimal, Decimal]],
    labels: list[int],
    *,
    low: Decimal,
    high: Decimal,
    step: Decimal,
) -> Decimal:
    """Grid search on negative log likelihood. A tie keeps the temperature closer to 1."""

    if len(logits) != len(labels) or not logits:
        raise RuntimeError("temperature sample")
    if step <= 0 or low <= 0 or high < low:
        raise RuntimeError("temperature grid")
    best_t = low
    best_loss: Decimal | None = None
    cursor = low
    for _ in range(40):
        if cursor > high:
            break
        loss = _nll(logits, labels, cursor)
        distance = abs(cursor - Decimal(1))
        best_distance = abs(best_t - Decimal(1))
        if (
            best_loss is None
            or loss < best_loss
            or (loss == best_loss and distance < best_distance)
        ):
            best_loss = loss
            best_t = cursor
        cursor += step
    return best_t


def brier(rows: list[tuple[tuple[Decimal, Decimal, Decimal], int]]) -> Decimal:
    if not rows:
        raise RuntimeError("empty brier")
    total = Decimal(0)
    for probs, label in rows:
        _check_row(probs, label)
        for index, prob in enumerate(probs):
            outcome = Decimal(1) if index == label else Decimal(0)
            gap = prob - outcome
            total += gap * gap
    return total / Decimal(len(rows))


def confidence_ece(
    rows: list[tuple[tuple[Decimal, Decimal, Decimal], int]], bins: int = 10
) -> Decimal:
    """Expected calibration error of the predicted class. Empty bins do not enter."""

    if not rows or bins <= 0:
        raise RuntimeError("ece")
    counts = [0] * bins
    correct = [Decimal(0)] * bins
    confidence = [Decimal(0)] * bins
    for probs, label in rows:
        _check_row(probs, label)
        conf = max(probs)
        guess = probs.index(conf)
        bucket = _bucket(conf, bins)
        counts[bucket] += 1
        confidence[bucket] += conf
        if guess == label:
            correct[bucket] += Decimal(1)
    total = Decimal(len(rows))
    score = Decimal(0)
    for count, conf_sum, hits in zip(counts, confidence, correct, strict=True):
        if count == 0:
            continue
        width = Decimal(count)
        score += (width / total) * abs(hits / width - conf_sum / width)
    return score


def reliability(
    rows: list[tuple[tuple[Decimal, Decimal, Decimal], int]], bins: int = 10
) -> dict[str, list[BinRow]]:
    if not rows or bins <= 0:
        raise RuntimeError("reliability")
    tables = {name: _empty_bins(bins) for name in ("confidence", *CLASSES)}
    for probs, label in rows:
        _check_row(probs, label)
        conf = max(probs)
        guess = probs.index(conf)
        _add(tables["confidence"][_bucket(conf, bins)], conf, guess == label)
        for index, name in enumerate(CLASSES):
            _add(tables[name][_bucket(probs[index], bins)], probs[index], label == index)
    return {name: _freeze(items, bins) for name, items in tables.items()}


def percentile(ordered: list[Decimal], numer: int, denom: int) -> Decimal:
    """Smallest order statistic whose rank / n is at least numer / denom."""

    count = len(ordered)
    if count == 0 or denom <= 0 or numer < 0:
        raise RuntimeError("percentile")
    threshold = numer * count
    index = 0
    while (index + 1) * denom < threshold:
        index += 1
    if index >= count:
        index = count - 1
    return ordered[index]


def bootstrap_interval(
    values: list[Decimal], draws: int, seed: int
) -> tuple[Decimal, Decimal] | None:
    if len(values) < 2 or draws <= 0:
        return None
    rng = LCG(seed)
    count = len(values)
    stats: list[Decimal] = []
    width = Decimal(count)
    for _ in range(draws):
        total = sum((values[rng.below(count)] for _ in range(count)), start=Decimal(0))
        stats.append(total / width)
    stats.sort()
    return percentile(stats, 1, 40), percentile(stats, 39, 40)


def bootstrap_ece_gap(
    raw: list[tuple[tuple[Decimal, Decimal, Decimal], int]],
    calibrated: list[tuple[tuple[Decimal, Decimal, Decimal], int]],
    draws: int,
    seed: int,
) -> tuple[Decimal, Decimal]:
    if len(raw) != len(calibrated) or len(raw) < 2 or draws <= 0:
        raise RuntimeError("ece gap")
    rng = LCG(seed)
    count = len(raw)
    gaps: list[Decimal] = []
    for _ in range(draws):
        picked = [rng.below(count) for _ in range(count)]
        raw_rows = [raw[index] for index in picked]
        cal_rows = [calibrated[index] for index in picked]
        gaps.append(confidence_ece(raw_rows) - confidence_ece(cal_rows))
    gaps.sort()
    return percentile(gaps, 1, 40), percentile(gaps, 39, 40)


def _nll(
    logits: list[tuple[Decimal, Decimal, Decimal]],
    labels: list[int],
    temperature: Decimal,
) -> Decimal:
    total = Decimal(0)
    for row, label in zip(logits, labels, strict=True):
        if label < 0 or label > 2:
            raise RuntimeError("label")
        prob = temper(row, temperature)[label]
        total -= _log(prob)
    return total / Decimal(len(logits))


def _log(prob: Decimal) -> Decimal:
    if prob <= 0:
        return _LOG_FLOOR
    return prob.ln()


def _clip(value: Decimal) -> Decimal:
    if value > Decimal(20):
        return Decimal(20)
    if value < Decimal(-20):
        return Decimal(-20)
    return value


def _check_row(probs: tuple[Decimal, Decimal, Decimal], label: int) -> None:
    if label < 0 or label > 2 or probs[0] + probs[1] + probs[2] != 1:
        raise RuntimeError("probability row")


def _bucket(prob: Decimal, bins: int) -> int:
    if prob < 0 or prob > 1:
        raise RuntimeError("probability")
    slot = int(prob * bins)
    if slot >= bins:
        return bins - 1
    return slot


def _empty_bins(bins: int) -> list[list[Decimal]]:
    return [[Decimal(0), Decimal(0), Decimal(0)] for _ in range(bins)]


def _add(slot: list[Decimal], prob: Decimal, hit: bool) -> None:
    slot[0] += Decimal(1)
    slot[1] += prob
    if hit:
        slot[2] += Decimal(1)


def _freeze(slots: list[list[Decimal]], bins: int) -> list[BinRow]:
    rows: list[BinRow] = []
    width = Decimal(1) / Decimal(bins)
    for index, (count, prob_sum, hits) in enumerate(slots):
        lower = width * Decimal(index)
        upper = Decimal(1) if index == bins - 1 else lower + width
        if count == 0:
            rows.append(BinRow(lower, upper, 0, None, None))
            continue
        rows.append(BinRow(lower, upper, int(count), prob_sum / count, hits / count))
    return rows
