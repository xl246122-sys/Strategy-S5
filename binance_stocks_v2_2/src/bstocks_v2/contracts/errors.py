"""Constitution and normalizer errors. Config files cannot catch and ignore these away."""


class ConstitutionError(Exception):
    """Base error for a rule the external config is not allowed to weaken."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class ConstitutionBypassError(ConstitutionError):
    """An external config tried to change a hardcoded rule."""


class LiveTradingDisabledError(ConstitutionError):
    """Order placement or cancellation while live_trade_enabled is false."""


class EndpointForbiddenError(ConstitutionError):
    """Path is outside the equity / funding-read / Alpaca-data whitelist."""


class ShortSellingForbiddenError(ConstitutionError):
    """SELL quantity exceeds the confirmed long quantity."""


class OrderRejected(ConstitutionError):
    """A single order failed a field, session, size, or friction rule."""


class FeeTrapError(ConstitutionError):
    """tc - FN was about to be booked as realized commission before a full fill."""


class NormalizerError(ConstitutionError):
    """WebSocket or REST payload could not be mapped into the canonical contract."""
