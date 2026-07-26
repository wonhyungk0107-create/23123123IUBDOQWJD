"""Trade-up mathematics.

Two deliberate choices:

* **Exact rationals internally.** Probabilities and normalised floats are computed
  with :class:`fractions.Fraction`, not Decimal division. ``n_c / (N * k_c)`` summed
  over outcomes is then *exactly* 1 -- the invariant is a property of the
  arithmetic rather than something we assert within a tolerance and hope. Decimal
  appears only at the presentation boundary.
* **Range membership is checked, not assumed.** A listing whose raw float sits
  outside its skin's published range is a metadata or adapter fault. It raises
  instead of clamping, because clamping would turn a data bug into a plausible
  contract.

Every function here is pure and rule-version agnostic; callers pass the resolved
:class:`~tradeup.domain.rules.TradeupRuleSet` and record its version alongside the
result.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal
from fractions import Fraction
from typing import Final

from tradeup.domain._guards import reject_float
from tradeup.domain.items import FloatRange, WearCondition, classify_wear
from tradeup.domain.rules import (
    EligibleOutputPool,
    FloatMethod,
    ProbabilityMethod,
    RuleViolation,
    TradeupRuleSet,
)

__all__ = [
    "FLOAT_QUANTUM",
    "MathematicsError",
    "OutcomeProbability",
    "average_normalized_float",
    "normalized_float",
    "normalized_float_exact",
    "outcome_probabilities",
    "project_output_float",
    "project_output_float_exact",
    "to_decimal",
]

#: Internal float resolution. CS2 item floats are single-precision; twelve decimal
#: places is well beyond that while staying far from Decimal's default context.
FLOAT_QUANTUM: Final[Decimal] = Decimal("1e-12")


class MathematicsError(Exception):
    """Raised when inputs violate a mathematical precondition."""


def to_decimal(value: Fraction, quantum: Decimal = FLOAT_QUANTUM) -> Decimal:
    """Render an exact rational as a Decimal at a fixed resolution."""
    return (Decimal(value.numerator) / Decimal(value.denominator)).quantize(
        quantum, rounding=ROUND_HALF_EVEN
    )


# ---------------------------------------------------------------------------
# Float normalisation
# ---------------------------------------------------------------------------


def normalized_float_exact(raw_float: Decimal, float_range: FloatRange) -> Fraction:
    """``z_i = (f_i - a_i) / (b_i - a_i)`` as an exact rational.

    Raises when ``f_i`` lies outside ``[a_i, b_i]``: an out-of-range float means the
    listing and the metadata disagree about what the item is.
    """
    reject_float(raw_float, "raw_float")
    if not float_range.contains(raw_float):
        raise MathematicsError(
            f"raw float {raw_float} outside skin range "
            f"[{float_range.minimum}, {float_range.maximum}]"
        )
    numerator = Fraction(raw_float) - Fraction(float_range.minimum)
    denominator = Fraction(float_range.maximum) - Fraction(float_range.minimum)
    return numerator / denominator


def normalized_float(raw_float: Decimal, float_range: FloatRange) -> Decimal:
    """Decimal-rendered :func:`normalized_float_exact`, guaranteed within ``[0, 1]``."""
    return to_decimal(normalized_float_exact(raw_float, float_range))


def average_normalized_float(values: Sequence[Fraction]) -> Fraction:
    """Mean normalised float. Exact, so input order cannot change the result."""
    if not values:
        raise MathematicsError("cannot average an empty set of inputs")
    for value in values:
        if not (0 <= value <= 1):
            raise MathematicsError(f"normalised float {value} outside [0, 1]")
    return sum(values, Fraction(0)) / len(values)


# ---------------------------------------------------------------------------
# Output float projection
# ---------------------------------------------------------------------------


def project_output_float_exact(
    average_normalized: Fraction,
    output_range: FloatRange,
    ruleset: TradeupRuleSet,
) -> Fraction:
    """``f_y = a_y + z̄ * (b_y - a_y)`` under the ruleset's float method."""
    if ruleset.float_method is not FloatMethod.NORMALIZED_AVERAGE_V1:
        raise RuleViolation(
            f"unsupported float method {ruleset.float_method} in rule {ruleset.rule_version}"
        )
    if not (0 <= average_normalized <= 1):
        raise MathematicsError(f"average normalised float {average_normalized} outside [0, 1]")
    low = Fraction(output_range.minimum)
    high = Fraction(output_range.maximum)
    return low + average_normalized * (high - low)


def project_output_float(
    average_normalized: Fraction,
    output_range: FloatRange,
    ruleset: TradeupRuleSet,
) -> Decimal:
    """Decimal output float, validated to sit inside the output's legal range.

    The quantisation step can in principle nudge a value a fraction of ``1e-12``
    past a bound, so the result is clamped back into range *after* the exact value
    has already been proven in-range. That is a rendering correction, not a
    silent repair of bad data.
    """
    exact = project_output_float_exact(average_normalized, output_range, ruleset)
    if not (Fraction(output_range.minimum) <= exact <= Fraction(output_range.maximum)):
        raise MathematicsError(
            f"projected output float {exact} escaped range "
            f"[{output_range.minimum}, {output_range.maximum}]"
        )
    return output_range.clamp(to_decimal(exact))


def project_output_wear(
    average_normalized: Fraction,
    output_range: FloatRange,
    ruleset: TradeupRuleSet,
) -> tuple[Decimal, WearCondition]:
    """Output float and the wear band it lands in."""
    value = project_output_float(average_normalized, output_range, ruleset)
    return value, classify_wear(value)


# ---------------------------------------------------------------------------
# Outcome probabilities
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OutcomeProbability:
    """One reachable output and its exact probability."""

    collection_id: str
    output_skin_id: str
    probability: Fraction

    @property
    def probability_decimal(self) -> Decimal:
        return to_decimal(self.probability, Decimal("1e-9"))

    @property
    def probability_percent(self) -> Decimal:
        return (self.probability_decimal * 100).quantize(Decimal("0.0001"))


def outcome_probabilities(
    input_counts_by_collection: Mapping[str, int],
    pools: Mapping[str, EligibleOutputPool],
    ruleset: TradeupRuleSet,
) -> tuple[OutcomeProbability, ...]:
    """``P(y in c) = n_c / (N * k_c)`` for every reachable output.

    Fails closed on an empty output pool. A collection that contributes inputs but
    can produce nothing at the next rarity makes the contract undefined -- the
    remaining probability mass has nowhere to go -- so the candidate is invalid
    rather than renormalised.
    """
    if ruleset.probability_method is not ProbabilityMethod.COLLECTION_WEIGHTED_UNIFORM_V1:
        raise RuleViolation(
            f"unsupported probability method {ruleset.probability_method} "
            f"in rule {ruleset.rule_version}"
        )
    if not input_counts_by_collection:
        raise MathematicsError("contract has no input collections")

    total_inputs = sum(input_counts_by_collection.values())
    if total_inputs <= 0:
        raise MathematicsError("contract has no inputs")

    outcomes: list[OutcomeProbability] = []
    for collection_id, count in sorted(input_counts_by_collection.items()):
        if count <= 0:
            raise MathematicsError(f"collection {collection_id} contributes {count} inputs")
        pool = pools.get(collection_id)
        if pool is None:
            raise MathematicsError(
                f"no eligible output pool for collection {collection_id}; "
                "refusing to guess the output universe"
            )
        if pool.is_empty:
            raise MathematicsError(
                f"collection {collection_id} has an empty output pool at "
                f"{pool.input_rarity} -> {pool.output_rarity}"
            )
        share = Fraction(count, total_inputs * pool.outcome_count)
        outcomes.extend(
            OutcomeProbability(
                collection_id=collection_id,
                output_skin_id=skin_id,
                probability=share,
            )
            for skin_id in pool.output_skin_ids
        )

    total = sum((o.probability for o in outcomes), Fraction(0))
    if total != 1:
        # Unreachable with correct pools; kept because a silent renormalisation
        # here would corrupt every downstream EV.
        raise MathematicsError(f"outcome probabilities sum to {total}, not 1")
    return tuple(outcomes)


def aggregate_by_skin(
    outcomes: Sequence[OutcomeProbability],
) -> dict[str, Fraction]:
    """Collapse per-collection outcomes onto skin identity.

    Two collections in one contract can share an output skin; valuation cares about
    the skin, the operator card cares about the breakdown.
    """
    merged: dict[str, Fraction] = {}
    for outcome in outcomes:
        merged[outcome.output_skin_id] = (
            merged.get(outcome.output_skin_id, Fraction(0)) + outcome.probability
        )
    return merged
