"""Create and renew the equity user-stream listenKey. Renewal is every 25 minutes."""

from bstocks_v2.market_ingestor.equity_rest import (
    ApiError,
    EquityReadClient,
    needs_listen_key_rebuild,
)

RENEW_AFTER_SECONDS = 25 * 60


class ListenKeyKeeper:
    def __init__(self, client: EquityReadClient) -> None:
        self._client = client
        self.listen_key: str | None = None
        self.obtained_at: float | None = None
        self.rebuild_required = False
        self.rebuilds = 0

    def current(self, now_seconds: float) -> str:
        expired = (
            self.obtained_at is None
            or now_seconds - self.obtained_at >= RENEW_AFTER_SECONDS
        )
        if self.listen_key is None or expired or self.rebuild_required:
            self.listen_key = self._client.create_listen_key()
            self.obtained_at = now_seconds
            if self.rebuild_required:
                self.rebuilds += 1
            self.rebuild_required = False
        return self.listen_key

    def observe_error(self, error: ApiError) -> None:
        if needs_listen_key_rebuild(error):
            self.rebuild_required = True
            self.listen_key = None
