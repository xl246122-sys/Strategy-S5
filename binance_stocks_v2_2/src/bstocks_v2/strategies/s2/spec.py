"""Frozen S2 settings. These were written before the validation run and are not fit to it."""

from datetime import date
from decimal import Decimal

TRAIN_START = date(2024, 11, 1)
TRAIN_END = date(2025, 10, 31)
VAL_START = date(2025, 11, 1)
CAL_END = date(2026, 1, 30)
SCORE_START = date(2026, 2, 1)
VAL_END = date(2026, 4, 30)
LOCKBOX_START = date(2026, 5, 1)

# Pullback geometry. The dip must sit inside the six closed bars before the reclaim.
LOOKBACK = 6
BASE_BARS = 6
COOLDOWN_BARS = 6
RSI_DIP = Decimal(40)
RSI_RECLAIM = Decimal(50)
VWAP_EXTENSION_ATR = Decimal(1)
STOP_ATR = Decimal(1)
TARGET_MULTIPLE = Decimal("1.5")
HOLD_SECONDS = 90 * 60
TTL_SECONDS = 10 * 60
RISK_BUDGET = Decimal(4)
ACCOUNT_EQUITY = Decimal(1600)
POSITION_FRACTION = Decimal("0.20")
HALF_SPREAD = Decimal("0.01")
COMMISSION_RATE = Decimal("0.0005")
PENETRATION = Decimal("0.02")

# The logistic prior is a Gaussian MAP, not a posterior sample.
PRIOR_VAR_FEATURE = Decimal(1)
PRIOR_VAR_SECTOR = Decimal("0.25")
PRIOR_VAR_VOL = Decimal("0.25")
NEWTON_STEPS = 20
NEWTON_TOL = Decimal("1e-8")
MIN_FIT = 30
MIN_CALIBRATION = 20
MIN_SCORE_METRICS = 20
TEMPERATURE_MIN = Decimal("0.50")
TEMPERATURE_MAX = Decimal("2.00")
TEMPERATURE_STEP = Decimal("0.05")
GATE_MIN_NET_BPS = Decimal(0)
BOOTSTRAP_DRAWS = 400
BOOTSTRAP_SEED = 20261102
FEATURE_COUNT = 5
CLASSES = ("TP", "SL", "TO")
