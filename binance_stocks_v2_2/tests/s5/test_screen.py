from datetime import date

from bstocks_v2.research_data.s5_screen import include_session


def test_the_screen_does_not_read_the_lockbox() -> None:
    assert include_session(date(2025, 1, 15))
    assert include_session(date(2026, 4, 30))
    assert not include_session(date(2026, 5, 1))
    assert not include_session(date(2024, 10, 31))
