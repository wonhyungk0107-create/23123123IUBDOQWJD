"""Crypto settlement rail: the round trip is priced correctly and fails closed.

The golden numbers are hand-computed from the stated synthetic fees and rate:

capital $500.00 at 100,000 USD/BTC, deposit 1%, FX spread 0.5%, withdrawal 2%,
network flat 20,000 sat each way, volatility haircut 2%:

* funding: fee 500 + spread 250 -> cover 50,750 cents -> 507,500 sat + 20,000 network
* withdrawal: fee 1,000 + spread 250 -> 48,750 cents -> 487,500 sat - 20,000 network
  = 467,500 sat, x0.98 haircut = 458,150 sat
* drag: 527,500 sat in ($527.50) - 458,150 sat out ($458.15) = $69.35
* amortised over 10 contracts: ceil(693.5) = 694 minor = $6.94
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from fractions import Fraction

import pytest
from tests.conftest import FROZEN_NOW

from tradeup.config import Settings
from tradeup.domain.conversion import (
    ConversionPairError,
    ConversionQuote,
    StaleConversionQuoteError,
)
from tradeup.domain.fees import FeeOperation, FeeRule, FeeSchedule, UnknownFeeError
from tradeup.domain.money import BalanceType, Currency, Money
from tradeup.valuation.settlement import CryptoSettlementPlanner

VENUE = "test-market"
RAIL = "test-rail"
EPOCH = datetime(2020, 1, 1, tzinfo=UTC)
SOURCE = "tests/unit/test_settlement.py synthetic fee -- NOT a real venue fee"


def usd(minor: int) -> Money:
    return Money(minor, Currency.USD, BalanceType.CASH_WITHDRAWABLE)


def btc(minor: int) -> Money:
    return Money(minor, Currency.BTC, BalanceType.CASH_WITHDRAWABLE)


def _rule(
    rule_id: str,
    operation: FeeOperation,
    percentage: str,
    *,
    venue: str = VENUE,
    currency: Currency = Currency.USD,
    fixed_minor: int = 0,
) -> FeeRule:
    return FeeRule(
        rule_id=rule_id,
        venue=venue,
        operation=operation,
        balance_type=BalanceType.CASH_WITHDRAWABLE,
        currency=currency,
        percentage=Decimal(percentage),
        fixed_minor=fixed_minor,
        effective_from=EPOCH,
        effective_until=None,
        source=SOURCE,
        last_verified=None,
    )


def full_schedule() -> FeeSchedule:
    return FeeSchedule(
        [
            _rule("t:deposit", FeeOperation.DEPOSIT, "0.01"),
            _rule("t:fx", FeeOperation.FX_CONVERSION, "0.005"),
            _rule("t:withdrawal", FeeOperation.WITHDRAWAL, "0.02"),
            _rule(
                "t:network",
                FeeOperation.NETWORK_TRANSFER,
                "0",
                venue=RAIL,
                currency=Currency.BTC,
                fixed_minor=20_000,
            ),
        ]
    )


def crypto_settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "database_url": "sqlite+pysqlite:///:memory:",
        "crypto_settlement_enabled": True,
        "settlement_currency": Currency.BTC,
        "crypto_volatility_haircut": Decimal("0.02"),
        "settlement_amortization_contracts": 10,
        "max_conversion_quote_age_seconds": 120,
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def planner(
    settings: Settings | None = None, schedule: FeeSchedule | None = None
) -> CryptoSettlementPlanner:
    return CryptoSettlementPlanner(
        settings=settings or crypto_settings(),
        fee_schedule=schedule or full_schedule(),
        funding_venue=VENUE,
        network_venue=RAIL,
    )


def fresh_quote(rate: str = "100000") -> ConversionQuote:
    return ConversionQuote(
        base=Currency.BTC,
        quote=Currency.USD,
        rate=Decimal(rate),
        observed_at=FROZEN_NOW - timedelta(seconds=30),
        source="tests/unit/test_settlement.py synthetic rate -- NOT a market observation",
        venue=RAIL,
    )


class TestGoldenRoundTrip:
    def test_funding_leg(self) -> None:
        plan = planner().plan(capital=usd(50_000), quote=fresh_quote(), moment=FROZEN_NOW)
        assert plan.funding.fiat_target == usd(50_000)
        assert plan.funding.deposit_fee == usd(500)
        assert plan.funding.conversion_spread_fee == usd(250)
        assert plan.funding.crypto_sent == btc(507_500)
        assert plan.funding.network_fee == btc(20_000)
        assert plan.funding.total_crypto_outlay == btc(527_500)

    def test_withdrawal_leg(self) -> None:
        plan = planner().plan(capital=usd(50_000), quote=fresh_quote(), moment=FROZEN_NOW)
        assert plan.withdrawal.withdrawal_fee == usd(1_000)
        assert plan.withdrawal.conversion_spread_fee == usd(250)
        assert plan.withdrawal.network_fee == btc(20_000)
        assert plan.withdrawal.crypto_received == btc(467_500)
        assert plan.withdrawal.crypto_after_haircut == btc(458_150)

    def test_round_trip_drag_and_charge_rate(self) -> None:
        plan = planner().plan(capital=usd(50_000), quote=fresh_quote(), moment=FROZEN_NOW)
        assert plan.round_trip_drag == usd(6_935)
        assert plan.drag_ratio == Fraction(6_935, 50_000)
        assert plan.per_contract_charge_rate(10) == Fraction(6_935, 500_000)
        assert plan.per_contract_charge_rate(1) == Fraction(6_935, 50_000)

    def test_zero_haircut_keeps_the_received_amount(self) -> None:
        settings = crypto_settings(crypto_volatility_haircut=Decimal("0"))
        plan = planner(settings).plan(capital=usd(50_000), quote=fresh_quote(), moment=FROZEN_NOW)
        assert plan.withdrawal.crypto_after_haircut == plan.withdrawal.crypto_received


class TestFailClosed:
    def test_missing_fx_rule_raises(self) -> None:
        schedule = FeeSchedule([r for r in full_schedule().rules if "fx" not in r.rule_id])
        with pytest.raises(UnknownFeeError, match="FX_CONVERSION"):
            planner(schedule=schedule).plan(
                capital=usd(50_000), quote=fresh_quote(), moment=FROZEN_NOW
            )

    def test_missing_network_rule_raises(self) -> None:
        schedule = FeeSchedule([r for r in full_schedule().rules if "network" not in r.rule_id])
        with pytest.raises(UnknownFeeError, match="NETWORK_TRANSFER"):
            planner(schedule=schedule).plan(
                capital=usd(50_000), quote=fresh_quote(), moment=FROZEN_NOW
            )

    def test_stale_quote_raises(self) -> None:
        stale = ConversionQuote(
            base=Currency.BTC,
            quote=Currency.USD,
            rate=Decimal("100000"),
            observed_at=FROZEN_NOW - timedelta(seconds=121),
            source="stated synthetic",
            venue=RAIL,
        )
        with pytest.raises(StaleConversionQuoteError, match="older"):
            planner().plan(capital=usd(50_000), quote=stale, moment=FROZEN_NOW)

    def test_wrong_pair_raises(self) -> None:
        eth_quote = ConversionQuote(
            base=Currency.ETH,
            quote=Currency.USD,
            rate=Decimal("5000"),
            observed_at=FROZEN_NOW - timedelta(seconds=30),
            source="stated synthetic",
            venue=RAIL,
        )
        with pytest.raises(ConversionPairError, match="settlement currency"):
            planner().plan(capital=usd(50_000), quote=eth_quote, moment=FROZEN_NOW)

    def test_foreign_capital_raises(self) -> None:
        with pytest.raises(ValueError, match="base currency"):
            planner().plan(
                capital=Money(50_000, Currency.EUR), quote=fresh_quote(), moment=FROZEN_NOW
            )

    def test_non_positive_capital_raises(self) -> None:
        with pytest.raises(ValueError, match="positive capital"):
            planner().plan(capital=usd(0), quote=fresh_quote(), moment=FROZEN_NOW)

    def test_amortisation_below_one_contract_raises(self) -> None:
        plan = planner().plan(capital=usd(50_000), quote=fresh_quote(), moment=FROZEN_NOW)
        with pytest.raises(ValueError, match="at least one contract"):
            plan.per_contract_charge_rate(0)


class TestSettlementSettings:
    def test_enabled_rail_requires_a_crypto_currency(self) -> None:
        with pytest.raises(ValueError, match="crypto settlement currency"):
            crypto_settings(settlement_currency=Currency.USD)

    def test_crypto_currency_requires_the_rail_enabled(self) -> None:
        with pytest.raises(ValueError, match="rail is disabled"):
            Settings(  # type: ignore[call-arg]
                database_url="sqlite+pysqlite:///:memory:",
                settlement_currency=Currency.BTC,
            )

    def test_cross_fiat_settlement_is_not_modelled(self) -> None:
        with pytest.raises(ValueError, match="cross-fiat"):
            Settings(  # type: ignore[call-arg]
                database_url="sqlite+pysqlite:///:memory:",
                settlement_currency=Currency.EUR,
            )

    def test_defaults_are_fiat_and_disabled(self) -> None:
        settings = Settings(database_url="sqlite+pysqlite:///:memory:")  # type: ignore[call-arg]
        assert not settings.crypto_settlement_enabled
        assert settings.settlement_currency is Currency.USD

    def test_gate_summary_reports_the_rail(self) -> None:
        summary = crypto_settings().gate_summary()
        assert summary["crypto_settlement_enabled"] == "True"
        assert summary["settlement_currency"] == "BTC"
        assert summary["crypto_volatility_haircut"] == "0.02"

    def test_wallet_address_presence_is_reported_without_the_value(self) -> None:
        settings = crypto_settings(crypto_wallet_address="bc1q-test-address-never-logged")
        presence = settings.available_credentials()
        assert presence["crypto_wallet_address"] is True
        assert "bc1q" not in str(settings.gate_summary())

    def test_a_blank_env_line_is_not_a_present_credential(self) -> None:
        """`NAME=` with no value in .env must report absent, not present."""
        settings = crypto_settings(crypto_wallet_address="", skinsnipe_api_key="   ")
        presence = settings.available_credentials()
        assert presence["crypto_wallet_address"] is False
        assert presence["skinsnipe_api_key"] is False
