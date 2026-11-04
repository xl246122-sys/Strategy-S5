"""Causal five-state regime gate. UNKNOWN and HIGH_VOL_PANIC block new longs."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from bstocks_v2.contracts.decimals import Quantity, parse_decimal
from bstocks_v2.feature_store.bars import SymbolSnapshot
from bstocks_v2.feature_store.errors import LookaheadError
from bstocks_v2.feature_store.hashing import sha256_rows
from bstocks_v2.research_data.calendar import NEW_YORK

BENCHMARKS: tuple[str, ...] = (
    "SPY",
    "QQQ",
    "XLK",
    "XLF",
    "XLV",
    "XLY",
    "XLC",
    "XLI",
    "XLP",
    "XLE",
    "XLU",
    "XLRE",
    "XLB",
)
SECTORS: tuple[str, ...] = BENCHMARKS[2:]
OPEN_START = time(9, 30)
OPEN_END = time(10, 0)
STALE_AFTER = timedelta(minutes=5)
MIN_DWELL = 3
CONFIDENCE_MIN = Decimal("0.75")
ENTER_TREND = Decimal("1")
EXIT_TREND = Decimal("0.40")
SIDEWAYS_BAND = Decimal("0.50")
BREADTH_UP = Decimal("0.55")
BREADTH_DOWN = Decimal("0.45")
PANIC_RVOL = Decimal("2")
PANIC_RMS = Decimal("2.5")
PANIC_TREND = Decimal("-0.5")
AMBIGUOUS_CONFIDENCE = Decimal("0.5")


class Regime(StrEnum):
    TREND_UP = "TREND_UP"
    SIDEWAYS = "SIDEWAYS"
    TREND_DOWN = "TREND_DOWN"
    HIGH_VOL_PANIC = "HIGH_VOL_PANIC"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class Route:
    entries_allowed: bool
    s1_enabled: bool
    s2_enabled: bool
    s3_enabled: bool
    s4_research_enabled: bool


def route(regime: Regime, in_opening_window: bool) -> Route:
    """Strategy gate. S4 here is offline research permission, not a live switch.

    S3 is an all-session opening-range break. The 09:30-10:00 clock is kept on
    the snapshot and does not close the strategy. New entries still stop at
    15:45 through entry_window_open.
    """

    del in_opening_window
    if regime in {Regime.UNKNOWN, Regime.HIGH_VOL_PANIC}:
        return Route(False, False, False, False, False)
    s1 = regime == Regime.TREND_UP
    s3 = regime in {Regime.TREND_UP, Regime.SIDEWAYS}
    s4 = regime == Regime.SIDEWAYS
    return Route(True, s1, s1, s3, s4)


class FeatureSnapshot(BaseModel):
    """Immutable market snapshot. The hash covers features, not the latched label."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    data_asof_utc: datetime
    vwap_intact: bool
    features_hash: str
    regime: Regime
    confidence: Quantity
    entries_allowed: bool
    s1_enabled: bool
    s2_enabled: bool
    s3_enabled: bool
    s4_research_enabled: bool
    in_opening_window: bool
    data_ready: bool
    dwell_bars: int = Field(ge=1)
    block_reason: str
    benchmarks_present: int = Field(ge=0, le=len(BENCHMARKS))
    breadth: Decimal | None
    trend_z: Decimal | None
    spy_rvol: Decimal | None
    spy_rms6: Decimal | None

    @model_validator(mode="after")
    def _lock_the_gate(self) -> "FeatureSnapshot":
        if self.data_asof_utc.tzinfo is None:
            raise ValueError("data_asof_utc must be timezone-aware")
        if self.regime == Regime.UNKNOWN and self.confidence >= CONFIDENCE_MIN:
            raise ValueError("UNKNOWN requires confidence below 0.75")
        for name in ("breadth", "trend_z", "spy_rvol", "spy_rms6"):
            value = getattr(self, name)
            if value is not None:
                parse_decimal(value)
        expected = route(self.regime, self.in_opening_window)
        actual = Route(
            self.entries_allowed,
            self.s1_enabled,
            self.s2_enabled,
            self.s3_enabled,
            self.s4_research_enabled,
        )
        if actual != expected:
            raise ValueError("route does not match the regime matrix")
        return self


@dataclass(frozen=True)
class RegimeObservation:
    data_asof_utc: datetime
    fresh: bool
    benchmarks_complete: bool
    vwap_intact: bool
    warmed: bool
    trend_z: Decimal | None
    vwap_slope: Decimal | None
    spy_above_vwap: bool | None
    qqq_above_vwap: bool | None
    breadth: Decimal | None
    rvol_mean: Decimal | None
    rms6: Decimal | None
    in_opening_window: bool
    benchmarks_present: int
    symbol_hashes: tuple[tuple[str, str], ...]


@dataclass
class _Memory:
    regime: Regime = Regime.UNKNOWN
    dwell: int = 0
    calm: int = 0


class MarketRegimeClassifier:
    """Latched classifier. A later bar cannot change an earlier decision."""

    def __init__(self) -> None:
        self._memory = _Memory()

    def reset(self) -> None:
        self._memory = _Memory()

    def replay(self, observations: list[RegimeObservation]) -> list[FeatureSnapshot]:
        self.reset()
        return [self.step(item) for item in observations]

    def decide(
        self,
        latest: Mapping[str, SymbolSnapshot],
        decision_time: datetime,
    ) -> FeatureSnapshot:
        return self.step(build_observation(latest, decision_time))

    def step(self, observation: RegimeObservation) -> FeatureSnapshot:
        raw, reason, confidence = _raw(observation)
        _latch(self._memory, raw, observation)
        regime = self._memory.regime
        if regime == Regime.HIGH_VOL_PANIC:
            reason = "HIGH_VOL_PANIC"
        elif regime == Regime.UNKNOWN and reason == "AMBIGUOUS":
            confidence = AMBIGUOUS_CONFIDENCE
        elif regime != Regime.UNKNOWN:
            reason = "OK"
            confidence = Decimal(1)
        chosen = route(regime, observation.in_opening_window)
        return FeatureSnapshot(
            data_asof_utc=observation.data_asof_utc,
            vwap_intact=observation.vwap_intact,
            features_hash=_market_hash(observation),
            regime=regime,
            confidence=confidence,
            entries_allowed=chosen.entries_allowed,
            s1_enabled=chosen.s1_enabled,
            s2_enabled=chosen.s2_enabled,
            s3_enabled=chosen.s3_enabled,
            s4_research_enabled=chosen.s4_research_enabled,
            in_opening_window=observation.in_opening_window,
            data_ready=_data_ready(observation),
            dwell_bars=self._memory.dwell,
            block_reason=reason,
            benchmarks_present=observation.benchmarks_present,
            breadth=observation.breadth,
            trend_z=observation.trend_z,
            spy_rvol=observation.rvol_mean,
            spy_rms6=observation.rms6,
        )


def build_observation(
    latest: Mapping[str, SymbolSnapshot],
    decision_time: datetime,
) -> RegimeObservation:
    if decision_time.tzinfo is None:
        raise ValueError("decision_time must be timezone-aware")
    spy = latest.get("SPY")
    if spy is None:
        return _blank(decision_time, 0)
    if decision_time < spy.data_asof_utc:
        raise LookaheadError("decision time is before the closed bar")
    present = 0
    hashes: list[tuple[str, str]] = []
    intact = True
    for symbol in BENCHMARKS:
        snap = latest.get(symbol)
        if snap is None or snap.data_asof_utc != spy.data_asof_utc:
            intact = False
            continue
        present += 1
        hashes.append((symbol, snap.features_hash))
        if not snap.vwap_intact or snap.vwap is None:
            intact = False
    complete = present == len(BENCHMARKS)
    qqq = latest.get("QQQ")
    breadth = None
    if complete and intact:
        above = sum(1 for symbol in SECTORS if latest[symbol].above_vwap is True)
        breadth = Decimal(above) / Decimal(len(SECTORS))
    local = spy.bar_start.astimezone(NEW_YORK).timetz().replace(tzinfo=None)
    return RegimeObservation(
        data_asof_utc=spy.data_asof_utc,
        fresh=decision_time - spy.data_asof_utc < STALE_AFTER,
        benchmarks_complete=complete,
        vwap_intact=complete and intact,
        warmed=complete and intact and spy.warmed,
        trend_z=spy.trend_z,
        vwap_slope=spy.vwap_slope,
        spy_above_vwap=spy.above_vwap,
        qqq_above_vwap=_aligned_flag(qqq, spy.data_asof_utc),
        breadth=breadth,
        rvol_mean=spy.rvol_mean,
        rms6=spy.rms6,
        in_opening_window=OPEN_START <= local < OPEN_END,
        benchmarks_present=present,
        symbol_hashes=tuple(hashes),
    )


def _aligned_flag(snapshot: SymbolSnapshot | None, asof: datetime) -> bool | None:
    if snapshot is None or snapshot.data_asof_utc != asof:
        return None
    return snapshot.above_vwap


def _blank(moment: datetime, present: int) -> RegimeObservation:
    return RegimeObservation(
        data_asof_utc=moment,
        fresh=False,
        benchmarks_complete=False,
        vwap_intact=False,
        warmed=False,
        trend_z=None,
        vwap_slope=None,
        spy_above_vwap=None,
        qqq_above_vwap=None,
        breadth=None,
        rvol_mean=None,
        rms6=None,
        in_opening_window=False,
        benchmarks_present=present,
        symbol_hashes=(),
    )


def _data_ready(observation: RegimeObservation) -> bool:
    return (
        observation.benchmarks_complete
        and observation.fresh
        and observation.vwap_intact
        and observation.warmed
    )


def _raw(observation: RegimeObservation) -> tuple[Regime | None, str, Decimal]:
    if not observation.benchmarks_complete:
        return Regime.UNKNOWN, "MISSING_BENCHMARK", Decimal(0)
    if not observation.fresh:
        return Regime.UNKNOWN, "STALE", Decimal(0)
    if not observation.vwap_intact:
        return Regime.UNKNOWN, "VWAP_NOT_INTACT", Decimal(0)
    if not observation.warmed:
        return Regime.UNKNOWN, "NOT_WARMED", Decimal(0)
    if _panic(observation):
        return Regime.HIGH_VOL_PANIC, "HIGH_VOL_PANIC", Decimal(1)
    if _trend_up(observation):
        return Regime.TREND_UP, "OK", Decimal(1)
    if _trend_down(observation):
        return Regime.TREND_DOWN, "OK", Decimal(1)
    if _sideways(observation):
        return Regime.SIDEWAYS, "OK", Decimal(1)
    return None, "AMBIGUOUS", Decimal(1)


def _latch(memory: _Memory, raw: Regime | None, observation: RegimeObservation) -> None:
    if raw == Regime.UNKNOWN:
        memory.regime = Regime.UNKNOWN
        memory.dwell = 1
        memory.calm = 0
        return
    if raw == Regime.HIGH_VOL_PANIC:
        memory.dwell = 1 if memory.regime != Regime.HIGH_VOL_PANIC else memory.dwell + 1
        memory.regime = Regime.HIGH_VOL_PANIC
        memory.calm = 0
        return
    if memory.regime == Regime.HIGH_VOL_PANIC:
        memory.calm += 1
        if raw is not None and memory.calm >= MIN_DWELL:
            memory.regime = raw
            memory.dwell = 1
            memory.calm = 0
            return
        memory.dwell += 1
        return
    if memory.regime == Regime.UNKNOWN:
        if raw is None:
            memory.dwell += 1
            return
        memory.regime = raw
        memory.dwell = 1
        return
    if raw == memory.regime:
        memory.dwell += 1
        return
    if memory.dwell < MIN_DWELL:
        memory.dwell += 1
        return
    if raw is not None:
        memory.regime = raw
        memory.dwell = 1
        return
    if _left_hold_band(memory.regime, observation):
        memory.regime = Regime.SIDEWAYS
        memory.dwell = 1
        return
    memory.dwell += 1


def _trend_up(observation: RegimeObservation) -> bool:
    return (
        observation.trend_z is not None
        and observation.trend_z >= ENTER_TREND
        and observation.vwap_slope is not None
        and observation.vwap_slope > 0
        and observation.spy_above_vwap is True
        and observation.qqq_above_vwap is True
        and observation.breadth is not None
        and observation.breadth >= BREADTH_UP
    )


def _trend_down(observation: RegimeObservation) -> bool:
    return (
        observation.trend_z is not None
        and observation.trend_z <= -ENTER_TREND
        and observation.vwap_slope is not None
        and observation.vwap_slope < 0
        and observation.spy_above_vwap is False
        and observation.qqq_above_vwap is False
        and observation.breadth is not None
        and observation.breadth <= BREADTH_DOWN
    )


def _sideways(observation: RegimeObservation) -> bool:
    return (
        observation.trend_z is not None
        and abs(observation.trend_z) <= SIDEWAYS_BAND
        and observation.rvol_mean is not None
        and observation.rms6 is not None
    )


def _panic(observation: RegimeObservation) -> bool:
    """Price is under VWAP, the normalized trend is down, and volume or range is elevated."""

    if observation.spy_above_vwap is not False or observation.trend_z is None:
        return False
    if observation.trend_z > PANIC_TREND:
        return False
    rvol_hit = observation.rvol_mean is not None and observation.rvol_mean >= PANIC_RVOL
    rms_hit = observation.rms6 is not None and observation.rms6 >= PANIC_RMS
    return rvol_hit or rms_hit


def _left_hold_band(regime: Regime, observation: RegimeObservation) -> bool:
    if observation.trend_z is None:
        return False
    if regime == Regime.TREND_UP:
        return observation.trend_z < EXIT_TREND
    if regime == Regime.TREND_DOWN:
        return observation.trend_z > -EXIT_TREND
    return False


def _market_hash(observation: RegimeObservation) -> str:
    rows: list[tuple[str, str]] = [
        ("schema", "5"),
        ("asof", observation.data_asof_utc.isoformat()),
        ("fresh", _flag(observation.fresh)),
        ("complete", _flag(observation.benchmarks_complete)),
        ("vwap_intact", _flag(observation.vwap_intact)),
        ("warmed", _flag(observation.warmed)),
        ("trend_z", _num(observation.trend_z)),
        ("slope", _num(observation.vwap_slope)),
        ("breadth", _num(observation.breadth)),
        ("rvol", _num(observation.rvol_mean)),
        ("rms6", _num(observation.rms6)),
        ("spy_above", _optional_flag(observation.spy_above_vwap)),
        ("qqq_above", _optional_flag(observation.qqq_above_vwap)),
        ("present", str(observation.benchmarks_present)),
    ]
    rows.extend(observation.symbol_hashes)
    return sha256_rows(rows)


def _num(value: Decimal | None) -> str:
    if value is None:
        return ""
    return format(value, "f")


def _flag(value: bool) -> str:
    return "true" if value else "false"


def _optional_flag(value: bool | None) -> str:
    if value is None:
        return ""
    return _flag(value)
