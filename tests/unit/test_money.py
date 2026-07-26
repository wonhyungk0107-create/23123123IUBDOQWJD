"""Money arithmetic, currency/balance incompatibility, and provenance staleness."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from fractions import Fraction

import pytest

from tradeup.domain.money import (
    BalanceType,
    BalanceTypeMismatchError,
    Currency,
    CurrencyMismatchError,
    FeeTreatment,
    MonetaryObservation,
    Money,
    SettlementStatus,
    money_sum,
)

NOW = datetime(2026, 7, 25, 12, 0, 0, tzinfo=UTC)


def usd(minor: int, balance: BalanceType = BalanceType.CASH_WITHDRAWABLE) -> Money:
    return Money(minor, Currency.USD, balance)


class TestConstruction:
    def test_from_major_string_is_exact(self) -> None:
        assert Money.from_major("42.10", Currency.USD).minor_units == 4210

    def test_from_major_rounds_half_up_at_the_minor_unit(self) -> None:
        assert Money.from_major(Decimal("0.005"), Currency.USD).minor_units == 1
        assert Money.from_major(Decimal("0.004"), Currency.USD).minor_units == 0

    def test_float_is_rejected_at_every_entry_point(self) -> None:
        with pytest.raises(TypeError, match="does not accept float"):
            Money.from_major(42.10, Currency.USD)  # type: ignore[arg-type]
        with pytest.raises(TypeError, match="must be int"):
            Money(42.1, Currency.USD)  # type: ignore[arg-type]

    def test_bool_is_not_an_acceptable_int(self) -> None:
        with pytest.raises(TypeError):
            Money(True, Currency.USD)  # type: ignore[arg-type]

    def test_as_major_round_trips(self) -> None:
        assert Money(4210, Currency.USD).as_major() == Decimal("42.10")

    def test_negative_amounts_are_legal(self) -> None:
        assert usd(-500).is_negative


class TestIncompatibility:
    def test_currency_mismatch_raises(self) -> None:
        with pytest.raises(CurrencyMismatchError):
            usd(100) + Money(100, Currency.EUR)

    def test_balance_type_mismatch_raises(self) -> None:
        wallet = usd(100, BalanceType.STEAM_WALLET)
        with pytest.raises(BalanceTypeMismatchError):
            usd(100) + wallet

    def test_steam_wallet_cannot_be_compared_to_cash(self) -> None:
        """The guard that stops Wallet credit being reported as profit."""
        with pytest.raises(BalanceTypeMismatchError):
            _ = usd(100) > usd(50, BalanceType.STEAM_WALLET)

    def test_mismatch_survives_subtraction_and_ratio(self) -> None:
        with pytest.raises(BalanceTypeMismatchError):
            usd(100) - usd(1, BalanceType.NON_WITHDRAWABLE_CREDIT)
        with pytest.raises(CurrencyMismatchError):
            usd(100).ratio_to(Money(100, Currency.GBP))

    def test_rebalanced_is_the_only_conversion_and_is_explicit(self) -> None:
        converted = usd(100, BalanceType.VENUE_REUSABLE_BALANCE).rebalanced(
            BalanceType.CASH_WITHDRAWABLE
        )
        assert converted == usd(100)


class TestArithmetic:
    def test_add_and_subtract(self) -> None:
        assert usd(4210) + usd(790) == usd(5000)
        assert usd(4210) - usd(210) == usd(4000)

    def test_scaled_accepts_fraction_exactly(self) -> None:
        # 1/3 of 300 minor units is exactly 100 with no division drift.
        assert usd(300).scaled(Fraction(1, 3)) == usd(100)

    def test_scaled_up_never_understates_a_cost(self) -> None:
        assert usd(101).scaled_up(Decimal("0.5")) == usd(51)

    def test_scaled_down_never_overstates_proceeds(self) -> None:
        assert usd(101).scaled_down(Decimal("0.5")) == usd(50)

    def test_scaled_rejects_float(self) -> None:
        with pytest.raises(TypeError):
            usd(100).scaled(0.5)  # type: ignore[arg-type]

    def test_ratio_to_is_exact(self) -> None:
        assert usd(538).ratio_to(usd(4210)) == Decimal(538) / Decimal(4210)

    def test_ratio_to_zero_raises(self) -> None:
        with pytest.raises(ZeroDivisionError):
            usd(100).ratio_to(usd(0))

    def test_money_sum_defines_the_empty_case(self) -> None:
        assert money_sum([], currency=Currency.USD) == usd(0)
        assert money_sum([usd(1), usd(2), usd(3)], currency=Currency.USD) == usd(6)

    def test_money_sum_still_enforces_compatibility(self) -> None:
        with pytest.raises(BalanceTypeMismatchError):
            money_sum([usd(1), usd(2, BalanceType.STEAM_WALLET)], currency=Currency.USD)

    def test_ordering(self) -> None:
        assert usd(1) < usd(2) <= usd(2) < usd(3)
        assert usd(3) > usd(2) >= usd(2)


class TestMonetaryObservation:
    def _observation(self, observed_at: datetime) -> MonetaryObservation:
        return MonetaryObservation(
            amount=usd(4210),
            observed_at=observed_at,
            venue="csfloat",
            fee_treatment=FeeTreatment.GROSS,
            settlement_status=SettlementStatus.QUOTED,
        )

    def test_naive_timestamps_are_rejected(self) -> None:
        with pytest.raises(ValueError, match="timezone-aware"):
            self._observation(datetime(2026, 7, 25, 12, 0, 0))

    def test_age_and_staleness(self) -> None:
        observation = self._observation(NOW - timedelta(seconds=400))
        assert observation.age_seconds(NOW) == Decimal("400.000")
        assert observation.is_stale(NOW, max_age_seconds=300)
        assert not observation.is_stale(NOW, max_age_seconds=600)

    def test_clock_skew_clamps_to_zero_rather_than_reporting_negative_age(self) -> None:
        observation = self._observation(NOW + timedelta(seconds=30))
        assert observation.age_seconds(NOW) == Decimal("0.000")

    def test_quoted_cash_is_not_realised_profit(self) -> None:
        assert not self._observation(NOW).is_realised_cash

    def test_settled_wallet_value_is_not_realised_cash(self) -> None:
        observation = MonetaryObservation(
            amount=usd(4210, BalanceType.STEAM_WALLET),
            observed_at=NOW,
            venue="steam",
            fee_treatment=FeeTreatment.NET_OF_SELLER_FEES,
            settlement_status=SettlementStatus.SETTLED,
        )
        assert not observation.is_realised_cash

    def test_settled_withdrawable_cash_is_the_only_realised_case(self) -> None:
        observation = MonetaryObservation(
            amount=usd(4210),
            observed_at=NOW,
            venue="dmarket",
            fee_treatment=FeeTreatment.ALL_IN,
            settlement_status=SettlementStatus.SETTLED,
        )
        assert observation.is_realised_cash
