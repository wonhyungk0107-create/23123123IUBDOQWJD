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

from tradeup.discovery.prospects import InputPlan, Prospect
from tradeup.domain.items import QualityType, Rarity, WearCondition
from tradeup.domain.money import BalanceType, Currency, Money
from tradeup.valuation.entry_targets import max_acquisition_cost, per_unit_entry_target

USD = Currency.USD
CASH = BalanceType.CASH_WITHDRAWABLE


def _usd(minor: int) -> Money:
    return Money(minor, USD, CASH)


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
    )
    assert prospect.input_count == 10
    assert prospect.entry_target_cost(Decimal("0.05")) == _usd(133)
    assert prospect.entry_target_unit_cost(Decimal("0.05")) == _usd(13)
