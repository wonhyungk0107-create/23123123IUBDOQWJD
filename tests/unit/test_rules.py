"""Versioned rule registry: resolution, eligibility, quality mixing."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from tradeup.domain.items import QualityType, Rarity
from tradeup.domain.rules import (
    DEFAULT_RULE_REGISTRY,
    RULESET_2026_05,
    RULESET_LEGACY_10_INPUT,
    EligibleOutputPool,
    FloatMethod,
    ProbabilityMethod,
    RuleRegistry,
    RuleResolutionError,
    RuleValidationStatus,
    RuleViolation,
    TradeupRuleSet,
)

BEFORE_CHANGE = datetime(2026, 4, 1, tzinfo=UTC)
AFTER_CHANGE = datetime(2026, 7, 25, tzinfo=UTC)


class TestResolution:
    def test_resolves_the_ruleset_governing_a_moment(self) -> None:
        assert DEFAULT_RULE_REGISTRY.resolve(BEFORE_CHANGE) is RULESET_LEGACY_10_INPUT
        assert DEFAULT_RULE_REGISTRY.resolve(AFTER_CHANGE) is RULESET_2026_05

    def test_boundary_belongs_to_the_new_ruleset(self) -> None:
        assert DEFAULT_RULE_REGISTRY.resolve(datetime(2026, 5, 1, tzinfo=UTC)) is RULESET_2026_05

    def test_no_covering_ruleset_fails_closed(self) -> None:
        with pytest.raises(RuleResolutionError, match="no trade-up ruleset covers"):
            DEFAULT_RULE_REGISTRY.resolve(datetime(2001, 1, 1, tzinfo=UTC))

    def test_naive_timestamp_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="timezone-aware"):
            DEFAULT_RULE_REGISTRY.resolve(datetime(2026, 7, 25))  # noqa: DTZ001

    def test_overlapping_rulesets_fail_closed_rather_than_picking_a_winner(self) -> None:
        overlapping = TradeupRuleSet(
            rule_version="overlapping",
            effective_from=datetime(2026, 1, 1, tzinfo=UTC),
            effective_until=None,
            input_count_by_rarity={Rarity.MIL_SPEC: 10},
            output_quality_by_input_group={frozenset({QualityType.NORMAL}): QualityType.NORMAL},
            attribute_stripping_qualities=frozenset(),
            float_method=FloatMethod.NORMALIZED_AVERAGE_V1,
            probability_method=ProbabilityMethod.COLLECTION_WEIGHTED_UNIFORM_V1,
            source_references=(),
            validated_at=None,
            validation_status=RuleValidationStatus.UNVERIFIED,
        )
        registry = RuleRegistry([RULESET_2026_05, overlapping])
        with pytest.raises(RuleResolutionError, match="registry is inconsistent"):
            registry.resolve(AFTER_CHANGE)

    def test_duplicate_versions_are_rejected_at_construction(self) -> None:
        with pytest.raises(ValueError, match="duplicate rule versions"):
            RuleRegistry([RULESET_2026_05, RULESET_2026_05])

    def test_empty_registry_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="at least one ruleset"):
            RuleRegistry([])

    def test_lookup_by_version(self) -> None:
        assert DEFAULT_RULE_REGISTRY.by_version("2026-05-souvenir-covert") is RULESET_2026_05
        with pytest.raises(RuleResolutionError, match="unknown rule version"):
            DEFAULT_RULE_REGISTRY.by_version("nope")


class TestInputCounts:
    def test_standard_contracts_take_ten_inputs(self) -> None:
        assert RULESET_2026_05.input_count_for(Rarity.CLASSIFIED) == 10

    def test_covert_contracts_take_five_under_the_current_ruleset(self) -> None:
        assert RULESET_2026_05.input_count_for(Rarity.COVERT) == 5

    def test_covert_contracts_did_not_exist_under_the_legacy_ruleset(self) -> None:
        assert not RULESET_LEGACY_10_INPUT.is_eligible_input_rarity(Rarity.COVERT)
        with pytest.raises(RuleViolation, match="defines no contract for rarity"):
            RULESET_LEGACY_10_INPUT.input_count_for(Rarity.COVERT)

    def test_unknown_rarity_raises_rather_than_defaulting_to_ten(self) -> None:
        with pytest.raises(RuleViolation):
            RULESET_2026_05.input_count_for(Rarity.EXTRAORDINARY)


class TestQualityMixing:
    def test_normal_inputs_yield_normal_output(self) -> None:
        group = frozenset({QualityType.NORMAL})
        assert RULESET_2026_05.output_quality_for(group) is QualityType.NORMAL

    def test_stattrak_stays_stattrak(self) -> None:
        group = frozenset({QualityType.STATTRAK})
        assert RULESET_2026_05.output_quality_for(group) is QualityType.STATTRAK

    def test_souvenir_may_mix_with_normal_and_is_stripped(self) -> None:
        group = frozenset({QualityType.NORMAL, QualityType.SOUVENIR})
        assert RULESET_2026_05.output_quality_for(group) is QualityType.NORMAL
        assert QualityType.SOUVENIR in RULESET_2026_05.attribute_stripping_qualities

    def test_souvenir_was_prohibited_under_the_legacy_ruleset(self) -> None:
        group = frozenset({QualityType.SOUVENIR})
        assert not RULESET_LEGACY_10_INPUT.permits_input_qualities(group)
        with pytest.raises(RuleViolation, match="does not permit an input mix"):
            RULESET_LEGACY_10_INPUT.output_quality_for(group)

    def test_stattrak_can_never_mix_with_normal(self) -> None:
        group = frozenset({QualityType.NORMAL, QualityType.STATTRAK})
        for ruleset in (RULESET_LEGACY_10_INPUT, RULESET_2026_05):
            assert not ruleset.permits_input_qualities(group)

    def test_permitted_input_qualities_reflects_the_version(self) -> None:
        assert QualityType.SOUVENIR not in RULESET_LEGACY_10_INPUT.permitted_input_qualities
        assert QualityType.SOUVENIR in RULESET_2026_05.permitted_input_qualities


class TestValidationStatus:
    def test_shipped_rulesets_do_not_overclaim_verification(self) -> None:
        """Neither shipped ruleset has been checked against an authoritative source."""
        for ruleset in DEFAULT_RULE_REGISTRY.all_rulesets:
            assert ruleset.validation_status is RuleValidationStatus.GOLDEN_FIXTURE_ONLY
            assert ruleset.validated_at is None

    def test_unverified_rulesets_are_not_approvable(self) -> None:
        assert not RuleValidationStatus.UNVERIFIED.approvable
        assert RuleValidationStatus.GOLDEN_FIXTURE_ONLY.approvable


class TestRulesetInvariants:
    def test_empty_validity_window_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="empty validity window"):
            TradeupRuleSet(
                rule_version="bad",
                effective_from=datetime(2026, 5, 1, tzinfo=UTC),
                effective_until=datetime(2026, 1, 1, tzinfo=UTC),
                input_count_by_rarity={Rarity.MIL_SPEC: 10},
                output_quality_by_input_group={},
                attribute_stripping_qualities=frozenset(),
                float_method=FloatMethod.NORMALIZED_AVERAGE_V1,
                probability_method=ProbabilityMethod.COLLECTION_WEIGHTED_UNIFORM_V1,
                source_references=(),
                validated_at=None,
                validation_status=RuleValidationStatus.UNVERIFIED,
            )

    def test_non_positive_input_count_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="must be positive"):
            TradeupRuleSet(
                rule_version="bad",
                effective_from=datetime(2026, 5, 1, tzinfo=UTC),
                effective_until=None,
                input_count_by_rarity={Rarity.MIL_SPEC: 0},
                output_quality_by_input_group={},
                attribute_stripping_qualities=frozenset(),
                float_method=FloatMethod.NORMALIZED_AVERAGE_V1,
                probability_method=ProbabilityMethod.COLLECTION_WEIGHTED_UNIFORM_V1,
                source_references=(),
                validated_at=None,
                validation_status=RuleValidationStatus.UNVERIFIED,
            )


class TestEligibleOutputPool:
    def test_duplicate_outputs_are_rejected(self) -> None:
        with pytest.raises(ValueError, match="duplicate outputs"):
            EligibleOutputPool(
                collection_id="col-a",
                input_rarity=Rarity.MIL_SPEC,
                output_rarity=Rarity.RESTRICTED,
                output_skin_ids=("s1", "s1"),
            )

    def test_outcome_count_is_k_c(self) -> None:
        pool = EligibleOutputPool(
            collection_id="col-a",
            input_rarity=Rarity.MIL_SPEC,
            output_rarity=Rarity.RESTRICTED,
            output_skin_ids=("s1", "s2", "s3"),
        )
        assert pool.outcome_count == 3
        assert not pool.is_empty
