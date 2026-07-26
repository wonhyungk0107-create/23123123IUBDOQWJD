"""Exit valuation, capital cost and partial-fill risk."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from fractions import Fraction

import pytest
from tests.factories import NOW, make_listing, make_observation, usd

from tradeup.domain.contracts import TradeupInput
from tradeup.domain.fees import FeeSchedule, UnknownFeeError
from tradeup.domain.items import QualityType, WearCondition
from tradeup.domain.money import BalanceType, Currency
from tradeup.domain.valuation import ValuationConfidence, ValuationSource
from tradeup.valuation.capital import CapitalModel
from tradeup.valuation.exit_prices import ExitPricePolicy, ExitPriceResolver
from tradeup.valuation.partial_fill import PartialFillModel, PartialFillPolicy


def resolver(fee_schedule: FeeSchedule, **policy_kwargs: object) -> ExitPriceResolver:
    return ExitPriceResolver(
        fee_schedule=fee_schedule,
        capital_model=CapitalModel(annual_rate=Decimal("0.12")),
        base_currency=Currency.USD,
        policy=ExitPricePolicy(**policy_kwargs),  # type: ignore[arg-type]
    )


def resolve(res: ExitPriceResolver, observations: list[object], skin_id: str = "a-out-1") -> object:
    return res.resolve(
        skin_id=skin_id,
        market_hash_name="Weapon | out (Field-Tested)",
        quality=QualityType.NORMAL,
        wear=WearCondition.FIELD_TESTED,
        output_float=Decimal("0.20"),
        observations=observations,  # type: ignore[arg-type]
        moment=NOW,
    )


class TestCapitalModel:
    def test_carry_cost_scales_with_time(self) -> None:
        model = CapitalModel(annual_rate=Decimal("0.365"))
        # 0.365/365 = 0.1% per day, so 10 days is 1% -- $1.00 on $100.00.
        assert model.carry_cost(usd(10_000), 10) == usd(100)
        assert model.carry_cost(usd(10_000), 20) == usd(200)

    def test_carry_cost_rounds_up(self) -> None:
        model = CapitalModel(annual_rate=Decimal("0.12"))
        assert model.carry_cost(usd(100), 1).minor_units >= 1

    def test_zero_days_costs_nothing(self) -> None:
        assert CapitalModel(annual_rate=Decimal("0.12")).carry_cost(usd(10_000), 0) == usd(0)

    def test_negative_days_rejected(self) -> None:
        with pytest.raises(ValueError, match="cannot be negative"):
            CapitalModel(annual_rate=Decimal("0.12")).carry_cost(usd(100), -1)

    def test_float_days_rejected(self) -> None:
        with pytest.raises(TypeError):
            CapitalModel(annual_rate=Decimal("0.12")).carry_cost(usd(100), 1.5)  # type: ignore[arg-type]

    def test_timeline_sums_sequential_stages(self) -> None:
        model = CapitalModel(annual_rate=Decimal("0.12"))
        timeline = model.timeline(max_input_trade_lock_days=7, expected_days_to_sale=5)
        assert timeline.total_days == 7 + 1 + 1 + 5 + 2
        assert timeline.breakdown()["input_trade_lock"] == 7

    def test_profit_per_capital_day_charges_at_least_one_day(self) -> None:
        """A zero-day contract must not read as infinite velocity."""
        model = CapitalModel(annual_rate=Decimal("0.12"))
        assert model.profit_per_capital_day(usd(1000), 0) == usd(1000)
        assert model.profit_per_capital_day(usd(1000), 10) == usd(100)


class TestExitPriceHierarchy:
    def test_prefers_the_best_evidence_rung(self, fee_schedule: FeeSchedule) -> None:
        observations = [
            make_observation(gross_minor=1000, source=ValuationSource.CROSS_MARKET_REFERENCE),
            make_observation(gross_minor=2000, source=ValuationSource.EXECUTABLE_CASH_BID),
        ]
        result = resolve(resolver(fee_schedule), observations)
        assert result.source is ValuationSource.EXECUTABLE_CASH_BID  # type: ignore[attr-defined]
        assert result.gross_reference == usd(2000)  # type: ignore[attr-defined]

    def test_takes_a_low_quantile_not_the_mean(self, fee_schedule: FeeSchedule) -> None:
        """Five sales at 100..500 read as 200 (the 0.25 quantile), not 300 (the mean).

        The mean is flattered by the lucky sales; a low quantile is a price we would
        probably still get on a bad day.
        """
        observations = [
            make_observation(gross_minor=minor, source=ValuationSource.COMPLETED_SALE)
            for minor in (10_000, 20_000, 30_000, 40_000, 50_000)
        ]
        result = resolve(resolver(fee_schedule), observations)
        assert result.gross_reference == usd(20_000)  # type: ignore[attr-defined]

    def test_a_single_observation_is_used_as_is(self, fee_schedule: FeeSchedule) -> None:
        result = resolve(
            resolver(fee_schedule),
            [make_observation(gross_minor=12_345, source=ValuationSource.COMPLETED_SALE)],
        )
        assert result.gross_reference == usd(12_345)  # type: ignore[attr-defined]

    def test_no_evidence_yields_unvaluable(self, fee_schedule: FeeSchedule) -> None:
        result = resolve(resolver(fee_schedule), [])
        assert result.source is ValuationSource.UNVALUABLE  # type: ignore[attr-defined]
        assert result.net_proceeds.is_zero  # type: ignore[attr-defined]
        assert not result.is_valuable  # type: ignore[attr-defined]

    def test_steam_wallet_evidence_is_excluded_not_converted(
        self, fee_schedule: FeeSchedule
    ) -> None:
        """Wallet value is not cash, and converting it at par would be a lie."""
        observations = [make_observation(gross_minor=50_000, balance=BalanceType.STEAM_WALLET)]
        result = resolve(resolver(fee_schedule), observations)
        assert result.source is ValuationSource.UNVALUABLE  # type: ignore[attr-defined]

    def test_foreign_currency_evidence_is_excluded(self, fee_schedule: FeeSchedule) -> None:
        observations = [make_observation(gross_minor=50_000, currency=Currency.EUR)]
        result = resolve(resolver(fee_schedule), observations)
        assert result.source is ValuationSource.UNVALUABLE  # type: ignore[attr-defined]

    def test_stale_evidence_is_excluded(self, fee_schedule: FeeSchedule) -> None:
        observations = [make_observation(gross_minor=50_000, observed_at=NOW - timedelta(days=30))]
        result = resolve(resolver(fee_schedule), observations)
        assert result.source is ValuationSource.UNVALUABLE  # type: ignore[attr-defined]

    def test_mismatched_wear_is_ignored(self, fee_schedule: FeeSchedule) -> None:
        observations = [make_observation(gross_minor=50_000, wear=WearCondition.FACTORY_NEW)]
        result = resolve(resolver(fee_schedule), observations)
        assert result.source is ValuationSource.UNVALUABLE  # type: ignore[attr-defined]

    def test_unknown_exit_fee_raises_rather_than_defaulting(self) -> None:
        """An unknown material fee must stop the candidate."""
        empty = FeeSchedule([])
        with pytest.raises(UnknownFeeError):
            resolve(resolver(empty), [make_observation(gross_minor=2000)])


class TestExitDeductions:
    def test_every_deduction_is_itemised_and_nets_out(self, fee_schedule: FeeSchedule) -> None:
        observations = [
            make_observation(
                gross_minor=10_000, venue="dmarket", source=ValuationSource.COMPLETED_SALE
            )
        ]
        result = resolve(resolver(fee_schedule), observations)
        expected = (
            result.gross_reference  # type: ignore[attr-defined]
            - result.seller_fee  # type: ignore[attr-defined]
            - result.withdrawal_fee  # type: ignore[attr-defined]
            - result.liquidity_haircut  # type: ignore[attr-defined]
            - result.carry_cost  # type: ignore[attr-defined]
        )
        assert result.net_proceeds == expected  # type: ignore[attr-defined]

    def test_weaker_evidence_gets_a_bigger_haircut(self, fee_schedule: FeeSchedule) -> None:
        strong = resolve(
            resolver(fee_schedule),
            [make_observation(gross_minor=10_000, source=ValuationSource.EXECUTABLE_CASH_BID)],
        )
        weak = resolve(
            resolver(fee_schedule),
            [make_observation(gross_minor=10_000, source=ValuationSource.CROSS_MARKET_REFERENCE)],
        )
        assert weak.liquidity_haircut > strong.liquidity_haircut  # type: ignore[attr-defined]
        assert weak.net_proceeds < strong.net_proceeds  # type: ignore[attr-defined]

    def test_thin_evidence_reduces_confidence(self, fee_schedule: FeeSchedule) -> None:
        result = resolve(
            resolver(fee_schedule),
            [
                make_observation(
                    gross_minor=10_000,
                    source=ValuationSource.EXECUTABLE_CASH_BID,
                    evidence_count=1,
                )
            ],
        )
        assert result.confidence is ValuationConfidence.MEDIUM  # type: ignore[attr-defined]

    def test_lower_bound_proceeds_discounts_by_confidence(self, fee_schedule: FeeSchedule) -> None:
        result = resolve(
            resolver(fee_schedule),
            [make_observation(gross_minor=10_000, source=ValuationSource.EXECUTABLE_CASH_BID)],
        )
        assert result.lower_bound_proceeds < result.net_proceeds  # type: ignore[attr-defined]

    def test_costs_exceeding_gross_floor_at_zero(self, fee_schedule: FeeSchedule) -> None:
        """A tiny output is worth zero, never a negative asset."""
        result = resolve(
            resolver(fee_schedule),
            [
                make_observation(
                    gross_minor=1, venue="dmarket", source=ValuationSource.CONSERVATIVE_FALLBACK
                )
            ],
        )
        assert result.net_proceeds.is_zero  # type: ignore[attr-defined]


class TestPartialFill:
    def _inputs(self, count: int = 3, **kwargs: object) -> list[TradeupInput]:
        return [
            TradeupInput(
                listing=make_listing(f"P-{i}", price_minor=1000, **kwargs),  # type: ignore[arg-type]
                normalized=Fraction(1, 10),
            )
            for i in range(count)
        ]

    def test_completion_probability_is_the_product_of_survivals(self) -> None:
        model = PartialFillModel(base_currency=Currency.USD)
        assessment = model.assess(self._inputs(3), NOW)
        expected = Decimal(1)
        for probability in assessment.per_listing_survival.values():
            expected *= probability
        assert abs(assessment.completion_probability - expected) < Decimal("0.0001")

    def test_more_inputs_means_lower_completion_probability(self) -> None:
        model = PartialFillModel(base_currency=Currency.USD)
        few = model.assess(self._inputs(2), NOW).completion_probability
        many = model.assess(self._inputs(10), NOW).completion_probability
        assert many < few

    def test_older_quotes_are_less_likely_to_survive(self) -> None:
        model = PartialFillModel(base_currency=Currency.USD)
        fresh = make_listing("fresh", observed_at=NOW)
        stale = make_listing("stale", observed_at=NOW - timedelta(hours=2))
        fresh_p = model.survival_probability(
            TradeupInput(listing=fresh, normalized=Fraction(0)), NOW
        )
        stale_p = model.survival_probability(
            TradeupInput(listing=stale, normalized=Fraction(0)), NOW
        )
        assert stale_p < fresh_p

    def test_purchase_sequence_puts_the_most_fragile_first(self) -> None:
        """Fail fast, with the least capital committed."""
        model = PartialFillModel(base_currency=Currency.USD)
        fragile = make_listing("fragile", observed_at=NOW - timedelta(hours=2))
        solid = make_listing("solid", observed_at=NOW)
        inputs = [
            TradeupInput(listing=solid, normalized=Fraction(0)),
            TradeupInput(listing=fragile, normalized=Fraction(0)),
        ]
        assessment = model.assess(inputs, NOW)
        assert assessment.purchase_sequence[0].listing_id == "fragile"

    def test_first_failure_point_costs_nothing(self) -> None:
        model = PartialFillModel(base_currency=Currency.USD)
        assessment = model.assess(self._inputs(3), NOW)
        assert assessment.failure_points[0].cost_committed.is_zero
        assert assessment.failure_points[0].loss_if_fails_here.is_zero

    def test_later_failures_cost_more(self) -> None:
        model = PartialFillModel(base_currency=Currency.USD)
        assessment = model.assess(self._inputs(4), NOW)
        losses = [p.loss_if_fails_here.minor_units for p in assessment.failure_points]
        assert losses == sorted(losses)

    def test_salvage_rate_determines_the_orphan_loss(self) -> None:
        model = PartialFillModel(
            base_currency=Currency.USD,
            policy=PartialFillPolicy(salvage_rate=Decimal("0.50")),
        )
        assessment = model.assess(self._inputs(2), NOW)
        # One purchase costs $10.02 all-in ($10.00 + 2c buyer fee); half is salvaged.
        assert assessment.failure_points[1].loss_if_fails_here == usd(501)

    def test_expected_loss_is_positive_when_failure_is_possible(self) -> None:
        model = PartialFillModel(base_currency=Currency.USD)
        assert model.assess(self._inputs(5), NOW).expected_loss.is_positive

    def test_empty_bundle_rejected(self) -> None:
        with pytest.raises(ValueError, match="empty bundle"):
            PartialFillModel(base_currency=Currency.USD).assess([], NOW)

    def test_policy_validation(self) -> None:
        with pytest.raises(ValueError, match="within \\[0, 1\\]"):
            PartialFillPolicy(salvage_rate=Decimal("1.5"))
        with pytest.raises(ValueError, match="must be positive"):
            PartialFillPolicy(survival_half_life_seconds=0)
        with pytest.raises(ValueError, match="cannot exceed"):
            PartialFillPolicy(
                base_survival_probability=Decimal("0.5"), floor_survival=Decimal("0.9")
            )
