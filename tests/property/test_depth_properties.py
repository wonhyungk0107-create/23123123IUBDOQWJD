"""Property tests for the input-depth gate.

The verdict is checked against an independent Fraction-arithmetic model over
every generated board, and the fail-closed rule is absolute: below the
required count, no price makes the gate pass.
"""

from __future__ import annotations

from fractions import Fraction

from hypothesis import given
from hypothesis import strategies as st
from tests.factories import make_listing

from tradeup.discovery.depth import assess_input_depth
from tradeup.domain.listings import MarketplaceListing
from tradeup.domain.money import BalanceType, Currency, Money

USD = Currency.USD
CASH = BalanceType.CASH_WITHDRAWABLE


def _usd(minor: int) -> Money:
    return Money(minor, USD, CASH)


def _board(prices: list[int]) -> list[MarketplaceListing]:
    return [
        make_listing(f"L-{i:03d}", price_minor=price, buyer_fee_minor=0)
        for i, price in enumerate(prices)
    ]


prices_lists = st.lists(st.integers(min_value=1, max_value=10**6), max_size=30)
targets = st.integers(min_value=0, max_value=10**6)
depths = st.integers(min_value=1, max_value=12)


@given(prices=prices_lists, target=targets, depth=depths)
def test_verdict_matches_independent_model(prices: list[int], target: int, depth: int) -> None:
    evidence = assess_input_depth(
        _board(prices),
        skin_id="a-in-1",
        target_unit_price=_usd(target),
        required_depth=depth,
    )
    assert evidence.qualifying_count == len(prices)
    if len(prices) < depth:
        assert not evidence.passed
        assert evidence.cheapest_mean is None
        return
    cheapest = sorted(prices)[:depth]
    exact_mean = Fraction(sum(cheapest), depth)
    model_mean_minor = -(-sum(cheapest) // depth)
    assert evidence.cheapest_mean is not None
    assert evidence.cheapest_mean.minor_units == model_mean_minor
    # Rounding up means the stated mean is never below the exact mean.
    assert Fraction(evidence.cheapest_mean.minor_units) >= exact_mean
    assert evidence.passed == (model_mean_minor <= target)


@given(prices=st.lists(st.integers(min_value=1, max_value=100), max_size=9))
def test_below_count_no_price_passes(prices: list[int]) -> None:
    evidence = assess_input_depth(
        _board(prices),
        skin_id="a-in-1",
        target_unit_price=_usd(10**9),
        required_depth=10,
    )
    assert not evidence.passed
