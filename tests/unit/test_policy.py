"""Risk gates: every threshold, and the requirement that all reasons are reported."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from fractions import Fraction

import pytest
from tests.factories import NOW, make_candidate, make_listing, make_outcome, usd

from tradeup.adapters.base import ListingVerification
from tradeup.config import Settings
from tradeup.domain.contracts import CandidateEvaluation, RejectionReason, TradeupCandidate
from tradeup.domain.execution import ExecutionMode
from tradeup.domain.listings import ListingStatus
from tradeup.domain.rules import RULESET_2026_05, RuleValidationStatus, TradeupRuleSet
from tradeup.execution.policy import GateDecision, RiskPolicy


def settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "database_url": "sqlite+pysqlite:///:memory:",
        "discovery_min_net_roi": Decimal("0.12"),
        "final_min_net_roi": Decimal("0.08"),
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def evaluation(**overrides: object) -> CandidateEvaluation:
    """A comfortably passing evaluation; tests degrade one field at a time."""
    base: dict[str, object] = {
        "candidate_id": "TU-test",
        "rule_version": RULESET_2026_05.rule_version,
        "fee_schedule_id": "test",
        "evaluated_at": NOW,
        "input_cost": usd(1000),
        "buyer_fees": usd(20),
        "deposit_fees": usd(0),
        "fx_cost": usd(0),
        "payment_surcharge": usd(0),
        "acquisition_cost": usd(1020),
        "operational_cost": usd(0),
        "capital_carry_cost": usd(5),
        "partial_fill_reserve": usd(25),
        "all_in_cost": usd(1050),
        "expected_output_value": usd(2000),
        "expected_partial_fill_loss": usd(25),
        "ev_net": usd(950),
        "roi_net": Decimal("0.93"),
        "lower_bound_ev": usd(800),
        "probability_of_profit": Fraction(1),
        "worst_case_pnl": usd(-200),
        "best_case_pnl": usd(1200),
        "expected_capital_days": Decimal(6),
        "profit_per_capital_day": usd(158),
        "bundle_completion_probability": Decimal("0.9"),
        "expected_orphan_loss": usd(300),
        "unvaluable_probability_mass": Fraction(0),
        "max_quote_age_seconds": Decimal(30),
        "output_confidence_floor": "HIGH",
    }
    base.update(overrides)
    return CandidateEvaluation(**base)  # type: ignore[arg-type]


def all_verified(
    candidate: TradeupCandidate, **overrides: object
) -> dict[str, ListingVerification]:
    result: dict[str, ListingVerification] = {}
    for item in candidate.inputs:
        listing_id = item.identity.listing_id
        result[listing_id] = ListingVerification(
            identity=item.identity,
            status=ListingStatus.ACTIVE,
            verified_at=NOW,
            price=item.listing.price,
            raw_float=item.listing.raw_float,
            asset_id=item.listing.asset_id,
        )
    for key, value in overrides.items():
        result[key] = value  # type: ignore[assignment]
    return result


def final(
    policy: RiskPolicy,
    candidate: TradeupCandidate,
    ev: CandidateEvaluation,
    *,
    ruleset: TradeupRuleSet = RULESET_2026_05,
    verifications: dict[str, ListingVerification] | None = None,
    reserved: list[str] | None = None,
) -> GateDecision:
    return policy.approves_for_operator_card(
        candidate,
        ev,
        moment=NOW,
        ruleset=ruleset,
        verifications=verifications if verifications is not None else all_verified(candidate),
        reserved_listing_ids=(
            reserved if reserved is not None else [i.identity.listing_id for i in candidate.inputs]
        ),
    )


class TestDiscoveryGate:
    def test_approves_a_comfortable_candidate(self) -> None:
        policy = RiskPolicy(settings())
        decision = policy.evaluate_discovery(make_candidate(), evaluation(), moment=NOW)
        assert decision.approved

    def test_rejects_below_the_discovery_roi(self) -> None:
        policy = RiskPolicy(settings())
        decision = policy.evaluate_discovery(
            make_candidate(), evaluation(roi_net=Decimal("0.05")), moment=NOW
        )
        assert RejectionReason.BELOW_DISCOVERY_ROI in decision.reasons

    def test_rejects_an_expired_candidate(self) -> None:
        policy = RiskPolicy(settings())
        candidate = make_candidate(created_at=NOW - timedelta(hours=1), ttl=timedelta(minutes=5))
        decision = policy.evaluate_discovery(candidate, evaluation(), moment=NOW)
        assert RejectionReason.QUOTE_TOO_OLD in decision.reasons

    def test_rejects_a_stale_quote(self) -> None:
        policy = RiskPolicy(settings(max_quote_age_seconds=60))
        decision = policy.evaluate_discovery(
            make_candidate(), evaluation(max_quote_age_seconds=Decimal(600)), moment=NOW
        )
        assert RejectionReason.QUOTE_TOO_OLD in decision.reasons

    def test_rejects_an_oversized_contract(self) -> None:
        policy = RiskPolicy(settings(max_contract_cost_minor=500))
        decision = policy.evaluate_discovery(make_candidate(), evaluation(), moment=NOW)
        assert RejectionReason.CONTRACT_COST_EXCEEDED in decision.reasons


class TestFinalGate:
    def test_approves_when_everything_checks_out(self) -> None:
        policy = RiskPolicy(settings())
        assert final(policy, make_candidate(), evaluation()).approved

    def test_reports_every_failing_reason_not_just_the_first(self) -> None:
        """Rejection statistics by cause are the point; short-circuiting loses them."""
        policy = RiskPolicy(settings(max_worst_case_loss_minor=10))
        decision = final(
            policy,
            make_candidate(),
            evaluation(
                roi_net=Decimal("0.01"),
                lower_bound_ev=usd(-100),
                worst_case_pnl=usd(-99999),
                expected_capital_days=Decimal(999),
            ),
        )
        assert RejectionReason.BELOW_DISCOVERY_ROI in decision.reasons
        assert RejectionReason.BELOW_FINAL_ROI in decision.reasons
        assert RejectionReason.NEGATIVE_LOWER_BOUND_EV in decision.reasons
        assert RejectionReason.WORST_CASE_LOSS_EXCEEDED in decision.reasons
        assert RejectionReason.CAPITAL_DAYS_EXCEEDED in decision.reasons
        assert len(decision.reasons) >= 5

    def test_missing_revalidation_is_an_incomplete_bundle(self) -> None:
        policy = RiskPolicy(settings())
        decision = final(policy, make_candidate(), evaluation(), verifications={})
        assert RejectionReason.INCOMPLETE_BUNDLE in decision.reasons

    def test_disappeared_listing_rejects(self) -> None:
        policy = RiskPolicy(settings())
        candidate = make_candidate()
        target = candidate.inputs[0]
        verifications = all_verified(candidate)
        verifications[target.identity.listing_id] = ListingVerification(
            identity=target.identity,
            status=ListingStatus.WITHDRAWN,
            verified_at=NOW,
            detail="gone",
        )
        decision = final(policy, candidate, evaluation(), verifications=verifications)
        assert RejectionReason.LISTING_DISAPPEARED in decision.reasons

    def test_price_change_rejects_and_is_not_reported_as_disappearance(self) -> None:
        """A repriced listing is still there. The two events must stay distinct."""
        policy = RiskPolicy(settings())
        candidate = make_candidate()
        target = candidate.inputs[0]
        verifications = all_verified(candidate)
        verifications[target.identity.listing_id] = ListingVerification(
            identity=target.identity,
            status=ListingStatus.PRICE_CHANGED,
            verified_at=NOW,
            price=usd(99_999),
            raw_float=target.listing.raw_float,
            asset_id=target.listing.asset_id,
        )
        decision = final(policy, candidate, evaluation(), verifications=verifications)
        assert RejectionReason.LISTING_PRICE_CHANGED in decision.reasons
        assert RejectionReason.LISTING_DISAPPEARED not in decision.reasons

    def test_float_mismatch_rejects(self) -> None:
        policy = RiskPolicy(settings())
        candidate = make_candidate()
        target = candidate.inputs[0]
        verifications = all_verified(candidate)
        verifications[target.identity.listing_id] = ListingVerification(
            identity=target.identity,
            status=ListingStatus.ACTIVE,
            verified_at=NOW,
            price=target.listing.price,
            raw_float=Decimal("0.99"),
            asset_id=target.listing.asset_id,
        )
        decision = final(policy, candidate, evaluation(), verifications=verifications)
        assert RejectionReason.LISTING_FLOAT_MISMATCH in decision.reasons

    def test_asset_identity_mismatch_rejects(self) -> None:
        policy = RiskPolicy(settings())
        candidate = make_candidate()
        target = candidate.inputs[0]
        verifications = all_verified(candidate)
        verifications[target.identity.listing_id] = ListingVerification(
            identity=target.identity,
            status=ListingStatus.ACTIVE,
            verified_at=NOW,
            price=target.listing.price,
            raw_float=target.listing.raw_float,
            asset_id="a-completely-different-asset",
        )
        decision = final(policy, candidate, evaluation(), verifications=verifications)
        assert RejectionReason.LISTING_IDENTITY_MISMATCH in decision.reasons

    def test_unreserved_listings_reject(self) -> None:
        policy = RiskPolicy(settings())
        decision = final(policy, make_candidate(), evaluation(), reserved=[])
        assert RejectionReason.ASSET_ALREADY_RESERVED in decision.reasons

    def test_unverified_ruleset_rejects(self) -> None:
        policy = RiskPolicy(settings())
        unverified = TradeupRuleSet(
            rule_version="unverified",
            effective_from=RULESET_2026_05.effective_from,
            effective_until=None,
            input_count_by_rarity=RULESET_2026_05.input_count_by_rarity,
            output_quality_by_input_group=RULESET_2026_05.output_quality_by_input_group,
            attribute_stripping_qualities=frozenset(),
            float_method=RULESET_2026_05.float_method,
            probability_method=RULESET_2026_05.probability_method,
            source_references=(),
            validated_at=None,
            validation_status=RuleValidationStatus.UNVERIFIED,
        )
        decision = final(policy, make_candidate(), evaluation(), ruleset=unverified)
        assert RejectionReason.UNVERIFIED_RULESET in decision.reasons

    def test_unvaluable_output_mass_rejects(self) -> None:
        policy = RiskPolicy(settings(max_unvaluable_probability_mass=Decimal("0.05")))
        decision = final(
            policy, make_candidate(), evaluation(unvaluable_probability_mass=Fraction(1, 2))
        )
        assert RejectionReason.UNVALUABLE_OUTPUT_MASS in decision.reasons

    def test_no_output_confidence_rejects_on_liquidity(self) -> None:
        policy = RiskPolicy(settings())
        decision = final(policy, make_candidate(), evaluation(output_confidence_floor="NONE"))
        assert RejectionReason.INSUFFICIENT_OUTPUT_LIQUIDITY in decision.reasons

    def test_partial_fill_exposure_rejects(self) -> None:
        policy = RiskPolicy(settings(max_partial_fill_exposure_minor=100))
        decision = final(policy, make_candidate(), evaluation(expected_orphan_loss=usd(5000)))
        assert RejectionReason.PARTIAL_FILL_EXPOSURE_EXCEEDED in decision.reasons

    def test_below_minimum_absolute_profit_rejects(self) -> None:
        policy = RiskPolicy(settings(min_absolute_expected_profit_minor=100_000))
        decision = final(policy, make_candidate(), evaluation())
        assert RejectionReason.BELOW_MIN_ABSOLUTE_PROFIT in decision.reasons


class TestExecutionBoundary:
    def test_unsupported_execution_mode_is_never_approved(self) -> None:
        policy = RiskPolicy(settings())
        candidate = make_candidate()
        decision = policy.evaluate_final(
            candidate,
            evaluation(),
            moment=NOW,
            ruleset=RULESET_2026_05,
            verifications=all_verified(candidate),
            execution_mode=ExecutionMode.UNSUPPORTED,
            reserved_listing_ids=[i.identity.listing_id for i in candidate.inputs],
        )
        assert RejectionReason.EXECUTION_MODE_NOT_PERMITTED in decision.reasons

    def test_automated_execution_blocked_while_live_execution_is_off(self) -> None:
        policy = RiskPolicy(settings())
        candidate = make_candidate()
        decision = policy.evaluate_final(
            candidate,
            evaluation(),
            moment=NOW,
            ruleset=RULESET_2026_05,
            verifications=all_verified(candidate),
            execution_mode=ExecutionMode.AUTOMATED,
            reserved_listing_ids=[i.identity.listing_id for i in candidate.inputs],
        )
        assert RejectionReason.LIVE_EXECUTION_DISABLED in decision.reasons

    def test_daily_spend_limit_of_zero_blocks_automated_purchase(self) -> None:
        policy = RiskPolicy(settings())
        candidate = make_candidate()
        decision = policy.evaluate_final(
            candidate,
            evaluation(),
            moment=NOW,
            ruleset=RULESET_2026_05,
            verifications=all_verified(candidate),
            execution_mode=ExecutionMode.OPERATOR_APPROVAL_REQUIRED,
            reserved_listing_ids=[i.identity.listing_id for i in candidate.inputs],
        )
        assert RejectionReason.DAILY_SPEND_LIMIT_EXCEEDED in decision.reasons

    def test_operator_card_gate_waives_only_the_spend_and_live_switches(self) -> None:
        """A card is an instruction to a human, so the zero spend cap does not apply.

        Every economic and revalidation check still does.
        """
        policy = RiskPolicy(settings())
        assert final(policy, make_candidate(), evaluation()).approved

    def test_insufficient_balance_rejects(self) -> None:
        policy = RiskPolicy(settings())
        candidate = make_candidate()
        decision = policy.evaluate_final(
            candidate,
            evaluation(),
            moment=NOW,
            ruleset=RULESET_2026_05,
            verifications=all_verified(candidate),
            execution_mode=ExecutionMode.OPERATOR_APPROVAL_REQUIRED,
            reserved_listing_ids=[i.identity.listing_id for i in candidate.inputs],
            available_balance=usd(1),
        )
        assert RejectionReason.INSUFFICIENT_BALANCE in decision.reasons


class TestGateDecision:
    def test_an_approved_decision_cannot_carry_reasons(self) -> None:
        with pytest.raises(ValueError, match="cannot carry rejection reasons"):
            GateDecision(
                candidate_id="x",
                stage="s",
                approved=True,
                reasons=(RejectionReason.BELOW_FINAL_ROI,),
            )

    def test_a_rejection_must_carry_a_reason(self) -> None:
        with pytest.raises(ValueError, match="at least one reason code"):
            GateDecision(candidate_id="x", stage="s", approved=False, reasons=())

    def test_rejection_conversion_preserves_codes(self) -> None:
        decision = GateDecision(
            candidate_id="x",
            stage="final",
            approved=False,
            reasons=(RejectionReason.BELOW_FINAL_ROI,),
            details={"roi": "0.01"},
        )
        rejection = decision.to_rejection(NOW)
        assert rejection.primary_reason is RejectionReason.BELOW_FINAL_ROI
        assert rejection.details["roi"] == "0.01"

    def test_cannot_build_a_rejection_from_an_approval(self) -> None:
        decision = GateDecision(candidate_id="x", stage="final", approved=True)
        with pytest.raises(ValueError, match="cannot build a rejection"):
            decision.to_rejection(NOW)


def test_duplicate_asset_allocation_is_impossible_at_construction() -> None:
    """The candidate model refuses it, so the gate's check is a second line."""
    duplicate = make_listing("dup", asset_id="same-asset")
    other = make_listing("dup2", asset_id="same-asset")
    with pytest.raises(ValueError, match="physical asset cannot occupy two slots"):
        make_candidate(
            listings=[duplicate, other],
            outcomes=[make_outcome("a-out-1", probability=Fraction(1))],
        )
