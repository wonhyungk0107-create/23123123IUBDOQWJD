"""Property tests for the bundle optimizer.

The central claim -- that a Pareto-frontier dynamic program returns the same answer
as exhaustive enumeration -- is checked directly against brute force on small
instances. Everything else in the pipeline trusts this result, so it is worth
proving rather than assuming.
"""

from __future__ import annotations

import itertools
from collections.abc import Mapping, Sequence
from fractions import Fraction

from hypothesis import given
from hypothesis import strategies as st

from tradeup.domain.contracts import ContractComposition
from tradeup.domain.items import Rarity
from tradeup.domain.listings import ListingIdentity
from tradeup.optimizer.bundle import BundleOptimizer
from tradeup.optimizer.pareto import CandidateItem, prune_dominated

RULE = "2026-05-souvenir-covert"


def _item(index: int, collection: str, cost: int, z_twentieths: int) -> CandidateItem:
    return CandidateItem(
        identity=ListingIdentity("t", f"{collection}-{index}"),
        collection_id=collection,
        normalized=Fraction(z_twentieths, 20),
        cost_minor=cost,
    )


def _brute_force_cost(
    counts: Mapping[str, int],
    pools: Mapping[str, Sequence[CandidateItem]],
    budget: Fraction,
) -> int | None:
    options: list[list[tuple[int, Fraction]]] = []
    for collection_id in sorted(counts):
        pool = list(pools[collection_id])
        combos = [
            (sum(i.cost_minor for i in c), sum((i.normalized for i in c), Fraction(0)))
            for c in itertools.combinations(pool, counts[collection_id])
        ]
        if not combos:
            return None
        options.append(combos)
    best: int | None = None
    for pick in itertools.product(*options):
        if sum((p[1] for p in pick), Fraction(0)) > budget:
            continue
        total = sum(p[0] for p in pick)
        if best is None or total < best:
            best = total
    return best


_pool_strategy = st.lists(
    st.tuples(st.integers(min_value=1, max_value=500), st.integers(min_value=0, max_value=20)),
    min_size=1,
    max_size=6,
)


@given(
    raw_pool=_pool_strategy,
    slots=st.integers(min_value=1, max_value=3),
    budget_twentieths=st.integers(min_value=0, max_value=60),
)
def test_single_collection_matches_brute_force(
    raw_pool: list[tuple[int, int]], slots: int, budget_twentieths: int
) -> None:
    pool = [_item(i, "c1", cost, z) for i, (cost, z) in enumerate(raw_pool)]
    if len(pool) < slots:
        return
    budget = Fraction(budget_twentieths, 20)
    composition = ContractComposition(
        input_rarity=Rarity.MIL_SPEC,
        counts_by_collection={"c1": slots},
        rule_version=RULE,
    )
    solution = BundleOptimizer().solve(
        composition, {"c1": pool}, max_average_normalized=budget / slots
    )
    expected = _brute_force_cost({"c1": slots}, {"c1": pool}, budget)

    if expected is None:
        assert solution is None
    else:
        assert solution is not None
        assert solution.total_cost_minor == expected


@given(
    pool_a=_pool_strategy,
    pool_b=_pool_strategy,
    slots_a=st.integers(min_value=1, max_value=2),
    slots_b=st.integers(min_value=1, max_value=2),
    budget_twentieths=st.integers(min_value=0, max_value=60),
)
def test_two_collection_composition_matches_brute_force(
    pool_a: list[tuple[int, int]],
    pool_b: list[tuple[int, int]],
    slots_a: int,
    slots_b: int,
    budget_twentieths: int,
) -> None:
    items_a = [_item(i, "c1", cost, z) for i, (cost, z) in enumerate(pool_a)]
    items_b = [_item(i, "c2", cost, z) for i, (cost, z) in enumerate(pool_b)]
    if len(items_a) < slots_a or len(items_b) < slots_b:
        return
    total_slots = slots_a + slots_b
    budget = Fraction(budget_twentieths, 20)
    composition = ContractComposition(
        input_rarity=Rarity.MIL_SPEC,
        counts_by_collection={"c1": slots_a, "c2": slots_b},
        rule_version=RULE,
    )
    solution = BundleOptimizer().solve(
        composition,
        {"c1": items_a, "c2": items_b},
        max_average_normalized=budget / total_slots,
    )
    expected = _brute_force_cost(
        {"c1": slots_a, "c2": slots_b}, {"c1": items_a, "c2": items_b}, budget
    )

    if expected is None:
        assert solution is None
    else:
        assert solution is not None
        assert solution.total_cost_minor == expected


@given(raw_pool=_pool_strategy, slots=st.integers(min_value=1, max_value=3))
def test_dominated_pruning_preserves_the_optimum(
    raw_pool: list[tuple[int, int]], slots: int
) -> None:
    """Pruning may shrink the pool but must never change the cheapest feasible cost."""
    pool = [_item(i, "c1", cost, z) for i, (cost, z) in enumerate(raw_pool)]
    if len(pool) < slots:
        return
    budget = Fraction(slots)  # loose enough that float is rarely binding
    kept, _ = prune_dominated(pool, keep_at_least=slots)
    assert len(kept) >= slots
    assert _brute_force_cost({"c1": slots}, {"c1": pool}, budget) == _brute_force_cost(
        {"c1": slots}, {"c1": kept}, budget
    )


@given(raw_pool=_pool_strategy, slots=st.integers(min_value=1, max_value=3))
def test_solution_never_reuses_an_asset(raw_pool: list[tuple[int, int]], slots: int) -> None:
    pool = [_item(i, "c1", cost, z) for i, (cost, z) in enumerate(raw_pool)]
    if len(pool) < slots:
        return
    solution = BundleOptimizer().solve(
        ContractComposition(
            input_rarity=Rarity.MIL_SPEC,
            counts_by_collection={"c1": slots},
            rule_version=RULE,
        ),
        {"c1": pool},
        max_average_normalized=Fraction(1),
    )
    if solution is not None:
        assert len(set(solution.selection)) == slots


@given(raw_pool=_pool_strategy, slots=st.integers(min_value=1, max_value=3))
def test_solution_always_respects_the_float_budget(
    raw_pool: list[tuple[int, int]], slots: int
) -> None:
    pool = [_item(i, "c1", cost, z) for i, (cost, z) in enumerate(raw_pool)]
    if len(pool) < slots:
        return
    cap = Fraction(1, 4)
    solution = BundleOptimizer().solve(
        ContractComposition(
            input_rarity=Rarity.MIL_SPEC,
            counts_by_collection={"c1": slots},
            rule_version=RULE,
        ),
        {"c1": pool},
        max_average_normalized=cap,
    )
    if solution is not None:
        assert solution.average_normalized <= cap
