"""Pre-registered S5 research settings.

These numbers were fixed before the train/validation screen. They are not
fit to one symbol or to a later winner.
"""

from datetime import date, time, timedelta
from decimal import Decimal

TRAIN_START = date(2024, 11, 1)
TRAIN_END = date(2025, 10, 31)
VAL_START = date(2025, 11, 1)
VAL_END = date(2026, 4, 30)
LOCKBOX_START = date(2026, 5, 1)

STRATEGY_ID = "S5_EOD_REVERSAL"
MODEL_VERSION = "s5-p3-v1"
SCHEMA = "s5-p3-1"

ARMS: tuple[str, ...] = ("A_RTH_TO_POST", "B_POST_TO_POST", "C_POST_TO_OVERNIGHT")
LEVELS: tuple[str, ...] = ("B0", "B1", "B2", "B3")
PATHS: tuple[str, ...] = ("TP_FIRST", "SL_FIRST", "TIMEOUT", "UNFILLABLE")

# Fixed drop. A print this far under the prior close is only a baseline screen.
B0_MAX_SIMPLE_RETURN = Decimal("-0.03")
# Residual must be at least two prior-session sigmas. Not a rebound claim.
B1_MAX_Z = Decimal("-2")
BETA_MIN_SESSIONS = 40
BETA_WINDOW = 60
RVOL_MIN_SESSIONS = 20

# Plan prices. A stop or target is not a guaranteed fill.
PRIMARY_NET_TARGET = Decimal("0.01")
NET_TARGETS: tuple[Decimal, ...] = (Decimal("0.01"), Decimal("0.015"), Decimal("0.02"))
PLANNED_STOP = Decimal("0.015")
PENETRATION = Decimal("0.01")
MAX_SPREAD_BPS = Decimal("20")
MIN_DEPTH = Decimal("100")
COMMISSION_RATE = Decimal("0.0005")
RISK_BUDGET = Decimal("4")
MIN_NOTIONAL = Decimal("120")
TTL = timedelta(minutes=5)
REGIME_STALE_AFTER = timedelta(minutes=5)

# One-vs-rest logistic is kept only when validation Brier improves by this much.
LOGISTIC_MARGIN = Decimal("0.01")
LOGISTIC_STEPS = 50
LOGISTIC_RATE = Decimal("0.5")
LOGISTIC_L2 = Decimal("0.1")
MIN_RESOLVED = 30
MIN_CLASS = 30
LAPLACE = Decimal(1)
ECE_BINS = 10
POST_EXIT = time(19, 30)
OVERNIGHT_EXIT = time(3, 30)
ARM_A = "A_RTH_TO_POST"
ARM_B = "B_POST_TO_POST"
ARM_C = "C_POST_TO_OVERNIGHT"


def research_split(day: date) -> str:
    """Label a session. The lockbox is named so a fitter can refuse it."""

    if TRAIN_START <= day <= TRAIN_END:
        return "train"
    if VAL_START <= day <= VAL_END:
        return "validation"
    if day >= LOCKBOX_START:
        return "lockbox"
    return "outside"
