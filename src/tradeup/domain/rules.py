"""Versioned trade-up rule registry.

CS2 trade-up mechanics change. Souvenir eligibility changed. Covert contracts
changed. A system that hard-codes "ten inputs, Souvenirs prohibited" produces
confidently wrong economics the day Valve ships a patch, and the failure is silent
because the arithmetic still balances.

So every rule-sensitive value is looked up from a :class:`TradeupRuleSet` resolved
by timestamp, and every calculation records which ``rule_version`` produced it.

Validation status is explicit and honest. A ruleset that has not been checked
against an authoritative source is marked ``UNVERIFIED`` and the policy layer
refuses to approve candidates that depend on it. Being unable to verify offline is
not a reason to pretend we did.
"""

from __future__ import annotations

import enum
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Final

from tradeup.domain.items import QualityType, Rarity

__all__ = [
    "DEFAULT_RULE_REGISTRY",
    "EligibleOutputPool",
    "FloatMethod",
    "ProbabilityMethod",
    "RuleRegistry",
    "RuleResolutionError",
    "RuleValidationStatus",
    "RuleViolation",
    "TradeupRuleSet",
]


class RuleViolation(Exception):
    """Raised when a contract breaks the resolved ruleset."""


class RuleResolutionError(Exception):
    """Raised when no single ruleset governs a timestamp. Always fail closed."""


class FloatMethod(enum.StrEnum):
    """Identifier for the output-float formula, recorded on every result."""

    NORMALIZED_AVERAGE_V1 = "NORMALIZED_AVERAGE_V1"
    """f_y = a_y + mean((f_i - a_i)/(b_i - a_i)) * (b_y - a_y)"""


class ProbabilityMethod(enum.StrEnum):
    """Identifier for the outcome-probability formula."""

    COLLECTION_WEIGHTED_UNIFORM_V1 = "COLLECTION_WEIGHTED_UNIFORM_V1"
    """P(y in c) = n_c / (N * k_c)"""


class RuleValidationStatus(enum.StrEnum):
    """How much we actually know about a ruleset's correctness."""

    VERIFIED_AGAINST_SOURCE = "VERIFIED_AGAINST_SOURCE"
    """Checked against an authoritative published source at ``validated_at``."""

    GOLDEN_FIXTURE_ONLY = "GOLDEN_FIXTURE_ONLY"
    """Reproduces known-good worked examples, but no source check was performed."""

    UNVERIFIED = "UNVERIFIED"
    """Encoded from secondary sources. Not approvable for money decisions."""

    @property
    def approvable(self) -> bool:
        return self is not RuleValidationStatus.UNVERIFIED


@dataclass(frozen=True, slots=True)
class EligibleOutputPool:
    """The outputs a single collection can produce at one rarity step.

    ``k_c`` in the probability formula is ``len(output_skin_ids)``. An empty pool
    means the collection cannot be used at this rarity -- not that it produces
    nothing at random.
    """

    collection_id: str
    input_rarity: Rarity
    output_rarity: Rarity
    output_skin_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if len(set(self.output_skin_ids)) != len(self.output_skin_ids):
            raise ValueError(f"duplicate outputs in pool for {self.collection_id}")

    @property
    def outcome_count(self) -> int:
        return len(self.output_skin_ids)

    @property
    def is_empty(self) -> bool:
        return not self.output_skin_ids


@dataclass(frozen=True)
class TradeupRuleSet:
    """A dated, sourced statement of how contracts work."""

    rule_version: str
    effective_from: datetime
    effective_until: datetime | None
    #: Input count keyed by the rarity being consumed. Absent rarity == ineligible.
    input_count_by_rarity: Mapping[Rarity, int]
    #: Quality combinations permitted in one contract, and what each produces.
    output_quality_by_input_group: Mapping[frozenset[QualityType], QualityType]
    #: Qualities whose special attributes are discarded by the contract.
    attribute_stripping_qualities: frozenset[QualityType]
    float_method: FloatMethod
    probability_method: ProbabilityMethod
    source_references: tuple[str, ...]
    validated_at: datetime | None
    validation_status: RuleValidationStatus
    fixture_ids: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if self.effective_from.tzinfo is None:
            raise ValueError("effective_from must be timezone-aware")
        if self.effective_until is not None:
            if self.effective_until.tzinfo is None:
                raise ValueError("effective_until must be timezone-aware")
            if self.effective_until <= self.effective_from:
                raise ValueError(f"ruleset {self.rule_version} has an empty validity window")
        for rarity, count in self.input_count_by_rarity.items():
            if count <= 0:
                raise ValueError(f"input count for {rarity} must be positive, got {count}")

    def covers(self, moment: datetime) -> bool:
        if moment < self.effective_from:
            return False
        return self.effective_until is None or moment < self.effective_until

    # -- eligibility queries -------------------------------------------------

    def input_count_for(self, rarity: Rarity) -> int:
        """How many inputs a contract at ``rarity`` consumes.

        Raises rather than defaulting to 10: an unknown rarity is a metadata gap,
        and defaulting would produce a plausible, wrong contract.
        """
        try:
            return self.input_count_by_rarity[rarity]
        except KeyError:
            raise RuleViolation(
                f"rule {self.rule_version} defines no contract for rarity {rarity}"
            ) from None

    def is_eligible_input_rarity(self, rarity: Rarity) -> bool:
        return rarity in self.input_count_by_rarity

    def output_quality_for(self, input_qualities: frozenset[QualityType]) -> QualityType:
        """Resolve the output quality for a set of input qualities.

        Raises when the combination is not permitted, which is what makes
        "Souvenir + StatTrak in one contract" impossible rather than merely unusual.
        """
        try:
            return self.output_quality_by_input_group[input_qualities]
        except KeyError:
            rendered = "+".join(sorted(q.value for q in input_qualities)) or "<empty>"
            raise RuleViolation(
                f"rule {self.rule_version} does not permit an input mix of {rendered}"
            ) from None

    def permits_input_qualities(self, input_qualities: frozenset[QualityType]) -> bool:
        return input_qualities in self.output_quality_by_input_group

    @property
    def permitted_input_qualities(self) -> frozenset[QualityType]:
        result: set[QualityType] = set()
        for group in self.output_quality_by_input_group:
            result |= group
        return frozenset(result)

    @property
    def eligible_input_rarities(self) -> tuple[Rarity, ...]:
        return tuple(sorted(self.input_count_by_rarity, key=lambda r: r.ladder_index))


class RuleRegistry:
    """Time-indexed collection of rulesets.

    Resolution is strict: exactly one ruleset must cover a timestamp. Zero means we
    have no sourced statement of the rules and must not trade; more than one means
    the registry is inconsistent and must not be trusted to pick a winner.
    """

    def __init__(self, rulesets: Sequence[TradeupRuleSet]) -> None:
        if not rulesets:
            raise ValueError("RuleRegistry requires at least one ruleset")
        versions = [r.rule_version for r in rulesets]
        duplicates = {v for v in versions if versions.count(v) > 1}
        if duplicates:
            raise ValueError(f"duplicate rule versions: {sorted(duplicates)}")
        self._rulesets: tuple[TradeupRuleSet, ...] = tuple(
            sorted(rulesets, key=lambda r: r.effective_from)
        )

    def __len__(self) -> int:
        return len(self._rulesets)

    def __iter__(self) -> Iterator[TradeupRuleSet]:
        return iter(self._rulesets)

    @property
    def all_rulesets(self) -> tuple[TradeupRuleSet, ...]:
        return self._rulesets

    def resolve(self, moment: datetime) -> TradeupRuleSet:
        if moment.tzinfo is None:
            raise ValueError("resolve() requires a timezone-aware timestamp")
        matches = [r for r in self._rulesets if r.covers(moment)]
        if not matches:
            raise RuleResolutionError(
                f"no trade-up ruleset covers {moment.isoformat()}; refusing to assume mechanics"
            )
        if len(matches) > 1:
            overlapping = ", ".join(r.rule_version for r in matches)
            raise RuleResolutionError(
                f"rulesets overlap at {moment.isoformat()}: {overlapping}; registry is inconsistent"
            )
        return matches[0]

    def by_version(self, rule_version: str) -> TradeupRuleSet:
        for ruleset in self._rulesets:
            if ruleset.rule_version == rule_version:
                return ruleset
        raise RuleResolutionError(f"unknown rule version {rule_version!r}")


# ---------------------------------------------------------------------------
# Shipped rulesets
# ---------------------------------------------------------------------------
# These encode the mechanics described in the project brief. Both are marked
# GOLDEN_FIXTURE_ONLY: they reproduce the worked examples in tests/golden, but no
# authoritative source check has been performed in this environment. Promoting
# either to VERIFIED_AGAINST_SOURCE requires a dated citation in
# docs/rule-registry.md and a matching `validated_at`.

_LEGACY_QUALITY_GROUPS: Final[Mapping[frozenset[QualityType], QualityType]] = {
    frozenset({QualityType.NORMAL}): QualityType.NORMAL,
    frozenset({QualityType.STATTRAK}): QualityType.STATTRAK,
}

_CURRENT_QUALITY_GROUPS: Final[Mapping[frozenset[QualityType], QualityType]] = {
    frozenset({QualityType.NORMAL}): QualityType.NORMAL,
    frozenset({QualityType.STATTRAK}): QualityType.STATTRAK,
    frozenset({QualityType.SOUVENIR}): QualityType.NORMAL,
    # Souvenir attributes are stripped, so a mixed Normal/Souvenir bundle yields a
    # plain Normal item one rarity higher.
    frozenset({QualityType.NORMAL, QualityType.SOUVENIR}): QualityType.NORMAL,
}

RULESET_LEGACY_10_INPUT: Final = TradeupRuleSet(
    rule_version="legacy-10-input",
    effective_from=datetime(2013, 8, 14, tzinfo=UTC),
    effective_until=datetime(2026, 5, 1, tzinfo=UTC),
    input_count_by_rarity={
        Rarity.CONSUMER: 10,
        Rarity.INDUSTRIAL: 10,
        Rarity.MIL_SPEC: 10,
        Rarity.RESTRICTED: 10,
        Rarity.CLASSIFIED: 10,
        # Covert is deliberately absent: under this ruleset there is no
        # Covert -> Extraordinary contract, and asking for one must raise.
    },
    output_quality_by_input_group=_LEGACY_QUALITY_GROUPS,
    attribute_stripping_qualities=frozenset(),
    float_method=FloatMethod.NORMALIZED_AVERAGE_V1,
    probability_method=ProbabilityMethod.COLLECTION_WEIGHTED_UNIFORM_V1,
    source_references=("project brief: 'Versioned rules registry'",),
    validated_at=None,
    validation_status=RuleValidationStatus.GOLDEN_FIXTURE_ONLY,
    fixture_ids=("golden/pure_collection_10", "golden/mixed_collection_10"),
)

RULESET_2026_05: Final = TradeupRuleSet(
    rule_version="2026-05-souvenir-covert",
    effective_from=datetime(2026, 5, 1, tzinfo=UTC),
    effective_until=None,
    input_count_by_rarity={
        Rarity.CONSUMER: 10,
        Rarity.INDUSTRIAL: 10,
        Rarity.MIL_SPEC: 10,
        Rarity.RESTRICTED: 10,
        Rarity.CLASSIFIED: 10,
        Rarity.COVERT: 5,
    },
    output_quality_by_input_group=_CURRENT_QUALITY_GROUPS,
    attribute_stripping_qualities=frozenset({QualityType.SOUVENIR}),
    float_method=FloatMethod.NORMALIZED_AVERAGE_V1,
    probability_method=ProbabilityMethod.COLLECTION_WEIGHTED_UNIFORM_V1,
    source_references=("project brief: 'N is 10, or 5 for eligible Covert contracts'",),
    validated_at=None,
    validation_status=RuleValidationStatus.GOLDEN_FIXTURE_ONLY,
    fixture_ids=("golden/covert_5_input", "golden/souvenir_mixed"),
)

DEFAULT_RULE_REGISTRY: Final = RuleRegistry([RULESET_LEGACY_10_INPUT, RULESET_2026_05])
