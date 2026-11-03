"""Content hashes for the research-layer manifest."""

import hashlib
from pathlib import Path


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def assert_output_isolated(output: Path, source: Path) -> None:
    """Derived files must not be written into the read-only SIP store."""

    out = output.resolve()
    fresh = source.resolve()
    if out == fresh or fresh in out.parents:
        raise ValueError("refusing to write inside the source SIP store")
    if out in fresh.parents:
        raise ValueError("refusing to use a parent of the source SIP store as output")
