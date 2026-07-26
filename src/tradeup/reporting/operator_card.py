"""Operator action cards.

The card is the only thing this system produces that causes money to move, and it
moves it by asking a person. So it has to be complete enough that the operator never
has to guess, and honest enough that they can decline.

Three properties matter:

* **Every assumption is on the card.** The valuation rung, the confidence level, the
  fee schedule, the rule version, the priors that have not been calibrated. An
  operator who cannot see what the number rests on cannot sensibly override it.
* **The purchase sequence is explicit and ordered.** Most-fragile-first, with the
  running exposure at each step, so the operator knows what they are risking when
  they click the third of ten buys.
* **The Steam steps are instructions to a human, not an API call.** This is the
  boundary the whole architecture exists to protect.

There is no "approve and buy" affordance. The operator records what they did; the
system never does it for them.
"""

from __future__ import annotations

import enum
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from tradeup.domain.contracts import CandidateEvaluation, TradeupCandidate
from tradeup.domain.money import Money
from tradeup.valuation.partial_fill import PartialFillAssessment

__all__ = [
    "CardInput",
    "CardOutcome",
    "OperatorAction",
    "OperatorCard",
    "PurchaseStep",
    "build_operator_card",
    "venue_listing_url",
]


class OperatorAction(enum.StrEnum):
    """What an operator can report back about a card."""

    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    PURCHASED = "PURCHASED"
    LISTING_GONE = "LISTING_GONE"
    PRICE_CHANGED = "PRICE_CHANGED"
    RECEIVED_IN_STEAM = "RECEIVED_IN_STEAM"
    TRADEUP_COMPLETED = "TRADEUP_COMPLETED"
    OUTPUT_RECORDED = "OUTPUT_RECORDED"
    LISTED_FOR_SALE = "LISTED_FOR_SALE"
    SOLD = "SOLD"
    SETTLED = "SETTLED"


#: Venues with a documented, stable public listing URL. Anything absent gets a
#: "search by name" instruction instead of a fabricated deep link -- a wrong link is
#: worse than none, because it invites buying the wrong asset.
_LISTING_URL_TEMPLATES: Mapping[str, str] = {
    "csfloat": "https://csfloat.com/item/{listing_id}",
}


def venue_listing_url(venue: str, listing_id: str) -> str | None:
    template = _LISTING_URL_TEMPLATES.get(venue)
    return template.format(listing_id=listing_id) if template else None


@dataclass(frozen=True, slots=True)
class CardInput:
    """One exact listing the operator must buy."""

    position: int
    venue: str
    listing_id: str
    asset_id: str
    market_hash_name: str
    collection_id: str
    raw_float: Decimal
    normalized_float: Decimal
    price: Money
    buyer_fee: Money
    all_in_cost: Money
    trade_lock_days: int
    url: str | None
    survival_probability: Decimal

    @property
    def link_or_instruction(self) -> str:
        if self.url:
            return self.url
        return f"open {self.venue} and locate listing {self.listing_id} by asset id {self.asset_id}"


@dataclass(frozen=True, slots=True)
class CardOutcome:
    """One possible result of the contract."""

    skin_id: str
    market_hash_name: str
    probability_percent: Decimal
    output_float: Decimal
    wear: str
    net_proceeds: Money
    valuation_source: str
    confidence: str
    evidence_count: int
    expected_days_to_sale: int
    exit_venue: str


@dataclass(frozen=True, slots=True)
class PurchaseStep:
    """One step of the recommended purchase sequence, with running exposure."""

    order: int
    venue: str
    listing_id: str
    cost: Money
    cumulative_exposure: Money
    orphan_loss_if_next_fails: Money
    survival_probability: Decimal


@dataclass(frozen=True)
class OperatorCard:
    """Everything a human needs to decide, act and report back."""

    contract_id: str
    generated_at: datetime
    expires_at: datetime
    rule_version: str
    fee_schedule_id: str
    metadata_revision: str

    input_count: int
    inputs: tuple[CardInput, ...]
    outcomes: tuple[CardOutcome, ...]
    purchase_sequence: tuple[PurchaseStep, ...]

    acquisition_cost: Money
    all_in_cost: Money
    expected_output_value: Money
    net_ev: Money
    net_roi: Decimal
    lower_bound_ev: Money
    probability_of_profit_percent: Decimal
    worst_case_pnl: Money
    best_case_pnl: Money
    expected_capital_days: Decimal
    profit_per_capital_day: Money
    bundle_completion_probability: Decimal
    max_orphan_exposure: Money

    recommended_exit_venue: str
    exit_rationale: str
    manual_steam_steps: tuple[str, ...]
    assumptions: tuple[str, ...]
    warnings: tuple[str, ...] = field(default_factory=tuple)

    @property
    def is_synthetic_warning_present(self) -> bool:
        return any("synthetic" in w.lower() for w in self.warnings)

    def to_dict(self) -> dict[str, object]:
        """Machine-readable form. Used for JSON evidence and the CSV/Parquet export."""
        return {
            "contract_id": self.contract_id,
            "generated_at": self.generated_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "rule_version": self.rule_version,
            "fee_schedule_id": self.fee_schedule_id,
            "metadata_revision": self.metadata_revision,
            "input_count": self.input_count,
            "acquisition_cost_minor": self.acquisition_cost.minor_units,
            "all_in_cost_minor": self.all_in_cost.minor_units,
            "expected_output_value_minor": self.expected_output_value.minor_units,
            "net_ev_minor": self.net_ev.minor_units,
            "net_roi": str(self.net_roi),
            "lower_bound_ev_minor": self.lower_bound_ev.minor_units,
            "probability_of_profit_percent": str(self.probability_of_profit_percent),
            "worst_case_pnl_minor": self.worst_case_pnl.minor_units,
            "best_case_pnl_minor": self.best_case_pnl.minor_units,
            "expected_capital_days": str(self.expected_capital_days),
            "profit_per_capital_day_minor": self.profit_per_capital_day.minor_units,
            "bundle_completion_probability": str(self.bundle_completion_probability),
            "max_orphan_exposure_minor": self.max_orphan_exposure.minor_units,
            "recommended_exit_venue": self.recommended_exit_venue,
            "exit_rationale": self.exit_rationale,
            "currency": self.all_in_cost.currency.value,
            "inputs": [
                {
                    "position": i.position,
                    "venue": i.venue,
                    "listing_id": i.listing_id,
                    "asset_id": i.asset_id,
                    "market_hash_name": i.market_hash_name,
                    "collection_id": i.collection_id,
                    "raw_float": str(i.raw_float),
                    "normalized_float": str(i.normalized_float),
                    "price_minor": i.price.minor_units,
                    "all_in_cost_minor": i.all_in_cost.minor_units,
                    "trade_lock_days": i.trade_lock_days,
                    "url": i.url,
                    "survival_probability": str(i.survival_probability),
                }
                for i in self.inputs
            ],
            "outcomes": [
                {
                    "skin_id": o.skin_id,
                    "market_hash_name": o.market_hash_name,
                    "probability_percent": str(o.probability_percent),
                    "output_float": str(o.output_float),
                    "wear": o.wear,
                    "net_proceeds_minor": o.net_proceeds.minor_units,
                    "valuation_source": o.valuation_source,
                    "confidence": o.confidence,
                    "evidence_count": o.evidence_count,
                    "expected_days_to_sale": o.expected_days_to_sale,
                    "exit_venue": o.exit_venue,
                }
                for o in self.outcomes
            ],
            "purchase_sequence": [
                {
                    "order": s.order,
                    "venue": s.venue,
                    "listing_id": s.listing_id,
                    "cost_minor": s.cost.minor_units,
                    "cumulative_exposure_minor": s.cumulative_exposure.minor_units,
                    "orphan_loss_if_next_fails_minor": s.orphan_loss_if_next_fails.minor_units,
                    "survival_probability": str(s.survival_probability),
                }
                for s in self.purchase_sequence
            ],
            "manual_steam_steps": list(self.manual_steam_steps),
            "assumptions": list(self.assumptions),
            "warnings": list(self.warnings),
        }

    def summary_row(self) -> dict[str, str]:
        """One row for the ranked candidate table."""
        return {
            "contract": self.contract_id,
            "inputs": str(self.input_count),
            "cost": f"{self.acquisition_cost.as_major()}",
            "net_ev": f"{self.net_ev.as_major()}",
            "net_roi": f"{(self.net_roi * 100).quantize(Decimal('0.01'))}%",
            "p_profit": f"{self.probability_of_profit_percent}%",
            "worst_case": f"{self.worst_case_pnl.as_major()}",
            "capital_days": str(self.expected_capital_days),
            "completion_p": str(self.bundle_completion_probability),
        }


DEFAULT_STEAM_STEPS: tuple[str, ...] = (
    "Accept each incoming trade offer in the Steam client and confirm on the mobile "
    "authenticator. Do this yourself -- no part of this step may be automated.",
    "Verify in Steam that all inputs arrived and that each float matches the card.",
    "Open the trade-up contract screen in CS2 and add exactly the listed inputs.",
    "Re-check the input count and that no unintended item was added.",
    "Execute the contract manually.",
    "Record the actual output skin and its exact float against this contract id.",
    "List the output for sale at the recommended exit venue.",
    "Record the sale and, once proceeds settle, record the settled amount.",
)


def build_operator_card(
    candidate: TradeupCandidate,
    evaluation: CandidateEvaluation,
    assessment: PartialFillAssessment,
    *,
    metadata_revision: str,
    generated_at: datetime,
    extra_warnings: Sequence[str] = (),
) -> OperatorCard:
    """Assemble a complete card from a candidate, its economics and its fill risk."""
    listings_by_identity = {i.identity: i.listing for i in candidate.inputs}
    survival = assessment.per_listing_survival

    inputs: list[CardInput] = []
    for position, item in enumerate(sorted(candidate.inputs, key=lambda i: i.identity), start=1):
        listing = item.listing
        inputs.append(
            CardInput(
                position=position,
                venue=listing.identity.venue,
                listing_id=listing.identity.listing_id,
                asset_id=listing.asset_id,
                market_hash_name=listing.market_hash_name,
                collection_id=listing.collection_id,
                raw_float=listing.raw_float,
                normalized_float=listing.normalized_float,
                price=listing.price,
                buyer_fee=listing.buyer_fee,
                all_in_cost=listing.venue_acquisition_cost,
                trade_lock_days=listing.trade_lock_days_remaining(generated_at),
                url=venue_listing_url(listing.identity.venue, listing.identity.listing_id),
                survival_probability=survival.get(listing.identity, Decimal(0)),
            )
        )

    outcomes = tuple(
        CardOutcome(
            skin_id=outcome.skin_id,
            market_hash_name=outcome.valuation.market_hash_name,
            probability_percent=(
                Decimal(outcome.probability.numerator)
                / Decimal(outcome.probability.denominator)
                * 100
            ).quantize(Decimal("0.0001")),
            output_float=outcome.output_float,
            wear=outcome.wear.value,
            net_proceeds=outcome.valuation.net_proceeds,
            valuation_source=outcome.valuation.source.value,
            confidence=outcome.valuation.confidence.value,
            evidence_count=outcome.valuation.evidence_count,
            expected_days_to_sale=outcome.valuation.expected_days_to_sale,
            exit_venue=outcome.valuation.exit_venue,
        )
        for outcome in sorted(candidate.outcomes, key=lambda o: (-o.probability, o.skin_id))
    )

    sequence: list[PurchaseStep] = []
    for step_index, point in enumerate(assessment.failure_points):
        listing = listings_by_identity[point.identity]
        sequence.append(
            PurchaseStep(
                order=step_index + 1,
                venue=point.identity.venue,
                listing_id=point.identity.listing_id,
                cost=listing.venue_acquisition_cost,
                cumulative_exposure=point.cost_committed + listing.venue_acquisition_cost,
                orphan_loss_if_next_fails=point.loss_if_fails_here,
                survival_probability=survival.get(point.identity, Decimal(0)),
            )
        )

    best_outcome = max(candidate.outcomes, key=lambda o: o.valuation.net_proceeds.minor_units)
    exit_venue = best_outcome.valuation.exit_venue
    exit_rationale = (
        f"Highest modelled net proceeds come from {exit_venue} using "
        f"{best_outcome.valuation.source.value} evidence "
        f"({best_outcome.valuation.evidence_count} observation(s), "
        f"{best_outcome.valuation.confidence.value} confidence). "
        "Reconfirm the bid directly before listing."
    )

    assumptions = [
        f"Rule version {candidate.rule_version} governs input count, quality mixing "
        "and the float/probability formulas.",
        f"Metadata revision {metadata_revision} supplied float caps and output pools.",
        f"Fee schedule {evaluation.fee_schedule_id} was applied; an unknown fee would "
        "have rejected this candidate rather than defaulting to zero.",
        f"Output values use a low quantile of the best available evidence rung, "
        f"weakest outcome confidence {evaluation.output_confidence_floor}.",
        f"Bundle completion probability {assessment.completion_probability} is a "
        "prior derived from quote age and seller reliability, NOT a measured fill "
        "rate. It is the single least-evidenced number on this card.",
        f"Capital carry charged over {evaluation.expected_capital_days} days.",
        "Liquidity haircut and days-to-sale come from stated priors pending "
        "calibration against realised sales.",
    ]

    warnings = [
        "This card is a recommendation to spend real money. Nothing here has been "
        "validated against a settled, fee-net result.",
        *extra_warnings,
    ]

    return OperatorCard(
        contract_id=candidate.candidate_id,
        generated_at=generated_at,
        expires_at=candidate.expires_at,
        rule_version=candidate.rule_version,
        fee_schedule_id=evaluation.fee_schedule_id,
        metadata_revision=metadata_revision,
        input_count=candidate.input_count,
        inputs=tuple(inputs),
        outcomes=outcomes,
        purchase_sequence=tuple(sequence),
        acquisition_cost=evaluation.acquisition_cost,
        all_in_cost=evaluation.all_in_cost,
        expected_output_value=evaluation.expected_output_value,
        net_ev=evaluation.ev_net,
        net_roi=evaluation.roi_net,
        lower_bound_ev=evaluation.lower_bound_ev,
        probability_of_profit_percent=evaluation.probability_of_profit_percent,
        worst_case_pnl=evaluation.worst_case_pnl,
        best_case_pnl=evaluation.best_case_pnl,
        expected_capital_days=evaluation.expected_capital_days,
        profit_per_capital_day=evaluation.profit_per_capital_day,
        bundle_completion_probability=evaluation.bundle_completion_probability,
        max_orphan_exposure=assessment.max_orphan_exposure,
        recommended_exit_venue=exit_venue,
        exit_rationale=exit_rationale,
        manual_steam_steps=DEFAULT_STEAM_STEPS,
        assumptions=tuple(assumptions),
        warnings=tuple(warnings),
    )
