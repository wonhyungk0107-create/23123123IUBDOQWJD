"""Explicit cross-currency conversion.

``Money`` refuses cross-currency arithmetic by design. The only sanctioned way to
value one currency in another -- including a crypto settlement currency against the
fiat base -- is through a :class:`ConversionQuote`, which carries the provenance
needed to judge whether the rate can be trusted: where it was observed, when, and
from what source.

Two properties are load-bearing:

* **Arithmetic is exact.** The rate becomes a :class:`~fractions.Fraction` and the
  conversion is computed as an exact rational, rounded once at the end. There is no
  intermediate precision to reason about and no accumulation error.
* **Rounding direction is the caller's economic statement.** An amount we must pay
  converts with :meth:`ConversionQuote.convert_cost` (rounds up); an amount we
  expect to receive converts with :meth:`ConversionQuote.convert_proceeds` (rounds
  down). There is no neutral conversion, because a neutral rounding would sometimes
  flatter the candidate.

A stale quote fails closed: callers enforce a freshness limit through
:meth:`ConversionQuote.is_stale` and must reject rather than convert on an old rate.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from fractions import Fraction

from tradeup.domain._guards import ensure_decimal
from tradeup.domain.money import Currency, Money

__all__ = [
    "ConversionError",
    "ConversionPairError",
    "ConversionQuote",
    "StaleConversionQuoteError",
]


class ConversionError(Exception):
    """Base class for conversion-domain violations."""


class ConversionPairError(ConversionError):
    """The amount's currency is not one of this quote's pair."""


class StaleConversionQuoteError(ConversionError):
    """The quote is older than the configured freshness limit."""


@dataclass(frozen=True, slots=True)
class ConversionQuote:
    """One observed exchange rate, with provenance.

    ``rate`` is quote-currency major units per one base-currency major unit: a
    BTC/USD quote with ``rate=Decimal("100000")`` says one BTC was worth 100,000.00
    USD when observed. The quote converts in either direction; the rounding
    direction, not the pair orientation, states which side of the trade we are on.
    """

    base: Currency
    quote: Currency
    rate: Decimal
    observed_at: datetime
    source: str
    venue: str

    def __post_init__(self) -> None:
        ensure_decimal(self.rate, "conversion rate")
        if self.rate <= 0:
            raise ValueError(f"conversion rate must be positive, got {self.rate}")
        if self.base is self.quote:
            raise ValueError(f"a conversion pair needs two currencies, got {self.base} twice")
        if self.observed_at.tzinfo is None:
            raise ValueError("observed_at must be timezone-aware UTC")
        if not self.source:
            raise ValueError("a conversion quote requires a source reference")
        if not self.venue:
            raise ValueError("a conversion quote requires the venue it was observed on")

    @property
    def pair(self) -> str:
        return f"{self.base.value}/{self.quote.value}"

    def covers(self, currency: Currency) -> bool:
        return currency is self.base or currency is self.quote

    # -- freshness -----------------------------------------------------------

    def age_seconds(self, now: datetime) -> Decimal:
        """Age of this quote. Negative ages are clamped to zero."""
        delta = (now - self.observed_at).total_seconds()
        return Decimal(str(max(delta, 0.0))).quantize(Decimal("0.001"))

    def is_stale(self, now: datetime, max_age_seconds: int) -> bool:
        return self.age_seconds(now) > Decimal(max_age_seconds)

    # -- conversion ----------------------------------------------------------

    def convert_cost(self, amount: Money) -> Money:
        """Convert an amount we must pay. Rounds up: a cost is never understated."""
        return self._convert(amount, round_up=True)

    def convert_proceeds(self, amount: Money) -> Money:
        """Convert an amount we expect to receive. Rounds down."""
        return self._convert(amount, round_up=False)

    def _convert(self, amount: Money, *, round_up: bool) -> Money:
        """Exact rational conversion, rounded once in the stated direction."""
        rate = Fraction(self.rate)
        if amount.currency is self.base:
            target = self.quote
            exact = Fraction(amount.minor_units) * rate * Fraction(target.scale, self.base.scale)
        elif amount.currency is self.quote:
            target = self.base
            exact = Fraction(amount.minor_units * target.scale, self.quote.scale) / rate
        else:
            raise ConversionPairError(
                f"quote {self.pair} cannot convert an amount in {amount.currency}"
            )
        if round_up:
            minor = -((-exact.numerator) // exact.denominator)
        else:
            minor = exact.numerator // exact.denominator
        return Money(minor, target, amount.balance_type)
