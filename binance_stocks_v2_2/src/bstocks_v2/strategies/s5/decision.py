"""Per-arm S5 ruling. One layer does not grant the next."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from bstocks_v2.stage14.spec import DSR_MIN
from bstocks_v2.strategies.s5.observe import LIVE_SESSION_FLOOR
from bstocks_v2.strategies.s5.spec import ARMS, MIN_RESOLVED

VERDICTS = ("GO_FOR_REVIEW", "SHADOW_MORE", "NO_GO")


@dataclass(frozen=True)
class ArmFacts:
    """Evidence for one arm. A missing expectancy stays missing."""

    arm: str
    closed_trades: int
    expectancy_usdc: Decimal | None
    slippage_known: bool
    exit_acceptable: bool
    dsr: Decimal | None
    neighbor_means: tuple[Decimal | None, ...]
    shadow_sessions: int
    shadow_exchange_fills: int
    quotes_traceable: bool

    def __post_init__(self) -> None:
        if self.arm not in ARMS:
            raise ValueError("arm")
        if self.closed_trades < 0 or self.shadow_sessions < 0 or self.shadow_exchange_fills < 0:
            raise ValueError("count")


@dataclass(frozen=True)
class ArmRuling:
    arm: str
    verdict: str
    statistical: str
    shadow: str
    exchange_fills: int
    closed_trades: int
    expectancy_usdc: str | None
    dsr: str | None
    reasons: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "arm": self.arm,
            "verdict": self.verdict,
            "statistical": self.statistical,
            "shadow": self.shadow,
            "exchange_fills": self.exchange_fills,
            "closed_trades": self.closed_trades,
            "expectancy_usdc": self.expectancy_usdc,
            "dsr": self.dsr,
            "reasons": list(self.reasons),
        }


def rule_arm(facts: ArmFacts, *, quote_tape: bool) -> ArmRuling:
    """NO_GO when the sample fails. Shadow time does not repair that failure."""

    reasons: list[str] = []
    if not quote_tape:
        reasons.append("NO_EXECUTABLE_QUOTES")
    if facts.closed_trades < MIN_RESOLVED or facts.expectancy_usdc is None:
        reasons.append("SAMPLE_OR_EXPECTANCY")
    elif facts.expectancy_usdc <= 0:
        reasons.append("EXPECTANCY_NOT_POSITIVE")
    if facts.dsr is None or facts.dsr < DSR_MIN:
        reasons.append("DSR")
    if not facts.neighbor_means or any(item is None or item <= 0 for item in facts.neighbor_means):
        reasons.append("NEIGHBORHOOD")
    if not facts.slippage_known:
        reasons.append("SLIPPAGE_UNRELIABLE")
    if not facts.exit_acceptable:
        reasons.append("EXIT_FAILURE")
    shadow = "PASS" if _shadow_met(facts) else "INCOMPLETE"
    if reasons:
        verdict = "NO_GO"
        statistical = "FAIL"
    elif shadow != "PASS":
        verdict = "SHADOW_MORE"
        statistical = "PASS"
        reasons.append("SHADOW_FLOOR")
    else:
        verdict = "GO_FOR_REVIEW"
        statistical = "PASS"
    if verdict not in VERDICTS:
        raise RuntimeError("verdict drifted")
    return ArmRuling(
        facts.arm,
        verdict,
        statistical,
        shadow,
        facts.shadow_exchange_fills,
        facts.closed_trades,
        None if facts.expectancy_usdc is None else format(facts.expectancy_usdc, "f"),
        None if facts.dsr is None else format(facts.dsr, "f"),
        tuple(reasons),
    )


def rule_arms(facts: tuple[ArmFacts, ...], *, quote_tape: bool) -> tuple[ArmRuling, ...]:
    """A, B, and C stay in registration order. Their results are not added."""

    names = tuple(item.arm for item in facts)
    if names != ARMS:
        raise ValueError("ARMS_NOT_POOLED")
    return tuple(rule_arm(item, quote_tape=quote_tape) for item in facts)


def pooled_expectancy(facts: tuple[ArmFacts, ...]) -> Decimal:
    """The three arms are separate experiments."""

    del facts
    raise ValueError("ARMS_NOT_POOLED")


def _shadow_met(facts: ArmFacts) -> bool:
    return facts.shadow_sessions >= LIVE_SESSION_FLOOR and facts.quotes_traceable
