"""Stage 8 still passes one S3 intent. Stage 10 ranks S1–S4 and does not send orders."""

from bstocks_v2.signal_arbitrator.stage8 import arbitrate
from bstocks_v2.signal_arbitrator.stage10 import SignalArbitrator

__all__ = ["SignalArbitrator", "arbitrate"]
