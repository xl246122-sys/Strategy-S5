"""Stage 12: fifteen fault injections for the independent guardian."""

import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest

from bstocks_v2.constitution.guard import (
    CLIENT_ORDER_ID,
    NEW_YORK,
    flatten_deadline,
    live_trade_enabled,
)
from bstocks_v2.contracts.errors import LiveTradingDisabledError, OrderRejected
from bstocks_v2.guardian.ids import client_order_id
from bstocks_v2.guardian.ladder import marketable_price, split_shares
from bstocks_v2.guardian.machine import BuyPlan, Guardian
from bstocks_v2.guardian.spec import (
    CANCEL_CONFIRM_DELAY_MS,
    CODE_ALREADY_FILLED,
    CODE_CANCEL_PENDING,
    CODE_EXTENDED_MARKET,
    CODE_MARKET_RESTRICTED,
    CODE_NO_DEPTH,
    CODE_TIMEOUT,
)
from bstocks_v2.reconciler.ledger import FundingBalance, LedgerFill

ROOT = Path(__file__).resolve().parents[1]
NY = NEW_YORK


def _plan(symbol: str, qty: str) -> BuyPlan:
    return BuyPlan(
        symbol=symbol,
        quantity=Decimal(qty),
        limit_price=Decimal("100"),
        stop_price=Decimal("90"),
        take_profit_price=Decimal("120"),
    )


def _open(path: Path) -> Guardian:
    return Guardian.open(path / "guardian.sqlite")


def _protect(guardian: Guardian, symbol: str, qty: str) -> None:
    guardian.adopt_buy(_plan(symbol, qty))
    guardian.on_buy_report(symbol, status="FILLED", filled_qty=Decimal(qty))


def _sells(guardian: Guardian, symbol: str | None = None) -> list[object]:
    rows = [
        row
        for row in guardian.broker.place_log()
        if row.side == "SELL" and (symbol is None or row.symbol == symbol)
    ]
    return rows


def _clock(
    guardian: Guardian,
    moment: datetime,
    *,
    half: bool,
) -> None:
    guardian.on_clock(
        moment,
        is_half_day=half,
        bid=Decimal("100"),
        trading_status="TRADING",
        multiplier_down=Decimal("1"),
        multiplier_up=Decimal("500"),
    )


def _flag(path: Path, key: str) -> str | None:
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA busy_timeout=5000")
    try:
        row = conn.execute("SELECT value FROM guardian_flag WHERE key = ?", (key,)).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    return str(row[0])


def _state(path: Path, symbol: str) -> str:
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA busy_timeout=5000")
    try:
        row = conn.execute(
            "SELECT state FROM guardian_position WHERE symbol = ?",
            (symbol,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        raise AssertionError(symbol)
    return str(row[0])


def _place_count(path: Path) -> int:
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA busy_timeout=5000")
    try:
        row = conn.execute("SELECT COUNT(*) FROM broker_place_log").fetchone()
    finally:
        conn.close()
    if row is None:
        return 0
    return int(row[0])


def _command(path: Path, name: str, payload: str) -> None:
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute(
        "INSERT INTO guardian_command (name, payload, done) VALUES (?, ?, 0)",
        (name, payload),
    )
    conn.commit()
    conn.close()


def _wait_flag(path: Path, key: str, expected: str | None = None) -> str:
    deadline = time.time() + 15
    while time.time() < deadline:
        value = _flag(path, key)
        if value is not None and (expected is None or value == expected):
            return value
        time.sleep(50 / 1000)
    raise AssertionError(key)


def _spawn(tmp_path: Path, name: str, argv: list[str]) -> tuple[subprocess.Popen[str], Path]:
    err_path = tmp_path / f"{name}.err"
    handle = err_path.open("w", encoding="utf-8")
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join([str(ROOT / "src"), env.get("PYTHONPATH", "")])
    proc = subprocess.Popen(
        argv,
        cwd=ROOT,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=handle,
        stderr=handle,
        text=True,
    )
    handle.close()
    return proc, err_path


def test_fault_01_partial_fill_is_what_the_guardian_protects(tmp_path: Path) -> None:
    assert live_trade_enabled() is False
    guardian = _open(tmp_path)
    try:
        guardian.adopt_buy(_plan("AAPL", "10"))
        state = guardian.on_buy_report(
            "AAPL",
            status="PARTIALLY_FILLED",
            filled_qty=Decimal("5"),
            total_cost=Decimal("1856.2"),
            filled_notional=Decimal("927.5"),
        )
        pos = guardian.get("AAPL")
        assert state == "PARTIAL_POSITION"
        assert pos.filled_buy_qty == Decimal("5")
        assert pos.stop_price == Decimal("90") and pos.take_profit_price == Decimal("120")
        assert pos.tp_client_order_id is None
        assert pos.realized_fee is None and pos.fee_source is None
        assert pos.ws_total_cost == Decimal("1856.2")
        assert pos.ws_filled_notional == Decimal("927.5")
        assert guardian.expire_buy("AAPL") == "TP_PENDING"
        sells = _sells(guardian, "AAPL")
        assert len(sells) == 1
        order = sells[0]
        assert order.order_type == "LIMIT"
        assert order.quantity == Decimal("5")
        assert order.price == Decimal("120")
        assert order.trading_session == "RTH"
        assert order.wallet_type == "CARD" and order.tokenize is False
        assert order.quote_asset == "USDC"
        assert CLIENT_ORDER_ID.fullmatch(order.client_order_id)
        detail = guardian.broker.detail(pos.buy_client_order_id)
        assert detail is not None
        assert detail.status == "CANCELED" and detail.filled_qty == Decimal("5")
        assert guardian.get("AAPL").realized_fee is None
    finally:
        guardian.close()


def test_fault_02_accept_status_s_waits_800ms_for_terminal(tmp_path: Path) -> None:
    guardian = _open(tmp_path)
    try:
        _protect(guardian, "AAPL", "10")
        assert guardian.arm_take_profit("AAPL") == "TP_PENDING"
        target = guardian.get("AAPL").tp_client_order_id
        assert target is not None
        guardian.broker.arm_cancel(
            target,
            code=None,
            accept_status="S",
            delay_ms=CANCEL_CONFIRM_DELAY_MS,
            hold=False,
            terminal_status="CANCELED",
            terminal_filled=Decimal(0),
        )
        before = len(_sells(guardian))
        assert guardian.request_exit("AAPL", "STOP") == "CANCEL_PENDING_FOR_SL"
        assert guardian.request_exit("AAPL", "STOP") == "CANCEL_PENDING_FOR_SL"
        assert len(_sells(guardian)) == before
        assert guardian.broker.cancel_count() == 1
        assert guardian.broker.cancel_log()[0][2] == "S"
        assert guardian.broker.frozen_reject_count() == 0
        visible = guardian.broker.visible_ns(target)
        assert visible is not None
        while time.time_ns() + 10_000_000 < visible:
            assert guardian.poll("AAPL") == "CANCEL_PENDING_FOR_SL"
            assert len(_sells(guardian)) == before
            time.sleep(50 / 1000)
        while time.time_ns() < visible:
            time.sleep(1 / 1000)
        assert guardian.poll("AAPL") == "FLAT_RECONCILED"
        fresh = [row for row in _sells(guardian) if row.client_order_id != target]
        assert len(fresh) == 1
        assert fresh[0].order_type == "MARKET"
        assert fresh[0].quantity == Decimal("10")
        assert fresh[0].price is None and fresh[0].trading_session is None
    finally:
        guardian.close()


def test_fault_03_486446_stays_frozen_until_the_cancel_is_terminal(tmp_path: Path) -> None:
    guardian = _open(tmp_path)
    try:
        _protect(guardian, "AAPL", "10")
        guardian.arm_take_profit("AAPL")
        target = guardian.get("AAPL").tp_client_order_id
        assert target is not None
        guardian.broker.arm_cancel(
            target,
            code=CODE_CANCEL_PENDING,
            accept_status="S",
            delay_ms=0,
            hold=True,
            terminal_status="CANCELED",
            terminal_filled=Decimal(0),
        )
        before = len(_sells(guardian))
        assert guardian.request_exit("AAPL", "STOP") == "CANCEL_PENDING_FOR_SL"
        assert guardian.poll("AAPL") == "CANCEL_PENDING_FOR_SL"
        assert len(_sells(guardian)) == before
        assert guardian.broker.frozen_reject_count() == 0
        assert guardian.broker.cancel_log()[-1][1] == CODE_CANCEL_PENDING
        guardian.broker.release(target)
        assert guardian.poll("AAPL") == "FLAT_RECONCILED"
        assert guardian.broker.frozen_reject_count() == 0
        fresh = [row for row in _sells(guardian) if row.client_order_id != target]
        assert len(fresh) == 1 and fresh[0].quantity == Decimal("10")
        assert fresh[0].order_type == "MARKET"
    finally:
        guardian.close()


def test_fault_04_486444_flat_reconciles_without_a_second_sell(tmp_path: Path) -> None:
    guardian = _open(tmp_path)
    try:
        _protect(guardian, "AAPL", "10")
        guardian.arm_take_profit("AAPL")
        target = guardian.get("AAPL").tp_client_order_id
        assert target is not None
        before = len(_sells(guardian))
        guardian.broker.set_fill(
            target,
            status="FILLED",
            filled_qty=Decimal("10"),
            execution_event="ORDER_TERMINAL",
        )
        assert guardian.request_exit("AAPL", "STOP") == "FLAT_RECONCILED"
        assert guardian.get("AAPL").alert == "TP_ALREADY_FILLED"
        assert len(_sells(guardian)) == before
        assert guardian.broker.duplicate_count() == 0
        assert guardian.broker.cancel_log()[-1][1] == CODE_ALREADY_FILLED
    finally:
        guardian.close()


def test_fault_05_partial_target_cancel_sells_only_the_remainder(tmp_path: Path) -> None:
    guardian = _open(tmp_path)
    try:
        _protect(guardian, "AAPL", "10")
        guardian.arm_take_profit("AAPL")
        target = guardian.get("AAPL").tp_client_order_id
        assert target is not None
        guardian.broker.set_fill(target, status="PARTIALLY_FILLED", filled_qty=Decimal("4"))
        assert guardian.request_exit("AAPL", "STOP") == "FLAT_RECONCILED"
        fresh = [row for row in _sells(guardian) if row.client_order_id != target]
        assert len(fresh) == 1
        assert fresh[0].order_type == "MARKET"
        assert fresh[0].quantity == Decimal("6")
    finally:
        guardian.close()


def test_fault_06_no_depth_steps_down_to_a_marketable_limit(tmp_path: Path) -> None:
    guardian = _open(tmp_path)
    try:
        _protect(guardian, "AAPL", "8")
        guardian.set_market(
            "AAPL",
            bid=Decimal("100.01"),
            trading_status="TRADING",
            multiplier_down=Decimal("90"),
            multiplier_up=Decimal("110"),
        )
        assert marketable_price(
            Decimal("100.01"), Decimal("90"), Decimal("110")
        ) == Decimal("99.70")
        assert marketable_price(
            Decimal("100.01"), Decimal("99.75"), Decimal("110")
        ) == Decimal("99.75")
        guardian.broker.queue_place_fault(CODE_NO_DEPTH, landed=False)
        assert guardian.request_exit("AAPL", "STOP") == "FLAT_RECONCILED"
        sells = _sells(guardian, "AAPL")
        assert sells[0].order_type == "MARKET" and sells[0].code == CODE_NO_DEPTH
        assert sells[0].price is None and sells[0].trading_session is None
        assert sells[1].order_type == "LIMIT" and sells[1].trading_session == "RTH"
        assert sells[1].price == Decimal("99.70") and sells[1].quantity == Decimal("8")
        assert sells[1].wallet_type == "CARD" and sells[1].tokenize is False
    finally:
        guardian.close()


def test_fault_07_486413_uses_the_same_marketable_limit(tmp_path: Path) -> None:
    guardian = _open(tmp_path)
    try:
        _protect(guardian, "AAPL", "8")
        guardian.set_market(
            "AAPL",
            bid=Decimal("100.01"),
            trading_status="TRADING",
            multiplier_down=Decimal("99.75"),
            multiplier_up=Decimal("110"),
        )
        guardian.broker.queue_place_fault(CODE_MARKET_RESTRICTED, landed=False)
        assert guardian.request_exit("AAPL", "STOP") == "FLAT_RECONCILED"
        sells = _sells(guardian, "AAPL")
        assert sells[0].code == CODE_MARKET_RESTRICTED and sells[0].order_type == "MARKET"
        assert sells[1].order_type == "LIMIT" and sells[1].trading_session == "RTH"
        assert sells[1].price == Decimal("99.75") and sells[1].quantity == Decimal("8")
    finally:
        guardian.close()


def test_fault_08_486441_sends_whole_shares_extended_and_keeps_the_fraction(
    tmp_path: Path,
) -> None:
    guardian = _open(tmp_path)
    try:
        moment = datetime(2024, 11, 1, 16, 0, 1, tzinfo=NY)
        guardian.set_clock(moment, is_half_day=False)
        _protect(guardian, "AAPL", "10.5")
        _protect(guardian, "IBM", "0.4")
        guardian.broker.queue_place_fault(CODE_EXTENDED_MARKET, landed=False, symbol="AAPL")
        guardian.broker.queue_place_fault(CODE_EXTENDED_MARKET, landed=False, symbol="IBM")
        assert split_shares(Decimal("10.5")) == (Decimal("10"), Decimal("0.5"))
        assert split_shares(Decimal("0.4")) == (Decimal("0"), Decimal("0.4"))
        assert guardian.request_exit("AAPL", "STOP") == "EXIT_REQUIRED"
        assert guardian.get("AAPL").residual_qty == Decimal("0.5")
        assert guardian.get("AAPL").alert == "FRACTIONAL_RESIDUAL"
        apple = _sells(guardian, "AAPL")
        assert apple[0].order_type == "MARKET" and apple[0].code == CODE_EXTENDED_MARKET
        assert apple[0].quantity == Decimal("10.5")
        assert apple[1].order_type == "LIMIT" and apple[1].trading_session == "EXTENDED"
        assert apple[1].quantity == Decimal("10")
        assert guardian.request_exit("IBM", "STOP") == "EXIT_REQUIRED"
        ibm = _sells(guardian, "IBM")
        assert len(ibm) == 1 and ibm[0].code == CODE_EXTENDED_MARKET
        assert guardian.get("IBM").residual_qty == Decimal("0.4")
        assert any(code == "EXIT_REQUIRED" for _, code, _symbol in guardian.alerts())
    finally:
        guardian.close()


def test_fault_09_flattens_at_1555_and_at_1255_on_a_half_day(tmp_path: Path) -> None:
    full = flatten_deadline(datetime(2024, 11, 1, 9, 0, tzinfo=NY), False)
    half = flatten_deadline(datetime(2024, 11, 1, 9, 0, tzinfo=NY), True)
    assert (full.hour, full.minute, full.second) == (15, 55, 0)
    assert (half.hour, half.minute, half.second) == (12, 55, 0)
    guardian = _open(tmp_path)
    try:
        guardian.adopt_buy(_plan("QQQ", "10"))
        _protect(guardian, "AAPL", "4")
        _clock(guardian, datetime(2024, 11, 1, 12, 55, tzinfo=NY), half=False)
        assert guardian.get("QQQ").state == "BUY_PENDING"
        assert guardian.get("AAPL").state == "PROTECTED_POSITION"
        assert guardian.broker.cancel_count() == 0 and _sells(guardian) == []
        _clock(guardian, datetime(2024, 11, 1, 15, 54, 59, tzinfo=NY), half=False)
        assert guardian.get("QQQ").state == "BUY_PENDING"
        assert guardian.broker.cancel_count() == 0
        _clock(guardian, datetime(2024, 11, 1, 15, 55, tzinfo=NY), half=False)
        assert guardian.get("QQQ").state == "FLAT_RECONCILED"
        assert guardian.get("QQQ").alert == "BUY_CANCELED"
        assert guardian.get("AAPL").state == "FLAT_RECONCILED"
        sells = _sells(guardian)
        assert len(sells) == 1 and sells[0].symbol == "AAPL"
        assert sells[0].order_type == "MARKET" and sells[0].quantity == Decimal("4")
    finally:
        guardian.close()
    half_book = Guardian.open(tmp_path / "half.sqlite")
    try:
        half_book.adopt_buy(_plan("QQQ", "10"))
        _protect(half_book, "AAPL", "4")
        _clock(half_book, datetime(2024, 11, 1, 12, 54, 59, tzinfo=NY), half=True)
        assert half_book.get("AAPL").state == "PROTECTED_POSITION"
        assert half_book.broker.cancel_count() == 0
        _clock(half_book, datetime(2024, 11, 1, 12, 55, tzinfo=NY), half=True)
        assert half_book.get("QQQ").state == "FLAT_RECONCILED"
        assert half_book.get("AAPL").state == "FLAT_RECONCILED"
    finally:
        half_book.close()


def test_fault_10_halt_defers_the_exit_until_trading_resumes(tmp_path: Path) -> None:
    guardian = _open(tmp_path)
    try:
        _protect(guardian, "AAPL", "10")
        guardian.arm_take_profit("AAPL")
        target = guardian.get("AAPL").tp_client_order_id
        before = len(_sells(guardian))
        cancels = guardian.broker.cancel_count()
        guardian.set_market(
            "AAPL",
            bid=Decimal("80"),
            trading_status="TRADING_HALTED",
            multiplier_down=Decimal("1"),
            multiplier_up=Decimal("500"),
        )
        assert guardian.request_exit("AAPL", "STOP") == "EXIT_REQUIRED"
        assert guardian.get("AAPL").alert == "TRADING_HALTED"
        assert len(_sells(guardian)) == before
        assert guardian.broker.cancel_count() == cancels
        guardian.set_market(
            "AAPL",
            bid=Decimal("100"),
            trading_status="TRADING",
            multiplier_down=Decimal("1"),
            multiplier_up=Decimal("500"),
        )
        assert guardian.poll("AAPL") == "FLAT_RECONCILED"
        assert guardian.broker.cancel_count() == cancels + 1
        fresh = [row for row in _sells(guardian) if row.client_order_id != target]
        assert len(fresh) == 1 and fresh[0].order_type == "MARKET"
    finally:
        guardian.close()


def test_fault_11_detail_and_three_way_audit_repair_a_missed_fill(tmp_path: Path) -> None:
    guardian = _open(tmp_path)
    try:
        _protect(guardian, "AAPL", "10")
        guardian.arm_take_profit("AAPL")
        target = guardian.get("AAPL").tp_client_order_id
        assert target is not None
        placed = len(guardian.broker.place_log())
        guardian.broker.set_fill(
            target,
            status="FILLED",
            filled_qty=Decimal("10"),
            execution_event="ORDER_TERMINAL",
        )
        guardian.broker.set_snapshot(
            [
                LedgerFill("AAPL", "BUY", Decimal("10")),
                LedgerFill("AAPL", "SELL", Decimal("10")),
            ],
            [FundingBalance("USDC", Decimal("1500"))],
        )
        reads = guardian.broker.detail_reads()
        recovered = guardian.recover()
        assert guardian.get("AAPL").state == "FLAT_RECONCILED"
        assert guardian.halted is False
        assert guardian.last_audit is not None and guardian.last_audit.matched
        assert len(guardian.broker.place_log()) == placed
        assert guardian.broker.detail_reads() > reads
        assert recovered.milliseconds < 100
    finally:
        guardian.close()


def test_fault_12_killing_the_strategy_process_does_not_stop_the_guardian(
    tmp_path: Path,
) -> None:
    db = tmp_path / "guardian.sqlite"
    guardian = Guardian.open(db)
    try:
        _protect(guardian, "AAPL", "6")
        assert guardian.arm_take_profit("AAPL") == "TP_PENDING"
    finally:
        guardian.close()
    guard_proc, guard_err = _spawn(
        tmp_path,
        "guardian",
        [sys.executable, "-m", "bstocks_v2.guardian.worker", "--db", str(db)],
    )
    strategy, _strategy_err = _spawn(
        tmp_path,
        "strategy",
        [sys.executable, "-m", "bstocks_v2.guardian.strategy_stub", str(db)],
    )
    try:
        pid = _wait_flag(db, "pid")
        _wait_flag(db, "heartbeat_ns")
        assert strategy.poll() is None and guard_proc.poll() is None
        strategy.kill()
        strategy.wait(timeout=10)
        assert guard_proc.poll() is None
        assert _flag(db, "pid") == pid
        _command(db, "EXIT", json.dumps({"symbol": "AAPL", "reason": "STOP"}))
        deadline = time.time() + 15
        state = ""
        while time.time() < deadline:
            state = _state(db, "AAPL")
            if state == "FLAT_RECONCILED":
                break
            time.sleep(50 / 1000)
        assert state == "FLAT_RECONCILED", guard_err.read_text(encoding="utf-8")
        assert guard_proc.poll() is None
    finally:
        if strategy.poll() is None:
            strategy.kill()
            strategy.wait(timeout=10)
        if guard_proc.poll() is None:
            _command(db, "SHUTDOWN", "{}")
            guard_proc.wait(timeout=10)


def test_fault_13_a_killed_guardian_reloads_the_book_within_100ms(tmp_path: Path) -> None:
    db = tmp_path / "guardian.sqlite"
    guardian = Guardian.open(db)
    try:
        _protect(guardian, "AAPL", "6")
        guardian.arm_take_profit("AAPL")
        assert guardian.get("AAPL").state == "TP_PENDING"
    finally:
        guardian.close()
    first, first_err = _spawn(
        tmp_path,
        "first",
        [sys.executable, "-m", "bstocks_v2.guardian.worker", "--db", str(db)],
    )
    try:
        assert _wait_flag(db, "recover_generation", "1") == "1"
        assert int(_wait_flag(db, "recover_ms")) < 100
        assert _state(db, "AAPL") == "TP_PENDING"
        placed = _place_count(db)
        first.kill()
        first.wait(timeout=10)
    finally:
        if first.poll() is None:
            first.kill()
            first.wait(timeout=10)
    second, second_err = _spawn(
        tmp_path,
        "second",
        [sys.executable, "-m", "bstocks_v2.guardian.worker", "--db", str(db)],
    )
    try:
        assert _wait_flag(db, "recover_generation", "2") == "2"
        assert int(_flag(db, "recover_ms") or "1000") < 100, second_err.read_text(encoding="utf-8")
        assert _state(db, "AAPL") == "TP_PENDING", first_err.read_text(encoding="utf-8")
        assert _place_count(db) == placed
    finally:
        if second.poll() is None:
            _command(db, "SHUTDOWN", "{}")
            second.wait(timeout=10)


def test_fault_14_timeout_rereads_detail_and_keeps_the_client_id(tmp_path: Path) -> None:
    guardian = _open(tmp_path)
    try:
        _protect(guardian, "AAPL", "10")
        guardian.broker.queue_place_fault(CODE_TIMEOUT, landed=True)
        assert guardian.arm_take_profit("AAPL") == "TP_PENDING"
        log = guardian.broker.place_log()
        assert len(log) == 1
        assert log[0].code == CODE_TIMEOUT
        assert log[0].status_at_place == "SUBMITTING"
        assert log[0].duplicate is False
        assert log[0].client_order_id == client_order_id("AAPL", "tp")
        assert CLIENT_ORDER_ID.fullmatch(log[0].client_order_id)
        guard = guardian.journal.guard(log[0].client_order_id)
        assert guard is not None
        assert guard.status == "DETAIL_CONFIRMED" and guard.place_attempts == 1
        assert guardian.broker.duplicate_count() == 0
        guardian.journal.mark_guard(
            log[0].client_order_id,
            "SUBMITTING",
            attempts=1,
            code=CODE_TIMEOUT,
        )
        guardian.recover()
        assert len(guardian.broker.place_log()) == 1
        again = guardian.journal.guard(log[0].client_order_id)
        assert again is not None and again.status == "DETAIL_CONFIRMED"
        with pytest.raises(LiveTradingDisabledError):
            guardian.live_place()
        with pytest.raises(LiveTradingDisabledError):
            guardian.live_cancel()
    finally:
        guardian.close()


def test_fault_15_a_quantity_break_halts_new_buys(tmp_path: Path) -> None:
    guardian = _open(tmp_path)
    path = Path(guardian.path)
    try:
        _protect(guardian, "AAPL", "10")
        guardian.broker.set_snapshot(
            [LedgerFill("AAPL", "BUY", Decimal("10"))],
            [
                FundingBalance("EQ_AAPL", Decimal("7")),
                FundingBalance("USDC", Decimal("1000")),
            ],
        )
        result = guardian.reconcile_now()
        assert result.matched is False and result.halt_new_entries is True
        assert result.quantity_breaks == ["AAPL"]
        assert guardian.halted is True
        assert guardian.get("AAPL").state == "UNCERTAIN"
        assert ("CRITICAL", "RECONCILE_BREAK", "AAPL") in guardian.alerts()
        with pytest.raises(OrderRejected, match="HALT_NEW_ENTRIES"):
            guardian.adopt_buy(_plan("MSFT", "1"))
    finally:
        guardian.close()
    again = Guardian.open(path)
    try:
        assert again.halted is True
        assert again.get("AAPL").state == "UNCERTAIN"
        with pytest.raises(OrderRejected, match="HALT_NEW_ENTRIES"):
            again.adopt_buy(_plan("MSFT", "1"))
    finally:
        again.close()
