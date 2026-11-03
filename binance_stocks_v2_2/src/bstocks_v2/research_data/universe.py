"""Point-in-time membership. A later market-cap print cannot admit an earlier session."""

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from bstocks_v2.research_data.exclusions import hard_exclusion

CAP_MIN_USD = Decimal("5000000000")
PRICE_MIN_USD = Decimal("20")
ETF_DOLLAR_VOLUME_MIN = Decimal("20000000")
ETF_LIQUIDITY_SESSIONS = 20
CASH_PROXIES = frozenset({"SGOV", "BIL"})


@dataclass(frozen=True)
class Candidate:
    symbol: str
    asset_type: str
    window_start: date
    window_end: date
    entity_id: str
    alpaca_asof: str
    in_main_backtest_pool: bool
    anomaly_flags: str

    @property
    def is_etf(self) -> bool:
        return self.asset_type == "etf"


@dataclass(frozen=True)
class Membership:
    symbol: str
    session: date
    asset_type: str
    entity_id: str
    alpaca_asof: str
    research_eligible: bool
    reason: str


def value_before(series: list[tuple[date, Decimal]], session: date) -> Decimal | None:
    """Last observation strictly before the session."""

    chosen: tuple[date, Decimal] | None = None
    for day, value in series:
        if day < session and (chosen is None or day >= chosen[0]):
            chosen = (day, value)
    if chosen is None:
        return None
    return chosen[1]


def values_before(series: list[tuple[date, Decimal]], session: date, limit: int) -> list[Decimal]:
    ordered = [value for day, value in sorted(series) if day < session]
    return ordered[-limit:]


def reclassify_unverified_cap(
    reason: str,
    session: date,
    cap_observations: list[tuple[date, Decimal]],
) -> str:
    """Replace only the missing-cap reason. Earlier gates stay as they were."""

    if reason != "market_cap_unverified":
        return reason
    cap = pit_cap(cap_observations, session)
    if cap is None:
        return "market_cap_unverified"
    if cap < CAP_MIN_USD:
        return "market_cap_below"
    return "ok"


def pit_cap(observations: list[tuple[date, Decimal]], session: date) -> Decimal | None:
    """Latest cap whose as-of date is strictly before the session."""

    chosen: tuple[date, Decimal] | None = None
    for asof, cap in observations:
        if asof < session and (chosen is None or asof >= chosen[0]):
            chosen = (asof, cap)
    if chosen is None:
        return None
    return chosen[1]


def rename_window_end(symbol: str, window_end: date, anomaly_flags: str) -> date:
    """Stop an old ticker the day before a recorded rename."""

    marker = f"RENAMED_{symbol}_TO_"
    end = window_end
    for token in anomaly_flags.split(";"):
        text = token.strip()
        if marker not in text:
            continue
        tail = text.split(marker, 1)[1]
        pieces = tail.split("_")
        if len(pieces) < 2:
            continue
        try:
            renamed_on = date.fromisoformat(pieces[-1])
        except ValueError:
            continue
        day_before = renamed_on - timedelta(days=1)
        if day_before < end:
            end = day_before
    return end


def classify_membership(
    candidate: Candidate,
    session: date,
    *,
    buy_sell: set[str],
    known_symbols: set[str],
    prior_close: Decimal | None,
    prior_dollar_volumes: list[Decimal],
    cap_observations: list[tuple[date, Decimal]],
) -> Membership:
    reason = _reason(
        candidate,
        session,
        buy_sell=buy_sell,
        known_symbols=known_symbols,
        prior_close=prior_close,
        prior_dollar_volumes=prior_dollar_volumes,
        cap_observations=cap_observations,
    )
    return Membership(
        symbol=candidate.symbol,
        session=session,
        asset_type=candidate.asset_type,
        entity_id=candidate.entity_id,
        alpaca_asof=candidate.alpaca_asof,
        research_eligible=reason == "ok",
        reason=reason,
    )


def _reason(
    candidate: Candidate,
    session: date,
    *,
    buy_sell: set[str],
    known_symbols: set[str],
    prior_close: Decimal | None,
    prior_dollar_volumes: list[Decimal],
    cap_observations: list[tuple[date, Decimal]],
) -> str:
    excluded = hard_exclusion(candidate.symbol)
    if excluded is not None:
        return excluded
    if candidate.symbol not in known_symbols:
        return "not_in_exchange"
    if candidate.symbol not in buy_sell:
        return "not_buy_sell"
    if session < candidate.window_start or session > candidate.window_end:
        return "outside_entity_window"
    if candidate.symbol in CASH_PROXIES:
        return "cash_proxy"
    if not candidate.in_main_backtest_pool:
        return "entity_isolated"
    if prior_close is None:
        return "price_unavailable"
    if prior_close < PRICE_MIN_USD:
        return "price_below_min"
    if candidate.is_etf:
        return _etf_liquidity(prior_dollar_volumes)
    cap = pit_cap(cap_observations, session)
    if cap is None:
        return "market_cap_unverified"
    if cap < CAP_MIN_USD:
        return "market_cap_below"
    return "ok"


def _etf_liquidity(prior_dollar_volumes: list[Decimal]) -> str:
    if len(prior_dollar_volumes) < ETF_LIQUIDITY_SESSIONS:
        return "etf_liquidity_unready"
    ordered = sorted(prior_dollar_volumes[-ETF_LIQUIDITY_SESSIONS:])
    midpoint = ETF_LIQUIDITY_SESSIONS // 2
    median = (ordered[midpoint - 1] + ordered[midpoint]) / Decimal(2)
    if median < ETF_DOLLAR_VOLUME_MIN:
        return "etf_illiquid"
    return "ok"
