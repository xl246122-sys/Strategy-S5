"""Load a JSON runtime file and refuse any attempt to weaken the constitution."""

import json
from pathlib import Path

from bstocks_v2.constitution.guard import ConstitutionGuard
from bstocks_v2.contracts.errors import ConstitutionBypassError


def load_runtime_config(path: Path, guard: ConstitutionGuard | None = None) -> dict[str, object]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ConstitutionBypassError("config root must be an object")
    incoming: dict[str, object] = {}
    for key, value in raw.items():
        if not isinstance(key, str):
            raise ConstitutionBypassError("config keys must be strings")
        incoming[key] = value
    (guard or ConstitutionGuard()).reject_config_override(incoming)
    return incoming
