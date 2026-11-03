"""S5 stays research-only. A config file cannot turn the code lock on."""

from pathlib import Path

from bstocks_v2.config.loader import load_runtime_config
from bstocks_v2.contracts.errors import ConstitutionBypassError, LiveTradingDisabledError

LIVE_ENABLED = False
MODE = "RESEARCH_ONLY"
_CONFIG = Path(__file__).with_name("research_config.json")


def s5_live_enabled() -> bool:
    """Literal false. A file that says true is rejected and still does not enable S5."""

    if LIVE_ENABLED is not False or MODE != "RESEARCH_ONLY":
        raise RuntimeError("S5 code lock drifted")
    payload = load_runtime_config(_CONFIG)
    if payload.get("live_enabled") is not False or payload.get("s5_live_enabled") is not False:
        raise ConstitutionBypassError("s5_live_enabled")
    if payload.get("live_trade_enabled") is not False:
        raise ConstitutionBypassError("s5_live_enabled")
    if payload.get("mode") != "RESEARCH_ONLY" or payload.get("strategy_id") != "S5_EOD_REVERSAL":
        raise ConstitutionBypassError("s5_live_enabled")
    return LIVE_ENABLED


def refuse_live_route(path: str) -> None:
    """S5 has no order route. Calling this always raises."""

    if s5_live_enabled():
        raise RuntimeError("S5 code lock drifted")
    raise LiveTradingDisabledError(path)
