"""Standing-order revalidation against the current board.

All prices and leads are synthetic constants. The verdict rules under test:
a standing price at or below today's ceiling is valid; above it is decay; a
lead the current sweep cannot price is caution, never reassurance.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from fractions import Fraction

import pytest

from tradeup.discovery.order_watch import (
    OrderWatchStatus,
    StandingOrder,
    review_standing_orders,
)
from tradeup.discovery.prospects import InputPlan, Prospect
from tradeup.domain.items import QualityType, Rarity, WearCondition
from tradeup.domain.money import BalanceType, Currency, Money

NOW = datetime(2026, 7, 25, 12, 0, 0, tzinfo=UTC)
FLOOR = Decimal("0.05")


def _usd(minor: int) -> Money:
    return Money(minor, Currency.USD, BalanceType.CASH_WITHDRAWABLE)


def _prospect(
    *,
    collection_id: str = "col-genesis",
    quality: QualityType = QualityType.STATTRAK,
    wear: WearCondition = WearCondition.WELL_WORN,
    output_value_minor: int = 140,
    roi: str = "0.1",
) -> Prospect:
    return Prospect(
        collection_id=collection_id,
        collection_name="Synthetic Collection",
        input_rarity=Rarity.MIL_SPEC,
        quality=quality,
        input_wear=wear,
        rule_version="2026-05-souvenir-covert",
        inputs=(
            InputPlan(
                skin_id="skin-a",
                market_hash_name="Synthetic Skin (Well-Worn)",
                units=10,
                unit_price=_usd(12),
            ),
        ),
        average_normalized=Fraction(1, 2),
        estimated_cost=_usd(120),
        estimated_output_value=_usd(output_value_minor),
        estimated_ev=_usd(output_value_minor - 120),
        estimated_roi=Decimal(roi),
        outcome_count=3,
        unpriced_probability=Fraction(0),
        observed_at=NOW,
        ask_haircut=Decimal("0.52"),
    )


def _order(max_price_minor: int) -> StandingOrder:
    return StandingOrder(
        collection_id="col-genesis",
        quality=QualityType.STATTRAK,
        input_wear=WearCondition.WELL_WORN,
        market_hash_name="Synthetic Skin (Well-Worn)",
        units=10,
        max_price=_usd(max_price_minor),
        placed_at=NOW,
        source_candidate_id="TU-abc123def456",
    )


def test_standing_price_within_ceiling_is_valid() -> None:
    # V = 140 at 5% floor -> total cap 133, per-unit 13 (see golden cases).
    verdicts = review_standing_orders([_order(13)], [_prospect()], target_roi=FLOOR)
    assert verdicts[0].status is OrderWatchStatus.STILL_VALID
    assert verdicts[0].current_unit_ceiling == _usd(13)
    assert not verdicts[0].needs_attention


def test_exact_confirmation_beats_the_sweep_estimate() -> None:
    # The first live pilot's shape: the sweep estimate prices the lead
    # optimistically (ceiling far above the standing order), but this batch's
    # exact confirmation put the true unit ceiling at 13 — below the standing
    # 21. The exact figure must win and flag decay.
    generous_estimate = _prospect(output_value_minor=14_000)
    verdicts = review_standing_orders(
        [_order(21)],
        [generous_estimate],
        target_roi=FLOOR,
        confirmed_unit_ceilings={
            ("col-genesis", "STATTRAK", "WELL_WORN"): _usd(13),
        },
    )
    assert verdicts[0].status is OrderWatchStatus.CEILING_DECAYED
    assert verdicts[0].current_unit_ceiling == _usd(13)
    assert "exact confirmation this batch" in verdicts[0].detail


def test_decayed_exits_flag_the_order() -> None:
    # Exits fell to V = 100 -> cap 95, per-unit 9. The standing 13 overpays.
    verdicts = review_standing_orders(
        [_order(13)], [_prospect(output_value_minor=100)], target_roi=FLOOR
    )
    assert verdicts[0].status is OrderWatchStatus.CEILING_DECAYED
    assert verdicts[0].current_unit_ceiling == _usd(9)
    assert verdicts[0].needs_attention
    assert "reprice or cancel" in verdicts[0].detail


def test_best_variant_of_the_lead_is_used() -> None:
    # Two sketches of the same lead; the better-ranked one (higher ROI) sets
    # the ceiling, mirroring lead selection.
    worse = _prospect(output_value_minor=100, roi="0.01")
    better = _prospect(output_value_minor=140, roi="0.1")
    verdicts = review_standing_orders([_order(13)], [worse, better], target_roi=FLOOR)
    assert verdicts[0].status is OrderWatchStatus.STILL_VALID


def test_missing_lead_is_caution_not_reassurance() -> None:
    verdicts = review_standing_orders([_order(13)], [], target_roi=FLOOR)
    assert verdicts[0].status is OrderWatchStatus.LEAD_UNPRICEABLE
    assert verdicts[0].current_unit_ceiling is None
    assert verdicts[0].needs_attention


def test_cross_currency_orders_are_never_compared() -> None:
    order = StandingOrder(
        collection_id="col-genesis",
        quality=QualityType.STATTRAK,
        input_wear=WearCondition.WELL_WORN,
        market_hash_name="Synthetic Skin (Well-Worn)",
        units=10,
        max_price=Money(1300, Currency.EUR, BalanceType.CASH_WITHDRAWABLE),
        placed_at=NOW,
    )
    verdicts = review_standing_orders([order], [_prospect()], target_roi=FLOOR)
    assert verdicts[0].status is OrderWatchStatus.LEAD_UNPRICEABLE
    assert "refusing to compare across" in verdicts[0].detail


def test_order_validation() -> None:
    with pytest.raises(ValueError):
        _order(13).__class__(
            collection_id="c",
            quality=QualityType.NORMAL,
            input_wear=WearCondition.FIELD_TESTED,
            market_hash_name="x",
            units=0,
            max_price=_usd(1),
            placed_at=NOW,
        )
    with pytest.raises(ValueError):
        StandingOrder(
            collection_id="c",
            quality=QualityType.NORMAL,
            input_wear=WearCondition.FIELD_TESTED,
            market_hash_name="x",
            units=1,
            max_price=_usd(-1),
            placed_at=NOW,
        )
    with pytest.raises(ValueError):
        StandingOrder(
            collection_id="c",
            quality=QualityType.NORMAL,
            input_wear=WearCondition.FIELD_TESTED,
            market_hash_name="x",
            units=1,
            max_price=_usd(1),
            placed_at=datetime(2026, 7, 25, 12, 0, 0),
        )
