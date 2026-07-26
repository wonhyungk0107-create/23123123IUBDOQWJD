"""Golden trade-up cases.

Each case in ``tests/fixtures/golden_tradeups.json`` carries hand-computed expected
values. The point is independence: if a refactor changes the float formula or the
probability weighting, these fail even though every other test still agrees with the
new (wrong) implementation.

Both shipped rule versions are exercised, and every case records the rule version it
was computed under -- a golden file that does not say which rules produced it is
worthless the first time the rules change.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest

from tradeup.domain.items import FloatRange, Rarity, WearCondition
from tradeup.domain.mathematics import (
    aggregate_by_skin,
    average_normalized_float,
    normalized_float_exact,
    outcome_probabilities,
    project_output_wear,
)
from tradeup.domain.rules import DEFAULT_RULE_REGISTRY, EligibleOutputPool

FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "golden_tradeups.json"


def _load() -> list[Mapping[str, Any]]:
    document = json.loads(FIXTURE.read_text(encoding="utf-8"), parse_float=Decimal)
    return list(document["cases"])


CASES = _load()
CASE_IDS = [case["id"] for case in CASES]


def _pools(case: Mapping[str, Any]) -> dict[str, EligibleOutputPool]:
    return {
        collection_id: EligibleOutputPool(
            collection_id=collection_id,
            input_rarity=Rarity.MIL_SPEC,
            output_rarity=Rarity.RESTRICTED,
            output_skin_ids=tuple(skin_ids),
        )
        for collection_id, skin_ids in case["pools"].items()
    }


@pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
def test_average_normalized_float_matches_the_worked_example(case: Mapping[str, Any]) -> None:
    normalized = [
        normalized_float_exact(
            Decimal(str(entry["raw_float"])),
            FloatRange(Decimal(str(entry["min_float"])), Decimal(str(entry["max_float"]))),
        )
        for entry in case["inputs"]
    ]
    assert average_normalized_float(normalized) == Fraction(
        str(case["expected_average_normalized"])
    )


@pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
def test_output_float_and_wear_match_the_worked_example(case: Mapping[str, Any]) -> None:
    ruleset = DEFAULT_RULE_REGISTRY.by_version(str(case["rule_version"]))
    average = Fraction(str(case["expected_average_normalized"]))
    output_range = FloatRange(
        Decimal(str(case["output"]["min_float"])), Decimal(str(case["output"]["max_float"]))
    )
    value, wear = project_output_wear(average, output_range, ruleset)
    assert value == Decimal(str(case["expected_output_float"]))
    assert wear is WearCondition(str(case["expected_output_wear"]))


@pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
def test_outcome_probabilities_match_the_worked_example(case: Mapping[str, Any]) -> None:
    ruleset = DEFAULT_RULE_REGISTRY.by_version(str(case["rule_version"]))
    outcomes = outcome_probabilities(
        {k: int(v) for k, v in case["collections"].items()}, _pools(case), ruleset
    )
    aggregated = aggregate_by_skin(outcomes)
    expected = {k: Fraction(str(v)) for k, v in case["expected_probabilities"].items()}
    assert aggregated == expected


@pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
def test_probabilities_sum_to_exactly_one(case: Mapping[str, Any]) -> None:
    ruleset = DEFAULT_RULE_REGISTRY.by_version(str(case["rule_version"]))
    outcomes = outcome_probabilities(
        {k: int(v) for k, v in case["collections"].items()}, _pools(case), ruleset
    )
    assert sum((o.probability for o in outcomes), Fraction(0)) == Fraction(1)


@pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
def test_input_count_matches_the_rule_version(case: Mapping[str, Any]) -> None:
    """A five-input case must be a Covert contract under a ruleset that permits one."""
    ruleset = DEFAULT_RULE_REGISTRY.by_version(str(case["rule_version"]))
    declared = sum(int(v) for v in case["collections"].values())
    assert declared == len(case["inputs"])
    if declared == 5:
        assert ruleset.input_count_for(Rarity.COVERT) == 5
    else:
        assert ruleset.input_count_for(Rarity.MIL_SPEC) == declared


@pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
def test_every_output_float_lands_inside_its_legal_range(case: Mapping[str, Any]) -> None:
    output_range = FloatRange(
        Decimal(str(case["output"]["min_float"])), Decimal(str(case["output"]["max_float"]))
    )
    assert output_range.contains(Decimal(str(case["expected_output_float"])))


def test_the_golden_file_covers_the_required_shapes() -> None:
    """Guard against the fixture quietly losing coverage in a future edit."""
    ids = set(CASE_IDS)
    assert "pure_collection_10_full_range" in ids
    assert "mixed_collection_7_3" in ids
    assert "covert_five_input_contract" in ids
    assert "boundary_float_lands_in_higher_wear_band" in ids
    assert "restricted_input_range_normalisation" in ids
    assert "capped_output_cannot_beat_field_tested" in ids
    assert "external_reference_3_7_worked_example" in ids


def test_legacy_ruleset_still_reproduces_the_ten_input_maths() -> None:
    """The same arithmetic under the older rules, so version drift is visible."""
    legacy = DEFAULT_RULE_REGISTRY.by_version("legacy-10-input")
    pools = {
        "col-a": EligibleOutputPool(
            collection_id="col-a",
            input_rarity=Rarity.MIL_SPEC,
            output_rarity=Rarity.RESTRICTED,
            output_skin_ids=("y1", "y2"),
        )
    }
    outcomes = outcome_probabilities({"col-a": 10}, pools, legacy)
    assert all(o.probability == Fraction(1, 2) for o in outcomes)

    value, wear = project_output_wear(
        Fraction(1, 5), FloatRange(Decimal("0.00"), Decimal("1.00")), legacy
    )
    assert value == Decimal("0.200000000000")
    assert wear is WearCondition.FIELD_TESTED
