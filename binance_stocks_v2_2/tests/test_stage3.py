"""Stage 3: point-in-time universe, SIP session labels, causal baselines."""

from datetime import date, time, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from bstocks_v2.research_data.adjust import (
    SplitEvent,
    backward_price_factors,
    jump_flag,
    split_adjusted_volume,
)
from bstocks_v2.research_data.calendar import load_xnys_calendar, utc
from bstocks_v2.research_data.domain_shift import ComparedBar, align, summarize
from bstocks_v2.research_data.exclusions import hard_exclusion
from bstocks_v2.research_data.features import build_rth_features
from bstocks_v2.research_data.manifest import assert_output_isolated, sha256_file
from bstocks_v2.research_data.quality import PriceBar, bar_quality
from bstocks_v2.research_data.universe import (
    Candidate,
    classify_membership,
    pit_cap,
    rename_window_end,
    value_before,
    values_before,
)

ROOT = Path(__file__).resolve().parents[1]


def _bar(
    start_hour: int,
    start_minute: int,
    *,
    day: int = 1,
    month: int = 11,
    year: int = 2024,
    close: str = "100",
    volume: str = "10",
    feed: str = "sip",
    minutes: int = 5,
) -> PriceBar:
    start = utc(year, month, day, start_hour, start_minute)
    return PriceBar(
        symbol="AAA",
        start=start,
        end=start + timedelta(minutes=minutes),
        open=Decimal(close),
        high=Decimal(close),
        low=Decimal(close),
        close=Decimal(close),
        volume=Decimal(volume),
        vwap=Decimal(close),
        feed=feed,
    )


def _candidate(symbol: str = "TSLA", asset_type: str = "stock", pool: bool = True) -> Candidate:
    return Candidate(
        symbol=symbol,
        asset_type=asset_type,
        window_start=date(2024, 11, 1),
        window_end=date(2026, 10, 30),
        entity_id=f"{symbol}@asof",
        alpaca_asof="2026-10-30",
        in_main_backtest_pool=pool,
        anomaly_flags="",
    )


def test_calendar_matches_the_research_window() -> None:
    calendar = load_xnys_calendar()
    sessions = calendar.sessions_between(date(2024, 11, 1), date(2026, 10, 30))
    assert len(sessions) == 500
    assert calendar.close_time(date(2024, 11, 29)) == time(13, 0)
    early = [day for day in sessions if calendar.close_time(day).hour == 13]
    assert early == [
        date(2024, 11, 29),
        date(2024, 12, 24),
        date(2025, 7, 3),
        date(2025, 11, 28),
        date(2025, 12, 24),
    ]
    assert date(2025, 1, 9) not in calendar.closes()


def test_session_labels_follow_dst_and_early_close() -> None:
    calendar = load_xnys_calendar()
    edt_open = utc(2024, 11, 1, 13, 30)
    est_open = utc(2024, 11, 4, 14, 30)
    assert calendar.classify(edt_open, edt_open + timedelta(minutes=5)) == "RTH"
    assert calendar.classify(est_open, est_open + timedelta(minutes=5)) == "RTH"
    pre = utc(2024, 11, 1, 13, 25)
    assert calendar.classify(pre, pre + timedelta(minutes=5)) == "EXTENDED"
    early = utc(2024, 11, 29, 18, 0)
    assert calendar.classify(early, early + timedelta(minutes=5)) == "EXTENDED"
    still_rth = utc(2024, 11, 29, 17, 55)
    assert calendar.classify(still_rth, still_rth + timedelta(minutes=5)) == "RTH"
    hour = utc(2024, 11, 1, 13, 0)
    assert calendar.classify(hour, hour + timedelta(hours=1)) == "MIXED"
    overnight = utc(2024, 11, 1, 7, 0)
    assert calendar.classify(overnight, overnight + timedelta(minutes=5)) == "OVERNIGHT"


def test_hard_exclusions_keep_tsla_and_tsm() -> None:
    assert hard_exclusion("TSLA") is None
    assert hard_exclusion("TSM") is None
    assert hard_exclusion("SPCX") == "spacex"
    assert hard_exclusion("BABA") == "china_mainland_us_listed"


def test_future_market_cap_cannot_admit_an_earlier_session() -> None:
    observations = [
        (date(2024, 11, 1), Decimal("6000000000")),
        (date(2024, 11, 6), Decimal("1000000000")),
    ]
    assert pit_cap(observations, date(2024, 11, 4)) == Decimal("6000000000")
    assert pit_cap(observations, date(2024, 11, 1)) is None
    assert pit_cap(observations, date(2024, 11, 7)) == Decimal("1000000000")


def test_same_day_volume_does_not_enter_the_liquidity_gate() -> None:
    series = [(date(2024, 11, 1), Decimal("1"))] * 20
    series.append((date(2024, 11, 4), Decimal("999999999")))
    prior = values_before(series, date(2024, 11, 4), 20)
    assert prior == [Decimal("1")] * 20
    assert value_before(series, date(2024, 11, 4)) == Decimal("1")


def test_membership_gates() -> None:
    buy = {"TSLA", "SPY", "BABA", "SPCX"}
    known = buy | {"HALTED"}
    rich = [(date(2024, 10, 31), Decimal("6000000000"))]
    liquid = [Decimal("30000000")] * 20
    tsla = classify_membership(
        _candidate("TSLA"),
        date(2024, 11, 4),
        buy_sell=buy,
        known_symbols=known,
        prior_close=Decimal("200"),
        prior_dollar_volumes=[],
        cap_observations=rich,
    )
    assert tsla.research_eligible is True
    unverified = classify_membership(
        _candidate("TSLA"),
        date(2024, 11, 4),
        buy_sell=buy,
        known_symbols=known,
        prior_close=Decimal("200"),
        prior_dollar_volumes=[],
        cap_observations=[],
    )
    assert unverified.reason == "market_cap_unverified"
    small = classify_membership(
        _candidate("TSLA"),
        date(2024, 11, 4),
        buy_sell=buy,
        known_symbols=known,
        prior_close=Decimal("200"),
        prior_dollar_volumes=[],
        cap_observations=[(date(2024, 11, 1), Decimal("4999999999"))],
    )
    assert small.reason == "market_cap_below"
    etf = classify_membership(
        _candidate("SPY", "etf"),
        date(2024, 11, 4),
        buy_sell=buy,
        known_symbols=known,
        prior_close=Decimal("500"),
        prior_dollar_volumes=liquid,
        cap_observations=[],
    )
    assert etf.research_eligible is True
    thin = classify_membership(
        _candidate("SPY", "etf"),
        date(2024, 11, 4),
        buy_sell=buy,
        known_symbols=known,
        prior_close=Decimal("500"),
        prior_dollar_volumes=[Decimal("100")] * 20,
        cap_observations=[(date(2024, 11, 1), Decimal("999999999999"))],
    )
    assert thin.reason == "etf_illiquid"
    assert (
        classify_membership(
            _candidate("BABA"),
            date(2024, 11, 4),
            buy_sell=buy,
            known_symbols=known,
            prior_close=Decimal("200"),
            prior_dollar_volumes=[],
            cap_observations=rich,
        ).reason
        == "china_mainland_us_listed"
    )
    assert (
        classify_membership(
            _candidate("SPCX"),
            date(2024, 11, 4),
            buy_sell=buy,
            known_symbols=known,
            prior_close=Decimal("200"),
            prior_dollar_volumes=[],
            cap_observations=rich,
        ).reason
        == "spacex"
    )
    halted = _candidate("HALTED")
    assert (
        classify_membership(
            halted,
            date(2024, 11, 4),
            buy_sell=buy,
            known_symbols=known,
            prior_close=Decimal("200"),
            prior_dollar_volumes=[],
            cap_observations=rich,
        ).reason
        == "not_buy_sell"
    )


def test_rename_closes_the_old_ticker_before_the_new_name() -> None:
    end = rename_window_end("EQR", date(2026, 10, 30), "RENAMED_EQR_TO_VMRK_2026-08-18")
    assert end == date(2026, 8, 17)


def test_split_factor_is_backward_and_raw_jump_is_flagged() -> None:
    sessions = [date(2025, 11, 14), date(2025, 11, 17)]
    factors = backward_price_factors(
        [SplitEvent("NFLX", date(2025, 11, 17), Decimal(10), Decimal(1))],
        sessions,
    )
    assert factors[date(2025, 11, 17)] == Decimal(1)
    assert factors[date(2025, 11, 14)] == Decimal("0.1")
    assert split_adjusted_volume(Decimal(100), Decimal("0.5")) == Decimal(200)
    assert jump_flag(Decimal("1112.17"), Decimal("110.29"), True) == "SPLIT_RAW"
    assert jump_flag(Decimal("100"), Decimal("130"), False) == "UNMATCHED_JUMP"
    assert jump_flag(Decimal("100"), Decimal("110"), False) is None


def test_non_sip_and_bad_ohlc_are_flagged() -> None:
    bar = _bar(13, 30, feed="iex")
    assert bar_quality(bar, duplicate=False) == "NON_SIP"
    broken = PriceBar(
        symbol="AAA",
        start=bar.start,
        end=bar.end,
        open=Decimal("10"),
        high=Decimal("9"),
        low=Decimal("8"),
        close=Decimal("8.5"),
        volume=Decimal(1),
        vwap=None,
        feed="sip",
    )
    assert bar_quality(broken, duplicate=False) == "OHLC_INVALID"


def test_vwap_resets_and_baseline_ignores_the_current_day() -> None:
    calendar = load_xnys_calendar()
    first = _bar(14, 30, day=4, close="10", volume="1")
    second = _bar(14, 35, day=4, close="30", volume="1")
    later = _bar(14, 30, day=5, close="20", volume="3")
    rows, days = build_rth_features([second, first, later], calendar, {})
    assert [row.cum_vwap for row in rows if row.ny_date == "2024-11-04"] == [
        Decimal(10),
        Decimal(20),
    ]
    assert rows[-1].cum_vwap == Decimal(20)
    assert rows[-1].tod_mean_volume == Decimal(1)
    assert rows[0].tod_mean_volume is None
    assert days[0].vwap_intact is True


def test_internal_gap_breaks_vwap_and_a_quiet_open_does_not() -> None:
    calendar = load_xnys_calendar()
    bars = [_bar(14, 30, day=4), _bar(14, 40, day=4)]
    _rows, days = build_rth_features(bars, calendar, {})
    assert days[0].internal_gaps == 1
    assert days[0].vwap_intact is False
    open_only = [_bar(14, 30, day=4)]
    _rows, quiet = build_rth_features(open_only, calendar, {})
    assert quiet[0].internal_gaps == 0
    assert quiet[0].vwap_intact is True


def test_domain_shift_refuses_a_short_overlap() -> None:
    start = utc(2024, 11, 4, 14, 30)
    left = [
        ComparedBar("AAPL", start, Decimal("100"), Decimal("10"), Decimal("100"))
    ]
    right = [
        ComparedBar("AAPL", start, Decimal("101"), Decimal("12"), Decimal("100.5"))
    ]
    summary = summarize(align(left, right))
    assert summary.overlap == 1
    assert summary.status == "insufficient_overlap"
    assert summary.close_median_bps == Decimal(100)


def test_output_cannot_overwrite_the_source_store(tmp_path: Path) -> None:
    source = tmp_path / "fresh"
    source.mkdir()
    with pytest.raises(ValueError):
        assert_output_isolated(source, source)
    target = tmp_path / "data_v2_2"
    target.mkdir()
    assert_output_isolated(target, source)
    sample = target / "note.txt"
    sample.write_text("sip", encoding="utf-8")
    assert sha256_file(sample) == sha256_file(sample)


def test_stage3_modules_do_not_enable_live_trading() -> None:
    text = (ROOT / "src" / "bstocks_v2" / "research_data" / "build.py").read_text(encoding="utf-8")
    assert "order/place" not in text
    assert "live_trade_enabled = True" not in text
