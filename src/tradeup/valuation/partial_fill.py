"""Partial-fill risk.

A trade-up contract is a basket order with no atomic cross-venue execution. You buy
ten specific assets one at a time, and any of them can vanish between the scan and
the click. What you are left holding is not a contract -- it is an orphan inventory
of items you bought at retail and must now liquidate at a loss.

That cost is charged to EV *before* the first purchase, not discovered afterwards.

The survival model is a stated prior: a listing's probability of still being there
decays with quote age and is scaled by any venue-published seller reliability. It is
deliberately simple and deliberately pessimistic, and its only real justification is
that it will be replaced by measured 30-second / 5-minute / 1-hour survival rates
from the shadow scanner.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from tradeup.domain.contracts import TradeupInput
from tradeup.domain.listings import ListingIdentity
from tradeup.domain.money import BalanceType, Currency, Money, money_sum

__all__ = [
    "FailurePoint",
    "PartialFillAssessment",
    "PartialFillModel",
    "PartialFillPolicy",
]


@dataclass(frozen=True, slots=True)
class PartialFillPolicy:
    """Stated priors for bundle completion. Replace with measurements."""

    #: Probability a freshly observed listing is still buyable when we reach it.
    base_survival_probability: Decimal = Decimal("0.97")
    #: Seconds over which survival probability halves the gap to ``floor_survival``.
    survival_half_life_seconds: int = 900
    #: Never model a listing as more doomed than this.
    floor_survival: Decimal = Decimal("0.50")
    #: Fraction of acquisition cost recovered when liquidating an orphaned input.
    #: Retail in, wholesale out -- the gap is the whole risk.
    salvage_rate: Decimal = Decimal("0.80")

    def __post_init__(self) -> None:
        for name in ("base_survival_probability", "floor_survival", "salvage_rate"):
            value: Decimal = getattr(self, name)
            if not (Decimal(0) <= value <= Decimal(1)):
                raise ValueError(f"{name} must be within [0, 1]")
        if self.survival_half_life_seconds <= 0:
            raise ValueError("survival_half_life_seconds must be positive")
        if self.floor_survival > self.base_survival_probability:
            raise ValueError("floor_survival cannot exceed base_survival_probability")


@dataclass(frozen=True, slots=True)
class FailurePoint:
    """What it costs if the bundle dies at exactly this step."""

    position: int
    identity: ListingIdentity
    cost_committed: Money
    """Cash already spent on earlier inputs when this one fails."""

    salvage_value: Money
    loss_if_fails_here: Money
    probability: Decimal


@dataclass(frozen=True)
class PartialFillAssessment:
    """Completion odds and the expected cost of not completing."""

    completion_probability: Decimal
    expected_loss: Money
    max_orphan_exposure: Money
    purchase_sequence: tuple[ListingIdentity, ...]
    per_listing_survival: Mapping[ListingIdentity, Decimal]
    failure_points: tuple[FailurePoint, ...]

    @property
    def failure_probability(self) -> Decimal:
        return Decimal(1) - self.completion_probability


class PartialFillModel:
    """Scores a bundle's chance of being assembled, and what failure costs."""

    def __init__(
        self,
        *,
        base_currency: Currency,
        policy: PartialFillPolicy | None = None,
    ) -> None:
        self._currency = base_currency
        self._policy = policy or PartialFillPolicy()

    @property
    def policy(self) -> PartialFillPolicy:
        return self._policy

    def survival_probability(self, item: TradeupInput, moment: datetime) -> Decimal:
        """Probability this specific listing is still purchasable.

        Exponential decay toward ``floor_survival`` with quote age, then scaled by
        venue-published seller reliability where one exists.
        """
        age = item.listing.quote_age_seconds(moment)
        half_lives = age / Decimal(self._policy.survival_half_life_seconds)
        # 0.5 ** half_lives, via Decimal exponentiation on a bounded exponent.
        decay = Decimal(2) ** (-half_lives)
        spread = self._policy.base_survival_probability - self._policy.floor_survival
        survival = self._policy.floor_survival + spread * decay

        reliability = item.listing.seller_reliability
        if reliability is not None:
            survival = survival * reliability

        survival = min(max(survival, Decimal(0)), Decimal(1))
        return survival.quantize(Decimal("0.000001"))

    def assess(
        self,
        inputs: Sequence[TradeupInput],
        moment: datetime,
    ) -> PartialFillAssessment:
        """Order the purchases riskiest-first and price the failure tree.

        Buying the most fragile listing first is not a preference -- it is the
        sequence that minimises committed capital at the moment of discovery. If the
        item most likely to vanish is bought last, we learn it is gone only after
        paying for everything else.
        """
        if not inputs:
            raise ValueError("cannot assess an empty bundle")

        zero = Money.zero(self._currency, BalanceType.CASH_WITHDRAWABLE)
        survival = {item.identity: self.survival_probability(item, moment) for item in inputs}

        # Ascending survival: most fragile first. Identity breaks ties so the
        # sequence is deterministic across runs.
        ordered = sorted(inputs, key=lambda i: (survival[i.identity], i.identity))

        failure_points: list[FailurePoint] = []
        reached = Decimal(1)  # probability we get as far as this position
        committed = zero
        expected_loss = zero
        max_exposure = zero

        for position, item in enumerate(ordered):
            p_survive = survival[item.identity]
            p_fail_here = reached * (Decimal(1) - p_survive)
            salvage = committed.scaled_down(self._policy.salvage_rate)
            loss = committed - salvage

            failure_points.append(
                FailurePoint(
                    position=position,
                    identity=item.identity,
                    cost_committed=committed,
                    salvage_value=salvage,
                    loss_if_fails_here=loss,
                    probability=p_fail_here.quantize(Decimal("0.000001")),
                )
            )
            expected_loss = expected_loss + loss.scaled_up(p_fail_here)
            max_exposure = max(max_exposure, loss)

            committed = committed + item.acquisition_cost
            reached = reached * p_survive

        completion = reached.quantize(Decimal("0.000001"))
        return PartialFillAssessment(
            completion_probability=completion,
            expected_loss=expected_loss,
            max_orphan_exposure=max_exposure,
            purchase_sequence=tuple(i.identity for i in ordered),
            per_listing_survival=survival,
            failure_points=tuple(failure_points),
        )

    def bundle_cost(self, inputs: Sequence[TradeupInput]) -> Money:
        return money_sum(
            (i.acquisition_cost for i in inputs),
            currency=self._currency,
            balance_type=BalanceType.CASH_WITHDRAWABLE,
        )
