"""Load exchange credentials from the environment or a local file. Values are never logged."""

import os
import re
from dataclasses import dataclass
from pathlib import Path


class CredentialError(ValueError):
    """The credential file or environment does not contain a usable key pair."""


@dataclass(frozen=True)
class ApiPair:
    key: str
    secret: str

    def __repr__(self) -> str:
        return "ApiPair(redacted)"


def binance_from_env() -> ApiPair:
    return ApiPair(_env("BINANCE_API_KEY"), _env("BINANCE_API_SECRET"))


def alpaca_from_env() -> ApiPair:
    return ApiPair(_env("ALPACA_API_KEY_ID"), _env("ALPACA_API_SECRET_KEY"))


def read_binance_file(path: Path) -> ApiPair:
    return _pair_from_labeled_lines(
        path,
        key_labels=("API密钥", "API Key", "API KEY"),
        secret_labels=("秘钥", "Secret Key", "API Secret"),
    )


def read_alpaca_file(path: Path) -> ApiPair:
    text = path.read_text(encoding="utf-8-sig")
    key = _inline(text, "API Key ID")
    secret = _inline(text, "Secret Key")
    if not key or not secret:
        raise CredentialError("credential_file_format_invalid")
    return ApiPair(key, secret)


def _env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise CredentialError(f"missing {name}")
    return value


def _inline(text: str, label: str) -> str | None:
    match = re.search(
        r"(?mi)^\s*" + re.escape(label) + r"\s*[:：]\s*([A-Za-z0-9]+)\s*$",
        text,
    )
    return match.group(1) if match else None


def _pair_from_labeled_lines(
    path: Path,
    key_labels: tuple[str, ...],
    secret_labels: tuple[str, ...],
) -> ApiPair:
    lines = [line.strip() for line in path.read_text(encoding="utf-8-sig").splitlines()]
    key = _labeled(lines, key_labels)
    secret = _labeled(lines, secret_labels)
    if not key or not secret:
        raise CredentialError("credential_file_format_invalid")
    return ApiPair(key, secret)


def _labeled(lines: list[str], labels: tuple[str, ...]) -> str | None:
    for index, line in enumerate(lines):
        for label in labels:
            if line.casefold().startswith(label.casefold()):
                for sep in ("：", ":"):
                    if sep in line:
                        value = line.split(sep, 1)[1].strip()
                        if value:
                            return value
                if index + 1 < len(lines) and lines[index + 1]:
                    return lines[index + 1].strip()
    return None
