"""Collection-composition enumeration.

Enumerating listing combinations is hopeless: ten inputs drawn from a few hundred
listings is astronomically many subsets. Enumerating *collection compositions* is
tractable -- a composition is just "seven from A, three from B" -- and each one fixes
the output universe and the probabilities. The optimizer then finds the cheapest
listings satisfying that shape.

Two constraints keep the space small and honest:

* ``max_distinct_collections`` bounds how many collections a contract may mix. The
  default of 2 is a deliberate, logged limit rather than a search over all subsets:
  with 88 usable collections, allowing 3 would mean ~3.9M shapes per rarity.
* A collection can only contribute as many inputs as we actually have listings for.
  Availability is a hard bound, not a filter applied afterwards.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from itertools import combinations

from tradeup.domain.contracts import ContractComposition
from tradeup.domain.items import Rarity

__all__ = ["CompositionConstraints", "count_compositions", "enumerate_compositions"]


@dataclass(frozen=True, slots=True)
class CompositionConstraints:
    """Bounds on the shapes we are willing to consider."""

    max_distinct_collections: int = 2
    #: Skip shapes where a collection contributes fewer than this many inputs.
    #: A one-input minority collection adds outcomes with tiny probability while
    #: consuming a slot; raising this trades coverage for signal.
    min_inputs_per_collection: int = 1

    def __post_init__(self) -> None:
        if self.max_distinct_collections < 1:
            raise ValueError("max_distinct_collections must be at least 1")
        if self.min_inputs_per_collection < 1:
            raise ValueError("min_inputs_per_collection must be at least 1")


def _partitions(
    total: int,
    caps: Sequence[int],
    minimum: int,
) -> Iterator[tuple[int, ...]]:
    """Ordered compositions of ``total`` into ``len(caps)`` parts within bounds."""
    parts = len(caps)
    if parts == 0:
        return
    if parts == 1:
        if minimum <= total <= caps[0]:
            yield (total,)
        return
    remaining_min = minimum * (parts - 1)
    upper = min(caps[0], total - remaining_min)
    for first in range(minimum, upper + 1):
        for rest in _partitions(total - first, caps[1:], minimum):
            yield (first, *rest)


def enumerate_compositions(
    *,
    input_rarity: Rarity,
    input_count: int,
    available_by_collection: Mapping[str, int],
    rule_version: str,
    constraints: CompositionConstraints | None = None,
) -> Iterator[ContractComposition]:
    """Yield every viable collection shape, in a deterministic order.

    ``available_by_collection`` maps collection id to how many usable listings we
    hold for it at this rarity. Collections with fewer than
    ``min_inputs_per_collection`` listings are dropped before enumeration.
    """
    limits = constraints or CompositionConstraints()
    if input_count < 1:
        raise ValueError("input_count must be positive")

    usable = {
        collection_id: min(count, input_count)
        for collection_id, count in sorted(available_by_collection.items())
        if count >= limits.min_inputs_per_collection
    }
    if not usable:
        return

    collection_ids = sorted(usable)
    max_distinct = min(limits.max_distinct_collections, len(collection_ids), input_count)

    for size in range(1, max_distinct + 1):
        for subset in combinations(collection_ids, size):
            caps = [usable[c] for c in subset]
            if sum(caps) < input_count:
                continue
            for counts in _partitions(input_count, caps, limits.min_inputs_per_collection):
                yield ContractComposition(
                    input_rarity=input_rarity,
                    counts_by_collection=dict(zip(subset, counts, strict=True)),
                    rule_version=rule_version,
                )


def count_compositions(
    *,
    input_rarity: Rarity,
    input_count: int,
    available_by_collection: Mapping[str, int],
    rule_version: str,
    constraints: CompositionConstraints | None = None,
) -> int:
    """Size of the shape space, for logging what the scan actually searched."""
    return sum(
        1
        for _ in enumerate_compositions(
            input_rarity=input_rarity,
            input_count=input_count,
            available_by_collection=available_by_collection,
            rule_version=rule_version,
            constraints=constraints,
        )
    )
