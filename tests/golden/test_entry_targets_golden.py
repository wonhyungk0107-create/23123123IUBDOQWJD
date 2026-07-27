"""Hand-computed entry-target cases.

Every expected value below is derived by hand in the accompanying comment, per
the golden-case rule in ``.claude/rules/money-and-math.md``: values are never
copied from the implementation. The cases live here rather than in
``tests/fixtures/golden_tradeups.json`` because that fixture's schema pins the
float and probability engines; these pin the money inversion.

All dollar figures are synthetic test constants, not market observations.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from fractions import Fraction

import pytest

from tradeup.discovery.prospects import InputPlan, Prospect
from tradeup.domain.fees import FeeOperation, FeeRule, FeeSchedule, UnknownFeeError
from tradeup.domain.items import QualityType, Rarity, WearCondition
from tradeup.domain.money import BalanceType, Currency, Money
from tradeup.valuation.entry_targets import (
    max_acquisition_cost,
    max_sticker_price,
    per_unit_entry_target,
)

USD = Currency.USD
CASH = BalanceType.CASH_WITHDRAWABLE
MOMENT = datetime(2026, 7, 25, 12, 0, 0, tzinfo=UTC)
_FEE_EPOCH = datetime(2020, 1, 1, tzinfo=UTC)


def _usd(minor: int) -> Money:
    return Money(minor, USD, CASH)


def _rule(
    rule_id: str,
    percentage: str,
    *,
    operation: FeeOperation = FeeOperation.PURCHASE,
    fixed_minor: int = 0,
    lower: int = 0,
    upper: int | None = None,
) -> FeeRule:
    return FeeRule(
        rule_id=rule_id,
        venue="testvenue",
        operation=operation,
        balance_type=CASH,
        currency=USD,
        percentage=Decimal(percentage),
        fixed_minor=fixed_minor,
        effective_from=_FEE_EPOCH,
        effective_until=None,
        source="synthetic test rule -- NOT a real venue fee",
        last_verified=None,
        tier_lower_minor=lower,
        tier_upper_minor=upper,
    )


def test_five_percent_floor_plain() -> None:
    # V = $14.00 = 1400 minor, r = 5%. Largest A with A * 21/20 <= 1400:
    # 1333 * 1.05 = 1399.65 <= 1400, and 1334 * 1.05 = 1400.70 > 1400.
    # Cap $13.33; per-unit over ten inputs: floor(1333 / 10) = 133 = $1.33.
    cap = max_acquisition_cost(expected_net_output_value=_usd(1400), target_roi=Decimal("0.05"))
    assert cap == _usd(1333)
    assert per_unit_entry_target(cap, 10) == _usd(133)


def test_five_percent_floor_small_contract() -> None:
    # The recalibrated-entry arithmetic at pocket scale: V = $1.40 = 140 minor,
    # r = 5%. 133 * 1.05 = 139.65 <= 140; 134 * 1.05 = 140.70 > 140.
    # Cap $1.33; per-unit over ten inputs: floor(133 / 10) = 13 = $0.13.
    cap = max_acquisition_cost(expected_net_output_value=_usd(140), target_roi=Decimal("0.05"))
    assert cap == _usd(133)
    assert per_unit_entry_target(cap, 10) == _usd(13)


def test_loaded_model_with_fixed_and_proportional_overheads() -> None:
    # V = $20.00 = 2000, F = $1.00 = 100, q = 2% = 1/50, r = 10%.
    # Headroom = 1900. Constraint: 1.1 * A + ceil(A / 50) <= 1900.
    #   A = 1696: 1.1 * 1696 = 1865.6; ceil(1696 / 50) = ceil(33.92) = 34;
    #             1865.6 + 34 = 1899.6 <= 1900  -> clears.
    #   A = 1697: 1.1 * 1697 = 1866.7; ceil(1697 / 50) = ceil(33.94) = 34;
    #             1866.7 + 34 = 1900.7 > 1900   -> fails.
    # Cap $16.96. The closed form 1900 / (1.1 + 0.02) = 1696.43 rounds to the
    # same integer here, but the ceiling on q * A is what the check must obey.
    cap = max_acquisition_cost(
        expected_net_output_value=_usd(2000),
        target_roi=Decimal("0.10"),
        fixed_overhead=_usd(100),
        proportional_overhead=Decimal("0.02"),
    )
    assert cap == _usd(1696)


def test_exact_division_boundary() -> None:
    # V = $2.10 = 210, r = 5%. 210 / 1.05 = 200 exactly:
    # 200 * 1.05 = 210 <= 210 (boundary holds with equality);
    # 201 * 1.05 = 211.05 > 210. Cap $2.00.
    cap = max_acquisition_cost(expected_net_output_value=_usd(210), target_roi=Decimal("0.05"))
    assert cap == _usd(200)


def test_negative_floor_allows_spending_above_value() -> None:
    # A floor of -50% tolerates losing half the spend. V = $1.00 = 100:
    # (100 - 200) / 200 = -0.5 >= -0.5 at A = 200 exactly;
    # (100 - 201) / 201 = -0.5024... < -0.5. Cap $2.00.
    cap = max_acquisition_cost(expected_net_output_value=_usd(100), target_roi=Decimal("-0.5"))
    assert cap == _usd(200)


def test_value_too_small_for_any_spend() -> None:
    # V = $0.01 = 1 minor, r = 5%. Even one minor unit fails:
    # 1 * 1.05 = 1.05 > 1. Cap is zero: no positive spend clears the floor.
    cap = max_acquisition_cost(expected_net_output_value=_usd(1), target_roi=Decimal("0.05"))
    assert cap.is_zero


def test_prospect_entry_target_methods() -> None:
    # A synthetic ten-input sketch whose netted output value is $1.40. The
    # methods must reproduce test_five_percent_floor_small_contract through
    # the Prospect surface: cap $1.33, per-unit $0.13.
    prospect = Prospect(
        collection_id="col-genesis",
        collection_name="Synthetic Collection",
        input_rarity=Rarity.MIL_SPEC,
        quality=QualityType.STATTRAK,
        input_wear=WearCondition.WELL_WORN,
        rule_version="2026-05-souvenir-covert",
        inputs=(
            InputPlan(
                skin_id="skin-a",
                market_hash_name="Synthetic Skin (Well-Worn)",
                units=10,
                unit_price=_usd(24),
            ),
        ),
        average_normalized=Fraction(1, 2),
        estimated_cost=_usd(240),
        estimated_output_value=_usd(140),
        estimated_ev=_usd(-100),
        estimated_roi=Decimal("-0.4166666666666666666666666667"),
        outcome_count=3,
        unpriced_probability=Fraction(0),
        observed_at=datetime(2026, 7, 25, 12, 0, 0, tzinfo=UTC),
        ask_haircut=Decimal("0.12"),
    )
    assert prospect.input_count == 10
    assert prospect.entry_target_cost(Decimal("0.05")) == _usd(133)
    assert prospect.entry_target_unit_cost(Decimal("0.05")) == _usd(13)


def test_sticker_flat_percentage() -> None:
    # 2% purchase fee, cap $10.00 = 1000. Largest s with s + ceil(0.02 s) <= 1000:
    #   s = 980: 980 + ceil(19.60) = 980 + 20 = 1000 <= 1000  -> fits.
    #   s = 981: 981 + ceil(19.62) = 981 + 20 = 1001 > 1000   -> fails.
    # Sticker target $9.80.
    schedule = FeeSchedule([_rule("flat", "0.02")])
    sticker = max_sticker_price(
        unit_acquisition_cap=_usd(1000), venue="testvenue", fee_schedule=schedule, moment=MOMENT
    )
    assert sticker == _usd(980)


def test_sticker_percentage_plus_fixed() -> None:
    # 2% + $0.30 fixed, cap $10.00. s + ceil(0.02 s) + 30 <= 1000:
    #   s = 950: 950 + ceil(19.00) + 30 = 950 + 19 + 30 = 999 <= 1000  -> fits.
    #   s = 951: 951 + ceil(19.02) + 30 = 951 + 20 + 30 = 1001 > 1000  -> fails.
    # Sticker target $9.50. (The ceiling makes the loaded cost jump by two
    # minor units at 951, which is why 999 is the closest reachable total.)
    schedule = FeeSchedule([_rule("flatfix", "0.02", fixed_minor=30)])
    sticker = max_sticker_price(
        unit_acquisition_cap=_usd(1000), venue="testvenue", fee_schedule=schedule, moment=MOMENT
    )
    assert sticker == _usd(950)


def test_sticker_cross_tier_non_monotone() -> None:
    # Two tiers: 5% below $5.00, 2% at and above it. Cap $5.20 = 520.
    # Lower tier [1, 499]:  s = 495: 495 + ceil(24.75) = 495 + 25 = 520 -> fits;
    #                       s = 496: 496 + ceil(24.80) = 521 -> fails. Tier best 495.
    # Upper tier [500, ..]: s = 509: 509 + ceil(10.18) = 509 + 11 = 520 -> fits;
    #                       s = 510: 510 + ceil(10.20) = 521 -> fails. Tier best 509.
    # Affordability is non-monotone -- $4.96 fails while $5.00 fits
    # (500 + ceil(10.00) = 510 <= 520) -- and the answer is the cross-tier
    # maximum: $5.09.
    schedule = FeeSchedule(
        [
            _rule("small", "0.05", upper=500),
            _rule("large", "0.02", lower=500),
        ]
    )
    sticker = max_sticker_price(
        unit_acquisition_cap=_usd(520), venue="testvenue", fee_schedule=schedule, moment=MOMENT
    )
    assert sticker == _usd(509)


def test_sticker_partial_coverage_uses_covered_range_only() -> None:
    # Only stickers below $1.00 have a sourced fee (5%). Cap $50.00. The
    # uncovered range above is not assumed cheap; the answer comes from the
    # covered tier: s = 99: 99 + ceil(4.95) = 99 + 5 = 104 <= 5000. Target $0.99.
    schedule = FeeSchedule([_rule("small-only", "0.05", upper=100)])
    sticker = max_sticker_price(
        unit_acquisition_cap=_usd(5000), venue="testvenue", fee_schedule=schedule, moment=MOMENT
    )
    assert sticker == _usd(99)


def test_sticker_no_coverage_fails_closed() -> None:
    # No purchase rule for the venue at all: affordability is unknowable, and
    # unknowable must never read as "free". UnknownFeeError, not zero.
    schedule = FeeSchedule([_rule("sale-only", "0.02", operation=FeeOperation.SALE)])
    with pytest.raises(UnknownFeeError):
        max_sticker_price(
            unit_acquisition_cap=_usd(1000),
            venue="testvenue",
            fee_schedule=schedule,
            moment=MOMENT,
        )


def test_sticker_multiple_operations_sum() -> None:
    # Purchase 2% and deposit 1%, both quoted on the sticker. Cap $10.00:
    #   s = 970: 970 + ceil(19.40) + ceil(9.70) = 970 + 20 + 10 = 1000 -> fits.
    #   s = 971: 971 + ceil(19.42) + ceil(9.71) = 971 + 20 + 10 = 1001 -> fails.
    # Sticker target $9.70.
    schedule = FeeSchedule(
        [
            _rule("buy", "0.02"),
            _rule("fund", "0.01", operation=FeeOperation.DEPOSIT),
        ]
    )
    sticker = max_sticker_price(
        unit_acquisition_cap=_usd(1000),
        venue="testvenue",
        fee_schedule=schedule,
        moment=MOMENT,
        operations=(FeeOperation.PURCHASE, FeeOperation.DEPOSIT),
    )
    assert sticker == _usd(970)


def test_sticker_zero_cap_is_zero() -> None:
    # Nothing positive fits under a zero cap regardless of the schedule.
    schedule = FeeSchedule([_rule("flat2", "0.02")])
    sticker = max_sticker_price(
        unit_acquisition_cap=_usd(0), venue="testvenue", fee_schedule=schedule, moment=MOMENT
    )
    assert sticker.is_zero
