"""Stage 2: read-only equity access, shard caps, staleness, and WAL recovery."""

import subprocess
import sys
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from bstocks_v2.config.credentials import ApiPair, read_alpaca_file, read_binance_file
from bstocks_v2.contracts.errors import LiveTradingDisabledError, OrderRejected
from bstocks_v2.market_ingestor.alpaca_backfill import MinuteBar, aggregate_minutes
from bstocks_v2.market_ingestor.equity_rest import ApiError, EquityReadClient, payload_is_empty
from bstocks_v2.market_ingestor.listen_key import ListenKeyKeeper
from bstocks_v2.market_ingestor.messages import parse_ws_message
from bstocks_v2.market_ingestor.shards import (
    MAX_STREAMS_PER_SOCKET,
    PoolLimitError,
    combined_url,
    l3_commands,
    plan_l2,
    select_l2_symbols,
)
from bstocks_v2.market_ingestor.staleness import ConnectionWatch, assess
from bstocks_v2.market_ingestor.wal import (
    BAR_MS,
    NEW_YORK,
    IntradayBarStore,
    StoredBar,
    recover_session,
    wilder_rsi,
)
from bstocks_v2.market_ingestor.wal_restart import BAR_COUNT, sample_session
from bstocks_v2.reconciler.ledger import (
    FundingBalance,
    LedgerFill,
    OpenOrderView,
    PositionLedgerReconciler,
)


class Scripted:
    def __init__(self, responses: list[tuple[int, bytes]]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, str]] = []

    def exchange(self, method: str, url: str, headers: dict[str, str]) -> tuple[int, bytes, float]:
        self.calls.append((method, url))
        status, body = self.responses.pop(0)
        return status, body, 4.0


def _client(transport: Scripted) -> EquityReadClient:
    return EquityReadClient(ApiPair("key", "secret"), transport)


def test_empty_200_does_not_parse_json() -> None:
    transport = Scripted([(200, b"")])
    result = _client(transport).quote("HALTED")
    assert result.status == 200
    assert payload_is_empty(result)
    assert result.raw_length == 0


def test_order_history_rejects_non_terminal_status_before_the_network() -> None:
    transport = Scripted([(200, b"{}")])
    client = _client(transport)
    with pytest.raises(OrderRejected):
        client.order_history("NEW")
    with pytest.raises(OrderRejected):
        client.order_history("ACCEPTED")
    assert transport.calls == []
    result = client.order_history("FILLED", start_time_ms=10, end_time_ms=20)
    assert result.status == 200
    assert "orderStatus=FILLED" in transport.calls[0][1]
    assert "startTime=10" in transport.calls[0][1]
    assert "endTime=20" in transport.calls[0][1]
    assert "signature=" in transport.calls[0][1]


def test_place_and_cancel_stay_blocked() -> None:
    client = _client(Scripted([]))
    with pytest.raises(LiveTradingDisabledError):
        client._signed("POST", "/sapi/v1/equity/order/place")
    with pytest.raises(LiveTradingDisabledError):
        client._signed("DELETE", "/sapi/v1/equity/order/cancel")


def test_public_get_retries_with_signature_when_the_exchange_asks() -> None:
    transport = Scripted(
        [
            (400, b'{"code":-1022,"msg":"Signature for this request is not valid."}'),
            (200, b'{"symbols":[]}'),
        ]
    )
    result = _client(transport).exchange_info()
    assert result.status == 200
    assert transport.calls[0][0] == "GET"
    assert "signature=" not in transport.calls[0][1]
    assert "signature=" in transport.calls[1][1]


def test_listen_key_renews_every_25_minutes_and_rebuilds_after_1125() -> None:
    transport = Scripted([(200, b'{"listenKey":"first"}')])
    keeper = ListenKeyKeeper(_client(transport))
    assert keeper.current(0) == "first"
    assert keeper.current(24 * 60) == "first"
    assert len(transport.calls) == 1
    keeper.observe_error(ApiError(-1125, "expired"))
    transport.responses.append((200, b'{"listenKey":"third"}'))
    assert keeper.current(24 * 60) == "third"
    assert keeper.rebuilds == 1
    transport.responses.append((200, b'{"listenKey":"second"}'))
    assert keeper.current(24 * 60 + 25 * 60) == "second"
    assert "abc123key" not in repr(ApiPair("abc123key", "xyz789secret"))


def test_kline_null_vwap_and_quote_null_time() -> None:
    typical = parse_ws_message(
        '{"e":"kline","E":1,"k":{"t":10,"ct":20,"s":"AAPL","x":true,'
        '"o":"10","h":"12","l":"8","c":"11","v":"0","vw":null}}'
    )
    assert typical.vwap == (Decimal(12) + Decimal(8) + Decimal(11)) / Decimal(3)
    assert typical.vwap_source == "typical"
    turnover = parse_ws_message(
        '{"stream":"AAPL@kline_5m","data":{"e":"kline","E":1,"k":{"t":10,"ct":20,'
        '"s":"AAPL","x":false,"o":"10","h":"12","l":"8","c":"11","v":"4","q":"40","vw":null}}}'
    )
    assert turnover.vwap == Decimal(10)
    assert turnover.vwap_source == "turnover"
    assert turnover.closed is False
    quote = parse_ws_message(
        '{"stream":"AAPL@quote","data":{"e":"quote","s":"EQ_AAPL","bp":"1.01","ap":"1.02","T":null},"E":99}'
    )
    assert quote.symbol == "AAPL"
    assert quote.quote_time_ms == 99
    assert quote.quote_time_source == "event_time"
    price = parse_ws_message(
        '{"stream":"price","data":{"e":"price","rates":['
        '{"s":"AAPL","mp":"CLOSED"},{"s":"EQ_MSFT","mp":"CLOSED"}]}}'
    )
    assert price.market_phase == "CLOSED"
    assert price.phases == (("AAPL", "CLOSED"), ("MSFT", "CLOSED"))


def test_state_streams_stay_fresh_through_a_silent_session() -> None:
    silent = 24 * 60 * 60 * 1000
    for stream in ("calendar", "AAPL@tradability", "AAPL@tradingStatus"):
        health = assess(
            stream,
            last_event_ms=None,
            now_ms=silent,
            connection_alive=True,
            has_initial_snapshot=True,
        )
        assert health.stale is False
    stale_price = assess(
        "price",
        last_event_ms=0,
        now_ms=10_001,
        connection_alive=True,
        has_initial_snapshot=True,
    )
    assert stale_price.stale is True
    fresh_kline = assess(
        "AAPL@kline_5m",
        last_event_ms=0,
        now_ms=60_000,
        connection_alive=True,
        has_initial_snapshot=True,
    )
    assert fresh_kline.stale is False
    watch = ConnectionWatch()
    assert watch.alive(1_000) is False
    watch.note_pong(1_000)
    assert watch.alive(10_000) is True
    assert watch.alive(30_000) is False


def test_l2_300_symbols_use_eight_sockets_of_at_most_120_streams() -> None:
    symbols = [f"S{index:03d}" for index in range(300)]
    shards = plan_l2(symbols)
    assert len(shards) == 8
    assert all(len(shard) <= MAX_STREAMS_PER_SOCKET for shard in shards)
    assert sum(len(shard) for shard in shards) == 900
    assert "wss://nbstream.binance.com/equity/stream?streams=" in combined_url(shards[0])
    with pytest.raises(PoolLimitError):
        plan_l2(symbols + ["EXTRA"])
    with pytest.raises(PoolLimitError):
        l3_commands(set(), {f"Q{index}" for index in range(21)}, 1)
    commands = l3_commands(set(), {"MSFT", "AAPL"}, 7)
    assert commands[0]["method"] == "SUBSCRIBE"
    assert commands[0]["params"] == ["AAPL@quote", "MSFT@quote"]
    undo = l3_commands({"AAPL", "MSFT"}, {"AAPL"}, 9)
    assert undo[0]["method"] == "UNSUBSCRIBE"
    assert undo[0]["params"] == ["MSFT@quote"]


def test_l2_selector_caps_at_300_and_prefers_liquid_names() -> None:
    rows = [{"symbol": f"S{index:04d}", "tradability": "BUY_SELL"} for index in range(400)]
    rows.append({"symbol": "AAPL", "tradability": "BUY_SELL"})
    rows.append({"symbol": "HALT", "tradability": "NONE"})
    chosen = select_l2_symbols({"symbols": rows})
    assert len(chosen) == 300
    assert chosen[0] == "AAPL"
    assert "HALT" not in chosen


def test_credentials_read_the_next_line_and_repr_is_redacted(tmp_path: Path) -> None:
    binance = tmp_path / "binance.txt"
    binance.write_text("API密钥\nabc123key\n秘钥\nxyz789secret\n", encoding="utf-8")
    pair = read_binance_file(binance)
    assert pair.key == "abc123key"
    assert pair.secret == "xyz789secret"
    assert "abc123key" not in repr(pair)
    alpaca = tmp_path / "alpaca.txt"
    alpaca.write_text("API Key ID: PAPERKEY\nSecret Key: PAPERSECRET\n", encoding="utf-8")
    loaded = read_alpaca_file(alpaca)
    assert loaded.key == "PAPERKEY"
    assert loaded.secret == "PAPERSECRET"


def test_wal_gap_clears_vwap_until_backfill_and_rsi_is_50(tmp_path: Path) -> None:
    session = datetime(2026, 11, 2, 9, 30, tzinfo=NEW_YORK)
    closes = [Decimal(value) for value in (10, 11, 12, 11, 10, 9, 10)]
    bars = [_bar(session, index, closes[index]) for index in range(7)]
    store = IntradayBarStore(str(tmp_path / "bars.sqlite"))
    for bar in bars:
        if bar.open_time_ms != bars[3].open_time_ms:
            store.upsert_bar(bar)
    asof = session + timedelta(minutes=35)
    broken = recover_session(store, "AAPL", session, asof)
    assert broken.vwap_intact is False
    assert broken.rth_vwap is None
    assert bars[3].open_time_ms in broken.missing_open_times_ms
    repaired = recover_session(store, "AAPL", session, asof, backfill=[bars[3]])
    assert repaired.vwap_intact is True
    assert repaired.rth_vwap == Decimal(10)
    assert repaired.rsi6 == Decimal(50)
    assert repaired.orb_high_5m == Decimal(12)
    assert repaired.orb_low_5m == Decimal(9)
    assert wilder_rsi(closes) == Decimal(50)
    store.close()


def test_open_kline_is_not_stored_and_kill_restarts_with_the_same_vwap(tmp_path: Path) -> None:
    event = parse_ws_message(
        '{"e":"kline","E":1,"k":{"t":10,"ct":20,"s":"AAPL","x":false,'
        '"o":"10","h":"12","l":"8","c":"11","v":"4","vw":"10"}}'
    )
    store = IntradayBarStore(str(tmp_path / "ignore.sqlite"))
    store.upsert_event(event)
    assert store.load_session("AAPL", "1970-01-01") == []
    store.close()

    db = tmp_path / "restart.sqlite"
    writer = subprocess.Popen(
        [sys.executable, "-m", "bstocks_v2.market_ingestor.wal_restart", "write", str(db)],
        stdout=subprocess.PIPE,
        text=True,
    )
    assert writer.stdout is not None
    assert writer.stdout.readline().strip() == "READY"
    writer.kill()
    writer.wait(timeout=10)
    session, asof, bars, expected = sample_session()
    fresh = IntradayBarStore(str(db))
    started = __import__("time").perf_counter()
    state = recover_session(fresh, "AAPL", session, asof)
    elapsed_ms = (__import__("time").perf_counter() - started) * 1000
    fresh.close()
    assert state.vwap_intact is True
    assert state.rth_vwap == expected
    assert len(state.bars) == BAR_COUNT == len(bars)
    assert elapsed_ms < 100
    completed = subprocess.run(
        [sys.executable, "-m", "bstocks_v2.market_ingestor.wal_restart", "read", str(db)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "match=True" in completed.stdout


def test_three_way_ledger_halts_when_the_wallet_disagrees(tmp_path: Path) -> None:
    ledger = PositionLedgerReconciler(str(tmp_path / "ledger.sqlite"))
    ledger.record_fill("a" * 32, "1", LedgerFill("AAPL", "BUY", Decimal(2)), "FILLED")
    trades = [LedgerFill("EQ_AAPL", "BUY", Decimal(2))]
    funding = [FundingBalance("USDC", Decimal(100)), FundingBalance("EQ_AAPL", Decimal(2))]
    open_orders = [OpenOrderView("MSFT", "BUY", "99", "NEW")]
    matched = ledger.reconcile(trades, open_orders, funding)
    assert matched.matched is True
    assert matched.usdc == Decimal(100)
    assert matched.open_order_ids == ["99"]
    assert matched.halt_new_entries is False
    broken = ledger.reconcile(trades, [], [FundingBalance("EQ_AAPL", Decimal(1))])
    assert broken.matched is False
    assert broken.quantity_breaks == ["AAPL"]
    assert broken.halt_new_entries is True
    ledger.close()


def test_alpaca_minutes_fold_into_one_five_minute_bar() -> None:
    base = 1_700_000_000_000
    minutes = [
        MinuteBar(
            "AAPL",
            base,
            Decimal("10"),
            Decimal("11"),
            Decimal("9"),
            Decimal("10.5"),
            Decimal(2),
            Decimal("10"),
        ),
        MinuteBar(
            "AAPL",
            base + 60_000,
            Decimal("10.5"),
            Decimal("12"),
            Decimal("10"),
            Decimal("11"),
            Decimal(2),
            Decimal("11"),
        ),
    ]
    bar = aggregate_minutes("AAPL", "2026-11-02", minutes, base)
    assert bar is not None
    assert bar.high == Decimal(12)
    assert bar.low == Decimal(9)
    assert bar.volume == Decimal(4)
    assert bar.vwap == Decimal("10.5")
    assert bar.vwap_source == "alpaca_sip"


def _bar(session: datetime, index: int, close: Decimal) -> StoredBar:
    open_ms = int(session.timestamp() * 1000) + index * BAR_MS
    return StoredBar(
        symbol="AAPL",
        session_date=session.date().isoformat(),
        open_time_ms=open_ms,
        close_time_ms=open_ms + BAR_MS - 1,
        open=Decimal(10),
        high=Decimal(12),
        low=Decimal(9),
        close=close,
        volume=Decimal(100),
        vwap=Decimal(10),
        vwap_source="wire",
    )
