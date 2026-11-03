"""Count SIP session coverage. This module only reads the source store."""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from bstocks_v2.research_data.manifest import assert_output_isolated, sha256_file
from bstocks_v2.research_data.paths import FRESH_DEFAULT, OUT_DEFAULT

_COLUMNS = ("t", "session", "v", "feed", "adjustment")
_CLOCK = ("OVERNIGHT", "PRE", "RTH_CLOCK", "POST")


@dataclass
class TableSummary:
    rows: int
    stored: dict[str, int]
    clock: dict[str, int]
    clock_positive: dict[str, int]
    feed: dict[str, int]
    adjustment: dict[str, int]
    zero_volume: int
    columns: tuple[str, ...]


@dataclass
class SymbolCoverage:
    symbol: str
    timeframe: str
    rows: int = 0
    stored: Counter[str] = field(default_factory=Counter)
    clock: Counter[str] = field(default_factory=Counter)
    clock_positive: Counter[str] = field(default_factory=Counter)
    zero_volume: int = 0

    def add(self, summary: TableSummary) -> None:
        self.rows += summary.rows
        self.stored.update(summary.stored)
        self.clock.update(summary.clock)
        self.clock_positive.update(summary.clock_positive)
        self.zero_volume += summary.zero_volume

    def as_dict(self) -> dict[str, object]:
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "rows": self.rows,
            "stored_pre": self.stored.get("pre", 0),
            "stored_rth": self.stored.get("rth", 0),
            "stored_post": self.stored.get("post", 0),
            "stored_other": sum(
                count for name, count in self.stored.items() if name not in {"pre", "rth", "post"}
            ),
            "clock_pre": self.clock.get("PRE", 0),
            "clock_rth": self.clock.get("RTH_CLOCK", 0),
            "clock_post": self.clock.get("POST", 0),
            "clock_overnight": self.clock.get("OVERNIGHT", 0),
            "positive_overnight": self.clock_positive.get("OVERNIGHT", 0),
            "zero_volume": self.zero_volume,
        }


def summarize_table(table: pa.Table) -> TableSummary:
    """Classify one in-memory bar table. Missing bid/ask stays missing."""

    rows = table.num_rows
    columns = tuple(table.column_names)
    if rows == 0:
        empty = dict.fromkeys(_CLOCK, 0)
        return TableSummary(0, {}, empty, dict(empty), {}, {}, 0, columns)
    wall = pc.local_timestamp(table.column("t").cast(pa.timestamp("us", tz="America/New_York")))
    minute = pc.add(pc.multiply(pc.hour(wall), 60), pc.minute(wall))
    positive = pc.greater(table.column("v"), 0)
    clock = {
        "OVERNIGHT": _mask_or(pc.less(minute, 240), pc.greater_equal(minute, 1200)),
        "PRE": _between(minute, 240, 570),
        "RTH_CLOCK": _between(minute, 570, 960),
        "POST": _between(minute, 960, 1200),
    }
    return TableSummary(
        rows,
        _value_counts(table.column("session")),
        {name: _count(mask) for name, mask in clock.items()},
        {name: _count(pc.and_(mask, positive)) for name, mask in clock.items()},
        _value_counts(table.column("feed")),
        _value_counts(table.column("adjustment")),
        _count(pc.equal(table.column("v"), 0)),
        columns,
    )


def audit_timeframe(source: Path, timeframe: str) -> tuple[dict[str, object], list[SymbolCoverage]]:
    root = source / "parquet" / timeframe
    if not root.is_dir():
        raise ValueError(f"missing timeframe directory: {root}")
    stored: Counter[str] = Counter()
    clock: Counter[str] = Counter()
    clock_positive: Counter[str] = Counter()
    feed: Counter[str] = Counter()
    adjustment: Counter[str] = Counter()
    symbols: dict[str, SymbolCoverage] = {}
    rows = 0
    zero_volume = 0
    files = 0
    schema_names: tuple[str, ...] = ()
    for symbol_dir in sorted(path for path in root.iterdir() if path.is_dir()):
        coverage = SymbolCoverage(symbol_dir.name, timeframe)
        for path in sorted(symbol_dir.glob("*.parquet")):
            table = pq.read_table(path, columns=list(_COLUMNS))
            summary = summarize_table(table)
            coverage.add(summary)
            stored.update(summary.stored)
            clock.update(summary.clock)
            clock_positive.update(summary.clock_positive)
            feed.update(summary.feed)
            adjustment.update(summary.adjustment)
            rows += summary.rows
            zero_volume += summary.zero_volume
            files += 1
            if not schema_names:
                schema_names = tuple(pq.read_schema(path).names)
        symbols[symbol_dir.name] = coverage
        if len(symbols) % 500 == 0:
            print(f"{timeframe} symbols={len(symbols)} rows={rows}", flush=True)
    listed = list(symbols.values())
    payload = {
        "timeframe": timeframe,
        "files": files,
        "symbols": len(listed),
        "rows": rows,
        "stored_session": dict(stored),
        "clock": dict(clock),
        "clock_positive_volume": dict(clock_positive),
        "feed": dict(feed),
        "adjustment": dict(adjustment),
        "zero_volume": zero_volume,
        "columns": list(schema_names),
        "has_bid_ask": "bid" in schema_names or "ask" in schema_names,
        "symbols_with_stored_post": sum(1 for item in listed if item.stored.get("post", 0) > 0),
        "symbols_with_positive_overnight": sum(
            1 for item in listed if item.clock_positive.get("OVERNIGHT", 0) > 0
        ),
        "symbols_with_full_rth_grid": _full_grid(listed, timeframe),
    }
    return payload, listed


def write_audit(source: Path, output: Path, timeframes: tuple[str, ...] = ("1Min", "5Min")) -> Path:
    """Write coverage and a manifest. The SIP store is not opened for write."""

    assert_output_isolated(output, source)
    output.mkdir(parents=True, exist_ok=True)
    frames: list[dict[str, object]] = []
    symbol_rows: list[dict[str, object]] = []
    for timeframe in timeframes:
        payload, listed = audit_timeframe(source, timeframe)
        frames.append(payload)
        symbol_rows.extend(item.as_dict() for item in listed)
    coverage_path = output / "coverage.json"
    symbols_path = output / "symbol_coverage.json"
    _dump(coverage_path, {"source": str(source), "timeframes": frames})
    _dump(symbols_path, symbol_rows)
    manifest = {
        "created_utc": datetime.now(UTC).isoformat(timespec="seconds"),
        "mode": "prospective_forward",
        "reason": "historical overnight tape is not a continuous session; Binance overlap is 0",
        "timezone": "America/New_York",
        "source": str(source),
        "source_opened_for_write": False,
        "license": (
            "Alpaca SIP historical bars, feed=sip, adjustment=raw, read only. "
            "No Binance equity kline was synthesized to fill a gap."
        ),
        "historical_use": {
            "rth_ohlcv": "present",
            "pre_ohlcv": "present_not_a_quote_tape",
            "post_ohlcv": "present_not_a_quote_tape",
            "overnight_ohlcv": "absent",
            "bid_ask": "absent",
            "binance_overlap_bars": 0,
            "after_2000_et": "NO_SIP_REFERENCE",
        },
        "files": [
            _file_record(coverage_path, "coverage"),
            _file_record(symbols_path, "symbol_coverage"),
        ],
    }
    manifest_path = output / "manifest.json"
    _dump(manifest_path, manifest)
    return manifest_path


def _full_grid(items: list[SymbolCoverage], timeframe: str) -> int:
    expected = 194_100 if timeframe == "1Min" else 38_820 if timeframe == "5Min" else -1
    return sum(1 for item in items if item.stored.get("rth", 0) == expected)


def _between(minute: pa.Array, start: int, stop: int) -> pa.Array:
    return pc.and_(pc.greater_equal(minute, start), pc.less(minute, stop))


def _mask_or(left: pa.Array, right: pa.Array) -> pa.Array:
    return pc.or_(left, right)


def _count(mask: pa.Array) -> int:
    total = pc.sum(pc.cast(mask, pa.int64())).as_py()
    return 0 if total is None else int(total)


def _value_counts(array: pa.Array) -> dict[str, int]:
    counted = pc.value_counts(array).to_pylist()
    return {str(item["values"]): int(item["counts"]) for item in counted}


def _dump(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _file_record(path: Path, role: str) -> dict[str, object]:
    return {
        "role": role,
        "path": str(path),
        "sha256": sha256_file(path),
        "bytes": path.stat().st_size,
    }


def main() -> None:
    destination = OUT_DEFAULT / "s5"
    manifest = write_audit(FRESH_DEFAULT, destination)
    print(manifest)


if __name__ == "__main__":
    main()
