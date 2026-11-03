"""Go / no-go rule. Locked before the lockbox numbers exist."""

from dataclasses import dataclass
from decimal import Decimal

from bstocks_v2.stage14.spec import DSR_MIN


@dataclass(frozen=True)
class Decision:
    deployment: str
    statistical: str
    reasons: tuple[str, ...]


def judge(
    *,
    mean_net_bps: Decimal | None,
    dsr: Decimal | None,
    neighbor_means: tuple[Decimal | None, ...],
    selected: bool,
    live_locked: bool,
    p0_p1_clear: bool,
) -> Decision:
    """A strategy goes live only when every pre-registered bar is cleared.

    The neighborhood fails closed when a perturbed run has no closed trade:
    a missing expectancy is not a positive expectancy.
    """

    reasons: list[str] = []
    statistical = "PASS"
    if mean_net_bps is None or mean_net_bps <= 0:
        statistical = "FAIL"
        reasons.append("NET_EXPECTANCY")
    if dsr is None or dsr < DSR_MIN:
        statistical = "FAIL"
        reasons.append("DSR")
    if not neighbor_means or any(item is None or item <= 0 for item in neighbor_means):
        statistical = "FAIL"
        reasons.append("NEIGHBORHOOD")
    if not selected:
        reasons.append("NOT_SELECTED")
    if live_locked:
        reasons.append("LIVE_LOCKED")
    if not p0_p1_clear:
        reasons.append("P0_P1_OPEN")
    deployment = "NO_GO"
    if statistical == "PASS" and selected and not live_locked and p0_p1_clear:
        deployment = "GO"
    shown = statistical if selected else "NOT_SELECTED"
    return Decision(deployment, shown, tuple(reasons))
