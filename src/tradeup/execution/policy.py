"""Risk gates.

Two gates, run at different moments:

* **Discovery** -- cheap, runs on every candidate the optimizer produces. Uses the
  looser ROI threshold, because the point of the discovery buffer is to absorb the
  drift between the scan and the revalidation that follows it.
* **Final** -- runs only after every listing has been requeried directly. Uses the
  strict threshold and checks everything else the profitability model demands.

Both collect **all** failing reasons rather than short-circuiting on the first. A
candidate that fails on four counts is a different data point from one that fails on
one, and rejection statistics by cause are the primary evidence this whole system is
built to produce. Short-circuiting would quietly discard that.

The gate never approves execution. The most it returns is "this is worth showing a
human", and :meth:`RiskPolicy.evaluate_final` still refuses anything whose venue has
no sanctioned purchase path.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from tradeup.adapters.base import ListingVerification
from tradeup.config import Settings
from tradeup.domain.contracts import (
    CandidateEvaluation,
    CandidateRejection,
    RejectionReason,
    TradeupCandidate,
)
from tradeup.domain.execution import ExecutionMode
from tradeup.domain.money import Money
from tradeup.domain.rules import TradeupRuleSet

__all__ = ["GateDecision", "RiskPolicy"]


@dataclass(frozen=True)
class GateDecision:
    """Outcome of one gate. Carries every reason, not just the first."""

    candidate_id: str
    stage: str
    approved: bool
    reasons: tuple[RejectionReason, ...] = field(default_factory=tuple)
    details: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.approved and self.reasons:
            raise ValueError("an approved decision cannot carry rejection reasons")
        if not self.approved and not self.reasons:
            raise ValueError("a rejection must carry at least one reason code")

    def to_rejection(self, moment: datetime) -> CandidateRejection:
        if self.approved:
            raise ValueError("cannot build a rejection from an approved decision")
        return CandidateRejection(
            candidate_id=self.candidate_id,
            stage=self.stage,
            reasons=self.reasons,
            rejected_at=moment,
            details=dict(self.details),
        )


class RiskPolicy:
    """Applies the configured gates to an evaluated candidate."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    @property
    def settings(self) -> Settings:
        return self._settings

    # -- discovery -----------------------------------------------------------

    def evaluate_discovery(
        self,
        candidate: TradeupCandidate,
        evaluation: CandidateEvaluation,
        *,
        moment: datetime,
    ) -> GateDecision:
        """Cheap pre-filter using the looser ROI bar."""
        settings = self._settings
        reasons: list[RejectionReason] = []
        details: dict[str, str] = {
            "roi_net": str(evaluation.roi_net),
            "ev_net_minor": str(evaluation.ev_net.minor_units),
            "acquisition_cost_minor": str(evaluation.acquisition_cost.minor_units),
        }

        if evaluation.roi_net < settings.discovery_min_net_roi:
            reasons.append(RejectionReason.BELOW_DISCOVERY_ROI)
        if evaluation.acquisition_cost > settings.max_contract_cost:
            reasons.append(RejectionReason.CONTRACT_COST_EXCEEDED)
        if candidate.is_expired(moment):
            reasons.append(RejectionReason.QUOTE_TOO_OLD)
            details["expired_at"] = candidate.expires_at.isoformat()
        if evaluation.max_quote_age_seconds > Decimal(settings.max_quote_age_seconds):
            reasons.append(RejectionReason.QUOTE_TOO_OLD)
            details["max_quote_age_seconds"] = str(evaluation.max_quote_age_seconds)

        return GateDecision(
            candidate_id=candidate.candidate_id,
            stage="discovery",
            approved=not reasons,
            reasons=tuple(dict.fromkeys(reasons)),
            details=details,
        )

    # -- final ---------------------------------------------------------------

    def evaluate_final(
        self,
        candidate: TradeupCandidate,
        evaluation: CandidateEvaluation,
        *,
        moment: datetime,
        ruleset: TradeupRuleSet,
        verifications: Mapping[str, ListingVerification],
        execution_mode: ExecutionMode,
        reserved_listing_ids: Sequence[str],
        available_balance: Money | None = None,
        spent_today: Money | None = None,
    ) -> GateDecision:
        """The full gate. Every listing must already have been requeried directly."""
        settings = self._settings
        reasons: list[RejectionReason] = []
        details: dict[str, str] = {
            "roi_net": str(evaluation.roi_net),
            "lower_bound_ev_minor": str(evaluation.lower_bound_ev.minor_units),
            "worst_case_minor": str(evaluation.worst_case_pnl.minor_units),
            "capital_days": str(evaluation.expected_capital_days),
            "completion_probability": str(evaluation.bundle_completion_probability),
        }

        # 1. Still above the discovery threshold it was surfaced on.
        if evaluation.roi_net < settings.discovery_min_net_roi:
            reasons.append(RejectionReason.BELOW_DISCOVERY_ROI)

        # 2-4. Direct revalidation of every exact listing.
        for item in candidate.inputs:
            listing_id = item.identity.listing_id
            verification = verifications.get(listing_id)
            if verification is None:
                reasons.append(RejectionReason.INCOMPLETE_BUNDLE)
                details[f"unverified:{listing_id}"] = "no direct revalidation was performed"
                continue
            if not verification.is_present:
                reasons.append(RejectionReason.LISTING_DISAPPEARED)
                details[f"gone:{listing_id}"] = verification.status.value
                continue
            # Still there. A price move is recorded as a price move, and the
            # remaining identity checks still run against the listing we found.
            if verification.price_changed_from(item.listing.price):
                reasons.append(RejectionReason.LISTING_PRICE_CHANGED)
                details[f"price:{listing_id}"] = (
                    f"{item.listing.price.minor_units} -> "
                    f"{verification.price.minor_units if verification.price else 'unknown'}"
                )
            if verification.float_changed_from(item.listing.raw_float):
                reasons.append(RejectionReason.LISTING_FLOAT_MISMATCH)
                details[f"float:{listing_id}"] = (
                    f"{item.listing.raw_float} -> {verification.raw_float}"
                )
            if verification.identity_changed_from(item.listing.asset_id):
                reasons.append(RejectionReason.LISTING_IDENTITY_MISMATCH)
                details[f"asset:{listing_id}"] = (
                    f"{item.listing.asset_id} -> {verification.asset_id}"
                )
            if not item.listing.is_usable:
                reasons.append(RejectionReason.LISTING_NOT_PURCHASABLE)

        # 5. Quote freshness after revalidation.
        if evaluation.max_quote_age_seconds > Decimal(settings.max_quote_age_seconds):
            reasons.append(RejectionReason.QUOTE_TOO_OLD)
            details["max_quote_age_seconds"] = str(evaluation.max_quote_age_seconds)

        # 6. The governing ruleset must be trustworthy enough to bet on.
        if not ruleset.validation_status.approvable:
            reasons.append(RejectionReason.UNVERIFIED_RULESET)
            details["rule_validation_status"] = ruleset.validation_status.value

        # 7. Output valuation quality.
        if evaluation.unvaluable_probability_mass > 0:
            mass = Decimal(evaluation.unvaluable_probability_mass.numerator) / Decimal(
                evaluation.unvaluable_probability_mass.denominator
            )
            details["unvaluable_probability_mass"] = str(mass)
            if mass > settings.max_unvaluable_probability_mass:
                reasons.append(RejectionReason.UNVALUABLE_OUTPUT_MASS)
        weakest = evaluation.output_confidence_floor
        if weakest == "NONE":
            reasons.append(RejectionReason.INSUFFICIENT_OUTPUT_LIQUIDITY)
            details["output_confidence_floor"] = weakest

        # 8-9. The economics themselves.
        if evaluation.roi_net < settings.final_min_net_roi:
            reasons.append(RejectionReason.BELOW_FINAL_ROI)
        if evaluation.ev_net < settings.min_absolute_expected_profit:
            reasons.append(RejectionReason.BELOW_MIN_ABSOLUTE_PROFIT)
        if not evaluation.lower_bound_ev.is_positive:
            reasons.append(RejectionReason.NEGATIVE_LOWER_BOUND_EV)

        # 10. Downside limits.
        if abs(evaluation.worst_case_pnl) > settings.max_worst_case_loss:
            reasons.append(RejectionReason.WORST_CASE_LOSS_EXCEEDED)
        if evaluation.acquisition_cost > settings.max_contract_cost:
            reasons.append(RejectionReason.CONTRACT_COST_EXCEEDED)
        if evaluation.expected_orphan_loss > settings.max_partial_fill_exposure:
            reasons.append(RejectionReason.PARTIAL_FILL_EXPOSURE_EXCEEDED)
            details["orphan_exposure_minor"] = str(evaluation.expected_orphan_loss.minor_units)
        if evaluation.expected_capital_days > Decimal(settings.max_expected_capital_days):
            reasons.append(RejectionReason.CAPITAL_DAYS_EXCEEDED)

        # 11. No duplicated asset allocation.
        if len(set(candidate.asset_ids)) != len(candidate.asset_ids):
            reasons.append(RejectionReason.DUPLICATE_ASSET_ALLOCATION)

        # 12. Every listing must be reserved to this candidate.
        reserved = set(reserved_listing_ids)
        missing = [
            i.identity.listing_id for i in candidate.inputs if i.identity.listing_id not in reserved
        ]
        if missing:
            reasons.append(RejectionReason.ASSET_ALREADY_RESERVED)
            details["unreserved"] = ",".join(sorted(missing))

        # 13. Balance and spend limits.
        if available_balance is not None and available_balance < evaluation.acquisition_cost:
            reasons.append(RejectionReason.INSUFFICIENT_BALANCE)
        projected = evaluation.acquisition_cost
        if spent_today is not None:
            projected = projected + spent_today
        if projected > settings.max_daily_spend:
            reasons.append(RejectionReason.DAILY_SPEND_LIMIT_EXCEEDED)
            details["daily_spend_limit_minor"] = str(settings.max_daily_spend.minor_units)

        # 14. The execution method must be sanctioned.
        if execution_mode is ExecutionMode.UNSUPPORTED:
            reasons.append(RejectionReason.EXECUTION_MODE_NOT_PERMITTED)
        if not settings.live_execution_enabled and execution_mode is ExecutionMode.AUTOMATED:
            reasons.append(RejectionReason.LIVE_EXECUTION_DISABLED)

        return GateDecision(
            candidate_id=candidate.candidate_id,
            stage="final",
            approved=not reasons,
            reasons=tuple(dict.fromkeys(reasons)),
            details=details,
        )

    # -- operator-card gate --------------------------------------------------

    def approves_for_operator_card(
        self,
        candidate: TradeupCandidate,
        evaluation: CandidateEvaluation,
        *,
        moment: datetime,
        ruleset: TradeupRuleSet,
        verifications: Mapping[str, ListingVerification],
        reserved_listing_ids: Sequence[str],
    ) -> GateDecision:
        """Gate for producing an operator card rather than an automated purchase.

        Identical to the final gate except that the daily-spend ceiling and the
        live-execution switch do not apply: a card is an instruction to a human, and
        during groundwork the spend limit is deliberately zero. Every economic and
        revalidation check still applies in full -- the card is a recommendation to
        spend real money, so its bar is the strict one.
        """
        decision = self.evaluate_final(
            candidate,
            evaluation,
            moment=moment,
            ruleset=ruleset,
            verifications=verifications,
            execution_mode=ExecutionMode.OPERATOR_APPROVAL_REQUIRED,
            reserved_listing_ids=reserved_listing_ids,
            available_balance=None,
            spent_today=None,
        )
        filtered = tuple(
            reason
            for reason in decision.reasons
            if reason
            not in {
                RejectionReason.DAILY_SPEND_LIMIT_EXCEEDED,
                RejectionReason.LIVE_EXECUTION_DISABLED,
            }
        )
        return GateDecision(
            candidate_id=decision.candidate_id,
            stage="operator_card",
            approved=not filtered,
            reasons=filtered,
            details=decision.details,
        )
