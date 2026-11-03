"""Process entry for the independent guardian. The strategy engine is not imported."""

import argparse
import time

from bstocks_v2.guardian.machine import Guardian


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="V2.2 guardian")
    parser.add_argument("--db", required=True)
    args = parser.parse_args(argv)
    app = Guardian.open(args.db)
    try:
        app.mark_pid()
        app.recover()
        while True:
            if app.drain_commands():
                break
            app.touch_heartbeat()
            time.sleep(20 / 1000)
    finally:
        app.close()


if __name__ == "__main__":
    main()
