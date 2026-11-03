"""Stand-in strategy process. It records a pid and waits to be killed."""

import os
import sqlite3
import sys
import time


def main(argv: list[str] | None = None) -> None:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        raise SystemExit("usage: strategy_stub DB")
    conn = sqlite3.connect(args[0])
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("CREATE TABLE IF NOT EXISTS strategy_pulse (pid INTEGER NOT NULL)")
    conn.execute("DELETE FROM strategy_pulse")
    conn.execute("INSERT INTO strategy_pulse (pid) VALUES (?)", (os.getpid(),))
    conn.commit()
    conn.close()
    while True:
        time.sleep(10)


if __name__ == "__main__":
    main()
