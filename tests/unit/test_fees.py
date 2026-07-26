"""Fee schedule: resolution, tiering, rounding, and failing closed."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from tradeup.domain.fees import (
    FeeOperation,
    FeeRule,
    FeeSchedule,
    UnknownFeeError,
)
from tradeup.domain.money import BalanceType, Currency, Money

EPOCH = datetime(2020, 1, 1, tzinfo=UTC)
NOW = datetime(2026, 7, 25, 12, 0, 0, tzinfo=UTC)


def rule(
    rule_id: str = "r1",
    *,
    venue: str = "v",
    operation: FeeOperation = FeeOperation.SALE,
    percentage: str = "0.05",
    fixed_minor: int = 0,
    balance: BalanceType = BalanceType.CASH_WITHDRAWABLE,
    currency: Currency = Currency.USD,
    tier_lower: int = 0,
    tier_upper: int | None = None,
    minimum: int | None = None,
    maximum: int | None = None,
    effective_until: datetime | None = None,
) -> FeeRule:
    return FeeRule(
        rule_id=rule_id,
        venue=venue,
        operation=operation,
        balance_type=balance,
        currency=currency,
        percentage=Decimal(percentage),
        fixed_minor=fixed_minor,
        effective_from=EPOCH,
        effective_until=effective_until,
        source="test",
        last_verified=None,
        tier_lower_minor=tier_lower,
        tier_upper_minor=tier_upper,
        minimum_fee_minor=minimum,
        maximum_fee_minor=maximum,
    )


def usd(minor: int, balance: BalanceType = BalanceType.CASH_WITHDRAWABLE) -> Money:
    return Money(minor, Currency.USD, balance)


class TestFailClosed:
    def test_missing_rule_raises_rather_than_returning_zero(self) -> None:
        schedule = FeeSchedule([])
        with pytest.raises(UnknownFeeError, match="refusing to assume zero"):
            schedule.quote("v", FeeOperation.SALE, usd(1000), NOW)

    def test_ambiguous_rules_raise(self) -> None:
        """Two matching tiers means the schedule is wrong; picking one would bias."""
        schedule = FeeSchedule([rule("a"), rule("b")])
        with pytest.raises(UnknownFeeError, match="ambiguous"):
            schedule.quote("v", FeeOperation.SALE, usd(1000), NOW)

    def test_wrong_venue_does_not_match(self) -> None:
        schedule = FeeSchedule([rule(venue="other")])
        with pytest.raises(UnknownFeeError):
            schedule.quote("v", FeeOperation.SALE, usd(1000), NOW)

    def test_wrong_balance_type_does_not_match(self) -> None:
        """A cash fee schedule must not be applied to Steam Wallet value."""
        schedule = FeeSchedule([rule()])
        with pytest.raises(UnknownFeeError):
            schedule.quote("v", FeeOperation.SALE, usd(1000, BalanceType.STEAM_WALLET), NOW)

    def test_expired_rule_does_not_match(self) -> None:
        schedule = FeeSchedule([rule(effective_until=datetime(2021, 1, 1, tzinfo=UTC))])
        with pytest.raises(UnknownFeeError):
            schedule.quote("v", FeeOperation.SALE, usd(1000), NOW)

    def test_has_rule_reports_availability_without_raising(self) -> None:
        schedule = FeeSchedule([rule()])
        assert schedule.has_rule("v", FeeOperation.SALE, usd(1000), NOW)
        assert not schedule.has_rule("v", FeeOperation.PURCHASE, usd(1000), NOW)


class TestQuoting:
    def test_percentage_fee(self) -> None:
        schedule = FeeSchedule([rule(percentage="0.05")])
        quote = schedule.quote("v", FeeOperation.SALE, usd(1000), NOW)
        assert quote.fee == usd(50)
        assert quote.effective_rate == Decimal("0.05")

    def test_percentage_plus_fixed(self) -> None:
        schedule = FeeSchedule([rule(percentage="0.02", fixed_minor=25)])
        assert schedule.quote("v", FeeOperation.SALE, usd(1000), NOW).fee == usd(45)

    def test_fees_always_round_up(self) -> None:
        """We never understate a cost. 1% of 101 is 1.01, charged as 2."""
        schedule = FeeSchedule([rule(percentage="0.01")])
        assert schedule.quote("v", FeeOperation.SALE, usd(101), NOW).fee == usd(2)

    def test_minimum_fee_floor(self) -> None:
        schedule = FeeSchedule([rule(percentage="0.01", minimum=50)])
        assert schedule.quote("v", FeeOperation.SALE, usd(100), NOW).fee == usd(50)

    def test_maximum_fee_cap(self) -> None:
        schedule = FeeSchedule([rule(percentage="0.10", maximum=100)])
        assert schedule.quote("v", FeeOperation.SALE, usd(10_000), NOW).fee == usd(100)

    def test_negative_basis_uses_magnitude_and_stays_positive(self) -> None:
        schedule = FeeSchedule([rule(percentage="0.05")])
        assert schedule.quote("v", FeeOperation.SALE, usd(-1000), NOW).fee == usd(50)

    def test_zero_basis_has_zero_effective_rate(self) -> None:
        schedule = FeeSchedule([rule(percentage="0.05")])
        assert schedule.quote("v", FeeOperation.SALE, usd(0), NOW).effective_rate == Decimal(0)

    def test_quote_carries_provenance(self) -> None:
        schedule = FeeSchedule([rule("csfloat:sale:v3")])
        quote = schedule.quote("v", FeeOperation.SALE, usd(1000), NOW)
        assert quote.rule_id == "csfloat:sale:v3"
        assert quote.source == "test"
        assert quote.quoted_at == NOW


class TestTiering:
    def _tiered(self) -> FeeSchedule:
        return FeeSchedule(
            [
                rule("low", percentage="0.10", tier_lower=0, tier_upper=1000),
                rule("high", percentage="0.05", tier_lower=1000),
            ]
        )

    def test_selects_the_tier_containing_the_basis(self) -> None:
        schedule = self._tiered()
        assert schedule.quote("v", FeeOperation.SALE, usd(500), NOW).rule_id == "low"
        assert schedule.quote("v", FeeOperation.SALE, usd(5000), NOW).rule_id == "high"

    def test_tier_boundary_belongs_to_the_upper_tier(self) -> None:
        """Bounds are [lower, upper), so 1000 is in the high tier."""
        assert self._tiered().quote("v", FeeOperation.SALE, usd(1000), NOW).rule_id == "high"


class TestValidation:
    def test_duplicate_rule_ids_rejected(self) -> None:
        with pytest.raises(ValueError, match="duplicate fee rule ids"):
            FeeSchedule([rule("same"), rule("same", venue="other")])

    def test_negative_percentage_rejected(self) -> None:
        with pytest.raises(ValueError, match="negative percentage"):
            rule(percentage="-0.01")

    def test_float_percentage_rejected(self) -> None:
        with pytest.raises(TypeError):
            FeeRule(
                rule_id="f",
                venue="v",
                operation=FeeOperation.SALE,
                balance_type=BalanceType.CASH_WITHDRAWABLE,
                currency=Currency.USD,
                percentage=0.05,  # type: ignore[arg-type]
                fixed_minor=0,
                effective_from=EPOCH,
                effective_until=None,
                source="test",
                last_verified=None,
            )

    def test_source_is_required(self) -> None:
        with pytest.raises(ValueError, match="requires a source reference"):
            FeeRule(
                rule_id="f",
                venue="v",
                operation=FeeOperation.SALE,
                balance_type=BalanceType.CASH_WITHDRAWABLE,
                currency=Currency.USD,
                percentage=Decimal("0.05"),
                fixed_minor=0,
                effective_from=EPOCH,
                effective_until=None,
                source="",
                last_verified=None,
            )

    def test_empty_tier_rejected(self) -> None:
        with pytest.raises(ValueError, match="empty tier"):
            rule(tier_lower=100, tier_upper=100)

    def test_explicit_zero_requires_a_stated_reason(self) -> None:
        """A zero fee must be an asserted fact, not a missing lookup."""
        schedule = FeeSchedule([])
        quote = schedule.zero_quote(
            "v", FeeOperation.DEPOSIT, usd(1000), NOW, reason="venue documents no deposit fee"
        )
        assert quote.fee.is_zero
        assert "documents no deposit fee" in quote.source
