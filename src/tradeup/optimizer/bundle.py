"""Cheapest-bundle solver.

Problem, for one composition:

    minimise   Σ cost_j · x_j
    subject to Σ x_j                = N
               Σ_{j ∈ c} x_j        = n_c        for every collection c
               Σ normalized_j · x_j ≤ N · z_max
               x_j ∈ {0, 1}                       (one asset, one slot)

The structure that makes this cheap: the collection constraints are *separable*. Each
collection's choice interacts with the others only through the shared float budget.
So we build, per collection, the Pareto frontier of (total float, minimum cost) for
choosing exactly n_c items, then fold the collections together over that one shared
dimension.

Complexity is O(Σ_c m_c · n_c · |frontier|) rather than combinatorial in the number
of listings, and the result is exact -- no heuristic, no sampling. OR-Tools is not
used because nothing here has needed it; that decision is recorded in
docs/decision-log.md and should be revisited only with a measured benchmark.
"""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from fractions import Fraction

from tradeup.domain.contracts import ContractComposition
from tradeup.domain.listings import ListingIdentity
from tradeup.optimizer.pareto import (
    CandidateItem,
    FrontierPoint,
    merge_frontier,
    prune_dominated,
    prune_frontier,
)

__all__ = ["BundleOptimizer", "BundleSolution", "OptimizerExplanation"]


@dataclass(frozen=True)
class OptimizerExplanation:
    """Why this bundle, and what the search actually did.

    Recorded on every solve so a surprising selection can be interrogated without
    re-running the scan.
    """

    composition_signature: str
    float_budget: Fraction
    binding_constraints: tuple[str, ...]
    listings_considered: int
    listings_pruned: int
    frontier_points_examined: int
    search_space_before: str
    search_space_after: str
    runtime_seconds: float
    rejected_alternatives: tuple[str, ...] = field(default_factory=tuple)

    def summary(self) -> dict[str, str]:
        return {
            "composition": self.composition_signature,
            "float_budget": str(self.float_budget),
            "binding_constraints": ",".join(self.binding_constraints) or "none",
            "listings_considered": str(self.listings_considered),
            "listings_pruned": str(self.listings_pruned),
            "frontier_points_examined": str(self.frontier_points_examined),
            "search_space_before": self.search_space_before,
            "search_space_after": self.search_space_after,
            "runtime_seconds": f"{self.runtime_seconds:.6f}",
        }


@dataclass(frozen=True)
class BundleSolution:
    """The cheapest feasible bundle for a composition and float budget."""

    composition: ContractComposition
    selection: tuple[ListingIdentity, ...]
    total_cost_minor: int
    total_normalized: Fraction
    float_budget: Fraction
    explanation: OptimizerExplanation

    @property
    def average_normalized(self) -> Fraction:
        return self.total_normalized / len(self.selection)

    @property
    def float_headroom(self) -> Fraction:
        """Unused float budget. Zero means the float constraint is binding."""
        return self.float_budget - self.total_normalized


def _binomial(n: int, k: int) -> int:
    if k < 0 or k > n:
        return 0
    result = 1
    for i in range(k):
        result = result * (n - i) // (i + 1)
    return result


class BundleOptimizer:
    """Exact cheapest-bundle solver over cost/float Pareto frontiers."""

    def __init__(self, *, max_frontier_points: int = 4096) -> None:
        if max_frontier_points < 1:
            raise ValueError("max_frontier_points must be positive")
        self._max_frontier_points = max_frontier_points

    # -- per-collection dynamic program --------------------------------------

    def _collection_frontier(
        self,
        items: Sequence[CandidateItem],
        slots: int,
        budget: Fraction,
    ) -> tuple[FrontierPoint, ...]:
        """Pareto frontier of (float, cost) for choosing exactly ``slots`` items.

        Standard 0/1 knapsack shape: iterate items once, and for each, extend
        selections of size ``j-1`` into size ``j``. Iterating ``j`` downward is what
        stops an item being used twice.
        """
        if slots > len(items):
            return ()
        levels: list[tuple[FrontierPoint, ...]] = [() for _ in range(slots + 1)]
        levels[0] = (FrontierPoint(Fraction(0), 0, ()),)

        for item in items:
            for j in range(slots, 0, -1):
                source = levels[j - 1]
                if not source:
                    continue
                extended: list[FrontierPoint] = []
                for point in source:
                    total = point.total_normalized + item.normalized
                    if total > budget:
                        # Every later item is no cheaper on float, so this branch is
                        # dead for this point.
                        continue
                    extended.append(
                        FrontierPoint(
                            total_normalized=total,
                            cost_minor=point.cost_minor + item.cost_minor,
                            selection=(*point.selection, item.identity),
                        )
                    )
                if extended:
                    merged = prune_frontier([*levels[j], *extended])
                    levels[j] = merged[: self._max_frontier_points]
        return levels[slots]

    # -- solve ---------------------------------------------------------------

    def solve(
        self,
        composition: ContractComposition,
        items_by_collection: Mapping[str, Sequence[CandidateItem]],
        *,
        max_average_normalized: Fraction,
    ) -> BundleSolution | None:
        """Cheapest bundle satisfying the composition and float budget.

        Returns ``None`` when no feasible bundle exists -- which is a normal, common
        answer, not an error.
        """
        started = time.perf_counter()
        total_slots = composition.total_inputs
        budget = max_average_normalized * total_slots

        considered = 0
        pruned_total = 0
        space_before = 1
        space_after = 1

        per_collection: dict[str, tuple[FrontierPoint, ...]] = {}
        for collection_id, slots in sorted(composition.counts_by_collection.items()):
            raw = list(items_by_collection.get(collection_id, ()))
            considered += len(raw)
            space_before *= _binomial(len(raw), slots)
            if len(raw) < slots:
                return None

            kept, removed = prune_dominated(raw, keep_at_least=slots)
            pruned_total += removed
            space_after *= _binomial(len(kept), slots)

            # Cheapest-first ordering makes the frontier converge sooner.
            ordered = sorted(kept, key=lambda i: (i.cost_minor, i.normalized, i.identity))
            frontier = self._collection_frontier(ordered, slots, budget)
            if not frontier:
                return None
            per_collection[collection_id] = frontier

        # Fold collections together over the shared float budget.
        combined: tuple[FrontierPoint, ...] = (FrontierPoint(Fraction(0), 0, ()),)
        examined = 0
        for collection_id in sorted(per_collection):
            combined = merge_frontier(combined, per_collection[collection_id], budget=budget)[
                : self._max_frontier_points
            ]
            examined += len(combined)
            if not combined:
                return None

        feasible = [p for p in combined if p.total_normalized <= budget]
        if not feasible:
            return None
        best = min(feasible, key=lambda p: (p.cost_minor, p.total_normalized, p.selection))

        binding: list[str] = []
        if best.total_normalized == budget:
            binding.append("float_budget")
        for collection_id, slots in composition.counts_by_collection.items():
            available = len(items_by_collection.get(collection_id, ()))
            if available == slots:
                binding.append(f"supply:{collection_id}")

        explanation = OptimizerExplanation(
            composition_signature=composition.signature,
            float_budget=budget,
            binding_constraints=tuple(binding),
            listings_considered=considered,
            listings_pruned=pruned_total,
            frontier_points_examined=examined,
            search_space_before=f"{space_before:.3e}"
            if space_before > 10**6
            else str(space_before),
            search_space_after=f"{space_after:.3e}" if space_after > 10**6 else str(space_after),
            runtime_seconds=time.perf_counter() - started,
        )

        return BundleSolution(
            composition=composition,
            selection=best.selection,
            total_cost_minor=best.cost_minor,
            total_normalized=best.total_normalized,
            float_budget=budget,
            explanation=explanation,
        )
