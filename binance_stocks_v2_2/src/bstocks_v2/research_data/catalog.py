"""Read the candidate CSV. This module does not open Parquet files."""

import csv
from datetime import date
from pathlib import Path

from bstocks_v2.research_data.universe import Candidate, rename_window_end


def load_candidates(path: Path) -> list[Candidate]:
    candidates: list[Candidate] = []
    with path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            symbol = row["symbol"].strip()
            window_end = date.fromisoformat(row["window_end_ny"])
            flags = row.get("anomaly_flags", "")
            candidates.append(
                Candidate(
                    symbol=symbol,
                    asset_type=row["asset_type"].strip(),
                    window_start=date.fromisoformat(row["window_start_ny"]),
                    window_end=rename_window_end(symbol, window_end, flags),
                    entity_id=row.get("entity_id", ""),
                    alpaca_asof=row.get("alpaca_asof", ""),
                    in_main_backtest_pool=row.get("in_main_backtest_pool", "") == "True",
                    anomaly_flags=flags,
                )
            )
    return candidates
