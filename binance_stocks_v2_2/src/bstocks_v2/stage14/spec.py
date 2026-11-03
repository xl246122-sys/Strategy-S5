"""Frozen stage-14 rules. Written before the lockbox run and not fit to it."""

from datetime import date
from decimal import Decimal

from bstocks_v2.strategies.s1.spec import LOCKBOX_START, VAL_END, VAL_START

LOCKBOX_END = date(2026, 10, 30)
EMBARGO_SESSIONS = 2
ACCOUNT = Decimal("1600")
DSR_MIN = Decimal("0.95")
BOOTSTRAP_DRAWS = 400
BOOTSTRAP_SEED = 20261102
NEIGHBOR_DOWN = Decimal("0.85")
NEIGHBOR_UP = Decimal("1.15")
STRESS_COST_MULTIPLE = Decimal("2")
STRESS_DELTA_MULTIPLE = Decimal("2")
STRESS_LATENCIES_SECONDS = (1, 5)
STRESS_GAP = Decimal("1.02")
# Research stand-in for a missing historical price cage. Not searched.
CAGE_DOWN = Decimal("0.80")
CAGE_UP = Decimal("1.20")
# Stage 11 left the fill model unfitted and fixed the horizon at 2. This run fits
# drawdowns on the training window only, then does not touch the lockbox labels.
FILL_HORIZON = 2

# Configurations that were already compared on the validation window.
TRIALS: tuple[str, ...] = (
    "S1:kalman",
    "S1:rls",
    "S1:ols",
    "S2:rule",
    "S2:gate",
    "S3:5:baseline",
    "S3:5:sequential",
    "S3:15:baseline",
    "S3:15:sequential",
    "S3:30:baseline",
    "S3:30:sequential",
    "S4:baseline",
    "S4:guarded",
)

# Copied into the sealed lockbox on 2026-11-02. A later repair does not edit this
# list and does not rerun the lockbox. Section 9 makes any open P0 a no-go.
OPEN_P0_P1: tuple[str, ...] = (
    "stage3_key_exposure_not_rotated",
    "point_in_time_market_cap_empty",
    "binance_sip_overlap_empty",
)

if VAL_END >= LOCKBOX_START:
    raise RuntimeError("validation runs into the lockbox")
if len(TRIALS) != 13:
    raise RuntimeError("trial count drifted")
if VAL_START != date(2025, 11, 1):
    raise RuntimeError("validation start drifted")
