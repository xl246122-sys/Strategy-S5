"""Download SEC company facts and write symbol, asof, cap_usd.

The cache stays under the research output tree. The SIP store is not written.
"""

import json
import time
import urllib.request
from pathlib import Path

from bstocks_v2.research_data.adjust import SplitEvent, load_splits
from bstocks_v2.research_data.build import read_daily
from bstocks_v2.research_data.catalog import load_candidates
from bstocks_v2.research_data.paths import FRESH_DEFAULT, OUT_DEFAULT
from bstocks_v2.research_data.sec_caps import cap_series, share_prints

_TICKERS = "https://www.sec.gov/files/company_tickers.json"
_FACTS = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
_AGENT = "bstocks-v2-2/1.0 (point-in-time market-cap research; mailto:research@localhost)"


def main() -> None:
    fresh = FRESH_DEFAULT
    out = OUT_DEFAULT
    cache = out / "reference" / "sec"
    cache.mkdir(parents=True, exist_ok=True)
    tickers = _ticker_map(cache / "company_tickers.json")
    candidates = [
        item
        for item in load_candidates(fresh / "config" / "universe_clean.csv")
        if item.asset_type == "stock" and item.in_main_backtest_pool
    ]
    splits = load_splits(
        fresh / "corporate_actions" / "corporate_actions_20241101_20261030.csv"
    )
    by_symbol: dict[str, list[SplitEvent]] = {}
    for split in splits:
        by_symbol.setdefault(split.symbol, []).append(split)
    destination = out / "universe" / "pit_caps.csv"
    lines = ["symbol,asof,cap_usd"]
    missing: list[str] = []
    for index, candidate in enumerate(candidates, start=1):
        cik = tickers.get(candidate.symbol) or tickers.get(candidate.symbol.replace(".", "-"))
        if cik is None:
            missing.append(candidate.symbol)
            print(f"{index}/{len(candidates)} {candidate.symbol} no CIK", flush=True)
            continue
        payload = _facts(cache / f"CIK{cik}.json", cik)
        prints = share_prints(payload)
        closes = [(day, close) for day, close, _volume in read_daily(fresh, candidate.symbol)]
        series = cap_series(closes, prints, by_symbol.get(candidate.symbol, []))
        if not series:
            missing.append(candidate.symbol)
            print(f"{index}/{len(candidates)} {candidate.symbol} no public shares", flush=True)
            continue
        for asof, cap in series:
            lines.append(f"{candidate.symbol},{asof.isoformat()},{cap}")
        print(f"{index}/{len(candidates)} {candidate.symbol} caps {len(series)}", flush=True)
    destination.write_text("\n".join(lines) + "\n", encoding="utf-8")
    (out / "universe" / "pit_caps_missing.txt").write_text(
        "\n".join(missing) + ("\n" if missing else ""),
        encoding="utf-8",
    )
    print(f"wrote {destination} symbols {len(candidates) - len(missing)} missing {len(missing)}")


def _ticker_map(path: Path) -> dict[str, str]:
    payload = _json(path, _TICKERS)
    mapping: dict[str, str] = {}
    if not isinstance(payload, dict):
        raise RuntimeError("ticker file")
    for row in payload.values():
        if not isinstance(row, dict):
            continue
        ticker = row.get("ticker")
        cik = row.get("cik_str")
        if isinstance(ticker, str) and isinstance(cik, int):
            mapping[ticker.upper()] = f"{cik:010d}"
    return mapping


def _facts(path: Path, cik: str) -> dict[str, object]:
    payload = _json(path, _FACTS.format(cik=cik))
    if not isinstance(payload, dict):
        raise RuntimeError(cik)
    return payload


def _json(path: Path, url: str) -> object:
    if not path.exists():
        request = urllib.request.Request(
            url,
            headers={"User-Agent": _AGENT, "Accept": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=60) as response:
            path.write_bytes(response.read())
        time.sleep(0.12)
    return json.loads(path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    main()
