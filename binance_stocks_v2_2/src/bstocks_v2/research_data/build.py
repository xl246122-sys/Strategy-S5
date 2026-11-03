"""Build the V2.2 research layer without modifying the source SIP store."""

from __future__ import annotations

import argparse
import csv
import json
import sqlite3
from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pyarrow.dataset as ds
import pyarrow.parquet as pq

from bstocks_v2.research_data.adjust import backward_price_factors, jump_flag, load_splits
from bstocks_v2.research_data.calendar import (
    RESEARCH_END,
    RESEARCH_START,
    XnysCalendar,
    load_xnys_calendar,
)
from bstocks_v2.research_data.catalog import load_candidates
from bstocks_v2.research_data.domain_shift import ComparedBar, align, summarize
from bstocks_v2.research_data.exclusions import hard_exclusion
from bstocks_v2.research_data.features import FeatureRow, build_rth_features
from bstocks_v2.research_data.manifest import assert_output_isolated, sha256_file
from bstocks_v2.research_data.paths import FRESH_DEFAULT, OUT_DEFAULT
from bstocks_v2.research_data.quality import PriceBar
from bstocks_v2.research_data.universe import (
    classify_membership,
    value_before,
    values_before,
)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fresh", type=Path, default=FRESH_DEFAULT)
    parser.add_argument("--out", type=Path, default=OUT_DEFAULT)
    parser.add_argument("--exchange", type=Path, required=True)
    parser.add_argument("--caps", type=Path, default=None)
    parser.add_argument("--binance-bars", type=Path, default=None)
    args = parser.parse_args(argv)
    assert_output_isolated(args.out, args.fresh)
    args.out.mkdir(parents=True, exist_ok=True)
    calendar = load_xnys_calendar()
    sessions = calendar.sessions_between(RESEARCH_START, RESEARCH_END)
    candidates = load_candidates(args.fresh / "config" / "universe_clean.csv")
    action_file = "corporate_actions_20241101_20261030.csv"
    splits = load_splits(args.fresh / "corporate_actions" / action_file)
    buy_sell, known = load_exchange(args.exchange)
    caps = load_caps(args.caps) if args.caps else {}
    splits_by_symbol: dict[str, list[date]] = defaultdict(list)
    for split in splits:
        splits_by_symbol[split.symbol].append(split.ex_date)

    closes: dict[str, list[tuple[date, Decimal]]] = {}
    jumps: list[dict[str, str]] = []
    for index, candidate in enumerate(candidates, start=1):
        daily = read_daily(args.fresh, candidate.symbol)
        closes[candidate.symbol] = [(day, close) for day, close, _volume in daily]
        split_days = set(splits_by_symbol.get(candidate.symbol, []))
        jumps.extend(_jumps(candidate.symbol, daily, split_days))
        if index % 50 == 0:
            print(f"daily {index}/{len(candidates)}", flush=True)

    feature_rows = 0
    dollar: dict[str, list[tuple[date, Decimal]]] = {}
    feed_values: set[str] = set()
    session_mismatches = 0
    symbols_written = 0
    for index, candidate in enumerate(candidates, start=1):
        if hard_exclusion(candidate.symbol) or candidate.symbol not in buy_sell:
            dollar[candidate.symbol] = []
            continue
        bars, mismatches, feeds = read_five_minute(
            args.fresh,
            candidate.symbol,
            candidate.window_start,
            candidate.window_end,
            calendar,
        )
        session_mismatches += mismatches
        feed_values.update(feeds)
        if any(feed.casefold() != "sip" for feed in feeds):
            dollar[candidate.symbol] = []
            print(f"quarantine {candidate.symbol}", flush=True)
            continue
        factors = {
            day.isoformat(): factor
            for day, factor in backward_price_factors(
                [split for split in splits if split.symbol == candidate.symbol],
                sessions,
            ).items()
        }
        features, days = build_rth_features(bars, calendar, factors)
        dollar[candidate.symbol] = [
            (date.fromisoformat(item.ny_date), item.rth_dollar_volume) for item in days
        ]
        if features:
            _write_features(args.out / "features" / "5m" / f"{candidate.symbol}.parquet", features)
            feature_rows += len(features)
            symbols_written += 1
        if index % 25 == 0:
            print(f"features {index}/{len(candidates)} rows={feature_rows}", flush=True)

    membership_rows: list[dict[str, str]] = []
    for candidate in candidates:
        symbol_caps = caps.get(candidate.symbol, [])
        for session in sessions:
            item = classify_membership(
                candidate,
                session,
                buy_sell=buy_sell,
                known_symbols=known,
                prior_close=value_before(closes.get(candidate.symbol, []), session),
                prior_dollar_volumes=values_before(dollar.get(candidate.symbol, []), session, 20),
                cap_observations=symbol_caps,
            )
            membership_rows.append(
                {
                    "symbol": item.symbol,
                    "session": item.session.isoformat(),
                    "asset_type": item.asset_type,
                    "entity_id": item.entity_id,
                    "alpaca_asof": item.alpaca_asof,
                    "research_eligible": "true" if item.research_eligible else "false",
                    "reason": item.reason,
                }
            )
    membership_path = args.out / "universe" / "membership.parquet"
    _write_dicts(membership_path, membership_rows)
    pit = [row for row in membership_rows if row["research_eligible"] == "true"]
    pit_path = args.out / "universe" / "pit.parquet"
    _write_dicts(pit_path, pit)
    jump_path = args.out / "quality" / "daily_jumps.parquet"
    _write_dicts(jump_path, jumps)
    source = _source_inventory(args.fresh)
    sample = _verify_sample(args.fresh, source["sample_rows"])
    shift = _domain_shift(args.binance_bars)
    _write_shift(args.out / "reports", shift)
    manifest = {
        "research_window": [RESEARCH_START.isoformat(), RESEARCH_END.isoformat()],
        "source_root": str(args.fresh),
        "source_policy": "read_only_reference_not_copied",
        "source_feed": source["feeds"],
        "source_timeframes": source["timeframes"],
        "source_manifest_sha256": sha256_file(args.fresh / "manifests" / "manifest.sqlite"),
        "sha256_sample": sample,
        "tables": [
            _table(membership_path, len(membership_rows)),
            _table(pit_path, len(pit)),
            _table(jump_path, len(jumps)),
        ],
        "feature_symbols": symbols_written,
        "feature_rows": feature_rows,
        "feed_values_seen_in_5m": sorted(feed_values),
        "session_mismatches_5m": session_mismatches,
        "domain_shift": shift,
        "eligible_rows": len(pit),
        "reason_counts": _counts([row["reason"] for row in membership_rows]),
    }
    manifest_path = args.out / "data_manifest_v2_2.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, default=str), encoding="utf-8")
    digest = sha256_file(manifest_path)
    (args.out / "data_manifest_v2_2.sha256").write_text(digest + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "eligible_rows": len(pit),
                "feature_rows": feature_rows,
                "reasons": manifest["reason_counts"],
            }
        )
    )


def load_exchange(path: Path) -> tuple[set[str], set[str]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows: object
    if isinstance(payload, dict) and isinstance(payload.get("symbols"), list):
        rows = payload["symbols"]
    elif isinstance(payload, list):
        rows = payload
    else:
        raise ValueError("exchange file has no symbols")
    buy_sell: set[str] = set()
    known: set[str] = set()
    if not isinstance(rows, list):
        return buy_sell, known
    for row in rows:
        if not isinstance(row, dict):
            continue
        symbol = row.get("symbol")
        if not isinstance(symbol, str):
            continue
        known.add(symbol)
        if row.get("tradability") == "BUY_SELL":
            buy_sell.add(symbol)
    return buy_sell, known


def load_caps(path: Path) -> dict[str, list[tuple[date, Decimal]]]:
    grouped: dict[str, list[tuple[date, Decimal]]] = defaultdict(list)
    with path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            grouped[row["symbol"].strip()].append(
                (date.fromisoformat(row["asof"]), Decimal(row["cap_usd"]))
            )
    return grouped


def read_daily(fresh: Path, symbol: str) -> list[tuple[date, Decimal, Decimal]]:
    folder = fresh / "parquet" / "1Day" / symbol
    table = _table_or_none(folder, ["ny_date", "c", "v", "feed"])
    if table is None:
        return []
    out: list[tuple[date, Decimal, Decimal]] = []
    payload = table.to_pydict()
    for day, close, volume, feed in zip(
        payload["ny_date"], payload["c"], payload["v"], payload["feed"], strict=True
    ):
        if str(feed).casefold() != "sip":
            continue
        out.append((day, _decimal(close), _decimal(volume)))
    out.sort()
    return out


def read_five_minute(
    fresh: Path,
    symbol: str,
    window_start: date,
    window_end: date,
    calendar: XnysCalendar,
) -> tuple[list[PriceBar], int, set[str]]:
    folder = fresh / "parquet" / "5Min" / symbol
    columns = [
        "symbol",
        "t",
        "o",
        "h",
        "l",
        "c",
        "v",
        "vw",
        "feed",
        "bar_end_utc",
        "session",
        "ny_date",
    ]
    table = _table_or_none(folder, columns)
    if table is None:
        return [], 0, set()
    payload = table.to_pydict()
    bars: list[PriceBar] = []
    mismatches = 0
    feeds: set[str] = set()
    for index, start in enumerate(payload["t"]):
        day = payload["ny_date"][index]
        if day < window_start or day > window_end:
            continue
        feed = str(payload["feed"][index])
        feeds.add(feed)
        end = payload["bar_end_utc"][index]
        label = calendar.classify(start, end)
        source = str(payload["session"][index])
        if (label == "RTH") != (source == "rth"):
            mismatches += 1
        if label != "RTH":
            continue
        vwap = payload["vw"][index]
        bars.append(
            PriceBar(
                symbol=str(payload["symbol"][index]),
                start=start,
                end=end,
                open=_decimal(payload["o"][index]),
                high=_decimal(payload["h"][index]),
                low=_decimal(payload["l"][index]),
                close=_decimal(payload["c"][index]),
                volume=_decimal(payload["v"][index]),
                vwap=None if vwap is None else _decimal(vwap),
                feed=feed,
            )
        )
    return bars, mismatches, feeds


def _jumps(
    symbol: str,
    daily: list[tuple[date, Decimal, Decimal]],
    split_days: set[date],
) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    previous: tuple[date, Decimal] | None = None
    for day, close, _volume in daily:
        if previous is not None:
            flag = jump_flag(previous[1], close, day in split_days)
            if flag is not None:
                rows.append(
                    {
                        "symbol": symbol,
                        "session": day.isoformat(),
                        "previous_close": str(previous[1]),
                        "close": str(close),
                        "quality_flag": flag,
                    }
                )
        previous = (day, close)
    return rows


def _table_or_none(folder: Path, columns: list[str]) -> Any:
    files = sorted(folder.glob("*.parquet")) if folder.exists() else []
    if not files:
        return None
    return ds.dataset([str(path) for path in files], format="parquet").to_table(columns=columns)


def _decimal(value: object) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        raise ValueError("bool is not a price")
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, str):
        return Decimal(value)
    if isinstance(value, float):
        return Decimal(format(value, ".10f"))
    raise ValueError(f"cannot read {type(value).__name__}")


def _write_features(path: Path, rows: list[FeatureRow]) -> None:
    encoded = [
        {
            "symbol": row.symbol,
            "start_utc": row.start.isoformat(),
            "ny_date": row.ny_date,
            "session": row.session,
            "bucket": row.bucket,
            "volume": str(row.volume),
            "cum_vwap": "" if row.cum_vwap is None else str(row.cum_vwap),
            "tod_mean_volume": "" if row.tod_mean_volume is None else str(row.tod_mean_volume),
            "tod_median_volume": ""
            if row.tod_median_volume is None
            else str(row.tod_median_volume),
            "baseline_days": str(row.baseline_days),
            "baseline_ready": "true" if row.baseline_ready else "false",
            "vwap_intact": "true" if row.vwap_intact else "false",
            "quality_flag": row.quality_flag,
            "feed": row.feed,
        }
        for row in rows
    ]
    _write_dicts(path, encoded)


def _write_dicts(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    import pyarrow as pa

    if not rows:
        pq.write_table(pa.table({"symbol": pa.array([], type=pa.string())}), path)
        return
    columns = list(rows[0])
    table = pa.table({column: [row[column] for row in rows] for column in columns})
    pq.write_table(table, path)


def _source_inventory(fresh: Path) -> dict[str, object]:
    connection = sqlite3.connect(fresh / "manifests" / "manifest.sqlite")
    feeds = connection.execute(
        "select feed, adjustment, status, count(*) from shards group by 1, 2, 3"
    ).fetchall()
    frames = connection.execute(
        "select timeframe, count(*), sum(row_count), min(min_ts), max(max_ts) "
        "from shards group by timeframe"
    ).fetchall()
    sample_rows = connection.execute(
        "select timeframe, file_relpath, sha256 from shards where status='completed' "
        "and timeframe in ('1Min','5Min','1Hour','1Day','1Week') group by timeframe"
    ).fetchall()
    return {
        "feeds": [list(row) for row in feeds],
        "timeframes": [
            {
                "timeframe": row[0],
                "files": row[1],
                "rows": row[2],
                "start": row[3],
                "end": row[4],
            }
            for row in frames
        ],
        "sample_rows": [(str(row[0]), str(row[1]), str(row[2])) for row in sample_rows],
    }


def _verify_sample(fresh: Path, sample_rows: object) -> list[dict[str, object]]:
    if not isinstance(sample_rows, list):
        return []
    checked: list[dict[str, object]] = []
    for item in sample_rows:
        if not isinstance(item, tuple) or len(item) != 3:
            continue
        timeframe, relative, expected = item
        path = fresh / str(relative)
        actual = sha256_file(path) if path.exists() else ""
        checked.append(
            {
                "timeframe": timeframe,
                "file": relative,
                "match": actual == expected,
            }
        )
    return checked


def _domain_shift(path: Path | None) -> dict[str, object]:
    if path is None or not path.exists():
        summary = summarize([])
        return {
            "status": summary.status,
            "overlap": summary.overlap,
            "note": "no Binance @kline_5m capture overlaps the SIP research window",
        }
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        return {"status": "insufficient_overlap", "overlap": 0}
    left = _compared(payload.get("alpaca"))
    right = _compared(payload.get("binance"))
    summary = summarize(align(left, right))
    return {
        "status": summary.status,
        "overlap": summary.overlap,
        "close_median_bps": summary.close_median_bps,
        "close_p95_bps": summary.close_p95_bps,
        "volume_median_ratio": summary.volume_median_ratio,
        "vwap_median_bps": summary.vwap_median_bps,
    }


def _compared(payload: object) -> list[ComparedBar]:
    if not isinstance(payload, list):
        return []
    bars: list[ComparedBar] = []
    for row in payload:
        if not isinstance(row, dict):
            continue
        start = row.get("start")
        if not isinstance(start, str):
            continue
        bars.append(
            ComparedBar(
                symbol=str(row.get("symbol", "")),
                start=datetime.fromisoformat(start),
                close=Decimal(str(row["close"])),
                volume=Decimal(str(row["volume"])),
                vwap=Decimal(str(row["vwap"])),
            )
        )
    return bars


def _write_shift(folder: Path, shift: dict[str, object]) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    text = json.dumps(shift, indent=2, default=str)
    (folder / "domain_shift.json").write_text(text, encoding="utf-8")
    lines = [
        "# 跨数据源偏差预算",
        "",
        f"- 状态：`{shift.get('status')}`",
        f"- 对齐根数：{shift.get('overlap')}",
        f"- 说明：{shift.get('note', '见 json 中的价格、成交量和 VWAP 偏差。')}",
        "",
        "比较键是同一标的、同一根 K 线的开始时间。",
        "没有重叠的币安 5 分钟 K 线时，不估算偏差预算。",
    ]
    (folder / "domain_shift.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _table(path: Path, rows: int) -> dict[str, object]:
    return {"path": str(path), "rows": rows, "sha256": sha256_file(path)}


def _counts(values: list[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return counts


if __name__ == "__main__":
    main()
