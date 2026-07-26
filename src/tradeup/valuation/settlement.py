"""The crypto settlement rail: what moving capital in and out actually costs.

The objective is settled, withdrawable profit in the operator's own hands. When the
operator funds venue balances with a crypto currency and takes proceeds back out the
same way, the round trip itself has a price: the venue's deposit and withdrawal
fees, the conversion spread, the on-chain network fee in each direction, and the
crypto's own price moving between quote and settlement. Ignoring that price would
overstate every contract's realisable profit by exactly the amount the rail eats.

This module prices the round trip explicitly from sourced fee rules and one
explicit :class:`~tradeup.domain.conversion.ConversionQuote`, then amortises it into
the per-contract charge the expected-value engine includes in ``all_in_cost``. It
fails closed everywhere the fee schedule or the quote cannot answer: an unknown fee
raises :class:`~tradeup.domain.fees.UnknownFeeError`, a stale quote raises
:class:`~tradeup.domain.conversion.StaleConversionQuoteError`, and a quote for the
wrong pair raises :class:`~tradeup.domain.conversion.ConversionPairError`.

Nothing here talks to a network or moves money. It is planning arithmetic, and the
volatility haircut and amortisation horizon it uses are stated priors pending
calibration, labelled as such wherever the result appears.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from fractions import Fraction

from tradeup.config import Settings
from tradeup.domain.conversion import (
    ConversionPairError,
    ConversionQuote,
    StaleConversionQuoteError,
)
from tradeup.domain.fees import FeeOperation, FeeSchedule
from tradeup.domain.money import Money

__all__ = [
    "CryptoSettlementPlanner",
    "FundingLeg",
    "SettlementPlan",
    "WithdrawalLeg",
]


@dataclass(frozen=True, slots=True)
class FundingLeg:
    """Placing fiat capital on a venue, paid for from a crypto wallet."""

    #: Venue balance the leg makes usable, in the fiat base currency.
    fiat_target: Money
    #: Venue deposit fee, fiat.
    deposit_fee: Money
    #: Conversion spread charged on the way in, fiat.
    conversion_spread_fee: Money
    #: Crypto principal sent to cover the target plus the fiat-side fees.
    crypto_sent: Money
    #: On-chain fee for the deposit transfer, crypto.
    network_fee: Money
    quote: ConversionQuote

    @property
    def total_crypto_outlay(self) -> Money:
        """Everything that leaves the wallet to fund this leg."""
        return self.crypto_sent + self.network_fee


@dataclass(frozen=True, slots=True)
class WithdrawalLeg:
    """Pulling venue proceeds back to the operator's wallet as crypto."""

    #: Venue balance being withdrawn, fiat.
    fiat_proceeds: Money
    #: Venue withdrawal fee, fiat.
    withdrawal_fee: Money
    #: Conversion spread charged on the way out, fiat.
    conversion_spread_fee: Money
    #: On-chain fee for the withdrawal transfer, crypto.
    network_fee: Money
    #: Crypto credited to the wallet after fees, before the volatility haircut.
    crypto_received: Money
    #: Crypto value after the configured volatility haircut. This is the number a
    #: conservative valuation may use; the haircut is a stated prior.
    crypto_after_haircut: Money
    quote: ConversionQuote


@dataclass(frozen=True, slots=True)
class SettlementPlan:
    """One complete capital round trip through the crypto rail."""

    funding: FundingLeg
    withdrawal: WithdrawalLeg
    #: Fiat value lost across the round trip, measured pessimistically at the quote.
    round_trip_drag: Money

    @property
    def drag_ratio(self) -> Fraction:
        """Fiat lost per unit of capital moved through the rail, exact."""
        return Fraction(self.round_trip_drag.minor_units, self.funding.fiat_target.minor_units)

    def per_contract_charge_rate(self, contracts: int) -> Fraction:
        """Fraction of a contract's acquisition cost the rail charges it.

        Each dollar of capital pays the rail once per round trip, and one round trip
        is assumed to support ``contracts`` contracts, so a contract is charged its
        own acquisition cost times ``drag_ratio / contracts``. Proportional charging
        is what keeps a small contract from bearing a whole capital block's drag.
        ``contracts`` is a stated prior, not a measurement, and it lives in typed
        configuration so the effective policy stays auditable.
        """
        if contracts < 1:
            raise ValueError(f"amortisation needs at least one contract, got {contracts}")
        return self.drag_ratio / contracts


class CryptoSettlementPlanner:
    """Prices the crypto capital round trip for one funding venue.

    ``funding_venue`` owns the deposit, withdrawal and conversion-spread fee rules;
    ``network_venue`` owns the on-chain transfer fee rules. Both must be present in
    the fee schedule or planning fails closed.
    """

    def __init__(
        self,
        *,
        settings: Settings,
        fee_schedule: FeeSchedule,
        funding_venue: str,
        network_venue: str,
    ) -> None:
        self._settings = settings
        self._fees = fee_schedule
        self._funding_venue = funding_venue
        self._network_venue = network_venue

    def plan(self, *, capital: Money, quote: ConversionQuote, moment: datetime) -> SettlementPlan:
        """Price one round trip of ``capital`` through the rail at ``quote``."""
        settings = self._settings
        if capital.currency is not settings.base_currency:
            raise ValueError(
                f"capital is in {capital.currency}, but the base currency is "
                f"{settings.base_currency}"
            )
        if not capital.is_positive:
            raise ValueError(f"a settlement plan needs positive capital, got {capital}")
        if not (quote.base is settings.settlement_currency and quote.quote is capital.currency):
            raise ConversionPairError(
                f"quote {quote.pair} does not price the settlement currency "
                f"{settings.settlement_currency} in {capital.currency}"
            )
        if quote.is_stale(moment, settings.max_conversion_quote_age_seconds):
            raise StaleConversionQuoteError(
                f"conversion quote {quote.pair} from {quote.observed_at.isoformat()} is older "
                f"than {settings.max_conversion_quote_age_seconds}s at {moment.isoformat()}"
            )

        funding = self._funding_leg(capital, quote, moment)
        withdrawal = self._withdrawal_leg(capital, quote, moment)

        # Drag is measured in fiat at the quote, pessimistically in both directions:
        # what went in values rounding up, what came back values rounding down.
        fiat_in = quote.convert_cost(funding.total_crypto_outlay)
        fiat_out = quote.convert_proceeds(withdrawal.crypto_after_haircut)
        return SettlementPlan(
            funding=funding,
            withdrawal=withdrawal,
            round_trip_drag=fiat_in - fiat_out,
        )

    # -- legs ----------------------------------------------------------------

    def _funding_leg(self, capital: Money, quote: ConversionQuote, moment: datetime) -> FundingLeg:
        deposit_fee = self._fees.quote(
            self._funding_venue, FeeOperation.DEPOSIT, capital, moment
        ).fee
        spread_fee = self._fees.quote(
            self._funding_venue, FeeOperation.FX_CONVERSION, capital, moment
        ).fee
        fiat_to_cover = capital + deposit_fee + spread_fee
        crypto_sent = quote.convert_cost(fiat_to_cover)
        network_fee = self._fees.quote(
            self._network_venue, FeeOperation.NETWORK_TRANSFER, crypto_sent, moment
        ).fee
        return FundingLeg(
            fiat_target=capital,
            deposit_fee=deposit_fee,
            conversion_spread_fee=spread_fee,
            crypto_sent=crypto_sent,
            network_fee=network_fee,
            quote=quote,
        )

    def _withdrawal_leg(
        self, proceeds: Money, quote: ConversionQuote, moment: datetime
    ) -> WithdrawalLeg:
        withdrawal_fee = self._fees.quote(
            self._funding_venue, FeeOperation.WITHDRAWAL, proceeds, moment
        ).fee
        spread_fee = self._fees.quote(
            self._funding_venue, FeeOperation.FX_CONVERSION, proceeds, moment
        ).fee
        net_fiat = proceeds - withdrawal_fee - spread_fee
        crypto_gross = quote.convert_proceeds(net_fiat)
        network_fee = self._fees.quote(
            self._network_venue, FeeOperation.NETWORK_TRANSFER, crypto_gross, moment
        ).fee
        crypto_received = crypto_gross - network_fee
        haircut_factor = Decimal(1) - self._settings.crypto_volatility_haircut
        crypto_after_haircut = crypto_received.scaled_down(haircut_factor)
        return WithdrawalLeg(
            fiat_proceeds=proceeds,
            withdrawal_fee=withdrawal_fee,
            conversion_spread_fee=spread_fee,
            network_fee=network_fee,
            crypto_received=crypto_received,
            crypto_after_haircut=crypto_after_haircut,
            quote=quote,
        )
