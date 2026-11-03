"""Frozen stage-11 execution constants. Written before the tests, not fit to a fill sample."""

from decimal import Decimal

TICK = Decimal("0.01")
# One cent under the bid. The passive candidate is that pullback, then truncated down.
PASSIVE_PULLBACK_TICKS = 1
# Utility horizon is two closed 5-minute bars. Callers may select 1 or 3. They may not select 4.
HORIZON_BARS = frozenset({1, 2, 3})
DEFAULT_HORIZON_BARS = 2
BAR_SECONDS = 300
# Stage 4's fixed penetration. A historical bar fills only when low <= limit - delta.
PENETRATION_DELTA = Decimal("0.02")
# CVaR weight. One keeps the penalty in USDC, the same unit as expected net.
LAMBDA_CVAR = Decimal("1")
CVAR_ALPHA = Decimal("0.95")
COMMISSION_RATE = Decimal("0.0005")
HALF_SPREAD = Decimal("0.01")
MAX_FRICTION_RATIO = Decimal("0.20")
MIN_VIABLE_NOTIONAL = Decimal("120")
MAX_POSITION_FRACTION = Decimal("0.20")
# Laplace smoothing. With n historical drawdowns, P = (hits + 1) / (n + 2).
LAPLACE_ADD = 1
TIER_ORDER = ("passive", "retest", "aggressive")
