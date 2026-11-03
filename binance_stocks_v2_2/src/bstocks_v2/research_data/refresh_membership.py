"""Apply a point-in-time cap file to an existing membership table.

ETF rows and every gate that already failed for another reason stay unchanged.
The previous parquet files are copied aside before the new tables are written.
"""

import shutil
from datetime import date
from pathlib import Path

import pyarrow.parquet as pq

from bstocks_v2.research_data.build import load_caps
from bstocks_v2.research_data.universe import reclassify_unverified_cap


def refresh(out_dir: Path, caps_path: Path) -> dict[str, int]:
    universe = out_dir / "universe"
    membership_path = universe / "membership.parquet"
    pit_path = universe / "pit.parquet"
    if not membership_path.exists():
        raise RuntimeError("membership parquet is missing")
    caps = load_caps(caps_path)
    rows = _rows(membership_path)
    counts = {"unchanged": 0, "still_unverified": 0, "below": 0, "admitted": 0}
    updated: list[dict[str, str]] = []
    for row in rows:
        reason = reclassify_unverified_cap(
            row["reason"],
            date.fromisoformat(row["session"]),
            caps.get(row["symbol"], []),
        )
        if reason == row["reason"]:
            if reason == "market_cap_unverified":
                counts["still_unverified"] += 1
            else:
                counts["unchanged"] += 1
        elif reason == "market_cap_below":
            counts["below"] += 1
        elif reason == "ok":
            counts["admitted"] += 1
        else:
            raise RuntimeError(f"unexpected cap reason {reason}")
        row["reason"] = reason
        row["research_eligible"] = "true" if reason == "ok" else "false"
        updated.append(row)
    _archive(membership_path)
    _archive(pit_path)
    _write(membership_path, updated)
    eligible = [row for row in updated if row["research_eligible"] == "true"]
    _write(pit_path, eligible)
    counts["eligible"] = len(eligible)
    counts["rows"] = len(updated)
    return counts


def _rows(path: Path) -> list[dict[str, str]]:
    table = pq.read_table(path)
    payload = table.to_pydict()
    columns = list(payload)
    count = len(payload[columns[0]]) if columns else 0
    return [{column: str(payload[column][index]) for column in columns} for index in range(count)]


def _archive(path: Path) -> None:
    if not path.exists():
        return
    target = path.with_name(path.stem + "_before_caps" + path.suffix)
    if target.exists():
        raise RuntimeError(f"archive already exists: {target.name}")
    shutil.copy2(path, target)


def _write(path: Path, rows: list[dict[str, str]]) -> None:
    import pyarrow as pa

    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        pq.write_table(pa.table({"symbol": pa.array([], type=pa.string())}), path)
        return
    columns = list(rows[0])
    table = pa.table({column: [row[column] for row in rows] for column in columns})
    pq.write_table(table, path)
