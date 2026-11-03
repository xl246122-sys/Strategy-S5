"""Replay one frozen intent book and summarize the split."""

from collections import Counter
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

from bstocks_v2.contracts.models import TradeIntent
from bstocks_v2.feature_store.bars import FeatureBar
from bstocks_v2.limit_optimizer import LimitOptimizer
from bstocks_v2.limit_optimizer.book import QuoteBook
from bstocks_v2.limit_optimizer.fill_model import FillCalibrator, drawdowns_before
from bstocks_v2.limit_optimizer.spec import HALF_SPREAD, PENETRATION_DELTA
from bstocks_v2.limit_optimizer.stage11 import EntryClock
from bstocks_v2.research_backtest.models import EngineConfig, FillMode, IntentRecord, SymbolFilter
from bstocks_v2.research_backtest.replay import OrderReplayEngine
from bstocks_v2.research_backtest.tape import BarTape, CausalView, ClosedBar
from bstocks_v2.research_data.calendar import NEW_YORK, XnysCalendar
from bstocks_v2.stage14.spec import (
    ACCOUNT,
    CAGE_DOWN,
    CAGE_UP,
    FILL_HORIZON,
    STRESS_COST_MULTIPLE,
    STRESS_DELTA_MULTIPLE,
    TRIALS,
)
from bstocks_v2.stage14.stats import deflated_sharpe, summarize, text, trade_bps
from bstocks_v2.strategies.s1.spec import TRADED, TRAIN_END

_CENT = Decimal("0.01")


class Lookup:
    def __init__(self, intents: dict[tuple[str, datetime], TradeIntent]) -> None:
        self._intents = intents

    def on_close(self, view: CausalView, bar: ClosedBar) -> TradeIntent | None:
        if view.asof != bar.end:
            raise RuntimeError("decision clock is not the closed bar")
        return self._intents.get((bar.symbol, bar.end))


def make_tape(bars: list[FeatureBar]) -> BarTape:
    traded = set(TRADED)
    return BarTape([_closed(bar) for bar in bars if bar.symbol in traded])


def loose_config() -> EngineConfig:
    return EngineConfig(mode=FillMode.LOOSE)


def conservative_config(
    *,
    delta: Decimal = PENETRATION_DELTA,
    commission: Decimal = Decimal("0.0005"),
    half_spread: Decimal = HALF_SPREAD,
    latency_seconds: int = 0,
) -> EngineConfig:
    return EngineConfig(
        mode=FillMode.CONSERVATIVE,
        penetration="fixed",
        fixed_delta=delta,
        commission_rate=commission,
        half_spread=half_spread,
        latency=timedelta(seconds=latency_seconds),
    )


def stress_configs() -> dict[str, EngineConfig]:
    base_fee = Decimal("0.0005")
    base_spread = HALF_SPREAD
    return {
        "cost_doubled": conservative_config(
            commission=base_fee * STRESS_COST_MULTIPLE,
            half_spread=base_spread * STRESS_COST_MULTIPLE,
        ),
        "delta_doubled": conservative_config(delta=PENETRATION_DELTA * STRESS_DELTA_MULTIPLE),
        "latency_1s": conservative_config(latency_seconds=1),
        "latency_5s": conservative_config(latency_seconds=5),
    }


def windowed(
    intents: dict[tuple[str, datetime], TradeIntent],
    days: frozenset[date],
) -> dict[tuple[str, datetime], TradeIntent]:
    return {key: intent for key, intent in intents.items() if _day(key[1]) in days}


def play(
    tape: BarTape,
    intents: dict[tuple[str, datetime], TradeIntent],
    days: frozenset[date],
    config: EngineConfig,
    trial_sharpes: list[Decimal],
) -> dict[str, object]:
    kept = windowed(intents, days)
    result = OrderReplayEngine(config).run(tape, Lookup(kept))
    closed = _closed_records(result.records)
    bps = [_bps(record) for record in closed]
    body = _pack(result.records, result.halt_new_entries, result.ending_cash)
    body["candidates"] = len(kept)
    body["dsr"] = deflated_sharpe(bps, trial_sharpes, len(TRIALS))
    return body


def train_fill_models(bars: list[FeatureBar]) -> dict[str, FillCalibrator]:
    """Drawdowns whose label window ends on or before the training end."""

    grouped: dict[str, list[ClosedBar]] = {}
    for bar in bars:
        if bar.symbol not in TRADED or _day(bar.start) > TRAIN_END:
            continue
        grouped.setdefault(bar.symbol, []).append(_closed(bar))
    models: dict[str, FillCalibrator] = {}
    for symbol, rows in grouped.items():
        rows.sort(key=lambda item: item.end)
        if not rows:
            continue
        asks = {row.end: row.close + HALF_SPREAD for row in rows}
        drawdowns = drawdowns_before(
            rows,
            asks,
            symbol=symbol,
            horizon_bars=FILL_HORIZON,
            cutoff=rows[-1].end,
        )
        if drawdowns:
            models[symbol] = FillCalibrator(drawdowns, delta=PENETRATION_DELTA)
    return models


def reprice(
    intents: dict[tuple[str, datetime], TradeIntent],
    closes: dict[tuple[str, datetime], Decimal],
    models: dict[str, FillCalibrator],
    calendar: XnysCalendar,
) -> tuple[dict[tuple[str, datetime], TradeIntent], Counter[str]]:
    kept: dict[tuple[str, datetime], TradeIntent] = {}
    reasons: Counter[str] = Counter()
    optimizer_cache: dict[str, LimitOptimizer] = {}
    for key, intent in intents.items():
        model = models.get(intent.symbol)
        if model is None:
            reasons["FILL_MODEL_UNTRAINED"] += 1
            continue
        close = closes.get(key)
        if close is None:
            reasons["CLOSE_MISSING"] += 1
            continue
        priced, reason = _one(intent, close, model, calendar, optimizer_cache)
        reasons[reason] += 1
        if priced is not None:
            kept[key] = priced
    return kept, reasons


def closes_of(bars: list[FeatureBar]) -> dict[tuple[str, datetime], Decimal]:
    return {(bar.symbol, bar.end): bar.close for bar in bars}


def gap_open(bars: list[FeatureBar], factor: Decimal) -> list[FeatureBar]:
    from dataclasses import replace

    shifted: list[FeatureBar] = []
    for bar in bars:
        local = bar.start.astimezone(NEW_YORK)
        if local.hour != 9 or local.minute != 30:
            shifted.append(bar)
            continue
        opened, high, low, closed = _repair(
            _cents(bar.open * factor),
            _cents(bar.high * factor),
            _cents(bar.low * factor),
            _cents(bar.close * factor),
        )
        vwap = None if bar.bar_vwap is None else bar.bar_vwap * factor
        shifted.append(
            replace(bar, open=opened, high=high, low=low, close=closed, bar_vwap=vwap)
        )
    return shifted


def _one(
    intent: TradeIntent,
    close: Decimal,
    model: FillCalibrator,
    calendar: XnysCalendar,
    cache: dict[str, LimitOptimizer],
) -> tuple[TradeIntent | None, str]:
    bid = close - HALF_SPREAD
    ask = close + HALF_SPREAD
    if bid <= 0 or bid > ask:
        return None, "BAD_QUOTE"
    book = QuoteBook(
        bid=bid,
        ask=ask,
        reference=close,
        multiplier_down=CAGE_DOWN,
        multiplier_up=CAGE_UP,
    )
    day = _day(intent.data_asof_utc)
    clock = EntryClock(
        now_et=intent.data_asof_utc,
        is_half_day=calendar.close_time(day).hour == 13,
        equity=ACCOUNT,
        hwm=ACCOUNT,
    )
    optimizer = cache.get(intent.symbol)
    if optimizer is None:
        optimizer = LimitOptimizer(model, horizon_bars=FILL_HORIZON, whole_share_safe_mode=True)
        cache[intent.symbol] = optimizer
    result = optimizer.optimize(intent, book, SymbolFilter(), clock)
    if result.order is None:
        return None, result.reason or "LIMIT_OPTIMIZER"
    price = result.order.draft.price
    if price is None or price > intent.entry_price_ceiling:
        return None, "LIMIT_ABOVE_CEILING"
    updated = intent.model_copy(update={"candidate_limit_prices": [price]})
    return updated, "OPTIMIZED"


def _pack(
    records: tuple[IntentRecord, ...],
    halt: bool,
    ending_cash: Decimal,
) -> dict[str, object]:
    reasons: Counter[str] = Counter()
    ordered = 0
    filled = 0
    unfilled = 0
    missed = Decimal(0)
    for record in records:
        if record.rejection_reasons:
            reasons[record.rejection_reasons[0]] += 1
        if record.ordered_qty > 0:
            ordered += 1
            if record.filled_qty > 0:
                filled += 1
            else:
                unfilled += 1
                missed += record.opportunity_cost_usdc
    closed = _closed_records(records)
    pnls = [record.net_pnl for record in closed]
    notionals = [
        record.entry_price * record.filled_qty
        for record in closed
        if record.entry_price is not None
    ]
    body = summarize(pnls, notionals)
    rate = (Decimal(filled) / Decimal(ordered)) if ordered else None
    body.update(
        {
            "risk_rejections": dict(reasons),
            "orders": ordered,
            "filled_orders": filled,
            "unfilled_orders": unfilled,
            "fill_rate": text(rate),
            "missed_opportunity_usdc": text(missed),
            "halt_new_entries": halt,
            "ending_cash": text(ending_cash),
        }
    )
    return body


def _closed_records(records: tuple[IntentRecord, ...]) -> list[IntentRecord]:
    closed = [
        record
        for record in records
        if record.filled_qty > 0
        and record.open_qty == 0
        and record.entry_price is not None
        and record.exit_price is not None
    ]
    closed.sort(key=lambda record: (record.exit_bar_end or record.signal_bar_end, record.intent_id))
    return closed


def _bps(record: IntentRecord) -> Decimal:
    if record.entry_price is None:
        raise RuntimeError("closed trade has no entry")
    return trade_bps(record.net_pnl, record.entry_price * record.filled_qty)


def _closed(bar: FeatureBar) -> ClosedBar:
    if bar.end - bar.start != timedelta(minutes=5):
        raise RuntimeError(f"{bar.symbol} bar is not five minutes")
    return ClosedBar(
        symbol=bar.symbol,
        start=bar.start,
        end=bar.end,
        open=bar.open,
        high=bar.high,
        low=bar.low,
        close=bar.close,
        volume=bar.volume,
        timeframe="5m",
    )


def _day(moment: datetime) -> date:
    return moment.astimezone(NEW_YORK).date()


def _cents(value: Decimal) -> Decimal:
    return value.quantize(_CENT, rounding=ROUND_HALF_UP)


def _repair(
    opened: Decimal,
    high: Decimal,
    low: Decimal,
    closed: Decimal,
) -> tuple[Decimal, Decimal, Decimal, Decimal]:
    if high < low:
        high, low = low, high
    if opened < low:
        opened = low
    if opened > high:
        opened = high
    if closed < low:
        closed = low
    if closed > high:
        closed = high
    return opened, high, low, closed
