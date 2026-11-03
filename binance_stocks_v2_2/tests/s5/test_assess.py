"""S5-P5 assessment. OHLCV screens do not become overnight profits."""

from datetime import date
from decimal import Decimal

import pytest

from bstocks_v2.strategies.s5.assess import (
    Evidence,
    ScreenPoint,
    assess_evidence,
    assess_screen,
    book_match,
    drop_losses,
    fill_account,
    neighbor_report,
    pooled_expectancy,
    require_research_mode,
    stress_evidence,
)
from bstocks_v2.strategies.s5.spec import ARM_A, ARM_B

TRAIN = date(2025, 1, 15)
VALIDATION = date(2025, 11, 14)


def test_ohlcv_screen_is_no_go_and_claims_no_overnight_profit() -> None:
    body = assess_screen(_screen(), _coverage())
    assert body["strategy_verdict"] == "NO_GO"
    assert body["research_mode"] == "RESEARCH_ONLY"
    assert body["shadow_authorized"] is False
    assert body["overnight_profit_claimed"] is False
    assert body["phase_gate"] == "PASS"
    assert body["quote_backed_fills"] == 0
    assert "pooled_expectancy_usdc" not in body
    b5 = [row for row in _rows(body["ablation"]) if row["level"] == "B5"]
    assert [row["unfillable"] for row in b5] == [4, 5, 6]
    assert all(row["expectancy_usdc"] is None for row in b5)
    assert all(row["closed_trades"] == 0 for row in _rows(body["sectors"]))


def test_a_priced_path_without_quotes_is_refused() -> None:
    screen = _screen()
    summary = _map(screen["summary"])
    arms = _map(summary["arms"])
    arm = _map(arms[ARM_A])
    arm["path_labels"] = {"UNFILLABLE": 3, "TP_FIRST": 1}
    arms[ARM_A] = arm
    summary["arms"] = arms
    screen["summary"] = summary
    with pytest.raises(ValueError, match="PATH_WITHOUT_QUOTES"):
        assess_screen(screen, _coverage())


def test_the_screen_refuses_a_lockbox_read() -> None:
    screen = _screen()
    screen["lockbox_read"] = True
    with pytest.raises(ValueError, match="LOCKBOX"):
        assess_screen(screen, _coverage())


def test_quotes_in_the_screen_are_not_scored_here() -> None:
    screen = _screen()
    screen["quotes"] = 1
    with pytest.raises(ValueError, match="SCREEN_QUOTES"):
        assess_screen(screen, _coverage())


def test_loss_filter_and_pooled_expectancy_are_refused() -> None:
    with pytest.raises(ValueError, match="LOSS_FILTER_FORBIDDEN"):
        drop_losses(())
    with pytest.raises(ValueError, match="ARMS_NOT_POOLED"):
        pooled_expectancy(())


def test_a_match_without_a_quote_is_not_booked() -> None:
    assert book_match("FILLED", Decimal(1), Decimal("100"), quote_backed=False) is None
    assert book_match("UNFILLED", Decimal(0), None, quote_backed=True) is None
    partial = book_match("PARTIALLY_FILLED", Decimal(2), Decimal("100"), quote_backed=True)
    assert partial == Decimal(2)
    assert fill_account(Decimal(5), Decimal(2)) == {
        "signals": 1,
        "closed_qty": Decimal(2),
        "unfilled_qty": Decimal(3),
    }


def test_neighbor_counts_keep_the_frozen_threshold() -> None:
    report = neighbor_report(
        (
            ScreenPoint(Decimal("-0.04"), Decimal("-3")),
            ScreenPoint(Decimal("-0.026"), Decimal("-1.8")),
        )
    )
    assert report["reported_count"] == 1
    assert report["looser_count"] == 2
    assert report["adopted"] == "frozen"


def test_fee_and_delay_stress_can_remove_a_thin_edge() -> None:
    rows = [_row(index, gross="0.30", fee="0.20", spread="0", lag=500) for index in range(30)]
    stresses = {str(item["name"]): item for item in stress_evidence(rows)}
    assert stresses["fee_x2"]["keeps_positive_expectancy"] is False
    assert stresses["delay_1s"]["keeps_positive_expectancy"] is False


def test_a_gap_or_a_zero_bid_books_no_profit() -> None:
    rows = [_row(index, arm=ARM_B) for index in range(30)]
    stresses = {str(item["name"]): item for item in stress_evidence(rows)}
    assert stresses["gap_or_halt"]["books_profit"] is False
    assert stresses["overnight_zero_bid"]["books_profit"] is False


def test_dropping_intc_is_not_adopted() -> None:
    winners = [_row(index, symbol="AIQ", gross="2") for index in range(30)]
    losers = [_row(index + 30, symbol="INTC", gross="-10", path="SL_FIRST") for index in range(30)]
    body = assess_evidence([*winners, *losers], holdout_symbol="INTC")
    assert body["strategy_verdict"] == "NO_GO"
    held = _map(body["symbol_holdout"])
    assert held["adopted"] == "full"
    assert held["single_name"] is True
    assert "SINGLE_NAME" in body["reasons"]


def test_a_quote_backed_book_can_authorize_shadow_without_a_live_claim() -> None:
    body = assess_evidence(_robust())
    assert body["strategy_verdict"] == "SHADOW_ONLY"
    assert body["shadow_authorized"] is True
    assert body["overnight_profit_claimed"] is False
    assert body["live_enabled"] is False
    assert "pooled_expectancy_usdc" not in _map(body["score"])
    with pytest.raises(RuntimeError, match="lock drifted"):
        require_research_mode(True)


def test_a_lockbox_session_cannot_enter_the_sample() -> None:
    with pytest.raises(ValueError, match="lockbox"):
        _row(0, session=date(2026, 6, 1), split="train")


def _robust() -> list[Evidence]:
    rows: list[Evidence] = []
    for index in range(60):
        rows.append(
            _row(
                index,
                session=TRAIN if index < 30 else VALIDATION,
                split="train" if index < 30 else "validation",
                event_id=f"c{index % 10}",
            )
        )
    return rows


def _row(
    index: int,
    *,
    symbol: str = "AIQ",
    arm: str = ARM_A,
    gross: str = "2",
    fee: str = "0.10",
    spread: str = "0.10",
    lag: int = 10_000,
    path: str = "TP_FIRST",
    session: date = TRAIN,
    split: str = "train",
    event_id: str = "e1",
) -> Evidence:
    del index
    return Evidence(
        symbol=symbol,
        arm=arm,
        sector="XLK",
        session=session,
        split=split,
        event_id=event_id,
        path=path,
        quote_backed=True,
        gross_usdc=Decimal(gross),
        fee_usdc=Decimal(fee),
        spread_usdc=Decimal(spread),
        notional=Decimal("120"),
        penetration_cleared=Decimal("0.05"),
        quote_lag_ms=lag,
        exit_bid=Decimal("100"),
    )


def _screen() -> dict[str, object]:
    counts = {ARM_A: 4, "B_POST_TO_POST": 5, "C_POST_TO_OVERNIGHT": 6}
    arms = {
        name: {
            "decisions": count,
            "screens": {"B0": 1, "B1": 0, "B2": 0, "B3": 0},
            "admitted": {"B0": 0, "B1": 0, "B2": 0, "B3": 0},
            "path_labels": {"UNFILLABLE": count},
            "splits": {"train": count},
            "routable": 0,
        }
        for name, count in counts.items()
    }
    return {
        "mode": "rth_post_ohlcv_screen",
        "quotes": 0,
        "lockbox_read": False,
        "summary": {"arms": arms, "decisions": 15, "routable": 0},
    }


def _coverage() -> dict[str, object]:
    return {
        "timeframes": [
            {"timeframe": "1Min", "has_bid_ask": False, "clock": {"OVERNIGHT": 808}},
            {"timeframe": "5Min", "has_bid_ask": False, "clock": {"OVERNIGHT": 420}},
        ]
    }


def _rows(value: object) -> list[dict[str, object]]:
    assert isinstance(value, list)
    return [_map(item) for item in value]


def _map(value: object) -> dict[str, object]:
    assert isinstance(value, dict)
    return {str(key): item for key, item in value.items()}
