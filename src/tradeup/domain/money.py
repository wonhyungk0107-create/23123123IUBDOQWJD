"""Money primitives.

Design decision (see docs/decision-log.md, D-002): monetary *arithmetic* is carried
by :class:`Money`, which is the (minor_units, currency, balance_type) triple. The
provenance the economic model demands -- observation timestamp, source venue, fee
treatment, settlement status -- is carried by :class:`MonetaryObservation`, which
wraps a ``Money``.

Splitting them keeps arithmetic total and cheap while making it impossible to lose
provenance on anything that entered the system from a market: adapters may only
produce ``MonetaryObservation``.

Two hard rules, enforced by construction rather than convention:

1. Amounts are integer minor units. There is no float path into money.
2. Addition/subtraction/comparison across different currencies *or* different
   balance types raises. Steam Wallet credit is not withdrawable cash and the type
   system says so.
"""

from __future__ import annotations

import enum
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal
from fractions import Fraction
from typing import Final, Self

__all__ = [
    "BalanceType",
    "BalanceTypeMismatchError",
    "Currency",
    "CurrencyMismatchError",
    "FeeTreatment",
    "Money",
    "MoneyError",
    "MonetaryObservation",
    "SettlementStatus",
]


class MoneyError(Exception):
    """Base class for money-domain violations."""


class CurrencyMismatchError(MoneyError):
    """Raised when two amounts in different currencies are combined."""


class BalanceTypeMismatchError(MoneyError):
    """Raised when two amounts of different balance types are combined.

    This is the guard that stops the system from quietly treating Steam Wallet
    value or non-withdrawable promotional credit as realisable cash profit.
    """


class Currency(enum.StrEnum):
    """Supported settlement currencies.

    ``exponent`` is the number of minor units per major unit, so conversion to and
    from human-readable amounts never guesses.
    """

    USD = "USD"
    EUR = "EUR"
    GBP = "GBP"

    @property
    def exponent(self) -> int:
        return 2

    @property
    def scale(self) -> int:
        return 10**self.exponent


class BalanceType(enum.StrEnum):
    """What a unit of value can actually *become*.

    Ordered loosely by usefulness. ``CASH_WITHDRAWABLE`` is the only type that
    counts toward the objective function.
    """

    CASH_WITHDRAWABLE = "CASH_WITHDRAWABLE"
    VENUE_REUSABLE_BALANCE = "VENUE_REUSABLE_BALANCE"
    STEAM_WALLET = "STEAM_WALLET"
    NON_WITHDRAWABLE_CREDIT = "NON_WITHDRAWABLE_CREDIT"

    @property
    def is_withdrawable_cash(self) -> bool:
        return self is BalanceType.CASH_WITHDRAWABLE


class FeeTreatment(enum.StrEnum):
    """Which fees a quoted amount already accounts for."""

    GROSS = "GROSS"
    """Sticker price. No fees applied."""

    NET_OF_BUYER_FEES = "NET_OF_BUYER_FEES"
    """Buyer-side fees included; this is what leaves the wallet."""

    NET_OF_SELLER_FEES = "NET_OF_SELLER_FEES"
    """Seller-side fees deducted; this is what a sale actually credits."""

    ALL_IN = "ALL_IN"
    """Every modelled cost applied. Directly comparable to another ALL_IN amount."""


class SettlementStatus(enum.StrEnum):
    """How real an amount is."""

    QUOTED = "QUOTED"
    """A price seen on a market. Not committed, may vanish."""

    PENDING = "PENDING"
    """Committed but not yet final (in flight, in a hold period, unsold)."""

    SETTLED = "SETTLED"
    """Final and attributable. Only SETTLED amounts may be called profit."""


@dataclass(frozen=True, slots=True, order=False)
class Money:
    """An exact monetary amount.

    ``minor_units`` may be negative (fees, losses, reversals).
    """

    minor_units: int
    currency: Currency
    balance_type: BalanceType = BalanceType.CASH_WITHDRAWABLE

    def __post_init__(self) -> None:
        if not isinstance(self.minor_units, int) or isinstance(self.minor_units, bool):
            raise TypeError(
                f"minor_units must be int, got {type(self.minor_units).__name__}. "
                "Money never accepts float."
            )

    # -- construction --------------------------------------------------------

    @classmethod
    def zero(
        cls,
        currency: Currency,
        balance_type: BalanceType = BalanceType.CASH_WITHDRAWABLE,
    ) -> Self:
        return cls(0, currency, balance_type)

    @classmethod
    def from_major(
        cls,
        amount: Decimal | int | str,
        currency: Currency,
        balance_type: BalanceType = BalanceType.CASH_WITHDRAWABLE,
    ) -> Self:
        """Build from a major-unit amount (``"42.10"`` -> 4210 minor units).

        Rejects ``float`` outright: ``0.1 + 0.2`` must never reach a price.
        """
        if isinstance(amount, float):
            raise TypeError("from_major does not accept float; pass Decimal, int or str.")
        quantised = (Decimal(amount) * currency.scale).quantize(Decimal(1), rounding=ROUND_HALF_UP)
        return cls(int(quantised), currency, balance_type)

    # -- conversion ----------------------------------------------------------

    def as_major(self) -> Decimal:
        """Exact major-unit value. For display and ratio maths only."""
        return (Decimal(self.minor_units) / self.currency.scale).quantize(
            Decimal(1).scaleb(-self.currency.exponent)
        )

    def rebalanced(self, balance_type: BalanceType) -> Money:
        """Reinterpret this amount as another balance type at par.

        Only legitimate when a documented 1:1 conversion exists (for example, a
        venue crediting a withdrawal into cash). Anything with a haircut must go
        through the valuation layer, not this method.
        """
        return Money(self.minor_units, self.currency, balance_type)

    # -- guards --------------------------------------------------------------

    def _check_compatible(self, other: Money) -> None:
        if self.currency is not other.currency:
            raise CurrencyMismatchError(
                f"cannot combine {self.currency} with {other.currency}; "
                "convert through an explicit FX quote first"
            )
        if self.balance_type is not other.balance_type:
            raise BalanceTypeMismatchError(
                f"cannot combine {self.balance_type} with {other.balance_type}; "
                "balance types are not fungible"
            )

    # -- arithmetic ----------------------------------------------------------

    def __add__(self, other: Money) -> Money:
        self._check_compatible(other)
        return Money(self.minor_units + other.minor_units, self.currency, self.balance_type)

    def __sub__(self, other: Money) -> Money:
        self._check_compatible(other)
        return Money(self.minor_units - other.minor_units, self.currency, self.balance_type)

    def __neg__(self) -> Money:
        return Money(-self.minor_units, self.currency, self.balance_type)

    def __abs__(self) -> Money:
        return Money(abs(self.minor_units), self.currency, self.balance_type)

    def scaled(self, factor: Decimal | int | Fraction, *, rounding: str = ROUND_HALF_UP) -> Money:
        """Multiply by an exact ratio. Rounding is always explicit.

        ``Fraction`` is accepted and multiplied exactly before the single rounding
        step, so probability-weighted sums do not accumulate division error.
        """
        if isinstance(factor, float):
            raise TypeError("scaled() does not accept float; pass Decimal, int or Fraction.")
        if isinstance(factor, Fraction):
            exact = Decimal(self.minor_units * factor.numerator) / Decimal(factor.denominator)
        else:
            exact = Decimal(self.minor_units) * Decimal(factor)
        product = exact.quantize(Decimal(1), rounding=rounding)
        return Money(int(product), self.currency, self.balance_type)

    def scaled_up(self, factor: Decimal | int | Fraction) -> Money:
        """Scale rounding away from zero-cost. Use for costs and fees we pay."""
        return self.scaled(factor, rounding=ROUND_CEILING)

    def scaled_down(self, factor: Decimal | int | Fraction) -> Money:
        """Scale rounding toward zero-proceeds. Use for revenue we expect."""
        return self.scaled(factor, rounding=ROUND_FLOOR)

    def ratio_to(self, other: Money) -> Decimal:
        """Exact ratio ``self / other``. Raises on incompatible or zero divisor."""
        self._check_compatible(other)
        if other.minor_units == 0:
            raise ZeroDivisionError("cannot take a ratio against a zero denominator")
        return Decimal(self.minor_units) / Decimal(other.minor_units)

    # -- ordering ------------------------------------------------------------

    def __lt__(self, other: Money) -> bool:
        self._check_compatible(other)
        return self.minor_units < other.minor_units

    def __le__(self, other: Money) -> bool:
        self._check_compatible(other)
        return self.minor_units <= other.minor_units

    def __gt__(self, other: Money) -> bool:
        self._check_compatible(other)
        return self.minor_units > other.minor_units

    def __ge__(self, other: Money) -> bool:
        self._check_compatible(other)
        return self.minor_units >= other.minor_units

    # -- predicates ----------------------------------------------------------

    @property
    def is_zero(self) -> bool:
        return self.minor_units == 0

    @property
    def is_positive(self) -> bool:
        return self.minor_units > 0

    @property
    def is_negative(self) -> bool:
        return self.minor_units < 0

    def __str__(self) -> str:
        return f"{self.as_major()} {self.currency.value} [{self.balance_type.value}]"


def money_sum(
    amounts: Iterable[Money],
    *,
    currency: Currency,
    balance_type: BalanceType = BalanceType.CASH_WITHDRAWABLE,
) -> Money:
    """Sum ``Money`` values with an explicit zero.

    ``sum()`` would seed with ``int`` 0 and fail. Requiring the currency up front
    also makes an empty sequence well-defined instead of ambiguous, and every
    element is still checked for compatibility by ``__add__``.
    """
    total = Money.zero(currency, balance_type)
    for amount in amounts:
        total = total + amount
    return total


@dataclass(frozen=True, slots=True)
class MonetaryObservation:
    """A ``Money`` plus the provenance required to judge whether to trust it.

    Every amount that entered the system from a market must be represented this
    way. Internal derived amounts may use bare ``Money``.
    """

    amount: Money
    observed_at: datetime
    venue: str
    fee_treatment: FeeTreatment
    settlement_status: SettlementStatus

    def __post_init__(self) -> None:
        if self.observed_at.tzinfo is None:
            raise ValueError("observed_at must be timezone-aware UTC")

    def age_seconds(self, now: datetime) -> Decimal:
        """Age of this observation. Negative ages are clamped to zero."""
        delta = (now - self.observed_at).total_seconds()
        return Decimal(str(max(delta, 0.0))).quantize(Decimal("0.001"))

    def is_stale(self, now: datetime, max_age_seconds: int) -> bool:
        return self.age_seconds(now) > Decimal(max_age_seconds)

    @property
    def is_realised_cash(self) -> bool:
        """True only for settled, withdrawable cash. The profit test."""
        return (
            self.settlement_status is SettlementStatus.SETTLED
            and self.amount.balance_type.is_withdrawable_cash
        )


#: Tolerance for Decimal comparisons at decision boundaries. Chosen well below one
#: minor unit and well below any meaningful float precision.
DECIMAL_TOLERANCE: Final[Decimal] = Decimal("1e-9")
