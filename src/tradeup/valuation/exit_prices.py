"""Exit valuation: what an output is conservatively worth in withdrawable cash.

The resolver walks the evidence hierarchy, takes the best rung available, and then
deliberately reads the *low* end of that rung. Three rules do most of the work:

1. **A lower quantile, never the mean.** With several comparable sales, the mean is
   flattered by the lucky ones. We take a low quantile so the modelled exit is a
   price we would probably still get on a bad day.
2. **Only balance types we can actually withdraw.** A Steam Wallet price is not cash.
   Observations in another balance type are excluded outright rather than converted
   at par, unless an explicit documented conversion is supplied.
3. **Unknown material fee means no valuation.** A missing seller or withdrawal fee
   raises, and the caller turns that into an ``UNKNOWN_FEE`` rejection. It never
   becomes an implicit zero.

The haircut and days-to-sale tables below are *priors*. They are stated in one place
so they can be replaced with measurements from a shadow run, which is the only thing
that would make them real.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from tradeup.domain.fees import FeeOperation, FeeSchedule
from tradeup.domain.items import QualityType, WearCondition
from tradeup.domain.money import BalanceType, Currency, Money
from tradeup.domain.valuation import (
    OutputValuation,
    PriceObservation,
    ValuationConfidence,
    ValuationSource,
)
from tradeup.valuation.capital import CapitalModel

__all__ = ["ExitPricePolicy", "ExitPriceResolver"]


#: Fraction of gross removed to reflect that we are a price taker on exit. Weaker
#: evidence gets a bigger haircut. PRIOR -- calibrate from realised sales.
_DEFAULT_HAIRCUT: Mapping[ValuationSource, Decimal] = {
    ValuationSource.EXECUTABLE_CASH_BID: Decimal("0.00"),
    ValuationSource.EXACT_FLOAT_COMPLETED_SALE: Decimal("0.03"),
    ValuationSource.COMPLETED_SALE: Decimal("0.05"),
    ValuationSource.DEPTH_ADJUSTED_ASK: Decimal("0.12"),
    ValuationSource.CROSS_MARKET_REFERENCE: Decimal("0.18"),
    ValuationSource.CONSERVATIVE_FALLBACK: Decimal("0.25"),
}

#: Expected days to sell, by evidence rung. PRIOR -- calibrate from realised sales.
_DEFAULT_DAYS_TO_SALE: Mapping[ValuationSource, int] = {
    ValuationSource.EXECUTABLE_CASH_BID: 0,
    ValuationSource.EXACT_FLOAT_COMPLETED_SALE: 5,
    ValuationSource.COMPLETED_SALE: 7,
    ValuationSource.DEPTH_ADJUSTED_ASK: 14,
    ValuationSource.CROSS_MARKET_REFERENCE: 21,
    ValuationSource.CONSERVATIVE_FALLBACK: 30,
}

_BASE_CONFIDENCE: Mapping[ValuationSource, ValuationConfidence] = {
    ValuationSource.EXECUTABLE_CASH_BID: ValuationConfidence.HIGH,
    ValuationSource.EXACT_FLOAT_COMPLETED_SALE: ValuationConfidence.HIGH,
    ValuationSource.COMPLETED_SALE: ValuationConfidence.MEDIUM,
    ValuationSource.DEPTH_ADJUSTED_ASK: ValuationConfidence.LOW,
    ValuationSource.CROSS_MARKET_REFERENCE: ValuationConfidence.LOW,
    ValuationSource.CONSERVATIVE_FALLBACK: ValuationConfidence.LOW,
}


@dataclass(frozen=True)
class ExitPricePolicy:
    """Tunable, auditable valuation assumptions."""

    #: Quantile of the observation set to use. 0.25 reads the low end.
    quantile: Decimal = Decimal("0.25")
    haircut_by_source: Mapping[ValuationSource, Decimal] = field(
        default_factory=lambda: dict(_DEFAULT_HAIRCUT)
    )
    days_to_sale_by_source: Mapping[ValuationSource, int] = field(
        default_factory=lambda: dict(_DEFAULT_DAYS_TO_SALE)
    )
    confidence_by_source: Mapping[ValuationSource, ValuationConfidence] = field(
        default_factory=lambda: dict(_BASE_CONFIDENCE)
    )
    #: Evidence points required before a rung's base confidence is granted in full.
    min_evidence_for_full_confidence: int = 3
    #: Balance types we are willing to treat as realisable. Deliberately narrow.
    acceptable_balance_types: frozenset[BalanceType] = frozenset({BalanceType.CASH_WITHDRAWABLE})
    #: Maximum age of a price observation before it stops counting as evidence.
    max_observation_age_seconds: int = 7 * 24 * 3600

    def __post_init__(self) -> None:
        if not (Decimal(0) <= self.quantile <= Decimal(1)):
            raise ValueError("quantile must be within [0, 1]")
        if self.min_evidence_for_full_confidence < 1:
            raise ValueError("min_evidence_for_full_confidence must be at least 1")


class ExitPriceResolver:
    """Turns price observations into a conservative net-cash valuation."""

    def __init__(
        self,
        *,
        fee_schedule: FeeSchedule,
        capital_model: CapitalModel,
        base_currency: Currency,
        policy: ExitPricePolicy | None = None,
    ) -> None:
        self._fees = fee_schedule
        self._capital = capital_model
        self._currency = base_currency
        self._policy = policy or ExitPricePolicy()

    @property
    def policy(self) -> ExitPricePolicy:
        return self._policy

    # -- selection -----------------------------------------------------------

    def _usable(
        self,
        observations: Sequence[PriceObservation],
        skin_id: str,
        quality: QualityType,
        wear: WearCondition,
        moment: datetime,
    ) -> list[PriceObservation]:
        """Filter to evidence that is on-topic, fresh, and in realisable cash."""
        usable: list[PriceObservation] = []
        for obs in observations:
            if obs.skin_id != skin_id or obs.quality is not quality or obs.wear is not wear:
                continue
            if obs.source is ValuationSource.UNVALUABLE:
                continue
            if obs.gross.currency is not self._currency:
                # Cross-currency exit needs an FX rate we do not have a source for.
                # Excluding is honest; converting at an invented rate is not.
                continue
            if obs.gross.balance_type not in self._policy.acceptable_balance_types:
                continue
            if obs.age_seconds(moment) > Decimal(self._policy.max_observation_age_seconds):
                continue
            usable.append(obs)
        return usable

    @staticmethod
    def _best_rung(observations: Sequence[PriceObservation]) -> ValuationSource:
        return min((o.source for o in observations), key=lambda s: s.preference_rank)

    def _quantile_price(self, observations: Sequence[PriceObservation]) -> Money:
        """Low-quantile gross price across the chosen rung."""
        prices = sorted(o.gross.minor_units for o in observations)
        if len(prices) == 1:
            chosen = prices[0]
        else:
            index = int(
                (self._policy.quantile * Decimal(len(prices) - 1)).to_integral_value(
                    rounding="ROUND_FLOOR"
                )
            )
            chosen = prices[index]
        return Money(chosen, self._currency, BalanceType.CASH_WITHDRAWABLE)

    def _confidence(self, source: ValuationSource, evidence_count: int) -> ValuationConfidence:
        base = self._policy.confidence_by_source.get(source, ValuationConfidence.LOW)
        if evidence_count >= self._policy.min_evidence_for_full_confidence:
            return base
        # Thin evidence drops one rung, never below LOW.
        if base is ValuationConfidence.HIGH:
            return ValuationConfidence.MEDIUM
        return ValuationConfidence.LOW

    # -- resolution ----------------------------------------------------------

    def resolve(
        self,
        *,
        skin_id: str,
        market_hash_name: str,
        quality: QualityType,
        wear: WearCondition,
        output_float: Decimal,
        observations: Sequence[PriceObservation],
        moment: datetime,
    ) -> OutputValuation:
        """Best available conservative net valuation, or the UNVALUABLE case.

        Raises :class:`~tradeup.domain.fees.UnknownFeeError` when a material exit fee
        cannot be resolved -- the caller must convert that into an ``UNKNOWN_FEE``
        rejection rather than continuing.
        """
        reference = Money.zero(self._currency, BalanceType.CASH_WITHDRAWABLE)
        usable = self._usable(observations, skin_id, quality, wear, moment)
        if not usable:
            return OutputValuation.unvaluable(
                skin_id=skin_id,
                market_hash_name=market_hash_name,
                quality=quality,
                wear=wear,
                output_float=output_float,
                currency_reference=reference,
                observed_at=moment,
            )

        rung = self._best_rung(usable)
        at_rung = [o for o in usable if o.source is rung]

        # Ask-floor cap, found live 2026-07-26: a thin market's completed-sale
        # median can sit far above the lowest *current* ask (Tec-9 | Sultan MW:
        # skinport sale median $2.71 vs csfloat ask $0.96), and valuing an exit
        # above the standing cheapest offer is optimism, not evidence. When ask
        # observations exist and the chosen sale-history gross exceeds the lowest
        # ask, the valuation switches to the ask evidence — its venue, its fees,
        # its (larger) haircut. Executable bids are exempt: a live bid is real.
        if rung is not ValuationSource.EXECUTABLE_CASH_BID:
            asks = [o for o in usable if o.source is ValuationSource.DEPTH_ADJUSTED_ASK]
            if asks:
                lowest_ask = min(asks, key=lambda o: (o.gross.minor_units, o.venue))
                sale_gross = self._quantile_price(at_rung)
                if lowest_ask.gross < sale_gross:
                    rung = ValuationSource.DEPTH_ADJUSTED_ASK
                    at_rung = [
                        o
                        for o in asks
                        if o.gross.minor_units == lowest_ask.gross.minor_units
                        and o.venue == lowest_ask.venue
                    ]

        gross = self._quantile_price(at_rung)
        # Prefer the venue with the most evidence at this rung; ties break on name so
        # the choice is deterministic across runs.
        venue_counts: dict[str, int] = {}
        for obs in at_rung:
            venue_counts[obs.venue] = venue_counts.get(obs.venue, 0) + 1
        exit_venue = min(venue_counts, key=lambda v: (-venue_counts[v], v))
        evidence_count = sum(max(o.evidence_count, 1) for o in at_rung)

        # Material fees. Both raise on an unknown rule; neither may default to zero.
        seller_fee = self._fees.quote(exit_venue, FeeOperation.SALE, gross, moment).fee
        proceeds_after_sale = gross - seller_fee
        withdrawal_fee = self._fees.quote(
            exit_venue, FeeOperation.WITHDRAWAL, proceeds_after_sale, moment
        ).fee

        haircut_rate = self._policy.haircut_by_source.get(rung, Decimal("0.25"))
        liquidity_haircut = gross.scaled_up(haircut_rate)

        days_to_sale = self._policy.days_to_sale_by_source.get(rung, 30)
        carry_cost = self._capital.carry_cost(gross, days_to_sale)

        # No fabricated float or pattern premium. A premium must come from evidence,
        # and exact-float evidence is already captured by the rung we selected.
        float_adjustment = Money.zero(self._currency, BalanceType.CASH_WITHDRAWABLE)

        net = (
            gross - seller_fee - withdrawal_fee - liquidity_haircut - carry_cost + float_adjustment
        )
        if net.is_negative:
            # Costs exceed the reference price: this output is not worth exiting.
            # Model it as zero rather than a negative asset; the orphan/salvage path
            # handles genuine disposal costs.
            net = Money.zero(self._currency, BalanceType.CASH_WITHDRAWABLE)

        return OutputValuation(
            skin_id=skin_id,
            market_hash_name=market_hash_name,
            quality=quality,
            wear=wear,
            output_float=output_float,
            exit_venue=exit_venue,
            source=rung,
            observed_at=max(o.observed_at for o in at_rung),
            gross_reference=gross,
            seller_fee=seller_fee,
            withdrawal_fee=withdrawal_fee,
            fx_cost=Money.zero(self._currency, BalanceType.CASH_WITHDRAWABLE),
            liquidity_haircut=liquidity_haircut,
            float_adjustment=float_adjustment,
            carry_cost=carry_cost,
            net_proceeds=net,
            confidence=self._confidence(rung, evidence_count),
            evidence_count=evidence_count,
            expected_days_to_sale=days_to_sale,
        )
