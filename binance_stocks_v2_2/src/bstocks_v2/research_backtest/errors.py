"""Replay errors. These stay inside the research engine and never reach an exchange."""


class LookaheadError(Exception):
    """A decision tried to read a bar that had not closed, or a future excursion."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason
