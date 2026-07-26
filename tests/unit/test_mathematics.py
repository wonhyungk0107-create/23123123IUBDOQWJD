"""Float normalisation, output projection, and outcome probabilities."""

from __future__ import annotations

from decimal import Decimal
from fractions import Fraction

import pytest

from tradeup.domain.items import FloatRange, Rarity, WearCondition
from tradeup.domain.mathematics import (
    MathematicsError,
    aggregate_by_skin,
    average_normalized_float,
    normalized_float,
    normalized_float_exact,
    outcome_probabilities,
    project_output_float,
    project_output_wear,
    to_decimal,
)
from tradeup.domain.rules import (
    RULESET_2026_05,
    EligibleOutputPool,
    FloatMethod,
    ProbabilityMethod,
    RuleValidationStatus,
    RuleViolation,
    TradeupRuleSet,
)

FULL = FloatRange(Decimal("0.00"), Decimal("1.00"))
REDLINE = FloatRange(Decimal("0.10"), Decimal("0.70"))
RULES = RULESET_2026_05


def pool(collection_id: str, *skin_ids: str) -> EligibleOutputPool:
    return EligibleOutputPool(
        collection_id=collection_id,
        input_rarity=Rarity.MIL_SPEC,
        output_rarity=Rarity.RESTRICTED,
        output_skin_ids=skin_ids,
    )


class TestNormalization:
    def test_full_range_skin_normalises_to_itself(self) -> None:
        assert normalized_float(Decimal("0.20"), FULL) == Decimal("0.200000000000")

    def test_restricted_range_skin(self) -> None:
        # (0.25 - 0.10) / (0.70 - 0.10) = 0.15 / 0.60 = 0.25
        assert normalized_float_exact(Decimal("0.25"), REDLINE) == Fraction(1, 4)

    def test_endpoints_map_to_zero_and_one(self) -> None:
        assert normalized_float_exact(Decimal("0.10"), REDLINE) == Fraction(0)
        assert normalized_float_exact(Decimal("0.70"), REDLINE) == Fraction(1)

    def test_float_below_the_skin_minimum_raises(self) -> None:
        with pytest.raises(MathematicsError, match="outside skin range"):
            normalized_float(Decimal("0.05"), REDLINE)

    def test_float_above_the_skin_maximum_raises(self) -> None:
        """A listing claiming an impossible float is a data fault, not a bargain."""
        with pytest.raises(MathematicsError, match="outside skin range"):
            normalized_float(Decimal("0.99"), REDLINE)

    def test_binary_float_input_is_rejected(self) -> None:
        with pytest.raises(TypeError):
            normalized_float(0.25, REDLINE)  # type: ignore[arg-type]


class TestAveraging:
    def test_mean_is_exact(self) -> None:
        values = [Fraction(1, 3), Fraction(1, 3), Fraction(1, 3)]
        assert average_normalized_float(values) == Fraction(1, 3)

    def test_order_does_not_change_the_mean(self) -> None:
        values = [Fraction(1, 7), Fraction(2, 5), Fraction(9, 11)]
        assert average_normalized_float(values) == average_normalized_float(values[::-1])

    def test_empty_input_raises(self) -> None:
        with pytest.raises(MathematicsError, match="empty set of inputs"):
            average_normalized_float([])

    def test_out_of_range_normalised_value_raises(self) -> None:
        with pytest.raises(MathematicsError, match=r"outside \[0, 1\]"):
            average_normalized_float([Fraction(3, 2)])


class TestOutputProjection:
    def test_worked_example(self) -> None:
        # z̄ = 0.25, output range [0.06, 0.80] -> 0.06 + 0.25*0.74 = 0.245
        output_range = FloatRange(Decimal("0.06"), Decimal("0.80"))
        assert project_output_float(Fraction(1, 4), output_range, RULES) == Decimal(
            "0.245000000000"
        )

    def test_projection_reaches_both_endpoints(self) -> None:
        assert project_output_float(Fraction(0), REDLINE, RULES) == Decimal("0.100000000000")
        assert project_output_float(Fraction(1), REDLINE, RULES) == Decimal("0.700000000000")

    def test_output_wear_is_derived_from_the_projected_float(self) -> None:
        output_range = FloatRange(Decimal("0.00"), Decimal("0.80"))
        value, wear = project_output_wear(Fraction(1, 5), output_range, RULES)
        assert value == Decimal("0.160000000000")
        assert wear is WearCondition.FIELD_TESTED

    def test_a_capped_output_can_never_reach_a_lower_wear_band(self) -> None:
        """Even a perfect 0.00 input bundle cannot make this skin better than FT."""
        output_range = FloatRange(Decimal("0.15"), Decimal("0.38"))
        _, wear = project_output_wear(Fraction(0), output_range, RULES)
        assert wear is WearCondition.FIELD_TESTED

    def test_unsupported_float_method_raises(self) -> None:
        broken = TradeupRuleSet(
            rule_version="broken",
            effective_from=RULES.effective_from,
            effective_until=None,
            input_count_by_rarity={Rarity.MIL_SPEC: 10},
            output_quality_by_input_group={},
            attribute_stripping_qualities=frozenset(),
            float_method="SOMETHING_ELSE",  # type: ignore[arg-type]
            probability_method=ProbabilityMethod.COLLECTION_WEIGHTED_UNIFORM_V1,
            source_references=(),
            validated_at=None,
            validation_status=RuleValidationStatus.UNVERIFIED,
        )
        with pytest.raises(RuleViolation, match="unsupported float method"):
            project_output_float(Fraction(1, 2), FULL, broken)

    def test_average_outside_unit_interval_raises(self) -> None:
        with pytest.raises(MathematicsError, match=r"outside \[0, 1\]"):
            project_output_float(Fraction(3, 2), FULL, RULES)


class TestOutcomeProbabilities:
    def test_pure_collection_contract(self) -> None:
        outcomes = outcome_probabilities({"col-a": 10}, {"col-a": pool("col-a", "s1", "s2")}, RULES)
        assert len(outcomes) == 2
        assert all(o.probability == Fraction(1, 2) for o in outcomes)

    def test_mixed_collection_contract_worked_example(self) -> None:
        # 7 from A (k=2) -> 0.35 each; 3 from B (k=3) -> 0.10 each.
        outcomes = outcome_probabilities(
            {"col-a": 7, "col-b": 3},
            {"col-a": pool("col-a", "a1", "a2"), "col-b": pool("col-b", "b1", "b2", "b3")},
            RULES,
        )
        by_skin = aggregate_by_skin(outcomes)
        assert by_skin["a1"] == Fraction(7, 20)
        assert by_skin["b1"] == Fraction(1, 10)
        assert sum(by_skin.values()) == 1

    def test_five_input_covert_contract(self) -> None:
        outcomes = outcome_probabilities(
            {"col-a": 5},
            {"col-a": pool("col-a", "k1", "k2", "k3", "k4")},
            RULES,
        )
        assert len(outcomes) == 4
        assert all(o.probability == Fraction(1, 4) for o in outcomes)

    def test_probabilities_sum_to_exactly_one(self) -> None:
        outcomes = outcome_probabilities(
            {"col-a": 3, "col-b": 4, "col-c": 3},
            {
                "col-a": pool("col-a", "a1", "a2", "a3", "a4", "a5", "a6", "a7"),
                "col-b": pool("col-b", "b1"),
                "col-c": pool("col-c", "c1", "c2", "c3"),
            },
            RULES,
        )
        assert sum((o.probability for o in outcomes), Fraction(0)) == Fraction(1)

    def test_shared_output_across_collections_aggregates(self) -> None:
        outcomes = outcome_probabilities(
            {"col-a": 5, "col-b": 5},
            {"col-a": pool("col-a", "shared"), "col-b": pool("col-b", "shared")},
            RULES,
        )
        merged = aggregate_by_skin(outcomes)
        assert merged == {"shared": Fraction(1)}
        assert len(outcomes) == 2  # the operator card still sees the breakdown

    def test_empty_output_pool_fails_closed(self) -> None:
        with pytest.raises(MathematicsError, match="empty output pool"):
            outcome_probabilities({"col-a": 10}, {"col-a": pool("col-a")}, RULES)

    def test_missing_pool_fails_closed_rather_than_guessing(self) -> None:
        with pytest.raises(MathematicsError, match="refusing to guess"):
            outcome_probabilities({"col-a": 10}, {}, RULES)

    def test_no_collections_raises(self) -> None:
        with pytest.raises(MathematicsError, match="no input collections"):
            outcome_probabilities({}, {}, RULES)

    def test_zero_total_inputs_raises(self) -> None:
        with pytest.raises(MathematicsError, match="no inputs"):
            outcome_probabilities({"col-a": 0}, {"col-a": pool("col-a", "s1")}, RULES)

    def test_negative_contribution_raises_even_when_the_total_looks_sane(self) -> None:
        """A negative count would otherwise produce a negative probability."""
        with pytest.raises(MathematicsError, match="contributes -1"):
            outcome_probabilities(
                {"col-a": -1, "col-b": 11},
                {"col-a": pool("col-a", "a1"), "col-b": pool("col-b", "b1")},
                RULES,
            )

    def test_probability_rendering(self) -> None:
        outcomes = outcome_probabilities({"col-a": 10}, {"col-a": pool("col-a", "s1", "s2")}, RULES)
        assert outcomes[0].probability_decimal == Decimal("0.500000000")
        assert outcomes[0].probability_percent == Decimal("50.0000")

    def test_unsupported_probability_method_raises(self) -> None:
        broken = TradeupRuleSet(
            rule_version="broken-prob",
            effective_from=RULES.effective_from,
            effective_until=None,
            input_count_by_rarity={Rarity.MIL_SPEC: 10},
            output_quality_by_input_group={},
            attribute_stripping_qualities=frozenset(),
            float_method=FloatMethod.NORMALIZED_AVERAGE_V1,
            probability_method="SOMETHING_ELSE",  # type: ignore[arg-type]
            source_references=(),
            validated_at=None,
            validation_status=RuleValidationStatus.UNVERIFIED,
        )
        with pytest.raises(RuleViolation, match="unsupported probability method"):
            outcome_probabilities({"col-a": 10}, {"col-a": pool("col-a", "s1")}, broken)


def test_to_decimal_renders_repeating_fractions_at_fixed_resolution() -> None:
    assert to_decimal(Fraction(1, 3)) == Decimal("0.333333333333")
