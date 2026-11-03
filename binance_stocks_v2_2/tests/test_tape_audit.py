"""The sealed tape's cost formula and the session grid."""

from datetime import datetime, timedelta
from decimal import Decimal

from bstocks_v2.research_data.calendar import NEW_YORK
from bstocks_v2.research_data.tape_audit import (
    frozen_costs,
    long_holes,
    round_trip_cost,
    session_gaps,
)


def test_two_missing_slots_in_a_row_are_one_long_hole() -> None:
    start = datetime(2024, 11, 1, 9, 30, tzinfo=NEW_YORK)
    expected = [start + timedelta(minutes=5 * step) for step in range(4)]
    assert long_holes(expected, [expected[0], expected[3]]) == 1
    assert long_holes(expected, [expected[0], expected[2], expected[3]]) == 0
    start = datetime(2024, 11, 1, 9, 30, tzinfo=NEW_YORK)
    expected = [start, start + timedelta(minutes=5)]
    assert session_gaps(expected, [start]) == (1, 0)
    assert session_gaps(expected, expected) == (0, 0)


def test_round_trip_cost_charges_both_sides() -> None:
    cost = round_trip_cost(Decimal("100"), Decimal("101"), Decimal("10"))
    assert cost == Decimal("1.205")


def test_frozen_costs_match_the_sealed_run() -> None:
    costs = frozen_costs()
    assert costs["commission_rate"] == "0.0005"
    assert costs["half_spread"] == "0.01"
    assert costs["penetration_delta"] == "0.02"
