"""Frozen S4 settings. These were written before the validation run and are not fit to it."""

from datetime import date
from decimal import Decimal

TRAIN_START = date(2024, 11, 1)
TRAIN_END = date(2025, 10, 31)
VAL_START = date(2025, 11, 1)
VAL_END = date(2026, 4, 30)
LOCKBOX_START = date(2026, 5, 1)

# Oversold bounce. RSI(6) leaves 30 while price is still at least this many ATR under VWAP.
RSI_OVERSOLD = Decimal(30)
Z_STRETCH = Decimal("0.25")
STOP_ATR = Decimal(1)
HOLD_SECONDS = 90 * 60
TTL_SECONDS = 10 * 60
BAR_MINUTES = 5
RISK_BUDGET = Decimal(4)
ACCOUNT_EQUITY = Decimal(1600)
POSITION_FRACTION = Decimal("0.20")
HALF_SPREAD = Decimal("0.01")
COMMISSION_RATE = Decimal("0.0005")
PENETRATION = Decimal("0.02")

# Same-session AR(1) on z = (close - VWAP) / ATR. Twenty-four closes are two hours.
WINDOW = 24
PAIRS = WINDOW - 1
RESIDUAL_DF = PAIRS - 2
# One-sided Student t 5% quantile at RESIDUAL_DF = 21. Frozen from the table, not from PnL.
T_PHI_CRIT = Decimal("1.720743")
# MacKinnon (2010) response surface for the constant-only Dickey-Fuller t, N=1, 5%.
# crit(T) = b0 + b1/T + b2/T^2 + b3/T^3, T = number of AR pairs.
ADF_C_5: tuple[Decimal, Decimal, Decimal, Decimal] = (
    Decimal("-2.86154"),
    Decimal("-2.8903"),
    Decimal("-4.234"),
    Decimal("-40.040"),
)
HALF_LIFE_MAX_MINUTES = Decimal(25)
HALF_LIFE_QUANTUM = Decimal("0.00000001")

# Falling knife. RVOL uses the feature store's same-minute median, matching S3's volume base.
RVOL_KNIFE = Decimal("2.5")
LONG_BODY_ATR = Decimal("0.75")
BREAK_BARS = 6
BREAK_RECENT = 3
MARKET_BAR_ATR = Decimal("0.75")
MARKET_RET_NORM = Decimal("-1.5")

# The guarded arm replaces the baseline only if validation clears this bar. Train does not vote.
MIN_CLOSED = 30
EDGE = Decimal(2)
DRAWDOWN_RATIO = Decimal("1.2")

if HALF_LIFE_MAX_MINUTES * 2 > HOLD_SECONDS // 60:
    raise RuntimeError("half-life cap must stay within half of the maximum hold")
if RESIDUAL_DF != 21 or PAIRS != 23:
    raise RuntimeError("the frozen t critical value is for 23 pairs")
