"""Causal S5 features. A bar that ends after the decision is not a feature."""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from bstocks_v2.contracts.decimals import parse_decimal
from bstocks_v2.feature_store.errors import LookaheadError
from bstocks_v2.feature_store.hashing import sha256_rows
from bstocks_v2.research_data.calendar import NEW_YORK
from bstocks_v2.strategies.s5.spec import (
    ARM_A,
    ARM_B,
    ARM_C,
    B0_MAX_SIMPLE_RETURN,
    B1_MAX_Z,
    BETA_MIN_SESSIONS,
    BETA_WINDOW,
    RVOL_MIN_SESSIONS,
    SCHEMA,
)


@dataclass(frozen=True)
class ClosedBar:
    symbol: str
    start: datetime
    end: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    session: str
    feed: str = "sip"

    def __post_init__(self) -> None:
        if self.start.tzinfo is None or self.end.tzinfo is None:
            raise ValueError("bar timestamps must be timezone-aware")
        if self.end <= self.start:
            raise ValueError("bar end must be after bar start")
        for name in ("open", "high", "low", "close", "volume"):
            object.__setattr__(self, name, parse_decimal(getattr(self, name)))
        if self.high < self.low or self.close < self.low or self.close > self.high:
            raise ValueError("bar prices are inconsistent")
        if self.volume < 0 or self.close <= 0:
            raise ValueError("bar volume or close is unusable")
        if self.session not in {"PRE", "RTH", "POST", "OVERNIGHT"}:
            raise ValueError("bar session")


@dataclass(frozen=True)
class PriorDay:
    """One finished session. Its date must be strictly before the decision date."""

    session: date
    symbol_log: Decimal
    market_log: Decimal
    sector_log: Decimal

    def __post_init__(self) -> None:
        for name in ("symbol_log", "market_log", "sector_log"):
            object.__setattr__(self, name, parse_decimal(getattr(self, name)))


def arm_window(arm: str, rth_close: time) -> tuple[time, time]:
    """Inclusive clock bounds. Half days move with the official close."""

    close_m = rth_close.hour * 60 + rth_close.minute
    if arm == ARM_A:
        return _clock(close_m - 30), _clock(close_m - 6)
    if arm == ARM_B:
        return _clock(close_m + 5), _clock(close_m + 60)
    if arm == ARM_C:
        return _clock(close_m + 5), time(19, 30)
    raise ValueError("arm")


def clock_in_window(moment: datetime, start: time, end: time) -> bool:
    if moment.tzinfo is None:
        raise ValueError("bar timestamps must be timezone-aware")
    clock = moment.astimezone(NEW_YORK).timetz().replace(tzinfo=None)
    return start <= clock <= end


def require_closed(bars: tuple[ClosedBar, ...], decision_ts: datetime) -> None:
    if decision_ts.tzinfo is None:
        raise ValueError("decision timestamps must be timezone-aware")
    seen: set[datetime] = set()
    for bar in bars:
        if bar.end > decision_ts:
            raise LookaheadError("BAR_AFTER_DECISION")
        if bar.end in seen:
            raise ValueError("duplicate bar")
        seen.add(bar.end)


def simple_return(close: Decimal, prev_close: Decimal | None) -> Decimal | None:
    if prev_close is None or prev_close <= 0 or close <= 0:
        return None
    return close / prev_close - 1


def log_return(close: Decimal, prev_close: Decimal | None) -> Decimal | None:
    if prev_close is None or prev_close <= 0 or close <= 0:
        return None
    return (close / prev_close).ln()


def residual_z(
    symbol_log: Decimal | None,
    market_log: Decimal | None,
    sector_log: Decimal | None,
    prior: tuple[PriorDay, ...],
    decision_day: date,
) -> Decimal | None:
    """Beta and sigma use only sessions dated before the decision day."""

    fit = fit_residual(prior, decision_day)
    if fit is None or symbol_log is None or market_log is None or sector_log is None:
        return None
    _alpha, beta_m, beta_s, sigma = fit
    return (symbol_log - beta_m * market_log - beta_s * sector_log) / sigma


def fit_residual(
    prior: tuple[PriorDay, ...],
    decision_day: date,
) -> tuple[Decimal, Decimal, Decimal, Decimal] | None:
    usable = tuple(row for row in prior if row.session < decision_day)
    if len(usable) != len(prior):
        raise LookaheadError("PRIOR_NOT_PRIOR")
    ordered = tuple(sorted(usable, key=lambda row: row.session))[-BETA_WINDOW:]
    if len(ordered) < BETA_MIN_SESSIONS:
        return None
    count = Decimal(len(ordered))
    sx1 = sx2 = sy = sx1x1 = sx2x2 = sx1x2 = sx1y = sx2y = Decimal(0)
    for row in ordered:
        x1, x2, y = row.market_log, row.sector_log, row.symbol_log
        sx1 += x1
        sx2 += x2
        sy += y
        sx1x1 += x1 * x1
        sx2x2 += x2 * x2
        sx1x2 += x1 * x2
        sx1y += x1 * y
        sx2y += x2 * y
    coef = _solve(
        [
            [count, sx1, sx2],
            [sx1, sx1x1, sx1x2],
            [sx2, sx1x2, sx2x2],
        ],
        [sy, sx1y, sx2y],
    )
    if coef is None:
        return None
    alpha, beta_m, beta_s = coef
    residual = [
        row.symbol_log - alpha - beta_m * row.market_log - beta_s * row.sector_log
        for row in ordered
    ]
    dof = len(ordered) - 3
    rss = sum((item * item for item in residual), Decimal(0))
    if dof <= 0 or rss <= 0:
        return None
    return alpha, beta_m, beta_s, (rss / Decimal(dof)).sqrt()


def tape_state(window: tuple[ClosedBar, ...]) -> str:
    """Price structure inside the arm window. This is not a forecast."""

    if len(window) < 4:
        return "UNKNOWN"
    ordered = tuple(sorted(window, key=lambda bar: bar.end))
    lows = [bar.low for bar in ordered]
    running = lows[0]
    flags: list[bool] = []
    for low in lows:
        flags.append(low < running)
        if low < running:
            running = low
    last = ordered[-3:]
    made_lows = last[1].low < last[0].low and last[2].low < last[1].low
    if made_lows and last[2].low == min(lows):
        return "FALLING_KNIFE"
    half = len(flags) // 2
    early = sum(1 for flag in flags[:half] if flag)
    late = sum(1 for flag in flags[half:] if flag)
    higher_low = ordered[-1].low > min(bar.low for bar in ordered[:-1])
    up_close = ordered[-1].close > ordered[-1].open
    if late < early and higher_low and up_close:
        return "RECOVERY_CONFIRMATION"
    if late < early and higher_low:
        return "SELLING_EXHAUSTION"
    if flags[-1]:
        return "SELL_PRESSURE"
    return "UNKNOWN"


def vwap_fading(bars: tuple[ClosedBar, ...], decision_ts: datetime) -> bool:
    """True when the last deviation is less severe than an earlier one."""

    day = decision_ts.astimezone(NEW_YORK).date()
    session = [
        bar
        for bar in bars
        if bar.session == "RTH" and bar.end.astimezone(NEW_YORK).date() == day
    ]
    if len(session) < 4:
        return False
    ordered = tuple(sorted(session, key=lambda bar: bar.end))
    volume = sum((bar.volume for bar in ordered), Decimal(0))
    if volume <= 0:
        return False
    weighted = sum(
        ((bar.high + bar.low + bar.close) / 3 * bar.volume for bar in ordered),
        Decimal(0),
    )
    vwap = weighted / volume
    if vwap <= 0:
        return False
    deviations = [(bar.close - vwap) / vwap for bar in ordered]
    return deviations[-1] > min(deviations[:-1])


def rvol_faded(current: Decimal | None, prior: tuple[Decimal, ...]) -> bool:
    if current is None or len(prior) < RVOL_MIN_SESSIONS:
        return False
    ordered = sorted(parse_decimal(item) for item in prior)
    return current <= ordered[len(ordered) // 2]


def has_rth_gap(bars: tuple[ClosedBar, ...], decision_ts: datetime, rth_open: time) -> bool:
    day = decision_ts.astimezone(NEW_YORK).date()
    session = [
        bar
        for bar in bars
        if bar.session == "RTH" and bar.end.astimezone(NEW_YORK).date() == day
    ]
    if not session:
        return True
    ordered = tuple(sorted(session, key=lambda bar: bar.end))
    opened = datetime.combine(day, rth_open, tzinfo=NEW_YORK)
    if ordered[0].start > opened:
        return True
    return any(nxt.start > prev.end for prev, nxt in zip(ordered, ordered[1:], strict=False))


def drop_screen(simple: Decimal | None) -> bool:
    return simple is not None and simple <= B0_MAX_SIMPLE_RETURN


def z_screen(simple_pass: bool, z_value: Decimal | None) -> bool:
    return simple_pass and z_value is not None and z_value <= B1_MAX_Z


def feature_hash(rows: tuple[tuple[str, str], ...]) -> str:
    return sha256_rows((("schema", SCHEMA), *rows))


def _clock(minutes: int) -> time:
    if minutes < 0 or minutes >= 24 * 60:
        raise ValueError("arm window is outside the civil day")
    return time(minutes // 60, minutes % 60)


def _solve(matrix: list[list[Decimal]], target: list[Decimal]) -> list[Decimal] | None:
    size = len(target)
    board = [row[:] + [target[index]] for index, row in enumerate(matrix)]
    for col in range(size):
        pivot = max(range(col, size), key=lambda row: board[row][col].copy_abs())
        if board[pivot][col].copy_abs() < Decimal("1e-12"):
            return None
        board[col], board[pivot] = board[pivot], board[col]
        scale = board[col][col]
        for col_index in range(col, size + 1):
            board[col][col_index] /= scale
        for row in range(size):
            if row == col:
                continue
            factor = board[row][col]
            for col_index in range(col, size + 1):
                board[row][col_index] -= factor * board[col][col_index]
    return [board[row][size] for row in range(size)]


def decision_deadline(decision_ts: datetime, arm: str) -> datetime:
    local = decision_ts.astimezone(NEW_YORK)
    if arm == ARM_C:
        return datetime.combine(
            local.date() + timedelta(days=1),
            time(3, 30),
            tzinfo=NEW_YORK,
        )
    return datetime.combine(local.date(), time(19, 30), tzinfo=NEW_YORK)
