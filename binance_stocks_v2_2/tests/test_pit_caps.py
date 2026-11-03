"""Point-in-time caps use a filing only after it is public."""

from datetime import date
from decimal import Decimal

from bstocks_v2.research_data.adjust import SplitEvent
from bstocks_v2.research_data.sec_caps import cap_series, share_prints, shares_on_day
from bstocks_v2.research_data.universe import reclassify_unverified_cap

_FACTS = {
    "facts": {
        "dei": {
            "EntityCommonStockSharesOutstanding": {
                "units": {
                    "shares": [
                        {
                            "form": "10-Q",
                            "filed": "2024-11-01",
                            "end": "2024-09-28",
                            "val": 100_000,
                        },
                        {
                            "form": "10-K",
                            "filed": "2024-12-01",
                            "end": "2024-11-30",
                            "val": 180_000,
                        },
                        {
                            "form": "8-K",
                            "filed": "2024-10-01",
                            "end": "2024-09-30",
                            "val": 999,
                        },
                    ]
                }
            }
        }
    }
}


def test_a_filing_does_not_apply_before_it_is_public() -> None:
    prints = share_prints(_FACTS)
    assert shares_on_day(prints, date(2024, 10, 31), []) is None
    assert shares_on_day(prints, date(2024, 11, 20), []) == Decimal(100_000)
    assert shares_on_day(prints, date(2024, 12, 2), []) == Decimal(180_000)
    assert shares_on_day(prints, date(2026, 6, 3), []) is None


def test_a_later_split_rescales_the_share_count_into_the_raw_close() -> None:
    prints = share_prints(_FACTS)
    split = SplitEvent("AAA", date(2024, 11, 15), Decimal(2), Decimal(1))
    assert shares_on_day(prints, date(2024, 11, 14), [split]) == Decimal(100_000)
    assert shares_on_day(prints, date(2024, 11, 15), [split]) == Decimal(200_000)
    series = cap_series(
        [(date(2024, 11, 14), Decimal(10)), (date(2024, 11, 15), Decimal(5))],
        prints,
        [split],
    )
    assert series == [
        (date(2024, 11, 14), Decimal(1_000_000)),
        (date(2024, 11, 15), Decimal(1_000_000)),
    ]


def test_a_token_share_count_and_a_missing_us_tag_do_not_invent_a_cap() -> None:
    tiny = {
        "facts": {
            "dei": {
                "EntityCommonStockSharesOutstanding": {
                    "units": {
                        "shares": [
                            {"form": "10-Q", "filed": "2019-03-18", "end": "2019-03-18", "val": 4}
                        ]
                    }
                }
            },
            "ifrs-full": {
                "NumberOfSharesOutstanding": {
                    "units": {
                        "shares": [
                            {
                                "form": "20-F",
                                "filed": "2024-01-31",
                                "end": "2023-12-31",
                                "val": 3_000_000_000,
                            }
                        ]
                    }
                }
            },
        }
    }
    prints = share_prints(tiny)
    assert prints == [(date(2024, 1, 31), date(2023, 12, 31), Decimal(3_000_000_000))]


def test_reclassify_only_replaces_the_missing_cap_reason() -> None:
    rich = [(date(2024, 11, 1), Decimal("6000000000"))]
    small = [(date(2024, 11, 1), Decimal("1000"))]
    session = date(2024, 11, 4)
    assert reclassify_unverified_cap("market_cap_unverified", session, rich) == "ok"
    assert reclassify_unverified_cap("market_cap_unverified", session, small) == "market_cap_below"
    assert (
        reclassify_unverified_cap("market_cap_unverified", session, []) == "market_cap_unverified"
    )
    assert reclassify_unverified_cap("price_below_min", session, rich) == "price_below_min"
    assert reclassify_unverified_cap("ok", session, []) == "ok"
