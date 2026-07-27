"""Property tests for the entry-target inversion.

The contract is exact and two-sided: the returned cap clears the forward ROI
floor, and one more minor unit does not. Both sides are checked against an
independent Fraction-arithmetic model of the forward economics written here,
not against the implementation's own helpers.
"""

from __future__ import annotations

from fractions import Fraction

import pytest
from hypothesis import given
from hypothesis import strategies as st

from tradeup.domain.money import BalanceType, Currency, Money
from tradeup.valuation.entry_targets import max_acquisition_cost, per_unit_entry_target

USD = Currency.USD
CASH = BalanceType.CASH_WITHDRAWABLE


def _usd(minor: int) -> Money:
    return Money(minor, USD, CASH)


def _forward_clears(
    acquisition_minor: int,
    *,
    value_minor: int,
    fixed_minor: int,
    target: Fraction,
    overhead: Fraction,
) -> bool:
    """Independent forward model: does the fee-net ROI at this spend clear the floor?

    The proportional overhead rounds up, mirroring how the expected-value
    engine charges it (``Money.scaled_up``); every comparison is exact
    rational arithmetic.
    """
    overhead_minor = -((-overhead.numerator * acquisition_minor) // overhead.denominator)
    ev = Fraction(value_minor - fixed_minor - overhead_minor - acquisition_minor)
    return ev / acquisition_minor >= target


values = st.integers(min_value=0, max_value=10**9)
fixed_overheads = st.integers(min_value=0, max_value=10**6)
targets = st.fractions(min_value=Fraction(-99, 100), max_value=Fraction(5), max_denominator=1000)
overheads = st.fractions(min_value=0, max_value=Fraction(1, 2), max_denominator=1000)


@given(value=values, fixed=fixed_overheads, target=targets, overhead=overheads)
def test_cap_clears_the_floor_and_is_maximal(
    value: int, fixed: int, target: Fraction, overhead: Fraction
) -> None:
    cap = max_acquisition_cost(
        expected_net_output_value=_usd(value),
        target_roi=target,
        fixed_overhead=_usd(fixed),
        proportional_overhead=overhead,
    )
    assert not cap.is_negative
    assert cap.currency is USD
    assert cap.balance_type is CASH
    if cap.is_positive:
        assert _forward_clears(
            cap.minor_units,
            value_minor=value,
            fixed_minor=fixed,
            target=target,
            overhead=overhead,
        )
    assert not _forward_clears(
        cap.minor_units + 1,
        value_minor=value,
        fixed_minor=fixed,
        target=target,
        overhead=overhead,
    )


@given(value=values, target=targets)
def test_no_overhead_matches_plain_roi_inversion(value: int, target: Fraction) -> None:
    """With zero overheads the cap is the sweep-model inversion: the largest A
    with (V - A) / A >= r, checked directly."""
    cap = max_acquisition_cost(expected_net_output_value=_usd(value), target_roi=target)
    a = cap.minor_units
    if a > 0:
        assert Fraction(value - a, a) >= target
    assert not (Fraction(value - (a + 1), a + 1) >= target)


@given(value=values, fixed=fixed_overheads, target=targets)
def test_zero_when_fixed_overhead_consumes_the_value(
    value: int, fixed: int, target: Fraction
) -> None:
    cap = max_acquisition_cost(
        expected_net_output_value=_usd(value),
        target_roi=target,
        fixed_overhead=_usd(value + fixed),
    )
    assert cap.is_zero


@given(total=st.integers(min_value=0, max_value=10**9), count=st.integers(1, 10))
def test_per_unit_share_never_exceeds_the_cap(total: int, count: int) -> None:
    unit = per_unit_entry_target(_usd(total), count)
    assert unit.minor_units * count <= total
    assert (unit.minor_units + 1) * count > total


@given(
    total=st.integers(min_value=0, max_value=10**6),
    count=st.integers(1, 10),
    data=st.data(),
)
def test_average_at_or_below_unit_target_respects_the_cap(
    total: int, count: int, data: st.DataObject
) -> None:
    """The guarantee the alert relies on: any bundle whose average unit cost is
    at or below the per-unit target has a total at or below the cap."""
    unit = per_unit_entry_target(_usd(total), count)
    prices = data.draw(
        st.lists(
            st.integers(min_value=0, max_value=unit.minor_units),
            min_size=count,
            max_size=count,
        )
    )
    assert sum(prices) <= total


def test_float_ratios_are_rejected() -> None:
    with pytest.raises(TypeError):
        max_acquisition_cost(
            expected_net_output_value=_usd(100),
            target_roi=0.05,  # type: ignore[arg-type]
        )
    with pytest.raises(TypeError):
        max_acquisition_cost(
            expected_net_output_value=_usd(100),
            target_roi=Fraction(1, 20),
            proportional_overhead=0.01,  # type: ignore[arg-type]
        )


def test_impossible_parameters_are_rejected() -> None:
    with pytest.raises(ValueError):
        max_acquisition_cost(expected_net_output_value=_usd(100), target_roi=Fraction(-1))
    with pytest.raises(ValueError):
        max_acquisition_cost(
            expected_net_output_value=_usd(100),
            target_roi=Fraction(1, 20),
            proportional_overhead=Fraction(-1, 100),
        )
    with pytest.raises(ValueError):
        per_unit_entry_target(_usd(100), 0)
    with pytest.raises(ValueError):
        per_unit_entry_target(_usd(-1), 10)
