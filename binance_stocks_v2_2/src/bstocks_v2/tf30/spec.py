"""Clock restated onto a 30-minute bar. Dimensionless gates stay on the five-minute specs."""

from datetime import timedelta

BAR_MINUTES = 30
SOURCE_MINUTES = 5
BARS_PER_BAR = 6

# RSI(6) on five minutes is 30 minutes, so the 30-minute book uses period 2.
RSI_PERIOD = 2
# ATR(14) on five minutes is 70 minutes. Period 2 is ready on the second bar,
# which is the first bar that can break the opening range.
ATR_PERIOD = 2
TREND_BARS = 2
SLOPE_LAG = 1

# 78 five-minute updates is one regular session. A session has 13 thirty-minute bars.
MIN_UPDATES = 13
# Six five-minute sector bars is 30 minutes. Two thirty-minute bars keep a sum.
RS_BARS = 2

LOOKBACK = 1
BASE_BARS = 1
COOLDOWN_BARS = 1

# The next closed 30-minute bar is the first one that can fill a paper limit.
ORDER_TTL = timedelta(minutes=BAR_MINUTES)
