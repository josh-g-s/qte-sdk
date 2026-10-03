"""Regressions for the conformance session's ladder check, which run without an exchange."""

import pytest
from test_conformance import assert_ladder

from qte_sdk.contract.v1.market_data_pb2 import Book, WallLevel

TICK = 10_000


def book(bids: list[int], asks: list[int], size: int = 100) -> Book:
    return Book(
        instrument="TEST",
        grid_time=1,
        bid_levels=[WallLevel(price=p * TICK, size=size) for p in bids],
        ask_levels=[WallLevel(price=p * TICK, size=size) for p in asks],
    )


ASKS = list(range(11, 21))


def test_a_full_ladder_passes():
    assert_ladder(book(list(range(10, 0, -1)), ASKS))


def test_a_bid_ladder_cut_short_at_zero_passes():
    assert_ladder(book([3, 2, 1], [4 + k for k in range(10)]))


def test_bids_spaced_wider_than_the_asks_fail():
    with pytest.raises(AssertionError, match="evenly spaced"):
        assert_ladder(book([9, 1], ASKS))


def test_a_missing_interior_bid_level_fails():
    with pytest.raises(AssertionError, match="evenly spaced"):
        assert_ladder(book([10, 9, 7, 6, 5, 4, 3, 2, 1], ASKS))


def test_a_bid_ladder_cut_short_above_zero_fails():
    with pytest.raises(AssertionError, match="next would be positive"):
        assert_ladder(book([10, 9, 8], ASKS))


def test_a_missing_interior_ask_level_fails():
    with pytest.raises(AssertionError, match="evenly spaced"):
        assert_ladder(book(list(range(10, 0, -1)), [11, 12, 14, 15, 16, 17, 18, 19, 20, 21]))


def test_crossed_ladders_fail():
    with pytest.raises(AssertionError, match="crosses"):
        assert_ladder(book(list(range(12, 2, -1)), ASKS))


def test_levels_of_size_zero_fail():
    with pytest.raises(AssertionError, match="less than one share"):
        assert_ladder(book(list(range(10, 0, -1)), ASKS, size=0))
