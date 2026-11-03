"""Backtrader's default broker, used only as the optimistic ceiling for entry limits.

Prices cross a float boundary inside this module and are parsed back to Decimal
before they leave. The research ledger does not import this module.
"""

from decimal import Decimal

from bstocks_v2.research_backtest.tape import ClosedBar


def loose_limit_fill(
    bars: tuple[ClosedBar, ...] | list[ClosedBar],
    *,
    signal_len: int,
    limit: Decimal,
    size: int,
) -> Decimal | None:
    """Return the next-bar buy-limit print from Backtrader's default broker, if it fills."""

    import backtrader as bt

    class _Feed(bt.feed.DataBase):  # type: ignore[misc]
        params = (("rows", ()),)

        def start(self) -> None:
            super().start()
            self._cursor = 0

        def _load(self) -> bool:
            rows = self.p.rows
            if self._cursor >= len(rows):
                return False
            row = rows[self._cursor]
            self._cursor += 1
            self.lines.datetime[0] = bt.date2num(row.start.replace(tzinfo=None))
            self.lines.open[0] = float(row.open)
            self.lines.high[0] = float(row.high)
            self.lines.low[0] = float(row.low)
            self.lines.close[0] = float(row.close)
            self.lines.volume[0] = float(row.volume)
            self.lines.openinterest[0] = 0.0
            return True

    class _Once(bt.Strategy):  # type: ignore[misc]
        params = (("signal_len", 1), ("limit", None), ("size", 1))

        def __init__(self) -> None:
            self.prints: list[float] = []

        def next(self) -> None:
            if len(self.data) == self.p.signal_len and not self.position:
                self.buy(size=self.p.size, price=self.p.limit, exectype=bt.Order.Limit)

        def notify_order(self, order: bt.Order) -> None:
            if order.status == order.Completed and order.isbuy():
                self.prints.append(float(order.executed.price))

    feed = _Feed(rows=tuple(bars))
    cerebro = bt.Cerebro(stdstats=False)
    cerebro.adddata(feed)
    cerebro.addstrategy(
        _Once,
        signal_len=signal_len,
        limit=float(limit),
        size=size,
    )
    cerebro.broker.set_coc(False)
    cerebro.broker.set_coo(False)
    cerebro.broker.setcash(1_000_000.0)
    finished = cerebro.run()
    prints = finished[0].prints
    if not prints:
        return None
    return Decimal(format(prints[0], ".2f"))
