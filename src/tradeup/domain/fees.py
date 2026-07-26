"""Versioned fee schedules.

Fees are *data*, not constants. Venues change them, tier them by volume, vary them
by balance type, and apply different rates to deposits and withdrawals. A hard-coded
``0.02`` is a silent, permanent mispricing.

The controlling behaviour here is failing closed: :meth:`FeeSchedule.quote` raises
:class:`UnknownFeeError` when no rule covers a request. An economically material
unknown must stop a candidate, never default to zero -- a zero default makes every
unmodelled venue look like the cheapest one.
"""

from __future__ import annotations

import enum
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from tradeup.domain._guards import reject_float
from tradeup.domain.money import BalanceType, Currency, Money

__all__ = [
    "FeeOperation",
    "FeeQuote",
    "FeeRule",
    "FeeSchedule",
    "UnknownFeeError",
]


class UnknownFeeError(Exception):
    """No fee rule covers this request. Callers must abort, not assume zero."""


class FeeOperation(enum.StrEnum):
    """Every point at which value leaks out of a round trip."""

    PURCHASE = "PURCHASE"
    """Buyer-side fee charged on top of the sticker price."""

    DEPOSIT = "DEPOSIT"
    """Cost of funding a venue balance."""

    SALE = "SALE"
    """Seller-side commission deducted from proceeds."""

    WITHDRAWAL = "WITHDRAWAL"
    """Cost of turning a venue balance back into withdrawable cash."""

    FX_CONVERSION = "FX_CONVERSION"
    """Spread or margin charged on a currency conversion."""

    NETWORK_TRANSFER = "NETWORK_TRANSFER"
    """On-chain cost of moving a crypto amount to or from a venue."""

    PAYMENT_SURCHARGE = "PAYMENT_SURCHARGE"
    """Processor or regional tax surcharge, only ever from an explicit quote."""


@dataclass(frozen=True, slots=True)
class FeeQuote:
    """A resolved fee for a specific amount at a specific time, with provenance."""

    venue: str
    operation: FeeOperation
    basis: Money
    fee: Money
    rule_id: str
    source: str
    quoted_at: datetime
    last_verified: datetime | None

    def __post_init__(self) -> None:
        if self.basis.currency is not self.fee.currency:
            raise ValueError("fee and basis must share a currency")
        if self.fee.is_negative:
            raise ValueError(f"fee quote {self.rule_id} produced a negative fee")

    @property
    def effective_rate(self) -> Decimal:
        """Fee as a fraction of the basis. Zero basis yields zero."""
        if self.basis.minor_units == 0:
            return Decimal(0)
        return Decimal(self.fee.minor_units) / Decimal(abs(self.basis.minor_units))


@dataclass(frozen=True, slots=True)
class FeeRule:
    """One tier of one venue's fee for one operation, valid over a date window."""

    rule_id: str
    venue: str
    operation: FeeOperation
    balance_type: BalanceType
    currency: Currency
    #: Fractional rate. ``Decimal("0.02")`` is 2%.
    percentage: Decimal
    #: Flat component in minor units, added after the percentage.
    fixed_minor: int
    effective_from: datetime
    effective_until: datetime | None
    source: str
    last_verified: datetime | None
    #: Inclusive lower bound of the basis amount this tier applies to.
    tier_lower_minor: int = 0
    #: Exclusive upper bound, or ``None`` for the top tier.
    tier_upper_minor: int | None = None
    #: Optional floor/cap on the resulting fee.
    minimum_fee_minor: int | None = None
    maximum_fee_minor: int | None = None

    def __post_init__(self) -> None:
        reject_float(self.percentage, f"fee rule {self.rule_id} percentage")
        if self.percentage < 0:
            raise ValueError(f"fee rule {self.rule_id} has a negative percentage")
        if self.fixed_minor < 0:
            raise ValueError(f"fee rule {self.rule_id} has a negative fixed component")
        if self.effective_from.tzinfo is None:
            raise ValueError("effective_from must be timezone-aware")
        if self.effective_until is not None and self.effective_until <= self.effective_from:
            raise ValueError(f"fee rule {self.rule_id} has an empty validity window")
        if self.tier_upper_minor is not None and self.tier_upper_minor <= self.tier_lower_minor:
            raise ValueError(f"fee rule {self.rule_id} has an empty tier")
        if not self.source:
            raise ValueError(f"fee rule {self.rule_id} requires a source reference")

    def covers_time(self, moment: datetime) -> bool:
        if moment < self.effective_from:
            return False
        return self.effective_until is None or moment < self.effective_until

    def covers_amount(self, basis: Money) -> bool:
        magnitude = abs(basis.minor_units)
        if magnitude < self.tier_lower_minor:
            return False
        return self.tier_upper_minor is None or magnitude < self.tier_upper_minor

    def matches(
        self,
        venue: str,
        operation: FeeOperation,
        basis: Money,
        moment: datetime,
    ) -> bool:
        return (
            self.venue == venue
            and self.operation is operation
            and self.balance_type is basis.balance_type
            and self.currency is basis.currency
            and self.covers_time(moment)
            and self.covers_amount(basis)
        )

    def quote(self, basis: Money, moment: datetime) -> FeeQuote:
        """Compute the fee. Always rounds *up* -- we never understate a cost."""
        variable = abs(basis).scaled_up(self.percentage)
        fee = variable + Money(self.fixed_minor, basis.currency, basis.balance_type)
        if self.minimum_fee_minor is not None:
            floor = Money(self.minimum_fee_minor, basis.currency, basis.balance_type)
            fee = max(fee, floor)
        if self.maximum_fee_minor is not None:
            cap = Money(self.maximum_fee_minor, basis.currency, basis.balance_type)
            fee = min(fee, cap)
        return FeeQuote(
            venue=self.venue,
            operation=self.operation,
            basis=basis,
            fee=fee,
            rule_id=self.rule_id,
            source=self.source,
            quoted_at=moment,
            last_verified=self.last_verified,
        )


class FeeSchedule:
    """Resolves fee rules. Fails closed on gaps and on ambiguity."""

    def __init__(self, rules: Sequence[FeeRule]) -> None:
        rule_ids = [r.rule_id for r in rules]
        duplicates = {r for r in rule_ids if rule_ids.count(r) > 1}
        if duplicates:
            raise ValueError(f"duplicate fee rule ids: {sorted(duplicates)}")
        self._rules: tuple[FeeRule, ...] = tuple(rules)

    @property
    def rules(self) -> tuple[FeeRule, ...]:
        return self._rules

    def with_rules(self, extra: Sequence[FeeRule]) -> FeeSchedule:
        return FeeSchedule([*self._rules, *extra])

    def quote(
        self,
        venue: str,
        operation: FeeOperation,
        basis: Money,
        moment: datetime,
    ) -> FeeQuote:
        """Resolve exactly one rule, or raise.

        Ambiguity is as dangerous as absence: two overlapping tiers mean the
        schedule is wrong, and picking the cheaper one would bias every candidate
        toward approval.
        """
        matches = [r for r in self._rules if r.matches(venue, operation, basis, moment)]
        if not matches:
            raise UnknownFeeError(
                f"no fee rule for {venue}/{operation.value} on {basis} "
                f"at {moment.isoformat()}; refusing to assume zero"
            )
        if len(matches) > 1:
            ids = ", ".join(sorted(r.rule_id for r in matches))
            raise UnknownFeeError(f"ambiguous fee rules for {venue}/{operation.value}: {ids}")
        return matches[0].quote(basis, moment)

    def has_rule(
        self,
        venue: str,
        operation: FeeOperation,
        basis: Money,
        moment: datetime,
    ) -> bool:
        return sum(1 for r in self._rules if r.matches(venue, operation, basis, moment)) == 1

    def zero_quote(
        self,
        venue: str,
        operation: FeeOperation,
        basis: Money,
        moment: datetime,
        *,
        reason: str,
    ) -> FeeQuote:
        """An explicit, sourced zero fee.

        Used only where a venue documents that no fee applies. It is deliberately
        awkward to reach: a zero fee must be a stated fact with a reason, never the
        result of a missing lookup.
        """
        return FeeQuote(
            venue=venue,
            operation=operation,
            basis=basis,
            fee=Money.zero(basis.currency, basis.balance_type),
            rule_id=f"{venue}:{operation.value}:explicit-zero",
            source=reason,
            quoted_at=moment,
            last_verified=None,
        )
