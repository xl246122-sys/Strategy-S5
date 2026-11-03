"""Official equity quotes for names the websocket has not printed.

Binance publishes no equity kline REST route. The documented market-data
quote is at most about five seconds stale, so the board scans that route
on a five-second cadence. The key stays on the Japan host.
"""

import json
import os
import re
import subprocess
from dataclasses import dataclass
from decimal import Decimal

from bstocks_v2.constitution.guard import ConstitutionGuard
from bstocks_v2.contracts.decimals import parse_decimal

SCAN_SECONDS = 5
QUOTE_PATH = "/sapi/v1/equity/market/quote"
QUOTE_URL = "https://api.binance.com" + QUOTE_PATH
SCAN_HOST = "ubuntu@43.163.203.127"
ENV_FILE = "/home/ubuntu/.config/bstocks_v2/stage2.env"
WEIGHT_PAUSE = 8000
_WORKERS = 24
_SYMBOL = re.compile(r"^[A-Z][A-Z0-9.]{0,14}$")

_PROGRAM = """
import json, os, sys, urllib.parse, urllib.request
from concurrent.futures import ThreadPoolExecutor
from urllib.error import HTTPError

symbols = json.loads('''__SYMBOLS_JSON__''')
for line in open("__ENV_FILE__", encoding="utf-8"):
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    name, value = line.split("=", 1)
    os.environ[name.strip()] = value.strip().strip('"').strip("'")
key = os.environ.get("BINANCE_API_KEY", "").strip()
rows = []
weight = None
error = None
if not key:
    error = "unavailable"
else:
    def one(symbol):
        url = "__QUOTE_URL__?symbol=" + urllib.parse.quote(symbol)
        headers = {"X-MBX-APIKEY": key, "User-Agent": "bstocks-scan"}
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=8) as resp:
                return symbol, resp.read(), resp.headers.get("X-SAPI-USED-IP-WEIGHT-1M")
        except HTTPError as exc:
            used = exc.headers.get("X-SAPI-USED-IP-WEIGHT-1M") if exc.headers else None
            if exc.code == 429:
                return symbol, b"", used, "weight"
            return symbol, b"", used, None
        except Exception:
            return None
    try:
        with ThreadPoolExecutor(max_workers=__WORKERS__) as pool:
            for item in pool.map(one, symbols):
                if not item:
                    continue
                symbol, body, used, *rest = item
                if used and str(used).isdigit():
                    weight = int(used)
                if rest and rest[0] == "weight":
                    error = "weight"
                text = body.decode().strip()
                if not text:
                    continue
                payload = json.loads(text)
                bid = payload.get("bidPrice")
                ask = payload.get("askPrice")
                if isinstance(bid, str) and isinstance(ask, str):
                    rows.append({"symbol": symbol, "bid": bid, "ask": ask})
    except Exception:
        error = error or "unavailable"
sys.stdout.write(json.dumps({"quotes": rows, "weight": weight, "error": error}))
"""


@dataclass(frozen=True)
class ScanBatch:
    quotes: tuple[tuple[str, Decimal, Decimal], ...]
    weight: int | None
    error: str | None = None


def remote_program(symbols: tuple[str, ...]) -> str:
    clean = _names(symbols)
    return (
        _PROGRAM.replace("__SYMBOLS_JSON__", json.dumps(clean))
        .replace("__ENV_FILE__", ENV_FILE)
        .replace("__QUOTE_URL__", QUOTE_URL)
        .replace("__WORKERS__", str(_WORKERS))
    )


def decode_scan(raw: str) -> ScanBatch:
    payload = json.loads(raw, parse_float=Decimal)
    if not isinstance(payload, dict):
        raise ValueError("scan payload")
    rows = payload.get("quotes")
    quotes: list[tuple[str, Decimal, Decimal]] = []
    if isinstance(rows, list):
        for row in rows:
            if not isinstance(row, dict):
                continue
            parsed = _sides(row.get("symbol"), row.get("bid"), row.get("ask"))
            if parsed is not None:
                quotes.append(parsed)
    weight = payload.get("weight")
    used = weight if isinstance(weight, int) and not isinstance(weight, bool) else None
    error = payload.get("error")
    return ScanBatch(tuple(quotes), used, error if isinstance(error, str) else None)


def pull_official(symbols: tuple[str, ...]) -> ScanBatch:
    """Read the official quote route. A missing local key uses the Japan host."""

    ConstitutionGuard().check_endpoint("GET", QUOTE_PATH)
    if os.environ.get("BINANCE_API_KEY", "").strip():
        return _pull_local(symbols)
    return _pull_ssh(symbols)


def _pull_ssh(symbols: tuple[str, ...]) -> ScanBatch:
    program = remote_program(symbols)
    try:
        completed = subprocess.run(
            [
                "ssh",
                "-o",
                "BatchMode=yes",
                "-o",
                "ConnectTimeout=8",
                "-T",
                SCAN_HOST,
                "python3",
                "-",
            ],
            input=program.encode("utf-8"),
            check=False,
            capture_output=True,
            timeout=25,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ScanBatch((), None, "ssh")
    if completed.returncode != 0:
        return ScanBatch((), None, "ssh")
    try:
        return decode_scan(completed.stdout.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError, ValueError, ArithmeticError):
        return ScanBatch((), None, "scan")


def _pull_local(symbols: tuple[str, ...]) -> ScanBatch:
    from concurrent.futures import ThreadPoolExecutor
    from urllib.error import HTTPError
    from urllib.request import Request, urlopen

    key = os.environ["BINANCE_API_KEY"].strip()
    clean = _names(symbols)
    weight: int | None = None
    error: str | None = None
    documents: list[tuple[str, bytes]] = []

    def one(symbol: str) -> tuple[str, bytes, str | None, str | None]:
        request = Request(
            f"{QUOTE_URL}?symbol={symbol}",
            headers={"X-MBX-APIKEY": key, "User-Agent": "bstocks-scan"},
        )
        try:
            with urlopen(request, timeout=8) as response:
                used_weight = response.headers.get("X-SAPI-USED-IP-WEIGHT-1M")
                return symbol, response.read(), used_weight, None
        except HTTPError as exc:
            used = exc.headers.get("X-SAPI-USED-IP-WEIGHT-1M") if exc.headers else None
            return symbol, b"", used, "weight" if exc.code == 429 else None
        except Exception:
            return symbol, b"", None, None

    with ThreadPoolExecutor(max_workers=_WORKERS) as pool:
        for symbol, body, used, flag in pool.map(one, clean):
            if used and str(used).isdigit():
                weight = int(used)
            if flag == "weight":
                error = "weight"
            if body.strip():
                documents.append((symbol, body))
    return ScanBatch(_from_bodies(documents), weight, error)


def _from_bodies(documents: list[tuple[str, bytes]]) -> tuple[tuple[str, Decimal, Decimal], ...]:
    quotes: list[tuple[str, Decimal, Decimal]] = []
    for symbol, body in documents:
        try:
            payload = json.loads(body.decode("utf-8"), parse_float=Decimal)
        except (UnicodeError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        parsed = _sides(symbol, payload.get("bidPrice"), payload.get("askPrice"))
        if parsed is not None:
            quotes.append(parsed)
    return tuple(quotes)


def _sides(symbol: object, bid: object, ask: object) -> tuple[str, Decimal, Decimal] | None:
    if not isinstance(symbol, str) or _SYMBOL.fullmatch(symbol) is None:
        return None
    try:
        bid_price = parse_decimal(bid)
        ask_price = parse_decimal(ask)
    except (TypeError, ValueError, ArithmeticError):
        return None
    if bid_price <= 0 or ask_price <= 0:
        return None
    return symbol, bid_price, ask_price


def _names(symbols: tuple[str, ...]) -> list[str]:
    clean: list[str] = []
    for symbol in symbols:
        if _SYMBOL.fullmatch(symbol) is None:
            raise ValueError("symbol")
        clean.append(symbol)
    return clean
