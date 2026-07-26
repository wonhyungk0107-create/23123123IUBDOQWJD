"""Dominance pruning and cost-versus-float Pareto frontiers.

A listing is only ever interesting for two reasons: it is cheap, and its normalised
float is low. Anything that is beaten on both counts by enough alternatives can be
discarded without affecting the optimum.

The subtlety that makes naive pruning wrong: the contract needs *exactly* k items
from a collection. If listing B is dominated by exactly one listing A, B is still
needed whenever A is also in the bundle. So B may only be discarded when at least k
distinct listings dominate it -- then any solution containing B can be rewritten to
use a dominator that is not already selected, and the rewrite is never worse.

That rule is what :func:`prune_dominated` implements, and it is the property the
optimizer's correctness test pins down.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from fractions import Fraction

from tradeup.domain.listings import ListingIdentity

__all__ = [
    "CandidateItem",
    "FrontierPoint",
    "dominates",
    "merge_frontier",
    "prune_dominated",
    "prune_frontier",
]


@dataclass(frozen=True, slots=True)
class CandidateItem:
    """A listing reduced to the two dimensions the optimizer reasons about."""

    identity: ListingIdentity
    collection_id: str
    normalized: Fraction
    cost_minor: int

    def __post_init__(self) -> None:
        if not (0 <= self.normalized <= 1):
            raise ValueError(f"normalised float {self.normalized} outside [0, 1]")
        if self.cost_minor < 0:
            raise ValueError("cost cannot be negative")


def dominates(a: CandidateItem, b: CandidateItem) -> bool:
    """True when ``a`` is at least as good as ``b`` on both axes, and better on one.

    "Better" for float means *lower*: a lower normalised float leaves more of the
    budget for the rest of the bundle.
    """
    if a.identity == b.identity:
        return False
    no_worse = a.cost_minor <= b.cost_minor and a.normalized <= b.normalized
    strictly_better = a.cost_minor < b.cost_minor or a.normalized < b.normalized
    return no_worse and strictly_better


def prune_dominated(
    items: Sequence[CandidateItem], *, keep_at_least: int
) -> tuple[tuple[CandidateItem, ...], int]:
    """Drop listings dominated by at least ``keep_at_least`` others.

    Returns the surviving items and how many were removed. Pruning fewer than
    ``keep_at_least`` dominators would be unsound for an exactly-k selection.
    """
    if keep_at_least < 1:
        raise ValueError("keep_at_least must be at least 1")
    if len(items) <= keep_at_least:
        return tuple(items), 0

    # Sort by (cost, float) so dominators are encountered before the items they
    # dominate; the running count then only needs one pass per item.
    ordered = sorted(items, key=lambda i: (i.cost_minor, i.normalized, i.identity))
    survivors: list[CandidateItem] = []
    removed = 0
    for index, item in enumerate(ordered):
        dominator_count = 0
        for other in ordered[:index]:
            if dominates(other, item):
                dominator_count += 1
                if dominator_count >= keep_at_least:
                    break
        if dominator_count >= keep_at_least:
            removed += 1
        else:
            survivors.append(item)
    return tuple(survivors), removed


@dataclass(frozen=True, slots=True)
class FrontierPoint:
    """One non-dominated way to fill a fixed number of slots.

    ``selection`` is the exact set of listings, so the optimizer returns assets
    rather than an abstract cost.
    """

    total_normalized: Fraction
    cost_minor: int
    selection: tuple[ListingIdentity, ...]

    @property
    def slot_count(self) -> int:
        return len(self.selection)


def prune_frontier(points: Iterable[FrontierPoint]) -> tuple[FrontierPoint, ...]:
    """Keep only points on the lower-left frontier of (float, cost).

    Sweeping in ascending float order, a point survives only if it is cheaper than
    everything with a smaller or equal float budget.
    """
    ordered = sorted(points, key=lambda p: (p.total_normalized, p.cost_minor))
    survivors: list[FrontierPoint] = []
    best_cost: int | None = None
    for point in ordered:
        if best_cost is None or point.cost_minor < best_cost:
            survivors.append(point)
            best_cost = point.cost_minor
    return tuple(survivors)


def merge_frontier(
    left: Sequence[FrontierPoint],
    right: Sequence[FrontierPoint],
    *,
    budget: Fraction | None = None,
) -> tuple[FrontierPoint, ...]:
    """Combine two independent frontiers (e.g. two collections' selections).

    ``budget`` prunes combinations that already exceed the contract's total float
    allowance, which keeps the frontier small as collections are folded in.
    """
    combined: list[FrontierPoint] = []
    for a in left:
        for b in right:
            total = a.total_normalized + b.total_normalized
            if budget is not None and total > budget:
                continue
            combined.append(
                FrontierPoint(
                    total_normalized=total,
                    cost_minor=a.cost_minor + b.cost_minor,
                    selection=a.selection + b.selection,
                )
            )
    return prune_frontier(combined)
