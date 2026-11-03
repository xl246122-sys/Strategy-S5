"""Write the S5-P5 assessment from the saved screen. This does not reread prices into a fill."""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pyarrow.parquet as pq

from bstocks_v2.research_data.manifest import assert_output_isolated, sha256_file
from bstocks_v2.research_data.paths import FRESH_DEFAULT, OUT_DEFAULT
from bstocks_v2.strategies.s1.spec import TRADED
from bstocks_v2.strategies.s5.assess import assess_screen, require_research_mode
from bstocks_v2.strategies.s5.lock import s5_live_enabled

HOLDOUT_SYMBOL = "INTC"

SCREEN_SHA = "5cf2657931700d1ab58dc74b823e352e4788e850e0efc03a71f18fd899ad23a6"
COVERAGE_SHA = "23d24626f433aaf709590752c66f3300e9d582cb716b2e1408ee3bac6fd27c25"


def probe_schema(source: Path) -> dict[str, object]:
    """Read one 5-minute schema. Row groups are not loaded."""

    folder = source / "parquet" / "5Min" / "SPY"
    files = sorted(folder.glob("*.parquet")) if folder.is_dir() else []
    if not files:
        raise ValueError("missing SPY 5Min schema probe")
    names = [str(name) for name in pq.read_schema(files[0]).names]
    return {
        "path": str(files[0]),
        "columns": names,
        "has_bid_ask": "bid" in names or "ask" in names,
        "rows_loaded": 0,
    }


def run(source: Path = FRESH_DEFAULT, store: Path = OUT_DEFAULT / "s5") -> dict[str, object]:
    """Assess the frozen screen. A bid column stops the run instead of becoming a profit."""

    require_research_mode(s5_live_enabled())
    assessment_path = store / "s5_05_assessment.json"
    assert_output_isolated(assessment_path, source)
    screen_path = store / "screen_summary.json"
    coverage_path = store / "coverage.json"
    screen_sha = _require_hash(screen_path, SCREEN_SHA)
    coverage_sha = _require_hash(coverage_path, COVERAGE_SHA)
    probe = probe_schema(source)
    if probe["has_bid_ask"] is True:
        raise RuntimeError("QUOTE_TAPE_UNREVIEWED")
    body = assess_screen(_load(screen_path), _load(coverage_path))
    if body["overnight_profit_claimed"] is not False or body["strategy_verdict"] != "NO_GO":
        raise RuntimeError("screen assessment drifted")
    if body["phase_gate"] != "PASS" or body["shadow_authorized"] is not False:
        raise RuntimeError("screen assessment drifted")
    body["symbol_holdout"] = {
        "symbol": HOLDOUT_SYMBOL,
        "in_preregistered_universe": HOLDOUT_SYMBOL in TRADED,
        "removed_fills": 0,
        "adopted": "full",
        "full_expectancy_usdc": None,
        "reduced_expectancy_usdc": None,
        "single_name": False,
    }
    payload: dict[str, object] = {
        **body,
        "provenance": {
            "screen_sha256": screen_sha,
            "coverage_sha256": coverage_sha,
            "schema_probe": probe,
            "git": _git_state(Path(__file__).resolve()),
            "generated_at_utc": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
    }
    store.mkdir(parents=True, exist_ok=True)
    _dump(assessment_path, payload)
    _dump(store / "s5_05_experiment.json", body["experiment"])
    _dump(
        store / "s5_05_ledger.json",
        {
            "kind": "count_ledger",
            "quote_backed_fills": [],
            "partials": [],
            "loss_filter": "forbidden",
            "subset_paths": "UNFILLABLE",
            "signals": body["signals"],
            "quote_backed_fills_n": body["quote_backed_fills"],
            "arms": body["arms"],
            "ablation": body["ablation"],
        },
    )
    _dump(
        store / "s5_05_manifest.json",
        {
            "created_utc": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "mode": "rth_candidate_screen",
            "strategy_verdict": body["strategy_verdict"],
            "source_opened_for_write": False,
            "files": [
                _file(assessment_path, "assessment"),
                _file(store / "s5_05_experiment.json", "experiment"),
                _file(store / "s5_05_ledger.json", "ledger"),
            ],
        },
    )
    return payload


def _require_hash(path: Path, expected: str) -> str:
    digest = sha256_file(path)
    if digest != expected:
        raise ValueError(f"hash mismatch for {path.name}")
    return digest


def _load(path: Path) -> dict[str, object]:
    raw: object = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(path.name)
    loaded: dict[str, object] = {}
    for key, value in raw.items():
        if not isinstance(key, str):
            raise ValueError(path.name)
        loaded[key] = value
    return loaded


def _git_state(start: Path) -> dict[str, object]:
    root = start
    for candidate in (start, *start.parents):
        if (candidate / ".git").exists():
            root = candidate
            break
    sha = _git(root, "rev-parse", "HEAD")
    status = _git(root, "status", "--porcelain")
    return {"root": str(root), "sha": sha, "dirty": status != ""}


def _git(root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _dump(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _file(path: Path, role: str) -> dict[str, object]:
    return {
        "role": role,
        "path": str(path),
        "sha256": sha256_file(path),
        "bytes": path.stat().st_size,
    }


def main() -> None:
    payload = run()
    print(payload["strategy_verdict"], payload["phase_gate"])


if __name__ == "__main__":
    main()
