"""Contract compositions, candidates, evaluations and rejections.

A *composition* is a shape ("seven from collection A, three from collection B").
A *candidate* is a composition bound to exact listings. An *evaluation* is the
economics of a candidate at a moment in time. A *rejection* records why a candidate
died, with machine-readable codes so rejection statistics become the primary
evidence about whether the strategy is executable at all.

Candidate identity is a content hash of the exact listings, composition and rule
version. Re-scanning the same market state produces the same identifier, which is
what makes "reproducible candidate reconstruction" testable rather than aspirational.
"""

from __future__ import annotations

import enum
import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from fractions import Fraction

from tradeup.domain.items import QualityType, Rarity, WearCondition
from tradeup.domain.listings import ListingIdentity, MarketplaceListing
from tradeup.domain.money import Money
from tradeup.domain.valuation import OutputValuation

__all__ = [
    "CandidateEvaluation",
    "CandidateRejection",
    "ContractComposition",
    "ProcurementMode",
    "RejectionReason",
    "TradeupCandidate",
    "TradeupInput",
    "TradeupOutcome",
]


class ProcurementMode(enum.StrEnum):
    """How a bundle is assembled. Only the first is used in the vertical slice."""

    COMPLETE_BUNDLE_CONFIRMATION = "COMPLETE_BUNDLE_CONFIRMATION"
    """Every input verified purchasable now. No accumulation risk beyond the race."""

    TARGET_ACCUMULATION = "TARGET_ACCUMULATION"
    """Standing buy orders under float/price bounds, filling over time."""

    INVENTORY_ASSISTED_COMPLETION = "INVENTORY_ASSISTED_COMPLETION"
    """Completing a bundle from a held pool of repeatedly useful inputs."""


class RejectionReason(enum.StrEnum):
    """Machine-readable rejection codes.

    These are the dataset. Counting them by cause over a shadow run is how we learn
    whether opportunities fail on economics, on availability, or on our own model
    gaps -- a distinction that a free-text reason would destroy.
    """

    # -- economics ----------------------------------------------------------
    BELOW_DISCOVERY_ROI = "BELOW_DISCOVERY_ROI"
    BELOW_FINAL_ROI = "BELOW_FINAL_ROI"
    BELOW_MIN_ABSOLUTE_PROFIT = "BELOW_MIN_ABSOLUTE_PROFIT"
    NEGATIVE_LOWER_BOUND_EV = "NEGATIVE_LOWER_BOUND_EV"
    WORST_CASE_LOSS_EXCEEDED = "WORST_CASE_LOSS_EXCEEDED"
    CONTRACT_COST_EXCEEDED = "CONTRACT_COST_EXCEEDED"
    CAPITAL_DAYS_EXCEEDED = "CAPITAL_DAYS_EXCEEDED"

    # -- availability and freshness -----------------------------------------
    QUOTE_TOO_OLD = "QUOTE_TOO_OLD"
    LISTING_DISAPPEARED = "LISTING_DISAPPEARED"
    LISTING_PRICE_CHANGED = "LISTING_PRICE_CHANGED"
    LISTING_FLOAT_MISMATCH = "LISTING_FLOAT_MISMATCH"
    LISTING_IDENTITY_MISMATCH = "LISTING_IDENTITY_MISMATCH"
    LISTING_NOT_PURCHASABLE = "LISTING_NOT_PURCHASABLE"
    INCOMPLETE_BUNDLE = "INCOMPLETE_BUNDLE"

    # -- allocation ---------------------------------------------------------
    DUPLICATE_ASSET_ALLOCATION = "DUPLICATE_ASSET_ALLOCATION"
    ASSET_ALREADY_RESERVED = "ASSET_ALREADY_RESERVED"

    # -- partial fill -------------------------------------------------------
    PARTIAL_FILL_EXPOSURE_EXCEEDED = "PARTIAL_FILL_EXPOSURE_EXCEEDED"

    # -- model integrity ----------------------------------------------------
    UNKNOWN_FEE = "UNKNOWN_FEE"
    UNVERIFIED_RULESET = "UNVERIFIED_RULESET"
    RULE_VIOLATION = "RULE_VIOLATION"
    METADATA_DISCREPANCY = "METADATA_DISCREPANCY"
    UNSUPPORTED_BALANCE_CONVERSION = "UNSUPPORTED_BALANCE_CONVERSION"
    INSUFFICIENT_OUTPUT_LIQUIDITY = "INSUFFICIENT_OUTPUT_LIQUIDITY"
    UNVALUABLE_OUTPUT_MASS = "UNVALUABLE_OUTPUT_MASS"

    # -- policy -------------------------------------------------------------
    EXECUTION_MODE_NOT_PERMITTED = "EXECUTION_MODE_NOT_PERMITTED"
    LIVE_EXECUTION_DISABLED = "LIVE_EXECUTION_DISABLED"
    DAILY_SPEND_LIMIT_EXCEEDED = "DAILY_SPEND_LIMIT_EXCEEDED"
    INSUFFICIENT_BALANCE = "INSUFFICIENT_BALANCE"


@dataclass(frozen=True, slots=True)
class ContractComposition:
    """The collection shape of a contract, independent of any specific listing.

    Enumerating compositions rather than raw listing combinations is what keeps the
    search tractable: there are a handful of shapes per rarity, and millions of
    listing combinations.
    """

    input_rarity: Rarity
    #: collection_id -> number of inputs drawn from it.
    counts_by_collection: Mapping[str, int]
    rule_version: str

    def __post_init__(self) -> None:
        if not self.counts_by_collection:
            raise ValueError("a composition needs at least one collection")
        for collection_id, count in self.counts_by_collection.items():
            if count <= 0:
                raise ValueError(f"collection {collection_id} contributes {count} inputs")

    @property
    def total_inputs(self) -> int:
        return sum(self.counts_by_collection.values())

    @property
    def collection_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self.counts_by_collection))

    @property
    def signature(self) -> str:
        """Stable human-readable shape, e.g. ``MIL_SPEC:col-a=7,col-b=3``."""
        parts = ",".join(f"{cid}={self.counts_by_collection[cid]}" for cid in self.collection_ids)
        return f"{self.input_rarity.value}:{parts}"

    @property
    def is_pure(self) -> bool:
        return len(self.counts_by_collection) == 1


@dataclass(frozen=True, slots=True)
class TradeupInput:
    """One exact listing allocated to one contract slot."""

    listing: MarketplaceListing
    #: Exact normalised float, carried alongside the Decimal on the listing so the
    #: optimiser and the maths agree bit for bit.
    normalized: Fraction

    def __post_init__(self) -> None:
        if not (0 <= self.normalized <= 1):
            raise ValueError(f"normalised float {self.normalized} outside [0, 1]")

    @property
    def identity(self) -> ListingIdentity:
        return self.listing.identity

    @property
    def collection_id(self) -> str:
        return self.listing.collection_id

    @property
    def acquisition_cost(self) -> Money:
        return self.listing.venue_acquisition_cost


@dataclass(frozen=True, slots=True)
class TradeupOutcome:
    """One reachable output, with its probability and conservative valuation."""

    collection_id: str
    skin_id: str
    probability: Fraction
    output_float: Decimal
    wear: WearCondition
    quality: QualityType
    valuation: OutputValuation

    def __post_init__(self) -> None:
        if not (0 < self.probability <= 1):
            raise ValueError(f"outcome probability {self.probability} outside (0, 1]")

    @property
    def net_proceeds(self) -> Money:
        return self.valuation.net_proceeds

    @property
    def is_valuable(self) -> bool:
        return self.valuation.is_valuable


@dataclass(frozen=True)
class TradeupCandidate:
    """A composition bound to exact listings, with computed outcomes.

    Carries no economics: an evaluation is produced separately so the same candidate
    can be re-evaluated against fresher prices and fee schedules without being
    rebuilt.
    """

    composition: ContractComposition
    inputs: tuple[TradeupInput, ...]
    outcomes: tuple[TradeupOutcome, ...]
    rule_version: str
    output_quality: QualityType
    average_normalized_float: Fraction
    created_at: datetime
    expires_at: datetime
    procurement_mode: ProcurementMode = ProcurementMode.COMPLETE_BUNDLE_CONFIRMATION
    candidate_id: str = field(default="", compare=False)

    def __post_init__(self) -> None:
        if len(self.inputs) != self.composition.total_inputs:
            raise ValueError(
                f"composition expects {self.composition.total_inputs} inputs, "
                f"got {len(self.inputs)}"
            )
        identities = [i.identity for i in self.inputs]
        if len(set(identities)) != len(identities):
            raise ValueError("a listing cannot occupy two slots in one contract")
        asset_ids = [i.listing.asset_id for i in self.inputs]
        if len(set(asset_ids)) != len(asset_ids):
            raise ValueError("a physical asset cannot occupy two slots in one contract")
        actual_counts: dict[str, int] = {}
        for item in self.inputs:
            actual_counts[item.collection_id] = actual_counts.get(item.collection_id, 0) + 1
        if actual_counts != dict(self.composition.counts_by_collection):
            raise ValueError(
                f"inputs do not match composition: {actual_counts} != "
                f"{dict(self.composition.counts_by_collection)}"
            )
        if self.expires_at <= self.created_at:
            raise ValueError("candidate expiry must be after creation")
        if not self.outcomes:
            raise ValueError("a candidate must have at least one outcome")
        total = sum((o.probability for o in self.outcomes), Fraction(0))
        if total != 1:
            raise ValueError(f"outcome probabilities sum to {total}, not 1")
        if not self.candidate_id:
            object.__setattr__(self, "candidate_id", self.compute_id())

    # -- identity ------------------------------------------------------------

    def compute_id(self) -> str:
        """Deterministic content hash over the exact inputs and governing rules."""
        payload = "|".join(
            [
                self.rule_version,
                self.composition.signature,
                self.procurement_mode.value,
                *(str(i) for i in sorted(self.input_identities)),
            ]
        )
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]
        return f"TU-{digest}"

    @property
    def input_identities(self) -> tuple[ListingIdentity, ...]:
        return tuple(sorted(i.identity for i in self.inputs))

    @property
    def asset_ids(self) -> tuple[str, ...]:
        return tuple(sorted(i.listing.asset_id for i in self.inputs))

    @property
    def venues(self) -> tuple[str, ...]:
        return tuple(sorted({i.identity.venue for i in self.inputs}))

    # -- derived facts -------------------------------------------------------

    @property
    def input_count(self) -> int:
        return len(self.inputs)

    def oldest_quote_age_seconds(self, now: datetime) -> Decimal:
        return max(i.listing.quote_age_seconds(now) for i in self.inputs)

    def max_trade_lock_days(self, now: datetime) -> int:
        return max(i.listing.trade_lock_days_remaining(now) for i in self.inputs)

    @property
    def unvaluable_probability_mass(self) -> Fraction:
        """Probability mass we could not price. High mass means low trust."""
        return sum((o.probability for o in self.outcomes if not o.is_valuable), Fraction(0))

    def is_expired(self, now: datetime) -> bool:
        return now >= self.expires_at


@dataclass(frozen=True)
class CandidateEvaluation:
    """The economics of one candidate at one moment, under one fee schedule.

    Every field a gate reads is stored, so a rejection can be re-derived from the
    record without re-running the scan.
    """

    candidate_id: str
    rule_version: str
    fee_schedule_id: str
    evaluated_at: datetime

    # -- cost ---------------------------------------------------------------
    input_cost: Money
    buyer_fees: Money
    deposit_fees: Money
    fx_cost: Money
    payment_surcharge: Money
    operational_cost: Money
    capital_carry_cost: Money
    partial_fill_reserve: Money
    all_in_cost: Money

    # -- value --------------------------------------------------------------
    expected_output_value: Money
    expected_partial_fill_loss: Money
    ev_net: Money
    roi_net: Decimal
    lower_bound_ev: Money
    probability_of_profit: Fraction
    worst_case_pnl: Money
    best_case_pnl: Money

    # -- capital and risk ----------------------------------------------------
    expected_capital_days: Decimal
    profit_per_capital_day: Money
    bundle_completion_probability: Decimal
    expected_orphan_loss: Money
    unvaluable_probability_mass: Fraction
    max_quote_age_seconds: Decimal
    output_confidence_floor: str

    def __post_init__(self) -> None:
        if self.evaluated_at.tzinfo is None:
            raise ValueError("evaluated_at must be timezone-aware UTC")
        if self.all_in_cost.is_negative:
            raise ValueError("all-in cost cannot be negative")

    @property
    def is_profitable_point_estimate(self) -> bool:
        return self.ev_net.is_positive

    @property
    def roi_percent(self) -> Decimal:
        return (self.roi_net * 100).quantize(Decimal("0.01"))

    @property
    def probability_of_profit_percent(self) -> Decimal:
        return (
            Decimal(self.probability_of_profit.numerator)
            / Decimal(self.probability_of_profit.denominator)
            * 100
        ).quantize(Decimal("0.01"))


@dataclass(frozen=True, slots=True)
class CandidateRejection:
    """Why a candidate was not approved, in codes.

    ``stage`` distinguishes a candidate that never looked good from one that looked
    good and then died on revalidation -- the second is the interesting statistic,
    because it measures the gap between displayed and executable opportunity.
    """

    candidate_id: str
    stage: str
    reasons: tuple[RejectionReason, ...]
    rejected_at: datetime
    details: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.reasons:
            raise ValueError("a rejection must carry at least one reason code")
        if self.rejected_at.tzinfo is None:
            raise ValueError("rejected_at must be timezone-aware UTC")

    @property
    def primary_reason(self) -> RejectionReason:
        return self.reasons[0]

    def with_reasons(self, extra: Sequence[RejectionReason]) -> CandidateRejection:
        merged = tuple(dict.fromkeys([*self.reasons, *extra]))
        return CandidateRejection(
            candidate_id=self.candidate_id,
            stage=self.stage,
            reasons=merged,
            rejected_at=self.rejected_at,
            details=self.details,
        )
