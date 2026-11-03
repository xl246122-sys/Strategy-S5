"""Live 5-minute bars for research. This store never places an order."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from bstocks_v2.console.calendar import LiveCalendar
from bstocks_v2.research_data.calendar import NEW_YORK, OPEN

ARCHIVE_ROOT = Path(r"D:\Strategy\K-Line-DataBase")
_FOLDERS = {"盘前": "盘前", "盘中": "盘中", "盘后": "盘后"}
_HEADER = "symbol,session,start,end,open,high,low,close,prints,source\n"


@dataclass
class _Bar:
    symbol: str
    session: str
    start: datetime
    end: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    prints: int
    source: str


class SessionArchive:
    """One folder per New York session. Overnight prints are left out."""

    def __init__(self, root: Path, calendar: LiveCalendar | None = None) -> None:
        self.root = root
        self.calendar = calendar or LiveCalendar()
        self._open: dict[tuple[str, datetime], _Bar] = {}
        self._seen: dict[Path, set[str]] = {}
        self._compact_existing()

    def add_trade(self, symbol: str, price: Decimal, when: datetime) -> None:
        if price <= 0:
            return
        self._roll(when)
        session = _session(self.calendar, when)
        if session is None:
            return
        start = _bucket(when)
        key = (symbol, start)
        bar = self._open.get(key)
        if bar is None:
            self._open[key] = _Bar(
                symbol,
                session,
                start,
                start + timedelta(minutes=5),
                price,
                price,
                price,
                price,
                1,
                "quote",
            )
            return
        bar.high = max(bar.high, price)
        bar.low = min(bar.low, price)
        bar.close = price
        bar.prints += 1

    def add_kline(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
        open_: Decimal,
        high: Decimal,
        low: Decimal,
        close: Decimal,
        volume: Decimal,
    ) -> None:
        start = _bucket(start)
        end = start + timedelta(minutes=5)
        session = _session(self.calendar, start)
        if session is None:
            return
        self._open.pop((symbol, start), None)
        prints = int(volume) if volume == volume.to_integral_value() and volume >= 0 else 0
        self._write(
            _Bar(symbol, session, start, end, open_, high, low, close, prints, "kline")
        )

    def prior_rth_close(self, symbol: str, day) -> Decimal | None:
        chosen: tuple[str, Decimal] | None = None
        if not self.root.exists():
            return None
        for folder in self.root.iterdir():
            if not folder.is_dir() or folder.name >= day.isoformat():
                continue
            path = folder / "盘中" / f"{symbol}.csv"
            close = _last_close(path)
            if close is None:
                continue
            if chosen is None or folder.name > chosen[0]:
                chosen = (folder.name, close)
        return None if chosen is None else chosen[1]

    def _roll(self, when: datetime) -> None:
        finished = [key for key, bar in self._open.items() if bar.end <= when]
        for key in finished:
            self._write(self._open.pop(key))

    def _write(self, bar: _Bar) -> None:
        day = bar.start.astimezone(NEW_YORK).date().isoformat()
        path = self.root / day / bar.session / f"{bar.symbol}.csv"
        path.parent.mkdir(parents=True, exist_ok=True)
        rows = _rows(path)
        stamp = bar.start.isoformat()
        current = rows.get(stamp)
        if current is not None and not (bar.source == "kline" and current[9] != "kline"):
            self._seen[path] = set(rows)
            return
        rows[stamp] = _fields(bar)
        _dump(path, rows)
        self._seen[path] = set(rows)

    def _compact_existing(self) -> None:
        if not self.root.exists():
            return
        for path in self.root.rglob("*.csv"):
            rows = _rows(path)
            if rows:
                _dump(path, rows)


def _session(calendar: LiveCalendar, when: datetime) -> str | None:
    local = when.astimezone(NEW_YORK)
    label = calendar.classify(when, when + timedelta(seconds=1))
    if label == "RTH":
        return "盘中"
    if label != "EXTENDED":
        return None
    close = calendar.close_on(local.date())
    if close is None:
        return None
    clock = local.timetz().replace(tzinfo=None)
    if clock < OPEN:
        return "盘前"
    if clock >= close:
        return "盘后"
    return None


def _bucket(when: datetime) -> datetime:
    local = when.astimezone(NEW_YORK).replace(second=0, microsecond=0)
    return local.replace(minute=(local.minute // 5) * 5)


def _rows(path: Path) -> dict[str, list[str]]:
    rows: dict[str, list[str]] = {}
    if not path.exists():
        return rows
    for line in path.read_text(encoding="utf-8").splitlines()[1:]:
        parts = line.split(",")
        if len(parts) < 10:
            continue
        try:
            start = _bucket(datetime.fromisoformat(parts[2]))
        except ValueError:
            continue
        parts[2] = start.isoformat()
        parts[3] = (start + timedelta(minutes=5)).isoformat()
        current = rows.get(parts[2])
        if current is None or (parts[9] == "kline" and current[9] != "kline"):
            rows[parts[2]] = parts
    return rows


def _fields(bar: _Bar) -> list[str]:
    return [
        bar.symbol,
        bar.session,
        bar.start.isoformat(),
        bar.end.isoformat(),
        _text(bar.open),
        _text(bar.high),
        _text(bar.low),
        _text(bar.close),
        str(bar.prints),
        bar.source,
    ]


def _dump(path: Path, rows: dict[str, list[str]]) -> None:
    lines = [_HEADER]
    for stamp in sorted(rows):
        lines.append(",".join(rows[stamp]) + "\n")
    path.write_text("".join(lines), encoding="utf-8")


def _last_close(path: Path) -> Decimal | None:
    if not path.exists():
        return None
    last = ""
    for line in path.read_text(encoding="utf-8").splitlines()[1:]:
        if line.strip():
            last = line
    parts = last.split(",")
    if len(parts) < 8:
        return None
    try:
        return Decimal(parts[7])
    except ArithmeticError:
        return None


def _text(value: Decimal) -> str:
    return format(value, "f")
