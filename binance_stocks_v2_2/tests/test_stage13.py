"""Stage 13: equity identity, slot cap, 8% HWM lock, PDT, and a killed process."""

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from bstocks_v2.constitution.guard import HWM_RETAINED, NEW_YORK, live_trade_enabled
from bstocks_v2.contracts.errors import LiveTradingDisabledError, OrderRejected
from bstocks_v2.portfolio_risk.authority import EntryRequest, PortfolioRisk
from bstocks_v2.portfolio_risk.equity import OrderMark, explained_open_delta, open_order_trap
from bstocks_v2.portfolio_risk.spec import DB_NAME, FILE_NAME, PLANNED_EQUITY, UNLOCK_PHRASE
from bstocks_v2.portfolio_risk.store import sign_unlock
from bstocks_v2.reconciler.ledger import FundingBalance, LedgerFill, PositionLedgerReconciler
from bstocks_v2.research_data.calendar import load_xnys_calendar

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 4, 15, 10, 0, tzinfo=NEW_YORK)
NEXT = datetime(2026, 4, 16, 10, 0, tzinfo=NEW_YORK)
RETAINED_LINE = PLANNED_EQUITY * HWM_RETAINED
TEN = ("AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOG", "TSLA", "AMD", "NFLX", "AVGO")


def _cid(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()[:32]


def _lot(symbol: str, label: str, when: datetime = NOW) -> EntryRequest:
    return _entry(symbol, label, limit="60", qty="2", stop="59", take_profit="72", when=when)


def _entry(
    symbol: str,
    label: str,
    *,
    limit: str = "80",
    qty: str = "4",
    stop: str = "79",
    take_profit: str = "90",
    when: datetime = NOW,
) -> EntryRequest:
    return EntryRequest(
        symbol=symbol,
        client_order_id=_cid(label),
        limit_price=Decimal(limit),
        quantity=Decimal(qty),
        stop_price=Decimal(stop),
        take_profit_price=Decimal(take_profit),
        now_et=when,
    )


def _meta(root: Path, key: str) -> str | None:
    conn = sqlite3.connect(root / DB_NAME)
    conn.execute("PRAGMA busy_timeout=5000")
    try:
        row = conn.execute("SELECT value FROM risk_meta WHERE key = ?", (key,)).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    return str(row[0])


def _set_meta(root: Path, key: str, value: str) -> None:
    conn = sqlite3.connect(root / DB_NAME)
    conn.execute("PRAGMA busy_timeout=5000")
    try:
        conn.execute("UPDATE risk_meta SET value = ? WHERE key = ?", (value, key))
        conn.commit()
    finally:
        conn.close()


def _fill(
    book: PortfolioRisk,
    client_order_id: str,
    *,
    status: str,
    qty: str,
    notional: str,
    total: str | None,
    rest: str | None,
    bid: str,
    when: datetime = NOW,
) -> None:
    book.on_buy_report(
        client_order_id,
        status=status,
        filled_qty=Decimal(qty),
        filled_notional=Decimal(notional),
        total_cost_raw=None if total is None else Decimal(total),
        rest_fee=None if rest is None else Decimal(rest),
        bid=Decimal(bid),
        now_et=when,
    )


def _trap(book: PortfolioRisk) -> Decimal:
    total = Decimal(0)
    for order in book.snapshot().orders:
        total += open_order_trap(
            OrderMark(
                order.status,
                order.limit_price,
                order.quantity,
                order.filled_qty,
                order.filled_notional,
                order.total_cost_raw,
                order.rest_fee,
            )
        )
    return total


def _spawn(tmp_path: Path, argv: list[str]) -> tuple[subprocess.Popen[str], Path]:
    err_path = tmp_path / "worker.err"
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


def test_opening_hwm_is_at_least_the_planned_equity(tmp_path: Path) -> None:
    assert live_trade_enabled() is False
    low = PortfolioRisk.open(tmp_path / "low", verified_equity=Decimal("1500"))
    high = PortfolioRisk.open(tmp_path / "high", verified_equity=Decimal("1700"))
    line = PortfolioRisk.open(tmp_path / "line", verified_equity=Decimal("1472"))
    try:
        assert low.snapshot().hwm == Decimal("1600")
        assert low.snapshot().equity == Decimal("1500")
        assert low.snapshot().halt_new_entries is False
        assert high.snapshot().hwm == Decimal("1700")
        assert high.snapshot().halt_new_entries is False
        assert line.snapshot().equity == RETAINED_LINE
        assert line.snapshot().halt_new_entries is True
        conn = sqlite3.connect(tmp_path / "low" / DB_NAME)
        try:
            mode = conn.execute("PRAGMA journal_mode").fetchone()
        finally:
            conn.close()
        assert mode is not None and str(mode[0]).lower() == "wal"
        assert (tmp_path / "low" / FILE_NAME).exists()
    finally:
        low.close()
        high.close()
        line.close()


def test_buy_lifecycle_ignores_tc_minus_fn(tmp_path: Path) -> None:
    book = PortfolioRisk.open(tmp_path)
    try:
        decision = book.approve(_entry("AAPL", "life"))
        assert decision.accepted is True
        steps = (
            ("NEW", "0", "0", None),
            ("ACCEPTED", "0", "0", None),
            ("PARTIALLY_FILLED", "1", "80", None),
            ("PARTIALLY_FILLED", "2", "160", None),
            ("FILLED", "4", "320", "0.16"),
        )
        for status, qty, notional, rest in steps:
            _fill(
                book,
                decision.client_order_id,
                status=status,
                qty=qty,
                notional=notional,
                total="320.16",
                rest=rest,
                bid="80",
            )
            view = book.snapshot()
            buy_fee = Decimal(0) if rest is None else Decimal(rest)
            delta = explained_open_delta(
                deposits=Decimal(0),
                cost=Decimal(notional),
                bid_gross=Decimal(qty) * Decimal("80"),
                buy_fees=buy_fee,
            )
            assert view.equity == view.opening + delta
            assert view.available + view.locked + view.liquidation == view.equity
            assert view.halt_new_entries is False
            assert view.equity > RETAINED_LINE
            if status != "FILLED":
                assert view.orders[0].rest_fee is None
                assert _trap(book) > 0
                assert view.equity - _trap(book) <= RETAINED_LINE
            else:
                assert view.orders[0].rest_fee == Decimal("0.16")
                assert view.equity == Decimal("1599.68")
    finally:
        book.close()


def test_ten_concurrent_signals_keep_five_slots(tmp_path: Path) -> None:
    root = tmp_path / "slots"
    starter = PortfolioRisk.open(root)
    starter.close()
    barrier = threading.Barrier(10)

    def attempt(index: int) -> str:
        book = PortfolioRisk.open(root)
        try:
            barrier.wait(timeout=10)
            decision = book.approve(_lot(TEN[index], f"slot-{index}"))
            return decision.reason
        finally:
            book.close()

    with ThreadPoolExecutor(max_workers=10) as pool:
        reasons = list(pool.map(attempt, range(10)))
    assert reasons.count("ACCEPTED") == 5
    assert reasons.count("MAX_POSITIONS") == 5
    book = PortfolioRisk.open(root)
    try:
        view = book.snapshot()
        assert view.slots == 5
        assert view.halt_new_entries is False
        assert view.locked == Decimal("600")
        assert view.equity == Decimal("1600")
        sixth = book.approve(_lot("IBM", "slot-extra"))
        assert sixth.accepted is False and sixth.reason == "MAX_POSITIONS"
    finally:
        book.close()


def test_five_partial_fills_do_not_trip_the_hwm_lock(tmp_path: Path) -> None:
    book = PortfolioRisk.open(tmp_path)
    try:
        orders: list[str] = []
        for symbol in TEN[:5]:
            decision = book.approve(_entry(symbol, f"partial-{symbol}"))
            assert decision.accepted is True and decision.reason == "ACCEPTED"
            orders.append(decision.client_order_id)
        for client_order_id in orders:
            _fill(
                book,
                client_order_id,
                status="PARTIALLY_FILLED",
                qty="0.4",
                notional="32",
                total="320.16",
                rest=None,
                bid="80",
            )
        view = book.snapshot()
        assert view.slots == 5
        assert view.locked == Decimal("1440")
        assert view.equity == Decimal("1599.92")
        assert view.halt_new_entries is False
        assert _trap(book) == Decimal("1440.80")
        assert view.equity - _trap(book) == Decimal("159.12")
        assert view.equity - _trap(book) <= RETAINED_LINE
        assert all(order.rest_fee is None for order in view.orders)
    finally:
        book.close()


def test_eight_percent_drawdown_locks_cancels_and_survives_kill(tmp_path: Path) -> None:
    root = tmp_path / "lock"
    book = PortfolioRisk.open(root)
    try:
        owned = book.approve(_entry("AAPL", "drop-aapl"))
        assert owned.accepted is True
        _fill(
            book,
            owned.client_order_id,
            status="FILLED",
            qty="4",
            notional="320",
            total="320.16",
            rest=None,
            bid="80",
        )
        pending: list[str] = []
        for symbol in ("MSFT", "NVDA", "AMZN", "META"):
            decision = book.approve(_lot(symbol, f"drop-{symbol}"))
            assert decision.accepted is True, decision.reason
            pending.append(decision.client_order_id)
        above = book.mark({"AAPL": Decimal("48.03")}, NOW)
        assert above.halt_new_entries is False
        assert above.equity == Decimal("1472.02394")
        assert book.cancellations() == []
        locked = book.mark({"AAPL": Decimal("48.02")}, NOW)
        assert locked.halt_new_entries is True
        assert locked.halt_reason == "HWM_LOCK"
        assert locked.equity == Decimal("1471.98396")
        assert locked.equity <= RETAINED_LINE
        assert locked.locked == Decimal(0)
        assert locked.halt_transition_ms is not None and locked.halt_transition_ms < 100
        expected = list(zip(pending, ("MSFT", "NVDA", "AMZN", "META"), strict=True))
        assert sorted(book._gateway.cancels) == sorted(expected)
        assert sorted(item[0] for item in book.cancellations()) == sorted(pending)
        filled = [order for order in locked.orders if order.symbol == "AAPL"]
        assert len(filled) == 1 and filled[0].status == "FILLED"
        assert all(order.status == "CANCELED" for order in locked.orders if order.symbol != "AAPL")
        flat = book.reduce_position("AAPL", Decimal("4"), Decimal("48.02"), NOW)
        assert flat.halt_new_entries is True
        assert flat.positions == ()
        blocked = book.approve(_lot("IBM", "after-lock"))
        assert blocked.accepted is False and blocked.reason == "HALT_NEW_ENTRIES"
        with pytest.raises(LiveTradingDisabledError):
            book.live_place()
        with pytest.raises(LiveTradingDisabledError):
            book.live_cancel()
    finally:
        book.close()

    proc, err_path = _spawn(
        tmp_path,
        [
            sys.executable,
            "-m",
            "bstocks_v2.portfolio_risk.worker",
            "--root",
            str(root),
            "--code-version",
            "stage13.1",
        ],
    )
    try:
        deadline = time.time() + 15
        pid = ""
        while time.time() < deadline:
            pid = _meta(root, "pid") or ""
            if pid:
                break
            time.sleep(0.05)
        assert pid, err_path.read_text(encoding="utf-8")
        proc.kill()
        proc.wait(timeout=10)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)

    started = time.perf_counter_ns()
    again = PortfolioRisk.open(root, code_version="stage13.2")
    recover_ms = (time.perf_counter_ns() - started) // 1_000_000
    try:
        view = again.snapshot()
        assert recover_ms < 100
        assert view.halt_new_entries is True
        assert view.hwm == Decimal("1600")
        assert view.halt_reason == "HWM_LOCK"
        later = again.approve(_lot("IBM", "next-day", NEXT))
        assert later.accepted is False and later.reason == "HALT_NEW_ENTRIES"
        assert _meta(root, "code_version") == "stage13.2"
    finally:
        again.close()


def test_signature_unlock_does_not_reset_hwm(tmp_path: Path) -> None:
    root = tmp_path / "unlock"
    book = PortfolioRisk.open(root, verified_equity=Decimal("1472"))
    try:
        assert book.snapshot().halt_new_entries is True
        with pytest.raises(OrderRejected, match="UNLOCK_CONFIRMATION"):
            book.unlock("clear it", "0" * 64, NOW)
        with pytest.raises(OrderRejected, match="UNLOCK_SIGNATURE"):
            book.unlock(UNLOCK_PHRASE, "0" * 64, NOW)
        key = bytes.fromhex(_meta(root, "unlock_key") or "")
        view = book.snapshot()
        signature = sign_unlock(key, UNLOCK_PHRASE, format(view.hwm, "f"), view.generation)
        with pytest.raises(OrderRejected, match="HWM_STILL_BREACHED"):
            book.unlock(UNLOCK_PHRASE, signature, NOW)
        assert book.snapshot().halt_new_entries is True
        assert book.snapshot().hwm == Decimal("1600")
    finally:
        book.close()

    file_path = root / FILE_NAME
    payload = json.loads(file_path.read_text(encoding="utf-8"))
    payload["halt"] = False
    file_path.write_text(json.dumps(payload), encoding="utf-8")
    repaired = PortfolioRisk.open(root)
    try:
        assert repaired.snapshot().halt_new_entries is True
        assert repaired.snapshot().hwm == Decimal("1600")
        assert ("CRITICAL", "STATE_TAMPER", "1600") in repaired.alerts()
    finally:
        repaired.close()
    _set_meta(root, "hwm", "1000")
    _set_meta(root, "halt", "0")
    kept = PortfolioRisk.open(root)
    try:
        assert kept.snapshot().halt_new_entries is True
        assert kept.snapshot().hwm == Decimal("1600")
        funded = kept.credit_verified_deposit(Decimal("0.01"), NOW)
        assert funded.equity == Decimal("1472.01")
        assert funded.halt_new_entries is True
        assert funded.hwm == Decimal("1600")
        signature = sign_unlock(
            bytes.fromhex(_meta(root, "unlock_key") or ""),
            UNLOCK_PHRASE,
            format(funded.hwm, "f"),
            funded.generation,
        )
        cleared = kept.unlock(UNLOCK_PHRASE, signature, NOW)
        assert cleared.halt_new_entries is False
        assert cleared.hwm == Decimal("1600")
        assert cleared.generation == funded.generation + 1
    finally:
        kept.close()
    reopened = PortfolioRisk.open(root, code_version="stage13.1")
    try:
        assert reopened.snapshot().halt_new_entries is False
        assert reopened.snapshot().hwm == Decimal("1600")
    finally:
        reopened.close()


def test_pdt_counter_and_broker_codes_ban_buys_for_the_session(tmp_path: Path) -> None:
    assert load_xnys_calendar().is_session(date(2026, 4, 15))
    book = PortfolioRisk.open(tmp_path / "count")
    try:
        for index in range(3):
            decision = book.approve(_lot("AAPL", f"trip-{index}"))
            assert decision.accepted is True, decision.reason
            _fill(
                book,
                decision.client_order_id,
                status="FILLED",
                qty="2",
                notional="120",
                total="120.06",
                rest=None,
                bid="60",
            )
            book.reduce_position("AAPL", Decimal("2"), Decimal("60"), NOW)
        assert book.day_trade_count(date(2026, 4, 15)) == 3
        blocked = book.approve(_lot("AAPL", "trip-3"))
        assert blocked.accepted is False and blocked.reason == "PDT_LIMIT"
        assert any(code == "PDT_LIMIT" for _level, code, _detail in book.alerts())
        next_day = book.approve(_lot("MSFT", "trip-next", NEXT))
        assert next_day.accepted is False and next_day.reason == "PDT_LIMIT"
    finally:
        book.close()
    reopened = PortfolioRisk.open(tmp_path / "count", pdt_enabled=False)
    try:
        still = reopened.approve(_lot("NVDA", "trip-reopen"))
        assert still.accepted is False and still.reason == "PDT_LIMIT"
    finally:
        reopened.close()

    disabled = PortfolioRisk.open(tmp_path / "off", pdt_enabled=False)
    try:
        for index in range(3):
            decision = disabled.approve(_lot("AAPL", f"off-{index}"))
            assert decision.accepted is True, decision.reason
            _fill(
                disabled,
                decision.client_order_id,
                status="FILLED",
                qty="2",
                notional="120",
                total="120.06",
                rest=None,
                bid="60",
            )
            disabled.reduce_position("AAPL", Decimal("2"), Decimal("60"), NOW)
        allowed = disabled.approve(_lot("AAPL", "off-3"))
        assert allowed.accepted is True
    finally:
        disabled.close()

    banned = PortfolioRisk.open(tmp_path / "code")
    try:
        banned.on_broker_code(486440, NOW)
        pdt = banned.approve(_lot("AAPL", "pdt-code"))
        assert pdt.accepted is False and pdt.reason == "486440"
        assert any(code == "486440" for _level, code, _detail in banned.alerts())
        later = banned.approve(_lot("AAPL", "pdt-next", NEXT))
        assert later.accepted is True
        banned.on_broker_code(486439, NEXT)
        asset = banned.approve(_lot("MSFT", "asset-code", NEXT))
        assert asset.accepted is False and asset.reason == "486439"
        banned.on_broker_code(1, NEXT)
        assert banned.snapshot().buy_ban_code == "486439"
    finally:
        banned.close()


def test_reconcile_break_halts_new_entries(tmp_path: Path) -> None:
    book = PortfolioRisk.open(tmp_path / "audit")
    ledger = PositionLedgerReconciler(str(tmp_path / "ledger.sqlite"))
    try:
        clean = ledger.reconcile([], [], [FundingBalance("USDC", Decimal("1600"))])
        assert clean.matched is True
        assert book.absorb_audit(clean, NOW).halt_new_entries is False
        decision = book.approve(_lot("AAPL", "audit"))
        assert decision.accepted is True
        ledger.record_fill("local", "1", LedgerFill("AAPL", "BUY", Decimal("10")), "FILLED")
        broken = ledger.reconcile(
            [LedgerFill("AAPL", "BUY", Decimal("10"))],
            [],
            [FundingBalance("EQ_AAPL", Decimal("7")), FundingBalance("USDC", Decimal("1000"))],
        )
        assert broken.matched is False and broken.quantity_breaks == ["AAPL"]
        halted = book.absorb_audit(broken, NOW)
        assert halted.halt_new_entries is True
        assert halted.halt_reason == "RECONCILE_BREAK"
        assert book.cancellations()[0][1] == "AAPL"
        blocked = book.approve(_lot("MSFT", "audit-2"))
        assert blocked.reason == "HALT_NEW_ENTRIES"
    finally:
        book.close()
        ledger.close()


def test_size_gates_reject_before_a_buy_is_locked(tmp_path: Path) -> None:
    book = PortfolioRisk.open(tmp_path)
    try:
        small = book.approve(
            _entry("AAPL", "small", limit="100", qty="1", stop="99", take_profit="120")
        )
        assert small.reason == "NOTIONAL_TOO_SMALL"
        wide = book.approve(
            _entry("AAPL", "wide", limit="60", qty="2", stop="57", take_profit="72")
        )
        assert wide.reason == "RISK_BUDGET"
        heavy = book.approve(
            _entry("AAPL", "heavy", limit="80", qty="5", stop="79.20", take_profit="90")
        )
        assert heavy.reason == "CONCENTRATION"
        assert book.snapshot().slots == 0
        assert book.snapshot().locked == Decimal(0)
    finally:
        book.close()
