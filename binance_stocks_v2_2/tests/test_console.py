"""Paper console: isolated books, closed live route, fill alerts."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from bstocks_v2.console.board import tier, track, wave_points
from bstocks_v2.console.book import PaperBook
from bstocks_v2.console.calendar import LiveCalendar
from bstocks_v2.console.desks import Desk
from bstocks_v2.console.hub import Hub
from bstocks_v2.console.pool import SYMBOLS as POOL
from bstocks_v2.feature_store.bars import FeatureBar
from bstocks_v2.research_backtest.tape import ClosedBar

_DAY = date(2026, 10, 30)
_NOW = datetime(2026, 10, 30, 14, 0, tzinfo=UTC)


def _bar(
    start: datetime,
    *,
    low: str,
    high: str,
    open_: str = "100",
    close: str = "100.20",
) -> ClosedBar:
    return ClosedBar(
        symbol="SMH",
        start=start,
        end=start + timedelta(minutes=5),
        open=Decimal(open_),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=Decimal("1000"),
        timeframe="5m",
    )


def test_live_route_stays_closed_and_books_are_isolated() -> None:
    first = PaperBook("S1")
    second = PaperBook("S2")
    first.live_route_closed()
    alert = first.submit_entry(
        symbol="SMH",
        limit=Decimal("100"),
        stop=Decimal("99"),
        take_profit=Decimal("102"),
        now_et=_NOW,
        is_half_day=False,
        session_days=[_DAY],
    )
    assert alert is not None
    assert alert["kind"] == "WORKING"
    nxt = _NOW + timedelta(minutes=5)
    fills = first.on_bar(
        _bar(nxt, low="99.50", high="100.50"),
        is_half_day=False,
        session_days=[_DAY],
    )
    assert fills[0]["kind"] == "FILLED"
    assert fills[0]["side"] == "BUY"
    assert second.cash == Decimal("1600")
    assert first.cash < Decimal("1600")
    assert len(first.positions) == 1
    assert second.positions == {}


def test_same_bar_stop_and_target_uses_the_stop() -> None:
    book = PaperBook("S4")
    book.submit_entry(
        symbol="SMH",
        limit=Decimal("100"),
        stop=Decimal("99"),
        take_profit=Decimal("102"),
        now_et=_NOW,
        is_half_day=False,
        session_days=[_DAY],
    )
    nxt = _NOW + timedelta(minutes=5)
    book.on_bar(_bar(nxt, low="99.50", high="100.50"), is_half_day=False, session_days=[_DAY])
    exit_bar = _bar(
        nxt + timedelta(minutes=5),
        open_="99.50",
        low="98",
        high="103",
        close="101",
    )
    alerts = book.on_bar(exit_bar, is_half_day=False, session_days=[_DAY])
    assert alerts[0]["side"] == "SELL"
    assert alerts[0]["text"] == "模拟卖出 BOTH_TOUCHED_STOP"
    assert Decimal(alerts[0]["price"]) == Decimal("99")
    assert book.positions == {}


def test_weekend_entry_is_rejected() -> None:
    book = PaperBook("S3")
    alert = book.submit_entry(
        symbol="SMH",
        limit=Decimal("100"),
        stop=Decimal("99"),
        take_profit=Decimal("102"),
        now_et=datetime(2026, 10, 31, 14, 0, tzinfo=UTC),
        is_half_day=False,
        session_days=[_DAY],
    )
    assert alert is None
    assert book.reason == "NON_RTH"


def test_five_desks_do_not_share_accounts_and_s5_does_not_route() -> None:
    hub = Hub(None)
    assert [desk.strategy_id for desk in hub.desks] == ["S1", "S2", "S3", "S4", "S5"]
    state = hub.state()
    assert state["live_orders"] is False
    server = state["server"]
    assert isinstance(server, dict)
    assert server["worker"] == "stopped"
    assert server["live_orders"] is False
    assert len(state["desks"]) == 5
    assert all(desk["runtime"] == "running" for desk in state["desks"])
    start = _NOW
    bar = FeatureBar(
        symbol="SMH",
        start=start,
        end=start + timedelta(minutes=5),
        open=Decimal("100"),
        high=Decimal("101"),
        low=Decimal("99"),
        close=Decimal("100.40"),
        volume=Decimal("1000"),
        bar_vwap=Decimal("100.20"),
        closed=True,
        feed="binance",
    )
    s5 = Desk("S5", None, LiveCalendar())
    alerts = s5.replay_group([bar])
    assert alerts == []
    assert s5.paper.reason == "NO_PREV_CLOSE"
    names = s5.view()["names"]
    assert isinstance(names, list)
    assert len(names) == 31
    smh = s5.row("SMH")
    assert smh["progress"] == 0
    assert smh["price"] == "100.40"
    assert all(desk.paper.cash == Decimal("4000") for desk in hub.desks)
    s5.close()
    hub.close()


def test_progress_reaches_one_hundred_once_and_s5_never_does() -> None:
    desk = Desk("S1", None, LiveCalendar())
    assert desk.note_gate("NVDA", "BELOW_VWAP") is None
    assert desk.note_gate("XBI", "Z") is None
    first = desk.note_gate("NVDA", "SIGNAL")
    assert first is not None
    assert first["kind"] == "SIGNAL"
    assert first["text"] == "信号达成 100%"
    assert desk.note_gate("NVDA", "SIGNAL") is None
    assert desk.note_gate("NVDA", "Z") is None
    assert desk.note_gate("NVDA", "SIGNAL") is not None
    assert desk.row("NVDA")["progress"] == 100
    assert desk.row("XBI")["progress"] == 75
    names = desk.view()["names"]
    assert names[11]["symbol"] == "NVDA"
    assert names[11]["progress"] == 100
    assert tier("S5", "S5_ROUTE_CLOSED") == 0
    assert tier("S5", "SIGNAL") == 0
    research = Desk("S5", None, LiveCalendar())
    assert research.note_gate("SMH", "SIGNAL") is None
    start = datetime(2026, 10, 30, 13, 30, tzinfo=UTC)
    samples = [(start + timedelta(minutes=5 * i), Decimal(100 + i)) for i in range(6)]
    assert wave_points(samples) == ["100.00", "101.00", "102.00", "103.00", "104.00", "105.00"]
    samples.append((start + timedelta(minutes=30), Decimal("110")))
    assert wave_points(samples)[0] == "100.00"
    samples.append((start + timedelta(minutes=35), Decimal("111")))
    assert wave_points(samples) == ["110.00", "111.00"]
    prints: list[tuple[datetime, Decimal]] = []
    opened = datetime(2026, 11, 2, 20, 30, tzinfo=UTC)
    track(prints, opened, Decimal("96.10"))
    track(prints, opened + timedelta(seconds=1), Decimal("96.40"))
    track(prints, opened + timedelta(seconds=2), Decimal("96.80"))
    assert wave_points(prints) == ["96.10", "96.40", "96.80"]
    desk.on_print("CIBR", Decimal("96.10"), opened)
    desk.on_print("CIBR", Decimal("96.40"), opened + timedelta(seconds=20))
    shown = desk.row("CIBR")
    assert shown["price"] == "96.40"
    assert shown["wave"] == ["96.10", "96.40"]
    assert 0 <= shown["progress"] < 100
    assert len(desk.view()["names"]) == 31
    desk.close()
    research.close()


def test_official_price_and_quote_scan_fill_a_quiet_name() -> None:
    from bstocks_v2.console.scan import decode_scan, remote_program
    from bstocks_v2.market_ingestor.messages import parse_ws_message

    hub = Hub(None)
    hub.accept(
        parse_ws_message(
            '{"stream":"price","data":{"e":"price","rates":['
            '{"s":"EQ_AZO","p":"3158.20","t":1793694000000,"mp":"ON"},'
            '{"s":"AAPL","mp":"ON"}]}}'
        )
    )
    shown = hub.desks[0].row("AZO")
    assert shown["price"] == "3158.20"
    assert shown["source"] == "print"
    blank = hub.desks[0].row("AAL")
    assert blank["price"] == ""
    batch = decode_scan(
        '{"quotes":[{"symbol":"AAL","bid":"12.94","ask":"13.05"},'
        '{"symbol":"AZO","bid":"0","ask":"1"},'
        '{"symbol":"nope","bid":"1","ask":"1"}],"weight":12,"error":null}'
    )
    assert batch.weight == 12
    assert batch.quotes == (("AAL", Decimal("12.94"), Decimal("13.05")),)
    assert hub.apply_quotes(batch.quotes) == 1
    filled = hub.desks[0].row("AAL")
    assert filled["price"] == "13.00"
    assert filled["source"] == "quote"
    program = remote_program(("AAL", "AZO"))
    compile(program, "<official-scan>", "exec")
    assert "/sapi/v1/equity/market/quote" in program
    assert "AAL" in program
    assert "api.binance.com" in program
    hub.close()


def test_quote_checklist_ranks_the_closest_thirty() -> None:
    from bstocks_v2.console.achievement import quote_achievement

    flat = tuple(Decimal(100) for _ in range(8))
    rising = tuple(Decimal(100) + Decimal(index) / 10 for index in range(12))
    dipped = (
        Decimal("100"), Decimal("101"), Decimal("102"),
        Decimal("100.2"), Decimal("99.4"), Decimal("100.8"), Decimal("101.6"),
    )
    parent = tuple(Decimal(50) + Decimal(index) / 20 for index in range(12))
    faded = tuple(Decimal(100) - Decimal("0.15") * index for index in range(8))
    flat_progress = quote_achievement("S2", flat)[2]
    rise_progress = quote_achievement("S2", rising)[2]
    dip_progress = quote_achievement("S2", dipped)[2]
    assert flat_progress < rise_progress < dip_progress
    assert dip_progress < 100
    paths = tuple(
        tuple(Decimal(100) + Decimal(index) * Decimal(step) / 100 for index in range(10))
        for step in ("0.05", "0.15", "0.40", "0.80", "1.20", "2.00")
    )
    assert len({quote_achievement("S2", path)[2] for path in paths}) >= 4
    s5_flat = quote_achievement("S5", flat, parent, has_parent=True)[2]
    s5_fade = quote_achievement("S5", faded, parent, has_parent=True)[2]
    assert s5_fade > s5_flat
    assert 0 < s5_fade < 100
    window = quote_achievement("S3", rising, session="OVERNIGHT")
    opened = quote_achievement("S3", rising, session="RTH")
    assert window[0] == "WINDOW_OPEN"
    assert opened[2] > window[2]
    assert opened[2] < 100
    desk = Desk("S5", None, LiveCalendar())
    assert desk.note_gate("NVDA", "S5_ROUTE_CLOSED", locked=True) is None
    when = datetime(2026, 11, 3, 20, 0, tzinfo=UTC)
    for index, price in enumerate(faded):
        desk.on_print("NVDA", price, when + timedelta(seconds=20 * index))
        desk.on_print("XLK", parent[index], when + timedelta(seconds=20 * index))
    shown = desk.row("NVDA")
    assert shown["progress"] > 0
    assert desk.note_gate("NVDA", "S5_ROUTE_CLOSED") is None
    assert desk.row("NVDA")["progress"] == shown["progress"]
    assert len({item["progress"] for item in desk.view()["names"]}) > 1
    desk.close()


def test_each_board_leads_with_the_eleven_sector_etfs(tmp_path) -> None:
    from bstocks_v2.console.pool import POOL_PATH, SECTOR_ETFS, SECTOR_LABEL, load

    desk = Desk("S2", None, LiveCalendar())
    desk._score["XLE"] = 80
    desk._score["XLK"] = 40
    desk._score["NVDA"] = 90
    desk._score["AAPL"] = 70
    names = desk.view()["names"]
    head = names[:11]
    assert {row["symbol"] for row in head} == set(SECTOR_ETFS)
    assert [row["industry"] for row in head] == [SECTOR_LABEL[str(row["symbol"])] for row in head]
    head_progress = [row["progress"] for row in head]
    assert head_progress == sorted(head_progress, reverse=True)
    assert head[0]["symbol"] == "XLE"
    assert len(names) == 31
    assert all(row["kind"] == "个股" for row in names[11:])
    stock_progress = [row["progress"] for row in names[11:]]
    assert stock_progress == sorted(stock_progress, reverse=True)
    assert names[11]["symbol"] == "NVDA"
    assert names[12]["symbol"] == "AAPL"
    assert all(row["symbol"] not in SECTOR_ETFS for row in names[11:])
    desk.close()
    lines = [
        line for line in POOL_PATH.read_text(encoding="utf-8-sig").splitlines()
        if not line.startswith("XLRE,")
    ]
    missing = tmp_path / "pool.csv"
    missing.write_text("\n".join(lines) + "\n", encoding="utf-8")
    symbols, _industry, parent, kind = load(missing)
    assert "XLRE" in symbols
    assert kind["XLRE"] == "ETF"
    assert parent["XLRE"] == "XLRE"


def test_candidate_pool_is_subscribed_in_shards() -> None:
    from bstocks_v2.console.feed import stream_shards
    from bstocks_v2.console.pool import SECTOR_ETFS, parent_of

    assert len(POOL) == 479
    assert all(symbol in POOL for symbol in SECTOR_ETFS)
    assert parent_of("NVDA") == "XLK"
    assert parent_of("JPM") == "XLF"
    shards = stream_shards()
    assert all(len(shard) <= 100 for shard in shards)
    quotes = [item for shard in shards for item in shard if item.endswith("@quote")]
    assert len(quotes) == 479
    assert "NVDA@quote" in quotes
    assert "AAPL@kline_5m" in [item for shard in shards for item in shard]


def test_live_sessions_are_stored_and_s5_paper_can_plan(tmp_path) -> None:
    from bstocks_v2.console.archive import SessionArchive
    from bstocks_v2.console.paper_s5 import s5_paper_plan

    archive = SessionArchive(tmp_path)
    pre = datetime(2026, 11, 3, 9, 1, tzinfo=UTC)
    regular = datetime(2026, 11, 3, 14, 31, tzinfo=UTC)
    post = datetime(2026, 11, 4, 0, 1, tzinfo=UTC)
    night = datetime(2026, 11, 3, 8, 30, tzinfo=UTC)
    archive.add_trade("AAPL", Decimal("100"), pre)
    archive.add_trade("AAPL", Decimal("101"), pre + timedelta(minutes=1))
    archive.add_trade("AAPL", Decimal("110"), regular)
    archive.add_trade("AAPL", Decimal("90"), post)
    archive.add_trade("AAPL", Decimal("80"), night)
    archive.add_trade("AAPL", Decimal("102"), pre + timedelta(minutes=5))
    archive.add_trade("NVDA", Decimal("100"), regular)
    archive.add_trade("NVDA", Decimal("100"), regular + timedelta(minutes=5))
    archive.add_trade("AAPL", Decimal("111"), regular + timedelta(minutes=5))
    archive.add_trade("AAPL", Decimal("91"), post + timedelta(minutes=5))
    stored = "\n".join(path.read_text(encoding="utf-8") for path in tmp_path.rglob("*.csv"))
    assert (tmp_path / "2026-11-03" / "盘前" / "AAPL.csv").exists()
    assert (tmp_path / "2026-11-03" / "盘中" / "AAPL.csv").exists()
    assert (tmp_path / "2026-11-03" / "盘后" / "AAPL.csv").exists()
    assert "80" not in stored
    assert s5_paper_plan(archive, "NVDA", Decimal("98"), date(2026, 11, 4))[0] == "Z"
    reason, prices = s5_paper_plan(archive, "NVDA", Decimal("96"), date(2026, 11, 4))
    assert reason == "SIGNAL"
    assert prices is not None
    limit, stop, take_profit = prices
    assert stop < limit < take_profit
