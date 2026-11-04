"""Paper console entry. Orders stay on this machine."""

import argparse
from pathlib import Path

from bstocks_v2.console.archive import ARCHIVE_ROOT, SessionArchive
from bstocks_v2.console.feed import MarketFeed
from bstocks_v2.console.history import warm_hub
from bstocks_v2.console.hub import Hub
from bstocks_v2.console.server import serve


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="V2.2 paper console")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--root", type=Path, default=Path(r"D:\Strategy\data_v2_2\console"))
    parser.add_argument("--archive", type=Path, default=ARCHIVE_ROOT)
    parser.add_argument("--no-feed", action="store_true")
    args = parser.parse_args(argv)
    if args.host not in {"127.0.0.1", "0.0.0.0"}:
        raise SystemExit("console binds to localhost or the LAN only")
    hub = Hub(args.root, SessionArchive(args.archive))
    hub.set_bind(args.host, args.port)
    warm_hub(hub)
    feed = None if args.no_feed else MarketFeed(hub)
    if feed is not None:
        feed.start()
    try:
        serve(hub, args.host, args.port)
    finally:
        if feed is not None:
            feed.stop()
        hub.close()


if __name__ == "__main__":
    main()
