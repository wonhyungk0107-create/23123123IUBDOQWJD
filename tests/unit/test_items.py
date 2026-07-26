"""Rarity ladder, wear classification, float ranges, market-hash composition."""

from __future__ import annotations

from decimal import Decimal

import pytest

from tradeup.domain.items import (
    Collection,
    FloatRange,
    QualityType,
    Rarity,
    Skin,
    WearCondition,
    classify_wear,
)


class TestRarity:
    def test_ladder_progresses_one_step(self) -> None:
        assert Rarity.MIL_SPEC.next_rarity is Rarity.RESTRICTED
        assert Rarity.COVERT.next_rarity is Rarity.EXTRAORDINARY

    def test_extraordinary_is_terminal(self) -> None:
        assert Rarity.EXTRAORDINARY.next_rarity is None
        assert not Rarity.EXTRAORDINARY.can_be_contract_input

    def test_every_other_rarity_can_be_an_input(self) -> None:
        for rarity in Rarity:
            if rarity is not Rarity.EXTRAORDINARY:
                assert rarity.can_be_contract_input


class TestWearClassification:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("0.00", WearCondition.FACTORY_NEW),
            ("0.0699999", WearCondition.FACTORY_NEW),
            ("0.07", WearCondition.MINIMAL_WEAR),
            ("0.1499999", WearCondition.MINIMAL_WEAR),
            ("0.15", WearCondition.FIELD_TESTED),
            ("0.3799999", WearCondition.FIELD_TESTED),
            ("0.38", WearCondition.WELL_WORN),
            ("0.4499999", WearCondition.WELL_WORN),
            ("0.45", WearCondition.BATTLE_SCARRED),
            ("1.00", WearCondition.BATTLE_SCARRED),
        ],
    )
    def test_boundaries_belong_to_the_higher_wear_band(
        self, value: str, expected: WearCondition
    ) -> None:
        assert classify_wear(Decimal(value)) is expected

    def test_out_of_range_raises(self) -> None:
        with pytest.raises(ValueError, match=r"outside \[0, 1\]"):
            classify_wear(Decimal("1.01"))

    def test_float_is_rejected(self) -> None:
        with pytest.raises(TypeError):
            classify_wear(0.5)  # type: ignore[arg-type]


class TestFloatRange:
    def test_zero_width_range_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="minimum < maximum"):
            FloatRange(Decimal("0.1"), Decimal("0.1"))

    def test_inverted_range_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="minimum < maximum"):
            FloatRange(Decimal("0.5"), Decimal("0.1"))

    def test_bounds_outside_unit_interval_are_rejected(self) -> None:
        with pytest.raises(ValueError, match=r"outside \[0, 1\]"):
            FloatRange(Decimal("0"), Decimal("1.5"))

    def test_float_bounds_are_rejected(self) -> None:
        with pytest.raises(TypeError):
            FloatRange(0.0, Decimal("1"))  # type: ignore[arg-type]

    def test_width_and_membership(self) -> None:
        rng = FloatRange(Decimal("0.10"), Decimal("0.70"))
        assert rng.width == Decimal("0.60")
        assert rng.contains(Decimal("0.10"))
        assert rng.contains(Decimal("0.70"))
        assert not rng.contains(Decimal("0.09"))

    def test_reachable_wears_excludes_unattainable_bands(self) -> None:
        """A 0.45-1.00 skin can only ever be Battle-Scarred."""
        rng = FloatRange(Decimal("0.45"), Decimal("1.00"))
        assert rng.reachable_wears == (WearCondition.BATTLE_SCARRED,)

    def test_reachable_wears_for_a_full_range_skin(self) -> None:
        rng = FloatRange(Decimal("0.00"), Decimal("1.00"))
        assert len(rng.reachable_wears) == 5


class TestSkin:
    def _skin(self, qualities: frozenset[QualityType]) -> Skin:
        return Skin(
            skin_id="skin-ak-redline",
            name="AK-47 | Redline",
            market_hash_base="AK-47 | Redline",
            collection_id="col-huntsman",
            rarity=Rarity.CLASSIFIED,
            float_range=FloatRange(Decimal("0.10"), Decimal("0.70")),
            paint_index=282,
            available_qualities=qualities,
        )

    def test_market_hash_name_is_composed_not_parsed(self) -> None:
        skin = self._skin(frozenset({QualityType.NORMAL, QualityType.STATTRAK}))
        assert skin.market_hash_name(QualityType.NORMAL, WearCondition.FIELD_TESTED) == (
            "AK-47 | Redline (Field-Tested)"
        )
        assert skin.market_hash_name(QualityType.STATTRAK, WearCondition.MINIMAL_WEAR) == (
            "StatTrak™ AK-47 | Redline (Minimal Wear)"
        )

    def test_requesting_an_unavailable_quality_raises(self) -> None:
        skin = self._skin(frozenset({QualityType.NORMAL}))
        with pytest.raises(ValueError, match="does not exist in"):
            skin.market_hash_name(QualityType.SOUVENIR, WearCondition.FACTORY_NEW)

    def test_a_skin_must_have_at_least_one_quality(self) -> None:
        with pytest.raises(ValueError, match="at least one quality"):
            self._skin(frozenset())


def test_collection_requires_an_id() -> None:
    with pytest.raises(ValueError, match="collection_id is required"):
        Collection(collection_id="", name="x", skin_ids=frozenset())
