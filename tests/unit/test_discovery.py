"""Composition enumeration and float breakpoints."""

from __future__ import annotations

from decimal import Decimal
from fractions import Fraction

import pytest

from tradeup.discovery.boundaries import (
    EPSILON,
    candidate_float_budgets,
    wear_breakpoints,
)
from tradeup.discovery.enumerator import (
    CompositionConstraints,
    count_compositions,
    enumerate_compositions,
)
from tradeup.domain.items import FloatRange, Rarity

RULE = "2026-05-souvenir-covert"


def compositions(
    available: dict[str, int],
    *,
    input_count: int = 10,
    constraints: CompositionConstraints | None = None,
) -> list[dict[str, int]]:
    return [
        dict(c.counts_by_collection)
        for c in enumerate_compositions(
            input_rarity=Rarity.MIL_SPEC,
            input_count=input_count,
            available_by_collection=available,
            rule_version=RULE,
            constraints=constraints,
        )
    ]


class TestEnumeration:
    def test_a_single_collection_with_enough_supply_yields_the_pure_shape(self) -> None:
        assert compositions({"col-a": 20}) == [{"col-a": 10}]

    def test_a_collection_without_enough_supply_yields_nothing(self) -> None:
        assert compositions({"col-a": 3}) == []

    def test_two_collections_produce_every_split(self) -> None:
        result = compositions({"col-a": 20, "col-b": 20})
        assert {"col-a": 10} in result
        assert {"col-b": 10} in result
        assert {"col-a": 7, "col-b": 3} in result
        # 2 pure + 9 mixed splits (1..9 from A)
        assert len(result) == 11

    def test_availability_bounds_the_split(self) -> None:
        """Only two listings in B, so B can contribute at most two inputs."""
        result = compositions({"col-a": 20, "col-b": 2})
        assert {"col-a": 9, "col-b": 1} in result
        assert {"col-a": 8, "col-b": 2} in result
        assert not any(c.get("col-b", 0) > 2 for c in result)

    def test_max_distinct_collections_is_respected(self) -> None:
        result = compositions(
            {"col-a": 20, "col-b": 20, "col-c": 20},
            constraints=CompositionConstraints(max_distinct_collections=1),
        )
        assert all(len(c) == 1 for c in result)
        assert len(result) == 3

    def test_three_way_mixes_appear_when_permitted(self) -> None:
        result = compositions(
            {"col-a": 20, "col-b": 20, "col-c": 20},
            constraints=CompositionConstraints(max_distinct_collections=3),
        )
        assert any(len(c) == 3 for c in result)

    def test_minimum_contribution_filters_token_participation(self) -> None:
        result = compositions(
            {"col-a": 20, "col-b": 20},
            constraints=CompositionConstraints(min_inputs_per_collection=3),
        )
        assert all(all(v >= 3 for v in c.values()) for c in result)

    def test_collections_below_the_minimum_are_dropped_entirely(self) -> None:
        result = compositions(
            {"col-a": 20, "col-tiny": 1},
            constraints=CompositionConstraints(min_inputs_per_collection=2),
        )
        assert not any("col-tiny" in c for c in result)

    def test_five_input_contracts_enumerate(self) -> None:
        result = compositions({"col-a": 20, "col-b": 20}, input_count=5)
        assert {"col-a": 5} in result
        assert {"col-a": 3, "col-b": 2} in result

    def test_every_composition_totals_the_input_count(self) -> None:
        for composition in compositions({"col-a": 20, "col-b": 20, "col-c": 20}):
            assert sum(composition.values()) == 10

    def test_enumeration_order_is_deterministic(self) -> None:
        available = {"col-c": 20, "col-a": 20, "col-b": 20}
        assert compositions(available) == compositions(available)

    def test_empty_supply_yields_nothing(self) -> None:
        assert compositions({}) == []

    def test_count_matches_enumeration(self) -> None:
        available = {"col-a": 20, "col-b": 20}
        assert count_compositions(
            input_rarity=Rarity.MIL_SPEC,
            input_count=10,
            available_by_collection=available,
            rule_version=RULE,
        ) == len(compositions(available))

    def test_invalid_input_count_raises(self) -> None:
        with pytest.raises(ValueError, match="must be positive"):
            compositions({"col-a": 20}, input_count=0)

    def test_invalid_constraints_rejected(self) -> None:
        with pytest.raises(ValueError, match="at least 1"):
            CompositionConstraints(max_distinct_collections=0)
        with pytest.raises(ValueError, match="at least 1"):
            CompositionConstraints(min_inputs_per_collection=0)


class TestBreakpoints:
    def test_a_full_range_output_has_every_interior_boundary(self) -> None:
        points = wear_breakpoints([FloatRange(Decimal("0.00"), Decimal("1.00"))])
        assert points == (
            Fraction(7, 100),
            Fraction(15, 100),
            Fraction(38, 100),
            Fraction(45, 100),
        )

    def test_a_narrow_output_has_only_the_boundaries_it_spans(self) -> None:
        points = wear_breakpoints([FloatRange(Decimal("0.10"), Decimal("0.20"))])
        # Only 0.15 lies inside (0.10, 0.20].
        assert points == (Fraction(1, 2),)

    def test_an_output_spanning_no_boundary_has_none(self) -> None:
        assert wear_breakpoints([FloatRange(Decimal("0.16"), Decimal("0.20"))]) == ()

    def test_breakpoints_from_several_outputs_are_merged_and_sorted(self) -> None:
        points = wear_breakpoints(
            [
                FloatRange(Decimal("0.00"), Decimal("1.00")),
                FloatRange(Decimal("0.10"), Decimal("0.20")),
            ]
        )
        assert list(points) == sorted(points)
        assert len(set(points)) == len(points)

    def test_budgets_sit_just_below_each_breakpoint(self) -> None:
        """Landing exactly on 0.07 gives Minimal Wear, not Factory New."""
        budgets = candidate_float_budgets([FloatRange(Decimal("0.00"), Decimal("1.00"))])
        assert Fraction(7, 100) - EPSILON in budgets

    def test_the_unconstrained_budget_is_always_offered(self) -> None:
        """Sometimes the cheapest bundle in the worst wear band is still the winner."""
        budgets = candidate_float_budgets([FloatRange(Decimal("0.00"), Decimal("1.00"))])
        assert Fraction(1) in budgets

    def test_budgets_can_exclude_the_unconstrained_case(self) -> None:
        budgets = candidate_float_budgets(
            [FloatRange(Decimal("0.00"), Decimal("1.00"))], include_unconstrained=False
        )
        assert Fraction(1) not in budgets

    def test_an_output_with_no_boundaries_still_offers_the_open_budget(self) -> None:
        budgets = candidate_float_budgets([FloatRange(Decimal("0.16"), Decimal("0.20"))])
        assert budgets == (Fraction(1),)

    def test_budgets_are_sorted_and_positive(self) -> None:
        budgets = candidate_float_budgets([FloatRange(Decimal("0.00"), Decimal("1.00"))])
        assert list(budgets) == sorted(budgets)
        assert all(b > 0 for b in budgets)
