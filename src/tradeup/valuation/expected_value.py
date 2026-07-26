"""Expected value, ROI, downside and capital velocity for a bound candidate.

This module produces the single record every gate reads. Two properties matter more
than the formulas:

* **Exact probability weighting.** Outcome values are accumulated as exact rationals
  over minor units and rounded *once*, downward. Rounding each term separately would
  let ten small favourable roundings accumulate into a contract that clears the gate
  on arithmetic noise.
* **Conservative rounding throughout.** Costs round up, revenues round down. Where
  the direction is ambiguous the pessimistic reading wins, because the failure mode
  we care about is approving a contract that loses money, not skipping one that
  would have made three dollars.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_FLOOR, Decimal
from fractions import Fraction

from tradeup.config import Settings
from tradeup.domain.contracts import CandidateEvaluation, TradeupCandidate, TradeupOutcome
from tradeup.domain.money import BalanceType, Currency, Money, money_sum
from tradeup.domain.valuation import ValuationConfidence
from tradeup.valuation.capital import CapitalModel, CapitalTimeline
from tradeup.valuation.partial_fill import PartialFillAssessment, PartialFillModel

__all__ = ["ExpectedValueEngine", "probability_weighted"]


def probability_weighted(terms: Sequence[tuple[Fraction, Money]], currency: Currency) -> Money:
    """Exact ``Σ p·v``, rounded down once at the end.

    Accumulating in :class:`~fractions.Fraction` and rounding a single time is the
    difference between a defensible expectation and one that drifts by a cent per
    outcome in whichever direction the rounding mode happens to favour.
    """
    if not terms:
        return Money.zero(currency, BalanceType.CASH_WITHDRAWABLE)
    total = Fraction(0)
    for probability, value in terms:
        if value.currency is not currency:
            raise ValueError(f"expected {currency}, got {value.currency}")
        total += probability * value.minor_units
    minor = int(
        (Decimal(total.numerator) / Decimal(total.denominator)).to_integral_value(
            rounding=ROUND_FLOOR
        )
    )
    return Money(minor, currency, BalanceType.CASH_WITHDRAWABLE)


@dataclass(frozen=True)
class _CostBreakdown:
    input_cost: Money
    buyer_fees: Money
    deposit_fees: Money
    fx_cost: Money
    payment_surcharge: Money

    @property
    def acquisition_cost(self) -> Money:
        return (
            self.input_cost
            + self.buyer_fees
            + self.deposit_fees
            + self.fx_cost
            + self.payment_surcharge
        )


class ExpectedValueEngine:
    """Turns a candidate plus a moment into a complete :class:`CandidateEvaluation`."""

    def __init__(
        self,
        *,
        settings: Settings,
        capital_model: CapitalModel,
        partial_fill_model: PartialFillModel,
        settlement_charge_rate: Fraction | None = None,
    ) -> None:
        self._settings = settings
        self._capital = capital_model
        self._partial_fill = partial_fill_model
        self._currency = settings.base_currency
        if settlement_charge_rate is not None and settlement_charge_rate < 0:
            raise ValueError(
                f"a settlement charge rate cannot be negative, got {settlement_charge_rate}"
            )
        self._settlement_rate = settlement_charge_rate or Fraction(0)

    # -- components ----------------------------------------------------------

    def _costs(self, candidate: TradeupCandidate) -> _CostBreakdown:
        zero = Money.zero(self._currency, BalanceType.CASH_WITHDRAWABLE)
        listings = [i.listing for i in candidate.inputs]
        for listing in listings:
            if listing.currency is not self._currency:
                raise ValueError(
                    f"listing {listing.identity} is priced in {listing.currency}, "
                    f"but the base currency is {self._currency}; an FX quote is required"
                )
            if listing.balance_type is not BalanceType.CASH_WITHDRAWABLE:
                raise ValueError(
                    f"listing {listing.identity} is priced in {listing.balance_type}; "
                    "acquiring with non-cash balance needs a documented conversion"
                )
        return _CostBreakdown(
            input_cost=money_sum((s.price for s in listings), currency=self._currency),
            buyer_fees=money_sum((s.buyer_fee for s in listings), currency=self._currency),
            deposit_fees=money_sum((s.deposit_fee for s in listings), currency=self._currency),
            # Same-currency, same-balance-type inputs incur neither. Both are kept as
            # explicit zeros so the operator card shows they were considered.
            fx_cost=zero,
            payment_surcharge=zero,
        )

    def _expected_days_to_sale(self, outcomes: Sequence[TradeupOutcome]) -> int:
        """Probability-weighted days to sell, rounded up."""
        total = Fraction(0)
        for outcome in outcomes:
            total += outcome.probability * outcome.valuation.expected_days_to_sale
        return int(-(-total.numerator // total.denominator))

    def _timeline(
        self, candidate: TradeupCandidate, moment: datetime, outcomes: Sequence[TradeupOutcome]
    ) -> CapitalTimeline:
        return self._capital.timeline(
            max_input_trade_lock_days=candidate.max_trade_lock_days(moment),
            expected_days_to_sale=self._expected_days_to_sale(outcomes),
        )

    def _confidence_floor(self, outcomes: Sequence[TradeupOutcome]) -> str:
        valuable = [o.valuation.confidence for o in outcomes if o.is_valuable]
        if not valuable:
            return ValuationConfidence.NONE.value
        order = [
            ValuationConfidence.NONE,
            ValuationConfidence.LOW,
            ValuationConfidence.MEDIUM,
            ValuationConfidence.HIGH,
        ]
        return min(valuable, key=order.index).value

    # -- evaluation ----------------------------------------------------------

    def evaluate(
        self,
        candidate: TradeupCandidate,
        *,
        moment: datetime,
        fee_schedule_id: str,
        partial_fill: PartialFillAssessment | None = None,
    ) -> CandidateEvaluation:
        """Full economics for one candidate at one moment."""
        outcomes = candidate.outcomes
        costs = self._costs(candidate)
        acquisition = costs.acquisition_cost

        assessment = partial_fill or self._partial_fill.assess(candidate.inputs, moment)
        timeline = self._timeline(candidate, moment, outcomes)

        operational = self._settings.operational_cost_per_contract
        # The settlement rail charges each contract its share of the capital round
        # trip, proportional to the capital the contract actually uses.
        settlement = acquisition.scaled_up(self._settlement_rate)
        carry = self._capital.carry_cost(acquisition, timeline.total_days)
        reserve = assessment.expected_loss

        all_in = acquisition + operational + settlement + carry + reserve

        # Expected value, and the confidence-discounted lower bound.
        expected_output_value = probability_weighted(
            [(o.probability, o.valuation.net_proceeds) for o in outcomes], self._currency
        )
        lower_bound_output_value = probability_weighted(
            [(o.probability, o.valuation.lower_bound_proceeds) for o in outcomes], self._currency
        )

        ev_net = expected_output_value - all_in
        lower_bound_ev = lower_bound_output_value - all_in

        roi_net = ev_net.ratio_to(acquisition) if not acquisition.is_zero else Decimal(0)

        probability_of_profit = sum(
            (o.probability for o in outcomes if o.valuation.net_proceeds > all_in),
            Fraction(0),
        )

        net_values = [o.valuation.net_proceeds for o in outcomes]
        worst_case = min(net_values) - all_in
        best_case = max(net_values) - all_in

        expected_capital_days = Decimal(timeline.total_days)
        profit_per_day = self._capital.profit_per_capital_day(
            ev_net if ev_net.is_positive else Money.zero(self._currency),
            expected_capital_days,
        )

        return CandidateEvaluation(
            candidate_id=candidate.candidate_id,
            rule_version=candidate.rule_version,
            fee_schedule_id=fee_schedule_id,
            evaluated_at=moment,
            input_cost=costs.input_cost,
            buyer_fees=costs.buyer_fees,
            deposit_fees=costs.deposit_fees,
            fx_cost=costs.fx_cost,
            payment_surcharge=costs.payment_surcharge,
            acquisition_cost=acquisition,
            operational_cost=operational,
            settlement_cost=settlement,
            capital_carry_cost=carry,
            partial_fill_reserve=reserve,
            all_in_cost=all_in,
            expected_output_value=expected_output_value,
            expected_partial_fill_loss=assessment.expected_loss,
            ev_net=ev_net,
            roi_net=roi_net,
            lower_bound_ev=lower_bound_ev,
            probability_of_profit=probability_of_profit,
            worst_case_pnl=worst_case,
            best_case_pnl=best_case,
            expected_capital_days=expected_capital_days,
            profit_per_capital_day=profit_per_day,
            bundle_completion_probability=assessment.completion_probability,
            expected_orphan_loss=assessment.max_orphan_exposure,
            unvaluable_probability_mass=candidate.unvaluable_probability_mass,
            max_quote_age_seconds=candidate.oldest_quote_age_seconds(moment),
            output_confidence_floor=self._confidence_floor(outcomes),
        )
