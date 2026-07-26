"""Output valuation models.

The highest active ask is not a price -- it is an aspiration. Valuing outputs from
asks is the single easiest way to manufacture a profitable-looking contract that
loses money on exit.

So valuation is a *hierarchy* with explicit evidence quality, and every step from
gross reference to conservative net proceeds is itemised and stored. When evidence
is insufficient the answer is :attr:`ValuationSource.UNVALUABLE` with zero net
proceeds, not a guess.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from tradeup.domain.items import QualityType, WearCondition
from tradeup.domain.money import Money

__all__ = [
    "AcquisitionReference",
    "OutputValuation",
    "PriceObservation",
    "ValuationConfidence",
    "ValuationSource",
]


class ValuationSource(enum.StrEnum):
    """Evidence types, best first.

    ``preference_rank`` gives the search order: we take the best available evidence
    and record which rung we landed on, because a candidate priced off rung 5 is a
    materially weaker claim than one priced off rung 1.
    """

    EXECUTABLE_CASH_BID = "EXECUTABLE_CASH_BID"
    """A live bid we could hit right now. The only price that is truly executable."""

    EXACT_FLOAT_COMPLETED_SALE = "EXACT_FLOAT_COMPLETED_SALE"
    """A completed sale of a comparable float. Best historical evidence."""

    COMPLETED_SALE = "COMPLETED_SALE"
    """A completed sale at condition level."""

    DEPTH_ADJUSTED_ASK = "DEPTH_ADJUSTED_ASK"
    """An ask discounted for order-book depth. Weak: nobody has paid it."""

    CROSS_MARKET_REFERENCE = "CROSS_MARKET_REFERENCE"
    """An aggregator's figure. Never execution truth; requires direct requery."""

    CONSERVATIVE_FALLBACK = "CONSERVATIVE_FALLBACK"
    """A deliberately pessimistic condition-level reference."""

    UNVALUABLE = "UNVALUABLE"
    """Insufficient evidence. Contributes zero to EV."""

    @property
    def preference_rank(self) -> int:
        return _SOURCE_RANK[self]

    @property
    def is_executable(self) -> bool:
        return self is ValuationSource.EXECUTABLE_CASH_BID

    @property
    def is_direct_evidence(self) -> bool:
        """True when the figure came from the venue we would actually sell on."""
        return self in {
            ValuationSource.EXECUTABLE_CASH_BID,
            ValuationSource.EXACT_FLOAT_COMPLETED_SALE,
            ValuationSource.COMPLETED_SALE,
            ValuationSource.DEPTH_ADJUSTED_ASK,
        }


_SOURCE_RANK: dict[ValuationSource, int] = {
    ValuationSource.EXECUTABLE_CASH_BID: 1,
    ValuationSource.EXACT_FLOAT_COMPLETED_SALE: 2,
    ValuationSource.COMPLETED_SALE: 3,
    ValuationSource.DEPTH_ADJUSTED_ASK: 4,
    ValuationSource.CROSS_MARKET_REFERENCE: 5,
    ValuationSource.CONSERVATIVE_FALLBACK: 6,
    ValuationSource.UNVALUABLE: 99,
}


class ValuationConfidence(enum.StrEnum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    NONE = "NONE"

    @property
    def weight(self) -> Decimal:
        """Multiplier used when computing a lower-confidence-bound EV."""
        return _CONFIDENCE_WEIGHT[self]


_CONFIDENCE_WEIGHT: dict[ValuationConfidence, Decimal] = {
    ValuationConfidence.HIGH: Decimal("0.95"),
    ValuationConfidence.MEDIUM: Decimal("0.85"),
    ValuationConfidence.LOW: Decimal("0.70"),
    ValuationConfidence.NONE: Decimal("0"),
}


@dataclass(frozen=True, slots=True)
class PriceObservation:
    """A single piece of pricing evidence for one exact item variant."""

    skin_id: str
    quality: QualityType
    wear: WearCondition
    venue: str
    gross: Money
    source: ValuationSource
    observed_at: datetime
    #: How many independent data points back this figure.
    evidence_count: int
    #: Units available at or near this price, when the venue publishes depth.
    depth_units: int | None = None
    #: Float of the comparable sale, for EXACT_FLOAT_COMPLETED_SALE evidence.
    comparable_float: Decimal | None = None

    def __post_init__(self) -> None:
        if self.observed_at.tzinfo is None:
            raise ValueError("observed_at must be timezone-aware UTC")
        if self.evidence_count < 0:
            raise ValueError("evidence_count cannot be negative")
        if self.gross.is_negative:
            raise ValueError("a price observation cannot be negative")

    def age_seconds(self, now: datetime) -> Decimal:
        delta = (now - self.observed_at).total_seconds()
        return Decimal(str(max(delta, 0.0))).quantize(Decimal("0.001"))


@dataclass(frozen=True, slots=True)
class AcquisitionReference:
    """Name-level ask evidence for one targeted input name at one venue.

    Aggregate figures only: the venue's documented API states no floats and no
    listing IDs for these, and documents no purchase endpoint, so acting on them
    is a manual operator decision. They never enter the optimizer, the expected
    value engine or a gate — an exact-asset contract cannot be built from a
    number that does not identify an asset.
    """

    market_hash_name: str
    venue: str
    min_ask: Money | None
    median_ask: Money | None
    quantity: int
    observed_at: datetime

    def __post_init__(self) -> None:
        if self.observed_at.tzinfo is None:
            raise ValueError("observed_at must be timezone-aware UTC")
        if self.quantity < 0:
            raise ValueError("quantity cannot be negative")
        for label, price in (("min_ask", self.min_ask), ("median_ask", self.median_ask)):
            if price is not None and price.is_negative:
                raise ValueError(f"{label} cannot be negative")


@dataclass(frozen=True, slots=True)
class OutputValuation:
    """What one possible contract output is conservatively worth, net, in cash.

    Every deduction between ``gross_reference`` and ``net_proceeds`` is itemised so
    a realised sale can be diffed against the prediction component by component.
    That diff is the calibration signal; a single opaque net number would throw it
    away.
    """

    skin_id: str
    market_hash_name: str
    quality: QualityType
    wear: WearCondition
    output_float: Decimal
    exit_venue: str
    source: ValuationSource
    observed_at: datetime
    gross_reference: Money
    seller_fee: Money
    withdrawal_fee: Money
    fx_cost: Money
    liquidity_haircut: Money
    #: Positive for a pattern/float premium, negative for a discount.
    float_adjustment: Money
    carry_cost: Money
    net_proceeds: Money
    confidence: ValuationConfidence
    evidence_count: int
    expected_days_to_sale: int

    def __post_init__(self) -> None:
        if self.observed_at.tzinfo is None:
            raise ValueError("observed_at must be timezone-aware UTC")
        if self.expected_days_to_sale < 0:
            raise ValueError("expected_days_to_sale cannot be negative")
        if self.net_proceeds.is_negative:
            raise ValueError(
                f"valuation for {self.skin_id} produced negative net proceeds; "
                "an output we would pay to dispose of must be modelled explicitly"
            )
        if self.source is ValuationSource.UNVALUABLE:
            if not self.net_proceeds.is_zero:
                raise ValueError("an UNVALUABLE output must contribute zero")
            if self.confidence is not ValuationConfidence.NONE:
                raise ValueError("an UNVALUABLE output must have NONE confidence")

    @property
    def is_valuable(self) -> bool:
        return self.source is not ValuationSource.UNVALUABLE

    @property
    def total_deductions(self) -> Money:
        """Everything subtracted from the gross reference, excluding premiums."""
        return (
            self.seller_fee
            + self.withdrawal_fee
            + self.fx_cost
            + self.liquidity_haircut
            + self.carry_cost
        )

    @property
    def lower_bound_proceeds(self) -> Money:
        """Net proceeds discounted by evidence confidence.

        Used for the lower-confidence-bound EV gate, so a contract resting on thin
        evidence must clear a higher bar on its point estimate.
        """
        return self.net_proceeds.scaled_down(self.confidence.weight)

    @classmethod
    def unvaluable(
        cls,
        *,
        skin_id: str,
        market_hash_name: str,
        quality: QualityType,
        wear: WearCondition,
        output_float: Decimal,
        currency_reference: Money,
        observed_at: datetime,
        exit_venue: str = "none",
    ) -> OutputValuation:
        """Construct the zero-value case with all components explicitly zeroed."""
        zero = Money.zero(currency_reference.currency, currency_reference.balance_type)
        return cls(
            skin_id=skin_id,
            market_hash_name=market_hash_name,
            quality=quality,
            wear=wear,
            output_float=output_float,
            exit_venue=exit_venue,
            source=ValuationSource.UNVALUABLE,
            observed_at=observed_at,
            gross_reference=zero,
            seller_fee=zero,
            withdrawal_fee=zero,
            fx_cost=zero,
            liquidity_haircut=zero,
            float_adjustment=zero,
            carry_cost=zero,
            net_proceeds=zero,
            confidence=ValuationConfidence.NONE,
            evidence_count=0,
            expected_days_to_sale=0,
        )
