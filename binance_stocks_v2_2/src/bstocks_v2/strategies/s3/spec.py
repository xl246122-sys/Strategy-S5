"""Frozen S3 settings. These were written before the validation run and are not fit to it."""

from datetime import date
from decimal import Decimal

TRAIN_START = date(2024, 11, 1)
TRAIN_END = date(2025, 10, 31)
VAL_START = date(2025, 11, 1)
VAL_END = date(2026, 4, 30)
LOCKBOX_START = date(2026, 5, 1)

# Opening range in minutes, and the number of closed 5-minute bars that form it.
HORIZONS: tuple[int, ...] = (5, 15, 30)
BARS_IN_RANGE: dict[int, int] = {5: 1, 15: 3, 30: 6}

# Same-bucket cumulative volume must already have 20 prior sessions, matching the feature store.
RVOL_BASELINE_DAYS = 20
RVOL_MIN = Decimal("1")
CEILING_ATR = Decimal("0.25")
STOP_ATR = Decimal("1")
TARGET_MULTIPLE = Decimal("1.5")
HOLD_SECONDS = 90 * 60
TTL_SECONDS = 10 * 60
RISK_BUDGET = Decimal("4")
ACCOUNT_EQUITY = Decimal("1600")
POSITION_FRACTION = Decimal("0.20")
HALF_SPREAD = Decimal("0.01")
COMMISSION_RATE = Decimal("0.0005")
PENETRATION = Decimal("0.02")

# Page CUSUM and a normal-mean SPRT. Neither constant is searched on validation PnL.
MAX_STEPS = 3
CUSUM_K = Decimal("0.50")
CUSUM_H = Decimal("1.00")
SPRT_DELTA = Decimal("0.50")
# ln((1 - 0.10) / 0.05) and ln(0.10 / 0.95), fixed before the run.
SPRT_UPPER = Decimal("2.8903717579")
SPRT_LOWER = Decimal("-2.2512917986")
LOCATION_MIN = Decimal("0.50")

# The sequential arm replaces the baseline only if the validation window clears this bar.
MIN_CLOSED = 30
EDGE = Decimal("2")
DRAWDOWN_RATIO = Decimal("1.2")
