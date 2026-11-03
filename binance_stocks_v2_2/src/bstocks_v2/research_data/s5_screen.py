"""Train/validation OHLCV screen for S5. Quotes are absent, so paths stay unfillable."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from bstocks_v2.research_data.adjust import (
    SplitEvent,
    backward_price_factors,
    is_large_jump,
    load_splits,
)
from bstocks_v2.research_data.calendar import load_xnys_calendar
from bstocks_v2.research_data.manifest import assert_output_isolated
from bstocks_v2.research_data.paths import FRESH_DEFAULT, OUT_DEFAULT
from bstocks_v2.sessions.calendar import TradingCalendar
from bstocks_v2.strategies.s1.spec import PARENTS, TRADED
from bstocks_v2.strategies.s5.diagnose import summarize
from bstocks_v2.strategies.s5.features import ClosedBar, PriorDay, arm_window, clock_in_window
from bstocks_v2.strategies.s5.model import insufficient_model
from bstocks_v2.strategies.s5.signals import CausalView, ForwardTape, ResearchRecord, evaluate
from bstocks_v2.strategies.s5.spec import (
    ARM_A,
    B0_MAX_SIMPLE_RETURN,
    B1_MAX_Z,
    TRAIN_START,
    VAL_END,
    research_split,
)
from bstocks_v2.strategies.s5.universe import sector_parent

NEEDED: tuple[str, ...] = tuple(sorted(set(TRADED) | set(PARENTS) | {"SPY"}))
_COLUMNS = ["t", "o", "h", "l", "c", "v", "bar_end_utc", "ny_date", "feed"]
_ACTIONS = "corporate_actions_20241101_20261030.csv"


def include_session(day: date) -> bool:
    """The lockbox is not read. This screen does not fit parameters."""

    return research_split(day) in {"train", "validation"}


@dataclass
class _Tape:
    by_day: dict[date, list[int]] = field(default_factory=dict)
    starts: list[datetime] = field(default_factory=list)
    ends: list[datetime] = field(default_factory=list)
    opens: list[Decimal] = field(default_factory=list)
    highs: list[Decimal] = field(default_factory=list)
    lows: list[Decimal] = field(default_factory=list)
    closes: list[Decimal] = field(default_factory=list)
    volumes: list[Decimal] = field(default_factory=list)
    sessions: list[str] = field(default_factory=list)
    rth_close: dict[date, Decimal] = field(default_factory=dict)
    at: dict[datetime, Decimal] = field(default_factory=dict)


def run(
    source: Path = FRESH_DEFAULT,
    output: Path = OUT_DEFAULT / "s5" / "screen_summary.json",
) -> dict[str, object]:
    """Screen the pre-registered ETF universe once. Thresholds are not updated here."""

    assert_output_isolated(output, source)
    calendar = TradingCalendar.load()
    sessions = load_xnys_calendar().sessions_between(TRAIN_START, VAL_END)
    actions = source / "corporate_actions" / _ACTIONS
    if actions.exists():
        splits = [item for item in load_splits(actions) if item.ex_date <= VAL_END]
    else:
        splits = []
    tapes = {
        symbol: _load_symbol(source, symbol, calendar, sessions, splits) for symbol in NEEDED
    }
    model = insufficient_model()
    records: list[ResearchRecord] = []
    excluded_jump = 0
    missing_reference = 0
    for symbol in TRADED:
        parent = sector_parent(symbol)
        if parent is None:
            raise RuntimeError(f"{symbol} has no pre-registered sector")
        days = sorted(tapes[symbol].rth_close)
        for index, day in enumerate(days):
            if index == 0:
                continue
            previous_day = days[index - 1]
            previous = tapes[symbol].rth_close[previous_day]
            for arm in ("A_RTH_TO_POST", "B_POST_TO_POST", "C_POST_TO_OVERNIGHT"):
                moment = _last_in_window(tapes[symbol], calendar, day, arm)
                if moment is None:
                    continue
                decision_close = tapes[symbol].at.get(moment)
                if decision_close is None or is_large_jump(previous, decision_close):
                    excluded_jump += 1
                    continue
                market = _log(tapes["SPY"].at.get(moment), tapes["SPY"].rth_close.get(previous_day))
                sector = _log(
                    tapes[parent].at.get(moment),
                    tapes[parent].rth_close.get(previous_day),
                )
                if market is None or sector is None:
                    missing_reference += 1
                records.append(
                    evaluate(
                        _view(
                            symbol,
                            arm,
                            moment,
                            tapes,
                            parent,
                            days,
                            index,
                            previous,
                            market,
                            sector,
                            calendar,
                        ),
                        model,
                        ForwardTape((), moment, False),
                    )
                )
        print(f"screened {symbol} decisions={len(records)}", flush=True)
    payload: dict[str, object] = {
        "mode": "rth_post_ohlcv_screen",
        "quotes": 0,
        "news_snapshots": 0,
        "lockbox_read": False,
        "model_kind": model.kind,
        "model_note": model.note,
        "validation_brier": None,
        "validation_ece": None,
        "symbols": len(TRADED),
        "excluded_jump": excluded_jump,
        "missing_reference": missing_reference,
        "thresholds": {
            "b0_simple_return": format(B0_MAX_SIMPLE_RETURN, "f"),
            "b1_z": format(B1_MAX_Z, "f"),
        },
        "summary": summarize(records),
        "generated_at_utc": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return payload


def _view(
    symbol: str,
    arm: str,
    moment: datetime,
    tapes: dict[str, _Tape],
    parent: str,
    days: list[date],
    index: int,
    prev_close: Decimal,
    market: Decimal | None,
    sector: Decimal | None,
    calendar: TradingCalendar,
) -> CausalView:
    day = days[index]
    return CausalView(
        symbol=symbol,
        arm=arm,
        decision_ts=moment,
        bars=_bars_until(symbol, tapes[symbol], day, moment),
        prev_close=prev_close,
        prior=_prior(symbol, parent, tapes, days, index),
        market_log_return=market,
        sector_log_return=sector,
        regime="NOT_ATTACHED",
        entries_allowed=False,
        regime_asof=moment,
        news=None,
        quote=None,
        overnight_supported=None,
        rth_close=calendar.close_time(day),
        prior_slot_volumes=_slot_history(tapes[symbol], calendar, days, index, arm),
    )


def _prior(
    symbol: str,
    parent: str,
    tapes: dict[str, _Tape],
    days: list[date],
    index: int,
) -> tuple[PriorDay, ...]:
    rows: list[PriorDay] = []
    start = max(1, index - 60)
    for cursor in range(start, index):
        day = days[cursor]
        previous = days[cursor - 1]
        symbol_log = _log(tapes[symbol].rth_close.get(day), tapes[symbol].rth_close.get(previous))
        market_log = _log(tapes["SPY"].rth_close.get(day), tapes["SPY"].rth_close.get(previous))
        sector_log = _log(tapes[parent].rth_close.get(day), tapes[parent].rth_close.get(previous))
        if symbol_log is None or market_log is None or sector_log is None:
            continue
        rows.append(PriorDay(day, symbol_log, market_log, sector_log))
    return tuple(rows)


def _slot_history(
    tape: _Tape,
    calendar: TradingCalendar,
    days: list[date],
    index: int,
    arm: str,
) -> tuple[Decimal, ...]:
    volumes: list[Decimal] = []
    for cursor in range(max(0, index - 20), index):
        volume = _window_volume(tape, calendar, days[cursor], arm)
        if volume is not None:
            volumes.append(volume)
    return tuple(volumes)


def _window_volume(tape: _Tape, calendar: TradingCalendar, day: date, arm: str) -> Decimal | None:
    start, end = arm_window(arm, calendar.close_time(day))
    wanted = "RTH" if arm == ARM_A else "POST"
    total = Decimal(0)
    found = False
    for cursor in tape.by_day.get(day, []):
        if tape.sessions[cursor] != wanted:
            continue
        if clock_in_window(tape.ends[cursor], start, end):
            total += tape.volumes[cursor]
            found = True
    if not found:
        return None
    return total


def _last_in_window(tape: _Tape, calendar: TradingCalendar, day: date, arm: str) -> datetime | None:
    start, end = arm_window(arm, calendar.close_time(day))
    wanted = "RTH" if arm == ARM_A else "POST"
    chosen: datetime | None = None
    for cursor in tape.by_day.get(day, []):
        if tape.sessions[cursor] != wanted:
            continue
        moment = tape.ends[cursor]
        if clock_in_window(moment, start, end) and (chosen is None or moment > chosen):
            chosen = moment
    return chosen


def _bars_until(symbol: str, tape: _Tape, day: date, moment: datetime) -> tuple[ClosedBar, ...]:
    bars: list[ClosedBar] = []
    for cursor in tape.by_day.get(day, []):
        if tape.ends[cursor] > moment:
            continue
        bars.append(
            ClosedBar(
                symbol=symbol,
                start=tape.starts[cursor],
                end=tape.ends[cursor],
                open=tape.opens[cursor],
                high=tape.highs[cursor],
                low=tape.lows[cursor],
                close=tape.closes[cursor],
                volume=tape.volumes[cursor],
                session=tape.sessions[cursor],
            )
        )
    return tuple(bars)


def _load_symbol(
    source: Path,
    symbol: str,
    calendar: TradingCalendar,
    sessions: list[date],
    splits: list[SplitEvent],
) -> _Tape:
    import pyarrow.dataset as ds

    folder = source / "parquet" / "5Min" / symbol
    files = sorted(folder.glob("*.parquet")) if folder.is_dir() else []
    if not files:
        raise RuntimeError(f"{symbol} has no 5-minute file")
    dataset = ds.dataset([str(path) for path in files], format="parquet")
    table = dataset.to_table(
        columns=_COLUMNS,
        filter=(ds.field("ny_date") >= TRAIN_START)
        & (ds.field("ny_date") <= VAL_END)
        & (ds.field("feed") == "sip"),
    )
    payload = table.to_pydict()
    own = [item for item in splits if item.symbol == symbol]
    factors = backward_price_factors(own, sessions)
    tape = _Tape()
    seen: set[datetime] = set()
    ends = payload["bar_end_utc"]
    order = sorted(range(table.num_rows), key=lambda index: _as_datetime(ends[index]))
    rth_end: dict[date, datetime] = {}
    for index in order:
        day = _as_date(payload["ny_date"][index])
        if not include_session(day) or not calendar.is_session(day):
            continue
        start = _as_datetime(payload["t"][index])
        end = _as_datetime(payload["bar_end_utc"][index])
        if end in seen:
            continue
        seen.add(end)
        label = calendar.label(end).value
        if label not in {"RTH", "POST"}:
            continue
        factor = factors.get(day, Decimal(1))
        opened, high, low, closed = _repair(
            _number(payload["o"][index]) * factor,
            _number(payload["h"][index]) * factor,
            _number(payload["l"][index]) * factor,
            _number(payload["c"][index]) * factor,
        )
        cursor = len(tape.ends)
        tape.by_day.setdefault(day, []).append(cursor)
        tape.starts.append(start)
        tape.ends.append(end)
        tape.opens.append(opened)
        tape.highs.append(high)
        tape.lows.append(low)
        tape.closes.append(closed)
        tape.volumes.append(_number(payload["v"][index]) / factor)
        tape.sessions.append(label)
        tape.at[end] = closed
        if label == "RTH" and (day not in rth_end or end > rth_end[day]):
            rth_end[day] = end
            tape.rth_close[day] = closed
    return tape


def _log(close: Decimal | None, prev_close: Decimal | None) -> Decimal | None:
    if close is None or prev_close is None or close <= 0 or prev_close <= 0:
        return None
    return (close / prev_close).ln()


def _as_date(value: object) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    raise RuntimeError("session date is missing")


def _as_datetime(value: object) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise RuntimeError("bar timestamp is incomplete")
    return value


def _number(value: object) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise ValueError("missing number")
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, str):
        return Decimal(value)
    if isinstance(value, float):
        return Decimal(format(value, ".10f"))
    raise ValueError(f"cannot read {type(value).__name__}")


def _repair(
    opened: Decimal,
    high: Decimal,
    low: Decimal,
    closed: Decimal,
) -> tuple[Decimal, Decimal, Decimal, Decimal]:
    if high < low:
        high, low = low, high
    opened = min(max(opened, low), high)
    closed = min(max(closed, low), high)
    if closed <= 0:
        raise ValueError("adjusted close is not positive")
    return opened, high, low, closed


if __name__ == "__main__":
    run()

