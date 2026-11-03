"""Code-level checks for the stage-14 safety list. This does not place an order."""

import re
from pathlib import Path

from bstocks_v2.constitution.guard import live_trade_enabled
from bstocks_v2.strategies.s4.lock import LIVE_ENABLED, s4_live_enabled

_SRC = Path(__file__).resolve().parents[1]
_SECRET = re.compile(
    r"(api[_-]?key|api[_-]?secret|secret[_-]?key)\s*=\s*['\"][A-Za-z0-9+/=]{16,}",
    re.IGNORECASE,
)


def safety_audit() -> dict[str, object]:
    rows = (
        ("wallet CARD is the only buy wallet", "constitution/guard.py", '"wallet_type": "CARD"'),
        (
            "tokenize stays false on the order draft",
            "execution_gateway/idempotent.py",
            "tokenize is not False",
        ),
        ("fractional limit falls back by 486442", "research_backtest/sizing.py", '"486442"'),
        ("minimum economic notional is 120", "contracts/models.py", 'Decimal("120")'),
        ("funding symbols drop the EQ_ prefix", "reconciler/ledger.py", '.removeprefix("EQ_")'),
        (
            "open-order tc-FN is not equity",
            "portfolio_risk/equity.py",
            "tc-FN is not part of equity",
        ),
        ("intraday bars have a WAL table", "market_ingestor/wal.py", "intraday_bars_wal"),
        ("feature book can rebuild from that WAL", "feature_store/recover.py", "intraday_bars_wal"),
        ("guardian knows 486446", "guardian/spec.py", "486446"),
        ("guardian knows 486444", "guardian/spec.py", "486444"),
        ("guardian knows -1112", "guardian/spec.py", "-1112"),
        ("guardian knows 486441", "guardian/spec.py", "486441"),
        ("flatten uses the constitution deadline", "guardian/machine.py", "flatten_deadline"),
        ("8% lock keeps 92% of the high", "constitution/guard.py", 'Decimal("0.92")'),
        ("mint and redeem stay off the whitelist", "constitution/guard.py", '"/tokenized/mint"'),
        (
            "equity routes stay under the equity prefix",
            "constitution/guard.py",
            '"/sapi/v1/equity/"',
        ),
    )
    checks = [_has(name, path, needle) for name, path, needle in rows]
    secrets = _secrets()
    runtime = {
        "live_trade_enabled": live_trade_enabled(),
        "s4_live_enabled": s4_live_enabled(),
        "s4_constant": LIVE_ENABLED,
    }
    passed = all(bool(item["passed"]) for item in checks) and not secrets
    passed = passed and runtime["live_trade_enabled"] is False
    passed = passed and runtime["s4_live_enabled"] is False and runtime["s4_constant"] is False
    return {"passed": passed, "checks": checks, "secret_hits": secrets, "runtime": runtime}


def _has(name: str, relative: str, needle: str) -> dict[str, object]:
    path = _SRC / relative
    text = path.read_text(encoding="utf-8")
    line = _line(text, needle)
    return {"name": name, "path": str(path), "line": line, "passed": line is not None}


def _line(text: str, needle: str) -> int | None:
    for number, row in enumerate(text.splitlines(), start=1):
        if needle in row:
            return number
    return None


def _secrets() -> list[str]:
    found: list[str] = []
    for path in _SRC.rglob("*.py"):
        for number, row in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if _SECRET.search(row):
                found.append(f"{path}:{number}")
    return found
