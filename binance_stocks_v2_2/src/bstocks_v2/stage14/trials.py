"""Sharpes of the configurations already compared before the lockbox."""

import json
from decimal import Decimal
from pathlib import Path

from bstocks_v2.research_data.paths import OUT_DEFAULT
from bstocks_v2.stage14.spec import TRIALS


def validation_sharpes(root: Path = OUT_DEFAULT) -> tuple[list[Decimal], dict[str, str | None]]:
    """Per-trade Sharpe on the split each earlier stage used for selection.

    A trial with fewer than two closed trades has no Sharpe. It still counts
    in the trial total. The variance below uses only the defined Sharpes.
    """

    stage6 = _read(root / "reports" / "stage6_ablation.json")
    stage7 = _read(root / "reports" / "stage7_ablation.json")
    stage8 = _read(root / "reports" / "stage8_ablation.json")
    stage9 = _read(root / "reports" / "stage9_ablation.json")
    rows: dict[str, object] = {
        "S1:kalman": _score(_child(_child(stage6, "models"), "kalman"), "validation"),
        "S1:rls": _score(_child(_child(stage6, "models"), "rls"), "validation"),
        "S1:ols": _score(_child(_child(stage6, "models"), "ols"), "validation"),
        "S2:rule": _score(_child(_child(stage7, "rule"), "scores"), "score"),
        "S2:gate": _score(_child(_child(stage7, "gate"), "scores"), "score"),
        "S4:baseline": _score(_child(_child(stage9, "baseline"), "scores"), "validation"),
        "S4:guarded": _score(_child(_child(stage9, "guarded"), "scores"), "validation"),
    }
    horizons = _child(stage8, "horizons")
    for horizon in ("5", "15", "30"):
        block = _child(horizons, horizon)
        for arm in ("baseline", "sequential"):
            rows[f"S3:{horizon}:{arm}"] = _score(_child(_child(block, arm), "scores"), "validation")
    if set(rows) != set(TRIALS):
        raise RuntimeError("trial names drifted")
    defined: list[Decimal] = []
    printed: dict[str, str | None] = {}
    for name in TRIALS:
        sharpe = _one(rows[name])
        printed[name] = None if sharpe is None else format(sharpe, "f")
        if sharpe is not None:
            defined.append(sharpe)
    return defined, printed


def _read(path: Path) -> dict[str, object]:
    if not path.exists():
        raise RuntimeError(f"missing {path.name}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError(path.name)
    return payload


def _child(node: object, key: str) -> dict[str, object]:
    if not isinstance(node, dict) or key not in node:
        raise RuntimeError(key)
    found = node[key]
    if not isinstance(found, dict):
        raise RuntimeError(key)
    return found


def _score(node: dict[str, object], key: str) -> object:
    if key not in node:
        raise RuntimeError(key)
    return node[key]


def _one(score: object) -> Decimal | None:
    if not isinstance(score, dict):
        raise RuntimeError("score is not an object")
    count = score.get("closed_trades")
    mean = score.get("mean_net_bps")
    error = score.get("se_net_bps")
    if not isinstance(count, int) or count < 2:
        return None
    if not isinstance(mean, str) or not isinstance(error, str):
        return None
    width = Decimal(error)
    if width <= 0:
        return None
    return Decimal(mean) / (width * Decimal(count).sqrt())
