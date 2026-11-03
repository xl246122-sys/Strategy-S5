"""Process entry. Killing this process must not clear the HWM lock."""

import argparse
import os
import time
from pathlib import Path

from bstocks_v2.portfolio_risk.authority import PortfolioRisk


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="V2.2 portfolio risk")
    parser.add_argument("--root", required=True)
    parser.add_argument("--code-version", default="stage13")
    args = parser.parse_args(argv)
    app = PortfolioRisk.open(Path(args.root), code_version=str(args.code_version))
    try:
        app.note_pid(os.getpid())
        while app.command() != "SHUTDOWN":
            time.sleep(20 / 1000)
    finally:
        app.close()


if __name__ == "__main__":
    main()
