"""Read-only funding and exchangeInfo summary. Prints no credentials."""

import json
from pathlib import Path

from bstocks_v2.config.credentials import binance_from_env
from bstocks_v2.market_ingestor.equity_rest import EquityReadClient
from bstocks_v2.market_ingestor.http_util import UrllibTransport

OUT = Path("/tmp/stage3_exchange_slim.json")


def main() -> None:
    client = EquityReadClient(binance_from_env(), UrllibTransport(timeout=60))
    funding = client.funding_assets()
    print(json.dumps({"funding": _funding(funding.payload)}, sort_keys=True))
    info = client.exchange_info()
    payload = info.payload if isinstance(info.payload, dict) else {}
    rows = payload.get("symbols")
    slim: list[dict[str, object]] = []
    keys: list[str] = []
    if isinstance(rows, list):
        for row in rows:
            if not isinstance(row, dict):
                continue
            if not keys:
                keys = sorted(str(key) for key in row)
            symbol = row.get("symbol")
            if not isinstance(symbol, str):
                continue
            slim.append(
                {
                    "symbol": symbol,
                    "tradability": row.get("tradability"),
                    "listingTime": row.get("listingTime"),
                    "delistingTime": row.get("delistingTime"),
                }
            )
    OUT.write_text(json.dumps({"keys": keys, "symbols": slim}), encoding="utf-8")
    print(json.dumps({"exchange_symbols": len(slim), "keys": keys, "path": str(OUT)}))


def _funding(payload: object) -> dict[str, object]:
    assets: list[str] = []
    usdc: dict[str, object] = {"present": False}
    if isinstance(payload, list):
        for item in payload:
            if not isinstance(item, dict):
                continue
            asset = item.get("asset")
            if not isinstance(asset, str):
                continue
            assets.append(asset)
            if asset.upper() == "USDC":
                usdc = {
                    "present": True,
                    "free": item.get("free"),
                    "locked": item.get("locked"),
                    "freeze": item.get("freeze"),
                    "withdrawing": item.get("withdrawing"),
                }
    return {"assets": assets, "usdc": usdc}


if __name__ == "__main__":
    main()
