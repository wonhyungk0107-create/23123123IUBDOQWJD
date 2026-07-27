"""Property tests for the entry-target inversion.

The contract is exact and two-sided: the returned cap clears the forward ROI
floor, and one more minor unit does not. Both sides are checked against an
independent Fraction-arithmetic model of the forward economics written here,
not against the implementation's own helpers.
"""

from __future__ import annotations

import itertools
from datetime import UTC, datetime
from decimal import Decimal
from fractions import Fraction

import pytest
from hypothesis import given
from hypothesis import strategies as st

from tradeup.domain.fees import FeeOperation, FeeRule, FeeSchedule, UnknownFeeError
from tradeup.domain.money import BalanceType, Currency, Money
from tradeup.valuation.entry_targets import (
    assess_entry,
    max_acquisition_cost,
    max_sticker_price,
    per_unit_entry_target,
)

USD = Currency.USD
CASH = BalanceType.CASH_WITHDRAWABLE
MOMENT = datetime(2026, 7, 25, 12, 0, 0, tzinfo=UTC)
FEE_EPOCH = datetime(2020, 1, 1, tzinfo=UTC)


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


def _tier_rule(
    rule_id: str,
    percentage_hundredths: int,
    fixed_minor: int,
    lower: int,
    upper: int | None,
) -> FeeRule:
    return FeeRule(
        rule_id=rule_id,
        venue="testvenue",
        operation=FeeOperation.PURCHASE,
        balance_type=CASH,
        currency=USD,
        percentage=Decimal(percentage_hundredths) / 100,
        fixed_minor=fixed_minor,
        effective_from=FEE_EPOCH,
        effective_until=None,
        source="synthetic test rule -- NOT a real venue fee",
        last_verified=None,
        tier_lower_minor=lower,
        tier_upper_minor=upper,
    )


@st.composite
def _tiered_schedules(draw: st.DrawFn) -> FeeSchedule:
    """Non-overlapping tiered purchase-fee schedules, possibly with a gap.

    Tiers are built from sorted cut points so overlap is impossible by
    construction; dropping one tier (or the only tier) exercises the coverage
    gap and no-coverage paths.
    """
    cuts = draw(st.lists(st.integers(min_value=2, max_value=2999), max_size=2, unique=True))
    edges: list[int] = [0, *sorted(cuts)]
    tiers: list[tuple[int, int | None]] = [
        *itertools.pairwise(edges),
        (edges[-1], None),
    ]
    dropped = draw(st.integers(min_value=-1, max_value=len(tiers) - 1))
    rules = [
        _tier_rule(
            f"tier-{index}",
            draw(st.integers(min_value=0, max_value=30)),
            draw(st.integers(min_value=0, max_value=50)),
            lower,
            upper,
        )
        for index, (lower, upper) in enumerate(tiers)
        if index != dropped
    ]
    return FeeSchedule(rules)


def _brute_force_sticker(cap_minor: int, schedule: FeeSchedule) -> int | None:
    """Exhaustively scan every sticker in range. ``None`` means no coverage."""
    best = 0
    covered = False
    for sticker in range(1, cap_minor + 1):
        basis = _usd(sticker)
        try:
            fee = schedule.quote("testvenue", FeeOperation.PURCHASE, basis, MOMENT).fee
        except UnknownFeeError:
            continue
        covered = True
        if sticker + fee.minor_units <= cap_minor:
            best = max(best, sticker)
    return best if covered else None


@given(schedule=_tiered_schedules(), cap=st.integers(min_value=0, max_value=2500))
def test_sticker_inversion_matches_brute_force(schedule: FeeSchedule, cap: int) -> None:
    if cap < 1:
        assert max_sticker_price(
            unit_acquisition_cap=_usd(cap),
            venue="testvenue",
            fee_schedule=schedule,
            moment=MOMENT,
        ).is_zero
        return
    expected = _brute_force_sticker(cap, schedule)
    if expected is None:
        with pytest.raises(UnknownFeeError):
            max_sticker_price(
                unit_acquisition_cap=_usd(cap),
                venue="testvenue",
                fee_schedule=schedule,
                moment=MOMENT,
            )
        return
    result = max_sticker_price(
        unit_acquisition_cap=_usd(cap),
        venue="testvenue",
        fee_schedule=schedule,
        moment=MOMENT,
    )
    assert result.minor_units == expected


@given(
    value=values,
    fixed=fixed_overheads,
    target=targets,
    observed=st.integers(min_value=1, max_value=10**9),
    count=st.integers(1, 10),
)
def test_entry_assessment_agrees_with_forward_model(
    value: int, fixed: int, target: Fraction, observed: int, count: int
) -> None:
    """entry_met must equal the independent forward check at the observed
    spend, and shortfall must be zero exactly when entry is met."""
    assessment = assess_entry(
        expected_net_output_value=_usd(value),
        observed_acquisition_cost=_usd(observed),
        input_count=count,
        target_roi=target,
        fixed_overhead=_usd(fixed),
    )
    assert assessment.entry_met == _forward_clears(
        observed, value_minor=value, fixed_minor=fixed, target=target, overhead=Fraction(0)
    )
    assert assessment.entry_met == assessment.shortfall.is_zero
    assert not assessment.shortfall.is_negative
    assert (
        assessment.observed_total.minor_units - assessment.shortfall.minor_units
        <= assessment.target_total.minor_units
    )
    assert assessment.target_unit == per_unit_entry_target(assessment.target_total, count)


def test_entry_assessment_rejects_non_positive_observed_cost() -> None:
    with pytest.raises(ValueError):
        assess_entry(
            expected_net_output_value=_usd(1000),
            observed_acquisition_cost=_usd(0),
            input_count=10,
            target_roi=Fraction(1, 20),
        )


def test_overlapping_tiers_are_rejected() -> None:
    schedule = FeeSchedule(
        [
            _tier_rule("rule-a", 2, 0, 0, None),
            _tier_rule("rule-b", 5, 0, 0, None),
        ]
    )
    with pytest.raises(UnknownFeeError):
        max_sticker_price(
            unit_acquisition_cap=_usd(1000),
            venue="testvenue",
            fee_schedule=schedule,
            moment=MOMENT,
        )
