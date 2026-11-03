"""Feature-layer errors. These never reach an exchange."""


class LookaheadError(Exception):
    """A bar was not closed, or it arrived earlier than the last closed bar."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason
