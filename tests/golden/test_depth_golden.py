"""Hand-computed depth-gate cases.

Every expected number is derived in the accompanying comment, never from the
implementation. All prices are synthetic test constants; fees are zeroed so
the arithmetic is legible.
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

from tests.factories import make_listing

from tradeup.discovery.depth import (
    assess_input_depth,
    depth_rejection_reasons,
    qualifying_listings,
)
from tradeup.domain.contracts import RejectionReason
from tradeup.domain.items import QualityType
from tradeup.domain.listings import ListingStatus
from tradeup.domain.money import BalanceType, Currency, Money

USD = Currency.USD
CASH = BalanceType.CASH_WITHDRAWABLE


def _usd(minor: int) -> Money:
    return Money(minor, USD, CASH)


def _board(prices: list[int], *, venue: str = "csfloat") -> list:
    return [
        make_listing(f"L-{i:03d}", venue=venue, price_minor=price, buyer_fee_minor=0)
        for i, price in enumerate(prices)
    ]


def test_cheapest_ten_mean_boundary() -> None:
    # Twelve listings priced 10..21. Cheapest ten are 10..19:
    # sum = 10+11+...+19 = 145; mean = ceil(145/10) = ceil(14.5) = 15.
    # A reference of 15 passes on the boundary; 14 fails.
    board = _board(list(range(10, 22)))
    passing = assess_input_depth(board, skin_id="a-in-1", target_unit_price=_usd(15))
    assert passing.passed
    assert passing.qualifying_count == 12
    assert passing.cheapest_mean == _usd(15)
    failing = assess_input_depth(board, skin_id="a-in-1", target_unit_price=_usd(14))
    assert not failing.passed
    assert failing.cheapest_mean == _usd(15)
    assert "exceeds reference" in failing.detail


def test_nine_listings_fail_closed_at_any_price() -> None:
    # Nine one-cent listings against a generous reference: count fails first,
    # and no mean is stated — a mean over too few listings dresses thin
    # supply up as a price.
    evidence = assess_input_depth(_board([1] * 9), skin_id="a-in-1", target_unit_price=_usd(10_000))
    assert not evidence.passed
    assert evidence.qualifying_count == 9
    assert evidence.cheapest_mean is None
    assert "failing closed" in evidence.detail


def test_cross_venue_aggregation_and_venue_census() -> None:
    # Five listings on each of two venues, all priced 10: mean 10, and both
    # venues appear in the evidence.
    board = _board([10] * 5, venue="csfloat") + _board([10] * 5, venue="dmarket")
    evidence = assess_input_depth(board, skin_id="a-in-1", target_unit_price=_usd(10))
    assert evidence.passed
    assert evidence.venues == ("csfloat", "dmarket")


def test_qualifying_filter() -> None:
    keep = make_listing("K-1", price_minor=10, buyer_fee_minor=0)
    wrong_skin = make_listing("K-2", skin_id="b-in-1", price_minor=10)
    wrong_quality = make_listing("K-3", quality=QualityType.STATTRAK, price_minor=10)
    sold = make_listing("K-4", status=ListingStatus.SOLD, price_minor=10)
    low_float = make_listing("K-5", raw_float=Decimal("0.05"), price_minor=10)
    euro = make_listing("K-6", price_minor=10, buyer_fee_minor=0)
    euro = replace(
        euro,
        price=Money(10, Currency.EUR, CASH),
        buyer_fee=Money(0, Currency.EUR, CASH),
        deposit_fee=Money(0, Currency.EUR, CASH),
    )
    result = qualifying_listings(
        [keep, wrong_skin, wrong_quality, sold, low_float, euro],
        skin_id="a-in-1",
        quality=QualityType.NORMAL,
        currency=USD,
        float_low=Decimal("0.07"),
        float_high=Decimal("0.15"),
    )
    assert result == (keep,)


def test_rejection_reason_mapping() -> None:
    board = _board([10] * 10)
    ok = assess_input_depth(board, skin_id="a-in-1", target_unit_price=_usd(10))
    thin = assess_input_depth(_board([1] * 3), skin_id="a-in-1", target_unit_price=_usd(10))
    assert depth_rejection_reasons([ok]) == ()
    assert depth_rejection_reasons([ok, thin]) == (RejectionReason.INSUFFICIENT_INPUT_DEPTH,)
