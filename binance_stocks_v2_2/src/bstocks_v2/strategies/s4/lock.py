"""S4 cannot be turned on. The constant and the research file both have to stay false."""

from pathlib import Path

from bstocks_v2.config.loader import load_runtime_config
from bstocks_v2.contracts.errors import ConstitutionBypassError, LiveTradingDisabledError

LIVE_ENABLED = False
_CONFIG = Path(__file__).with_name("research_config.json")


def s4_live_enabled() -> bool:
    """Literal false. A file that says true is rejected and still does not enable S4."""

    if LIVE_ENABLED is not False:
        raise RuntimeError("S4 code lock drifted")
    payload = load_runtime_config(_CONFIG)
    if payload.get("live_enabled") is not False or payload.get("s4_live_enabled") is not False:
        raise ConstitutionBypassError("s4_live_enabled")
    if payload.get("strategy_id") != "S4":
        raise ConstitutionBypassError("s4_live_enabled")
    return LIVE_ENABLED


def refuse_live_route(path: str) -> None:
    """S4 has no order route. Calling this always raises."""

    if s4_live_enabled():
        raise RuntimeError("S4 code lock drifted")
    raise LiveTradingDisabledError(path)
