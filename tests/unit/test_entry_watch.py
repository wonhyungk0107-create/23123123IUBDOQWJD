"""The closed decision loop: calibration feedback, entry wiring, alert card.

Everything here runs offline. The batch test injects an empty quote fetcher and
a confirmer that must never be called, so the mandatory suite cannot touch the
network; the calibration rows it seeds are synthetic constants, not market
observations.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from fractions import Fraction

import pytest

from tradeup.config import Settings
from tradeup.discovery.calibration import (
    CalibrationPair,
    applied_haircuts,
    build_calibration_report,
)
from tradeup.discovery.prospects import InputPlan, Prospect, ProspectPolicy
from tradeup.domain.items import QualityType, Rarity, WearCondition
from tradeup.domain.money import BalanceType, Currency, Money
from tradeup.persistence.database import create_database
from tradeup.persistence.models import ProspectConfirmationRow
from tradeup.persistence.repositories import ConfirmationRepository
from tradeup.pipeline.confirm_batch import (
    ConfirmationOutcome,
    _write_entry_alert,
    run_confirmation_batch,
)
from tradeup.valuation.entry_targets import assess_entry

FROZEN_NOW = datetime(2026, 7, 25, 12, 0, 0, tzinfo=UTC)

USD = Currency.USD
CASH = BalanceType.CASH_WITHDRAWABLE


def _usd(minor: int) -> Money:
    return Money(minor, USD, CASH)


def _pair(quality: QualityType) -> CalibrationPair:
    # estimated 200, exact 100 -> value ratio 0.5. Implied haircut, by hand:
    # 1 - 0.08 - 0.5 * (1 - 0.08 - 0.12) = 0.92 - 0.40 = 0.52.
    return CalibrationPair(
        quality=quality,
        estimated_output_value_minor=200,
        exact_output_value_minor=100,
        estimated_roi=Decimal("0.5"),
        exact_roi=Decimal("-0.2"),
        ask_haircut_at_estimate=Decimal("0.12"),
    )


def test_reliable_recommendation_is_applied() -> None:
    report = build_calibration_report([_pair(QualityType.STATTRAK)] * 10)
    applied = applied_haircuts(report, default=Decimal("0.12"))
    assert applied[QualityType.STATTRAK] == Decimal("0.52")
    assert applied[QualityType.NORMAL] == Decimal("0.12")


def test_anecdote_steers_nothing() -> None:
    report = build_calibration_report([_pair(QualityType.STATTRAK)] * 9)
    applied = applied_haircuts(report, default=Decimal("0.12"))
    assert applied[QualityType.STATTRAK] == Decimal("0.12")


def test_haircut_for_prefers_quality_override() -> None:
    policy = ProspectPolicy(
        ask_haircut=Decimal("0.12"),
        ask_haircut_by_quality=((QualityType.STATTRAK, Decimal("0.78")),),
    )
    assert policy.haircut_for(QualityType.STATTRAK) == Decimal("0.78")
    assert policy.haircut_for(QualityType.NORMAL) == Decimal("0.12")


def test_policy_rejects_invalid_quality_haircuts() -> None:
    with pytest.raises(ValueError):
        ProspectPolicy(
            ask_haircut_by_quality=(
                (QualityType.STATTRAK, Decimal("0.5")),
                (QualityType.STATTRAK, Decimal("0.6")),
            )
        )
    with pytest.raises(ValueError):
        ProspectPolicy(ask_haircut_by_quality=((QualityType.NORMAL, Decimal("1")),))


def _synthetic_prospect() -> Prospect:
    return Prospect(
        collection_id="col-test",
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
                unit_price=_usd(12),
            ),
        ),
        average_normalized=Fraction(1, 2),
        estimated_cost=_usd(120),
        estimated_output_value=_usd(140),
        estimated_ev=_usd(20),
        estimated_roi=Decimal("0.1666666666666666666666666667"),
        outcome_count=3,
        unpriced_probability=Fraction(0),
        observed_at=FROZEN_NOW,
        ask_haircut=Decimal("0.52"),
    )


def test_alert_card_states_the_boundaries(settings_obj: Settings) -> None:
    # Observed bundle $1.20 against a $1.40 netted value at a 5% floor:
    # cap 133 (see the golden cases), so entry is met.
    assessment = assess_entry(
        expected_net_output_value=_usd(140),
        observed_acquisition_cost=_usd(120),
        input_count=10,
        target_roi=Decimal("0.05"),
    )
    assert assessment.entry_met
    outcome = ConfirmationOutcome(
        prospect=_synthetic_prospect(),
        status="CONFIRMED",
        listings_found=17,
        candidate_id="TU-abc123def456",
        exact_cost_minor=120,
        exact_output_value_minor=140,
        exact_ev_minor=20,
        exact_roi=Decimal("0.1666666666666666666666666667"),
        rejection_reasons=None,
        detail=None,
        assessment=assessment,
        approved=True,
        input_listings=("csfloat:1 Synthetic Skin (Well-Worn) float=0.42 price=0.12 USD",),
    )
    path = _write_entry_alert([outcome], settings=settings_obj, moment=FROZEN_NOW)
    assert path.exists()
    assert path.parent == settings_obj.artifacts_dir / "alerts"
    text = path.read_text(encoding="utf-8")
    assert "nothing was bought" in text
    assert "TU-abc123def456" in text
    assert "csfloat:1" in text
    assert "human-only" in text
    # Standing buy-order block: name, quantity, the per-unit entry ceiling
    # (cap 133 / 10 inputs = 13 minor = $0.13), and the WELL_WORN float band.
    assert "Standing buy orders" in text
    assert "`Synthetic Skin (Well-Worn)` x10: max price 0.13 USD" in text
    assert "float 0.38-0.45" in text
    assert "re-verify the buyer-side fee" in text


def test_batch_feeds_calibration_back_and_stays_offline(settings_obj: Settings) -> None:
    """Ten recorded StatTrak pairs steer the next sweep's StatTrak haircut;
    an empty catalogue produces no leads, no confirmations and no alert."""
    database = create_database(settings_obj.database_url)
    database.create_all()
    with database.session() as session:
        repository = ConfirmationRepository(session)
        for index in range(10):
            repository.record(
                ProspectConfirmationRow(
                    confirmed_at=FROZEN_NOW,
                    rule_version="2026-05-souvenir-covert",
                    collection_id=f"col-{index}",
                    counts_json={f"col-{index}": 10},
                    input_rarity=Rarity.MIL_SPEC.value,
                    quality=QualityType.STATTRAK.value,
                    input_wear=WearCondition.WELL_WORN.value,
                    estimated_cost_minor=120,
                    estimated_output_value_minor=200,
                    estimated_ev_minor=80,
                    estimated_roi="0.5",
                    estimated_ask_haircut="0.12",
                    status="CONFIRMED",
                    listings_found=17,
                    candidate_id=f"TU-{index:012d}",
                    exact_cost_minor=120,
                    exact_output_value_minor=100,
                    exact_ev_minor=-20,
                    exact_roi="-0.2",
                    rejection_reasons=None,
                    detail=None,
                )
            )
    database.dispose()

    def _never_confirm(
        prospect: Prospect,
        *,
        settings: Settings,
        now: datetime,
        per_name_limit: int,
        max_candidates: int,
        output_dir: object,
    ) -> tuple[str, None, None]:
        raise AssertionError("an empty catalogue must produce no leads to confirm")

    result = run_confirmation_batch(
        settings=settings_obj,
        clock=lambda: FROZEN_NOW,
        quotes_fetcher=lambda settings, moment: {},
        confirm=_never_confirm,
    )
    applied = dict(result.applied_ask_haircuts)
    assert applied[QualityType.STATTRAK] == Decimal("0.52")
    assert applied[QualityType.NORMAL] == Decimal("0.12")
    assert result.outcomes == ()
    assert result.alert_path is None
    assert result.history_rows == 10
    assert result.calibration.has_data
