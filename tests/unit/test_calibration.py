"""Calibration mathematics and batch helpers.

Golden pair, hand-computed: estimate valued outputs at 1000 minor under an 8%
sale fee and 12% haircut; the confirmation found 115 minor. r = 0.115, and the
haircut that would have matched: 1 - 0.08 - 0.115 * (1 - 0.08 - 0.12) = 0.828.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from fractions import Fraction
from pathlib import Path

import pytest
from tests.factories import NOW

from tradeup.discovery.calibration import (
    CalibrationPair,
    build_calibration_report,
)
from tradeup.discovery.prospects import Prospect
from tradeup.domain.items import QualityType, Rarity, WearCondition
from tradeup.domain.money import BalanceType, Currency, Money
from tradeup.persistence.database import create_database
from tradeup.persistence.models import ProspectConfirmationRow
from tradeup.persistence.repositories import ConfirmationRepository
from tradeup.pipeline.confirm_batch import calibration_pairs, select_leads


def usd(minor: int) -> Money:
    return Money(minor, Currency.USD, BalanceType.CASH_WITHDRAWABLE)


def pair(
    *,
    quality: QualityType = QualityType.STATTRAK,
    estimated: int = 1_000,
    exact: int = 115,
    est_roi: str = "5.28",
    exact_roi: str = "-0.2775",
    haircut: str = "0.12",
) -> CalibrationPair:
    return CalibrationPair(
        quality=quality,
        estimated_output_value_minor=estimated,
        exact_output_value_minor=exact,
        estimated_roi=Decimal(est_roi),
        exact_roi=Decimal(exact_roi),
        ask_haircut_at_estimate=Decimal(haircut),
    )


class TestCalibrationPair:
    def test_the_golden_pair_implies_the_hand_computed_haircut(self) -> None:
        golden = pair()
        assert golden.value_ratio == Decimal("0.115")
        assert golden.implied_haircut == Decimal("0.828")
        assert golden.roi_gap == Decimal("5.5575")

    def test_an_exact_value_above_the_estimate_clamps_at_zero_haircut(self) -> None:
        assert pair(exact=2_000).implied_haircut == Decimal(0)

    def test_a_worthless_confirmation_clamps_at_the_maximum_haircut(self) -> None:
        assert pair(exact=0).implied_haircut == Decimal("0.92")

    def test_invalid_pairs_are_rejected(self) -> None:
        with pytest.raises(ValueError, match="positive estimated"):
            pair(estimated=0)
        with pytest.raises(ValueError, match="cannot be negative"):
            pair(exact=-1)


class TestCalibrationReport:
    def test_no_pairs_means_no_data_not_a_fabricated_zero(self) -> None:
        report = build_calibration_report(())
        assert not report.has_data
        assert report.by_quality == ()

    def test_qualities_are_calibrated_separately(self) -> None:
        report = build_calibration_report(
            [
                pair(quality=QualityType.STATTRAK, exact=115),
                pair(quality=QualityType.NORMAL, exact=800),
            ]
        )
        assert report.has_data
        by_quality = {entry.quality: entry for entry in report.by_quality}
        assert by_quality[QualityType.STATTRAK].median_value_ratio == Decimal("0.115")
        assert by_quality[QualityType.NORMAL].median_value_ratio == Decimal("0.8")
        assert not by_quality[QualityType.NORMAL].reliable  # one sample is an anecdote

    def test_even_count_medians_pick_the_conservative_side(self) -> None:
        report = build_calibration_report([pair(exact=115), pair(exact=800)])
        assert report.overall is not None
        # Value ratio: the SMALLER middle value; haircut: the LARGER middle value.
        assert report.overall.median_value_ratio == Decimal("0.115")
        assert report.overall.recommended_ask_haircut == Decimal("0.828")

    def test_ten_samples_make_a_reliable_recommendation(self) -> None:
        report = build_calibration_report([pair() for _ in range(10)])
        assert report.overall is not None
        assert report.overall.reliable


def lead(
    collection: str,
    *,
    roi: str = "0.50",
    quality: QualityType = QualityType.NORMAL,
    wear: WearCondition = WearCondition.FIELD_TESTED,
) -> Prospect:
    return Prospect(
        collection_id=collection,
        collection_name=collection,
        input_rarity=Rarity.MIL_SPEC,
        quality=quality,
        input_wear=wear,
        rule_version="test",
        inputs=(),
        average_normalized=Fraction(1, 4),
        estimated_cost=usd(1_000),
        estimated_output_value=usd(1_500),
        estimated_ev=usd(500),
        estimated_roi=Decimal(roi),
        outcome_count=2,
        unpriced_probability=Fraction(0),
        observed_at=NOW,
        ask_haircut=Decimal("0.12"),
        counts_by_collection=((collection, 10),),
    )


class TestSelectLeads:
    def test_deduplicates_variants_of_the_same_market_reality(self) -> None:
        prospects = [
            lead("col-a", roi="0.90"),
            lead("col-a", roi="0.80"),  # a filler dilution of the same lead
            lead("col-b", roi="0.70"),
        ]
        chosen = select_leads(prospects, top=5, min_estimated_roi=Decimal(0))
        assert [p.collection_id for p in chosen] == ["col-a", "col-b"]
        assert chosen[0].estimated_roi == Decimal("0.90")

    def test_respects_the_roi_floor_and_the_cap(self) -> None:
        prospects = [
            lead("col-a", roi="0.90"),
            lead("col-b", roi="0.50"),
            lead("col-c", roi="-0.10"),
        ]
        chosen = select_leads(prospects, top=1, min_estimated_roi=Decimal(0))
        assert [p.collection_id for p in chosen] == ["col-a"]


class TestConfirmationPersistence:
    def _row(self, status: str = "CONFIRMED") -> ProspectConfirmationRow:
        return ProspectConfirmationRow(
            confirmed_at=datetime(2026, 7, 26, 12, 0, 0, tzinfo=UTC),
            rule_version="test",
            collection_id="col-a",
            counts_json={"col-a": 10},
            input_rarity=Rarity.MIL_SPEC.value,
            quality=QualityType.STATTRAK.value,
            input_wear=WearCondition.WELL_WORN.value,
            estimated_cost_minor=210,
            estimated_output_value_minor=1_000,
            estimated_ev_minor=790,
            estimated_roi="5.28",
            estimated_ask_haircut="0.12",
            status=status,
            listings_found=15,
            candidate_id="TU-test" if status == "CONFIRMED" else None,
            exact_cost_minor=209 if status == "CONFIRMED" else None,
            exact_output_value_minor=115 if status == "CONFIRMED" else None,
            exact_ev_minor=-58 if status == "CONFIRMED" else None,
            exact_roi="-0.2775" if status == "CONFIRMED" else None,
            rejection_reasons="BELOW_DISCOVERY_ROI" if status == "CONFIRMED" else None,
            detail=None,
        )

    def test_rows_round_trip_and_only_confirmed_pairs_calibrate(self, tmp_path: Path) -> None:
        database = create_database(f"sqlite+pysqlite:///{(tmp_path / 'c.db').as_posix()}")
        database.create_all()
        with database.session() as session:
            repository = ConfirmationRepository(session)
            repository.record(self._row("CONFIRMED"))
            repository.record(self._row("NO_LISTINGS"))
            rows = repository.all_rows()
        database.dispose()

        assert len(rows) == 2
        pairs = calibration_pairs(rows)
        assert len(pairs) == 1
        assert pairs[0].value_ratio == Decimal("0.115")
        assert pairs[0].quality is QualityType.STATTRAK
