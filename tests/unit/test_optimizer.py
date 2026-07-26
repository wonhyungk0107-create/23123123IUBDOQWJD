"""Bundle optimizer: known optima, dominance-pruning soundness, infeasibility."""

from __future__ import annotations

import itertools
from collections.abc import Mapping, Sequence
from fractions import Fraction

import pytest

from tradeup.domain.contracts import ContractComposition
from tradeup.domain.items import Rarity
from tradeup.domain.listings import ListingIdentity
from tradeup.optimizer.bundle import BundleOptimizer
from tradeup.optimizer.pareto import (
    CandidateItem,
    FrontierPoint,
    dominates,
    prune_dominated,
    prune_frontier,
)

RULE = "2026-05-souvenir-covert"


def item(name: str, collection: str, cost: int, z: Fraction) -> CandidateItem:
    return CandidateItem(
        identity=ListingIdentity("test", name),
        collection_id=collection,
        normalized=z,
        cost_minor=cost,
    )


def brute_force(
    counts: Mapping[str, int],
    items_by_collection: Mapping[str, Sequence[CandidateItem]],
    budget: Fraction,
) -> tuple[int, tuple[ListingIdentity, ...]] | None:
    """Exhaustive reference solver. Only viable for tiny inputs, which is the point."""
    per_collection_options: list[list[tuple[int, Fraction, tuple[ListingIdentity, ...]]]] = []
    for collection_id in sorted(counts):
        slots = counts[collection_id]
        pool = list(items_by_collection.get(collection_id, ()))
        options = [
            (
                sum(i.cost_minor for i in combo),
                sum((i.normalized for i in combo), Fraction(0)),
                tuple(i.identity for i in combo),
            )
            for combo in itertools.combinations(pool, slots)
        ]
        if not options:
            return None
        per_collection_options.append(options)

    best: tuple[int, tuple[ListingIdentity, ...]] | None = None
    for pick in itertools.product(*per_collection_options):
        total_cost = sum(p[0] for p in pick)
        total_z = sum((p[1] for p in pick), Fraction(0))
        if total_z > budget:
            continue
        selection = tuple(sorted(i for p in pick for i in p[2]))
        if best is None or total_cost < best[0]:
            best = (total_cost, selection)
    return best


class TestDominance:
    def test_cheaper_and_lower_float_dominates(self) -> None:
        a = item("a", "c1", 100, Fraction(1, 10))
        b = item("b", "c1", 200, Fraction(2, 10))
        assert dominates(a, b)
        assert not dominates(b, a)

    def test_cheaper_but_worse_float_does_not_dominate(self) -> None:
        a = item("a", "c1", 100, Fraction(5, 10))
        b = item("b", "c1", 200, Fraction(1, 10))
        assert not dominates(a, b)
        assert not dominates(b, a)

    def test_identical_items_do_not_dominate_each_other(self) -> None:
        a = item("a", "c1", 100, Fraction(1, 10))
        assert not dominates(a, a)

    def test_equal_on_both_axes_is_not_domination(self) -> None:
        a = item("a", "c1", 100, Fraction(1, 10))
        b = item("b", "c1", 100, Fraction(1, 10))
        assert not dominates(a, b)


class TestPruningSoundness:
    def test_a_single_dominator_does_not_justify_pruning_when_two_slots_are_needed(self) -> None:
        """The exactly-k trap: B is still needed whenever A is also selected."""
        a = item("a", "c1", 100, Fraction(1, 10))
        b = item("b", "c1", 200, Fraction(2, 10))
        kept, removed = prune_dominated([a, b], keep_at_least=2)
        assert removed == 0
        assert len(kept) == 2

    def test_pruning_applies_once_enough_dominators_exist(self) -> None:
        """With 2 slots, anything beaten by 2 others can always be swapped out.

        ``d2`` is dominated by ``d0`` and ``d1`` on cost at equal float, so it goes
        too -- if ``d2`` were selected, at most one other slot is filled, leaving a
        strictly cheaper dominator free to take its place.
        """
        dominators = [item(f"d{i}", "c1", 100 + i, Fraction(1, 100)) for i in range(3)]
        loser = item("loser", "c1", 900, Fraction(9, 10))
        kept, removed = prune_dominated([*dominators, loser], keep_at_least=2)
        assert removed == 2
        assert {i.identity.listing_id for i in kept} == {"d0", "d1"}

    def test_pruning_never_changes_the_optimum(self) -> None:
        """Brute force over the full pool must match brute force over the pruned pool."""
        pool = [
            item("a", "c1", 500, Fraction(1, 100)),
            item("b", "c1", 100, Fraction(9, 10)),
            item("c", "c1", 120, Fraction(8, 10)),
            item("d", "c1", 130, Fraction(85, 100)),
            item("e", "c1", 900, Fraction(95, 100)),
        ]
        budget = Fraction(18, 10)
        kept, _ = prune_dominated(pool, keep_at_least=2)
        full = brute_force({"c1": 2}, {"c1": pool}, budget)
        pruned = brute_force({"c1": 2}, {"c1": kept}, budget)
        assert full is not None and pruned is not None
        assert full[0] == pruned[0]

    def test_never_prunes_below_the_slot_count(self) -> None:
        items = [item(f"i{i}", "c1", 100, Fraction(1, 10)) for i in range(3)]
        kept, removed = prune_dominated(items, keep_at_least=5)
        assert removed == 0
        assert len(kept) == 3

    def test_keep_at_least_must_be_positive(self) -> None:
        with pytest.raises(ValueError, match="at least 1"):
            prune_dominated([], keep_at_least=0)


class TestFrontier:
    def test_prune_keeps_only_the_lower_left_hull(self) -> None:
        points = [
            FrontierPoint(Fraction(1, 10), 100, (ListingIdentity("t", "a"),)),
            FrontierPoint(Fraction(2, 10), 150, (ListingIdentity("t", "b"),)),  # dominated
            FrontierPoint(Fraction(3, 10), 50, (ListingIdentity("t", "c"),)),
        ]
        kept = prune_frontier(points)
        assert [p.cost_minor for p in kept] == [100, 50]


class TestSolve:
    def _composition(self, counts: dict[str, int]) -> ContractComposition:
        return ContractComposition(
            input_rarity=Rarity.MIL_SPEC,
            counts_by_collection=counts,
            rule_version=RULE,
        )

    def test_picks_the_cheapest_bundle_when_float_is_not_binding(self) -> None:
        pool = [
            item("cheap1", "c1", 100, Fraction(1, 2)),
            item("cheap2", "c1", 110, Fraction(1, 2)),
            item("pricey", "c1", 900, Fraction(1, 100)),
        ]
        optimizer = BundleOptimizer()
        solution = optimizer.solve(
            self._composition({"c1": 2}),
            {"c1": pool},
            max_average_normalized=Fraction(1),
        )
        assert solution is not None
        assert solution.total_cost_minor == 210
        assert sorted(i.listing_id for i in solution.selection) == ["cheap1", "cheap2"]

    def test_float_budget_forces_a_more_expensive_bundle(self) -> None:
        pool = [
            item("cheap1", "c1", 100, Fraction(9, 10)),
            item("cheap2", "c1", 110, Fraction(9, 10)),
            item("low1", "c1", 900, Fraction(1, 100)),
            item("low2", "c1", 950, Fraction(1, 100)),
        ]
        optimizer = BundleOptimizer()
        solution = optimizer.solve(
            self._composition({"c1": 2}),
            {"c1": pool},
            max_average_normalized=Fraction(1, 10),
        )
        assert solution is not None
        assert sorted(i.listing_id for i in solution.selection) == ["low1", "low2"]
        assert solution.average_normalized == Fraction(1, 100)

    def test_mixed_collection_composition(self) -> None:
        c1 = [item(f"a{i}", "c1", 100 + i * 10, Fraction(i + 1, 20)) for i in range(5)]
        c2 = [item(f"b{i}", "c2", 200 + i * 10, Fraction(i + 1, 20)) for i in range(4)]
        composition = self._composition({"c1": 3, "c2": 2})
        budget = Fraction(1, 4)
        optimizer = BundleOptimizer()
        solution = optimizer.solve(composition, {"c1": c1, "c2": c2}, max_average_normalized=budget)
        expected = brute_force({"c1": 3, "c2": 2}, {"c1": c1, "c2": c2}, budget * 5)
        assert expected is not None
        assert solution is not None
        assert solution.total_cost_minor == expected[0]

    def test_returns_none_when_supply_is_short(self) -> None:
        optimizer = BundleOptimizer()
        assert (
            optimizer.solve(
                self._composition({"c1": 5}),
                {"c1": [item("only", "c1", 100, Fraction(1, 10))]},
                max_average_normalized=Fraction(1),
            )
            is None
        )

    def test_returns_none_when_no_bundle_meets_the_float_budget(self) -> None:
        pool = [item(f"h{i}", "c1", 100, Fraction(9, 10)) for i in range(4)]
        optimizer = BundleOptimizer()
        assert (
            optimizer.solve(
                self._composition({"c1": 2}),
                {"c1": pool},
                max_average_normalized=Fraction(1, 100),
            )
            is None
        )

    def test_an_asset_is_never_used_twice(self) -> None:
        pool = [item(f"x{i}", "c1", 100, Fraction(1, 10)) for i in range(4)]
        optimizer = BundleOptimizer()
        solution = optimizer.solve(
            self._composition({"c1": 3}), {"c1": pool}, max_average_normalized=Fraction(1)
        )
        assert solution is not None
        assert len(set(solution.selection)) == 3

    def test_explanation_records_the_search(self) -> None:
        pool = [item(f"x{i}", "c1", 100 + i, Fraction(i + 1, 40)) for i in range(12)]
        optimizer = BundleOptimizer()
        solution = optimizer.solve(
            self._composition({"c1": 4}), {"c1": pool}, max_average_normalized=Fraction(1, 4)
        )
        assert solution is not None
        explanation = solution.explanation
        assert explanation.listings_considered == 12
        assert explanation.composition_signature == "MIL_SPEC:c1=4"
        assert explanation.runtime_seconds >= 0
        assert "float_budget" in explanation.binding_constraints or explanation.float_budget > 0

    def test_float_headroom_is_reported(self) -> None:
        pool = [item(f"x{i}", "c1", 100, Fraction(1, 100)) for i in range(3)]
        optimizer = BundleOptimizer()
        solution = optimizer.solve(
            self._composition({"c1": 2}), {"c1": pool}, max_average_normalized=Fraction(1, 2)
        )
        assert solution is not None
        assert solution.float_headroom == Fraction(1) - Fraction(2, 100)
