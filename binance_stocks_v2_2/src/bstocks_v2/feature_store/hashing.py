"""Stable hashes for feature snapshots. Float is not used."""

import hashlib


def sha256_rows(rows: tuple[tuple[str, str], ...] | list[tuple[str, str]]) -> str:
    payload = "\n".join(f"{key}={value}" for key, value in rows)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
