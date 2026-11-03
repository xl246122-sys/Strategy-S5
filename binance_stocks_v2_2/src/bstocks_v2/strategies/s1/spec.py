"""Frozen S1 settings. These were written before the validation run and are not fit to it."""

from datetime import date
from decimal import Decimal

TRAIN_START = date(2024, 11, 1)
TRAIN_END = date(2025, 10, 31)
VAL_START = date(2025, 11, 1)
VAL_END = date(2026, 4, 30)
LOCKBOX_START = date(2026, 5, 1)

# One regular session of 5-minute bars. All three filters stay silent until this many updates.
WINDOW = 78
MIN_UPDATES = 78
RS_BARS = 6
Z_MIN = Decimal("2")
LAMBDA = Decimal("0.98")
RLS_PRIOR = Decimal("100")
KALMAN_STATE_VAR = Decimal("0.0001")
KALMAN_OBS_VAR = Decimal("1")
STOP_ATR = Decimal("1")
TARGET_MULTIPLE = Decimal("1.5")
VWAP_EXTENSION_ATR = Decimal("1")
HOLD_SECONDS = 90 * 60
TTL_SECONDS = 10 * 60
RISK_BUDGET = Decimal("4")
ACCOUNT_EQUITY = Decimal("1600")
MAX_POSITION = Decimal("320")
HALF_SPREAD = Decimal("0.01")
COMMISSION_RATE = Decimal("0.0005")

# Industry ETF to its Select Sector SPDR parent. Assigned from the fund's stated market,
# not from realized correlation. Broad beta, bond, and ambiguous names are omitted.
SECTOR_PARENT: dict[str, str] = {
    "AIQ": "XLK",
    "AMLP": "XLE",
    "BOTZ": "XLK",
    "CIBR": "XLK",
    "COPX": "XLB",
    "FDN": "XLK",
    "GDX": "XLB",
    "GDXJ": "XLB",
    "GRID": "XLU",
    "HACK": "XLK",
    "IAI": "XLF",
    "IBB": "XLV",
    "IGV": "XLK",
    "IHF": "XLV",
    "IHI": "XLV",
    "ITA": "XLI",
    "ITB": "XLY",
    "IYR": "XLRE",
    "KBE": "XLF",
    "KRE": "XLF",
    "OIH": "XLE",
    "PPA": "XLI",
    "REMX": "XLB",
    "SIL": "XLB",
    "SKYY": "XLK",
    "SMH": "XLK",
    "SOXQ": "XLK",
    "SOXX": "XLK",
    "VNQ": "XLRE",
    "VPU": "XLU",
    "XAR": "XLI",
    "XBI": "XLV",
    "XHB": "XLY",
    "XOP": "XLE",
    "XRT": "XLY",
}

TRADED: tuple[str, ...] = tuple(sorted(SECTOR_PARENT))
PARENTS: tuple[str, ...] = tuple(sorted(set(SECTOR_PARENT.values())))
MODELS: tuple[str, ...] = ("kalman", "rls", "ols")
