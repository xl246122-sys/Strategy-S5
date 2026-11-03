"""Frozen stage-10 gates. These are not fit to a validation sample."""

from datetime import timedelta
from decimal import Decimal

# Six closed 5-minute bars. One call can override this; the default does not move.
COOLDOWN = timedelta(minutes=30)
# Five names cannot share one parent. Two is the cap, including open and pending names.
MAX_PER_SECTOR = 2
COMMISSION_RATE = Decimal("0.0005")
ASSUMED_HALF_SPREAD = Decimal("0.01")
SCORE_QUANTUM = Decimal("0.00000001")
ADMITTED = frozenset({"S1", "S2", "S3", "S4"})
