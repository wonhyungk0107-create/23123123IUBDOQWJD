"""The sourced live fee schedule: right numbers, right provenance discipline."""

from __future__ import annotations

from datetime import UTC, datetime

from tradeup.domain.fees import FeeOperation
from tradeup.domain.money import BalanceType, Currency, Money
from tradeup.valuation.venue_fees import LIVE_FEE_SCHEDULE_ID, build_live_fee_schedule

NOW = datetime(2026, 7, 26, 12, 0, 0, tzinfo=UTC)


def usd(minor: int) -> Money:
    return Money(minor, Currency.USD, BalanceType.CASH_WITHDRAWABLE)


class TestLiveFeeSchedule:
    def test_csfloat_sale_fee_is_two_percent(self) -> None:
        quote = build_live_fee_schedule().quote("csfloat", FeeOperation.SALE, usd(10_000), NOW)
        assert quote.fee == usd(200)

    def test_csfloat_withdrawal_takes_the_pessimistic_end_of_the_range(self) -> None:
        quote = build_live_fee_schedule().quote(
            "csfloat", FeeOperation.WITHDRAWAL, usd(10_000), NOW
        )
        assert quote.fee == usd(250)  # 2.5%, the top of the published 0.5-2.5% range

    def test_skinport_sale_fee_is_eight_percent(self) -> None:
        quote = build_live_fee_schedule().quote("skinport", FeeOperation.SALE, usd(10_000), NOW)
        assert quote.fee == usd(800)

    def test_skinport_payout_is_a_sourced_zero(self) -> None:
        quote = build_live_fee_schedule().quote(
            "skinport", FeeOperation.WITHDRAWAL, usd(10_000), NOW
        )
        assert quote.fee == usd(0)
        assert "payout-fees" in quote.source

    def test_every_rule_carries_provenance_and_no_false_verification_claim(self) -> None:
        for rule in build_live_fee_schedule().rules:
            assert rule.source, rule.rule_id
            # None of these figures has been confirmed at a venue account screen.
            assert rule.last_verified is None, rule.rule_id

    def test_schedule_id_is_dated(self) -> None:
        assert LIVE_FEE_SCHEDULE_ID == "live-sourced-2026-07-26"
