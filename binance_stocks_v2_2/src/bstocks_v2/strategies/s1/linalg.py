"""3x3 arithmetic for the S1 filters. Prices never enter this module."""

from decimal import Decimal

Vec = tuple[Decimal, Decimal, Decimal]
Mat = tuple[Vec, Vec, Vec]
_EPS = Decimal("1e-18")
_ZERO = Decimal(0)
_ONE = Decimal(1)


def dot(left: Vec, right: Vec) -> Decimal:
    return left[0] * right[0] + left[1] * right[1] + left[2] * right[2]


def add(left: Vec, right: Vec) -> Vec:
    return (left[0] + right[0], left[1] + right[1], left[2] + right[2])


def scale(vector: Vec, factor: Decimal) -> Vec:
    return (vector[0] * factor, vector[1] * factor, vector[2] * factor)


def matvec(matrix: Mat, vector: Vec) -> Vec:
    return (dot(matrix[0], vector), dot(matrix[1], vector), dot(matrix[2], vector))


def outer(left: Vec, right: Vec) -> Mat:
    return (
        (left[0] * right[0], left[0] * right[1], left[0] * right[2]),
        (left[1] * right[0], left[1] * right[1], left[1] * right[2]),
        (left[2] * right[0], left[2] * right[1], left[2] * right[2]),
    )


def transpose(matrix: Mat) -> Mat:
    return (
        (matrix[0][0], matrix[1][0], matrix[2][0]),
        (matrix[0][1], matrix[1][1], matrix[2][1]),
        (matrix[0][2], matrix[1][2], matrix[2][2]),
    )


def madd(left: Mat, right: Mat) -> Mat:
    return tuple(
        tuple(left[row][col] + right[row][col] for col in range(3)) for row in range(3)
    )  # type: ignore[return-value]


def mscale(matrix: Mat, factor: Decimal) -> Mat:
    return tuple(
        tuple(matrix[row][col] * factor for col in range(3)) for row in range(3)
    )  # type: ignore[return-value]


def msub(left: Mat, right: Mat) -> Mat:
    return madd(left, mscale(right, Decimal(-1)))


def matmul(left: Mat, right: Mat) -> Mat:
    flipped = transpose(right)
    return tuple(tuple(dot(left[row], flipped[col]) for col in range(3)) for row in range(3))  # type: ignore[return-value]


def identity() -> Mat:
    return (
        (_ONE, _ZERO, _ZERO),
        (_ZERO, _ONE, _ZERO),
        (_ZERO, _ZERO, _ONE),
    )


def diagonal(value: Decimal) -> Mat:
    return (
        (value, _ZERO, _ZERO),
        (_ZERO, value, _ZERO),
        (_ZERO, _ZERO, value),
    )


def symmetrize(matrix: Mat) -> Mat:
    flipped = transpose(matrix)
    half = Decimal("0.5")
    return mscale(madd(matrix, flipped), half)


def solve(matrix: Mat, target: Vec) -> Vec | None:
    """Gaussian elimination. A singular matrix returns None instead of a fake solution."""

    rows = [
        [matrix[row][0], matrix[row][1], matrix[row][2], target[row]]
        for row in range(3)
    ]
    for col in range(3):
        pivot = max(range(col, 3), key=lambda row: abs(rows[row][col]))
        if abs(rows[pivot][col]) <= _EPS:
            return None
        rows[col], rows[pivot] = rows[pivot], rows[col]
        divisor = rows[col][col]
        for column in range(col, 4):
            rows[col][column] /= divisor
        for row in range(3):
            if row == col:
                continue
            factor = rows[row][col]
            if factor == 0:
                continue
            for column in range(col, 4):
                rows[row][column] -= factor * rows[col][column]
    return (rows[0][3], rows[1][3], rows[2][3])


def invert(matrix: Mat) -> Mat | None:
    eyes: tuple[Vec, Vec, Vec] = (
        (Decimal(1), Decimal(0), Decimal(0)),
        (Decimal(0), Decimal(1), Decimal(0)),
        (Decimal(0), Decimal(0), Decimal(1)),
    )
    columns: list[Vec] = []
    for eye in eyes:
        column = solve(matrix, eye)
        if column is None:
            return None
        columns.append(column)
    return (
        (columns[0][0], columns[1][0], columns[2][0]),
        (columns[0][1], columns[1][1], columns[2][1]),
        (columns[0][2], columns[1][2], columns[2][2]),
    )
