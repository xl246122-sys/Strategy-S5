"""Stage-13 account limits. These match the constitution; they are not a second dial."""

from decimal import Decimal

from bstocks_v2.constitution.guard import HWM_RETAINED, MAX_OPEN_POSITIONS, MIN_VIABLE_NOTIONAL

PLANNED_EQUITY = Decimal("1600")
RISK_FRACTION = Decimal("0.0025")
MAX_POSITION_FRACTION = Decimal("0.20")
COMMISSION_RATE = Decimal("0.0005")
HALF_SPREAD = Decimal("0.01")
PDT_WINDOW_SESSIONS = 5
PDT_MAX_DAY_TRADES = 3
UNLOCK_PHRASE = "INVESTOR_CONFIRM_UNLOCK_HALT_NEW_ENTRIES"
CODE_PDT = 486440
CODE_ASSET = 486439
DB_NAME = "portfolio_risk.sqlite"
FILE_NAME = "portfolio_risk.hmac.json"
OPEN_BUY_STATUSES = frozenset({"NEW", "ACCEPTED", "PARTIALLY_FILLED"})

__all__ = [
    "CODE_ASSET",
    "CODE_PDT",
    "COMMISSION_RATE",
    "DB_NAME",
    "FILE_NAME",
    "HALF_SPREAD",
    "HWM_RETAINED",
    "MAX_OPEN_POSITIONS",
    "MAX_POSITION_FRACTION",
    "MIN_VIABLE_NOTIONAL",
    "OPEN_BUY_STATUSES",
    "PDT_MAX_DAY_TRADES",
    "PDT_WINDOW_SESSIONS",
    "PLANNED_EQUITY",
    "RISK_FRACTION",
    "UNLOCK_PHRASE",
]
