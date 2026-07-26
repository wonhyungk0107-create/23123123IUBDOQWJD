"""Crypto currencies and explicit cross-currency conversion.

Every expected value here is hand-computed from the stated rate and scales, never
copied from the implementation.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from tests.conftest import FROZEN_NOW

from tradeup.domain.conversion import ConversionPairError, ConversionQuote
from tradeup.domain.money import BalanceType, Currency, CurrencyMismatchError, Money


class TestCryptoCurrencies:
    def test_minor_units_are_the_chain_native_resolution(self) -> None:
        assert Currency.BTC.exponent == 8  # satoshi
        assert Currency.ETH.exponent == 18  # wei
        assert Currency.USDT.exponent == 6  # micro-USDT
        assert Currency.USD.exponent == 2

    def test_crypto_flag(self) -> None:
        assert Currency.BTC.is_crypto
        assert Currency.ETH.is_crypto
        assert Currency.USDT.is_crypto
        assert not Currency.USD.is_crypto
        assert not Currency.EUR.is_crypto
        assert not Currency.GBP.is_crypto

    def test_btc_major_round_trip_in_satoshi(self) -> None:
        amount = Money.from_major(Decimal("0.00012345"), Currency.BTC)
        assert amount.minor_units == 12_345
        assert amount.as_major() == Decimal("0.00012345")

    def test_eth_major_round_trip_in_wei(self) -> None:
        amount = Money.from_major(Decimal("1.5"), Currency.ETH)
        assert amount.minor_units == 1_500_000_000_000_000_000

    def test_crypto_and_fiat_still_refuse_to_mix(self) -> None:
        with pytest.raises(CurrencyMismatchError, match="explicit FX quote"):
            _ = Money(100, Currency.BTC) + Money(100, Currency.USD)


def quote(
    *,
    base: Currency = Currency.BTC,
    target: Currency = Currency.USD,
    rate: str = "100000",
    observed_at: datetime | None = None,
) -> ConversionQuote:
    return ConversionQuote(
        base=base,
        quote=target,
        rate=Decimal(rate),
        observed_at=observed_at or (FROZEN_NOW - timedelta(seconds=30)),
        source="tests/unit/test_conversion.py synthetic rate -- NOT a market observation",
        venue="test-rail",
    )


class TestQuoteValidation:
    def test_float_rate_is_rejected(self) -> None:
        with pytest.raises(TypeError, match="must be Decimal"):
            ConversionQuote(
                base=Currency.BTC,
                quote=Currency.USD,
                rate=100000.0,  # type: ignore[arg-type]
                observed_at=FROZEN_NOW,
                source="stated",
                venue="test-rail",
            )

    def test_zero_and_negative_rates_are_rejected(self) -> None:
        with pytest.raises(ValueError, match="must be positive"):
            quote(rate="0")
        with pytest.raises(ValueError, match="must be positive"):
            quote(rate="-1")

    def test_degenerate_pair_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="two currencies"):
            quote(base=Currency.BTC, target=Currency.BTC)

    def test_naive_timestamp_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="timezone-aware"):
            quote(observed_at=datetime(2026, 7, 25, 12, 0, 0))

    def test_missing_provenance_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="source"):
            ConversionQuote(
                base=Currency.BTC,
                quote=Currency.USD,
                rate=Decimal("100000"),
                observed_at=FROZEN_NOW,
                source="",
                venue="test-rail",
            )
        with pytest.raises(ValueError, match="venue"):
            ConversionQuote(
                base=Currency.BTC,
                quote=Currency.USD,
                rate=Decimal("100000"),
                observed_at=FROZEN_NOW,
                source="stated",
                venue="",
            )

    def test_pair_and_covers(self) -> None:
        q = quote()
        assert q.pair == "BTC/USD"
        assert q.covers(Currency.BTC)
        assert q.covers(Currency.USD)
        assert not q.covers(Currency.EUR)


class TestConversionArithmetic:
    def test_base_to_quote_is_exact_when_it_divides(self) -> None:
        # 1 BTC at 100,000 USD/BTC: 100,000,000 sat -> 10,000,000 cents.
        one_btc = Money(100_000_000, Currency.BTC)
        assert quote().convert_cost(one_btc) == Money(10_000_000, Currency.USD)
        assert quote().convert_proceeds(one_btc) == Money(10_000_000, Currency.USD)

    def test_cost_rounds_up_and_proceeds_round_down(self) -> None:
        # 1 satoshi at 100,000 USD/BTC is 0.1 cent: ceil 1, floor 0.
        one_sat = Money(1, Currency.BTC)
        assert quote().convert_cost(one_sat) == Money(1, Currency.USD)
        assert quote().convert_proceeds(one_sat) == Money(0, Currency.USD)

    def test_quote_to_base_is_exact_when_it_divides(self) -> None:
        # $1.00 at 100,000 USD/BTC: 100 cents -> 1,000 satoshi exactly.
        one_dollar = Money(100, Currency.USD)
        assert quote().convert_cost(one_dollar) == Money(1_000, Currency.BTC)
        assert quote().convert_proceeds(one_dollar) == Money(1_000, Currency.BTC)

    def test_quote_to_base_rounding_directions(self) -> None:
        # 100 cents / 99,999 USD/BTC = 0.0000100001... BTC = 1000.01... sat.
        one_dollar = Money(100, Currency.USD)
        q = quote(rate="99999")
        assert q.convert_cost(one_dollar) == Money(1_001, Currency.BTC)
        assert q.convert_proceeds(one_dollar) == Money(1_000, Currency.BTC)

    def test_balance_type_is_preserved(self) -> None:
        venue_balance = Money(100, Currency.USD, BalanceType.VENUE_REUSABLE_BALANCE)
        converted = quote().convert_cost(venue_balance)
        assert converted.balance_type is BalanceType.VENUE_REUSABLE_BALANCE

    def test_foreign_currency_is_refused(self) -> None:
        with pytest.raises(ConversionPairError, match="cannot convert"):
            quote().convert_cost(Money(100, Currency.EUR))

    def test_fractional_rate_stays_exact(self) -> None:
        # 3 units of base at rate 1/3: exactly 1 quote minor unit either way once
        # scales cancel. USDT (1e6) -> USD (1e2): 3_000_000 micro-USDT at 1/3 is
        # 1 USDT-third of a dollar... hand-compute: 3_000_000 * (1/3) * 100 / 1e6 = 100.
        q = ConversionQuote(
            base=Currency.USDT,
            quote=Currency.USD,
            rate=Decimal("0.3333333333333333333333333333"),
            observed_at=FROZEN_NOW,
            source="stated synthetic",
            venue="test-rail",
        )
        three_usdt = Money(3_000_000, Currency.USDT)
        # 3 USDT * 0.3333... = 0.99999... USD: ceil 100 cents, floor 99 cents.
        assert q.convert_cost(three_usdt) == Money(100, Currency.USD)
        assert q.convert_proceeds(three_usdt) == Money(99, Currency.USD)


class TestFreshness:
    def test_age_is_clamped_at_zero(self) -> None:
        future = quote(observed_at=FROZEN_NOW + timedelta(seconds=60))
        assert future.age_seconds(FROZEN_NOW) == Decimal("0.000")

    def test_staleness_boundary_is_exclusive(self) -> None:
        q = quote(observed_at=FROZEN_NOW - timedelta(seconds=120))
        assert not q.is_stale(FROZEN_NOW, 120)
        assert q.is_stale(FROZEN_NOW, 119)


def test_datetime_import_is_utc_aware() -> None:
    """Guard against the naive-timestamp fixture drifting."""
    assert FROZEN_NOW.tzinfo is UTC
