"""One lockbox execution. A second call refuses once the seal file exists."""

import hashlib
import json
from pathlib import Path


def sealed(path: Path) -> bool:
    return path.exists()


def write_seal(path: Path, result: Path, payload: str) -> str:
    if path.exists():
        raise RuntimeError("lockbox seal already exists")
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    path.write_text(
        json.dumps({"result": result.name, "sha256": digest}, indent=2),
        encoding="utf-8",
    )
    return digest
