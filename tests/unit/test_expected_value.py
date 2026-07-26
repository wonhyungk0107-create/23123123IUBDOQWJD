"""Expected-value engine: exact weighting, conservative rounding, input guards."""

from __future__ import annotations

from decimal import Decimal
from fractions import Fraction

import pytest
from tests.factories import NOW, make_candidate, make_listing, make_outcome, usd

from tradeup.config import Settings
from tradeup.domain.items import Rarity
from tradeup.domain.money import BalanceType, Currency, Money
from tradeup.valuation.capital import CapitalModel, CapitalTimeline
from tradeup.valuation.expected_value import ExpectedValueEngine, probability_weighted
from tradeup.valuation.partial_fill import PartialFillModel


def engine(**overrides: object) -> ExpectedValueEngine:
    base: dict[str, object] = {"database_url": "sqlite+pysqlite:///:memory:"}
    base.update(overrides)
    settings = Settings(**base)  # type: ignore[arg-type]
    return ExpectedValueEngine(
        settings=settings,
        capital_model=CapitalModel(annual_rate=settings.annual_capital_cost_rate),
        partial_fill_model=PartialFillModel(base_currency=Currency.USD),
    )


class TestProbabilityWeighting:
    def test_empty_terms_are_zero_not_an_error(self) -> None:
        assert probability_weighted([], Currency.USD) == usd(0)

    def test_exact_weighting_avoids_per_term_rounding_drift(self) -> None:
        """Ten thirds of 100 is exactly 1000, not 990 from ten roundings of 33.33."""
        terms = [(Fraction(1, 3), usd(100)) for _ in range(30)]
        assert probability_weighted(terms, Currency.USD) == usd(1000)

    def test_rounds_down_once_at_the_end(self) -> None:
        # 1/3 of 100 is 33.33...; conservative rounding gives 33.
        assert probability_weighted([(Fraction(1, 3), usd(100))], Currency.USD) == usd(33)

    def test_currency_mismatch_raises(self) -> None:
        with pytest.raises(ValueError, match="expected"):
            probability_weighted([(Fraction(1), Money(100, Currency.EUR))], Currency.USD)


class TestEvaluation:
    def test_produces_a_complete_evaluation(self) -> None:
        candidate = make_candidate()
        evaluation = engine().evaluate(candidate, moment=NOW, fee_schedule_id="test")
        assert evaluation.candidate_id == candidate.candidate_id
        assert evaluation.acquisition_cost.is_positive
        assert evaluation.all_in_cost >= evaluation.acquisition_cost
        assert evaluation.expected_output_value.is_positive

    def test_all_in_cost_is_acquisition_plus_the_modelled_charges(self) -> None:
        evaluation = engine().evaluate(make_candidate(), moment=NOW, fee_schedule_id="t")
        assert evaluation.all_in_cost == (
            evaluation.acquisition_cost
            + evaluation.operational_cost
            + evaluation.settlement_cost
            + evaluation.capital_carry_cost
            + evaluation.partial_fill_reserve
        )

    def test_ev_is_expected_value_minus_all_in_cost(self) -> None:
        evaluation = engine().evaluate(make_candidate(), moment=NOW, fee_schedule_id="t")
        assert evaluation.ev_net == evaluation.expected_output_value - evaluation.all_in_cost

    def test_roi_is_taken_against_acquisition_cost(self) -> None:
        evaluation = engine().evaluate(make_candidate(), moment=NOW, fee_schedule_id="t")
        assert evaluation.roi_net == evaluation.ev_net.ratio_to(evaluation.acquisition_cost)

    def test_lower_bound_ev_is_never_above_the_point_estimate(self) -> None:
        evaluation = engine().evaluate(make_candidate(), moment=NOW, fee_schedule_id="t")
        assert evaluation.lower_bound_ev <= evaluation.ev_net

    def test_cheaper_inputs_cannot_reduce_ev(self) -> None:
        """Monotonicity: holding everything else fixed, paying less is never worse."""
        expensive = make_candidate(
            listings=[make_listing(f"E-{i}", price_minor=500) for i in range(10)]
        )
        cheap = make_candidate(
            listings=[make_listing(f"E-{i}", price_minor=100) for i in range(10)]
        )
        ev_expensive = engine().evaluate(expensive, moment=NOW, fee_schedule_id="t")
        ev_cheap = engine().evaluate(cheap, moment=NOW, fee_schedule_id="t")
        assert ev_cheap.ev_net > ev_expensive.ev_net

    def test_higher_fees_cannot_increase_ev(self) -> None:
        low = make_candidate(
            listings=[make_listing(f"F-{i}", buyer_fee_minor=1) for i in range(10)]
        )
        high = make_candidate(
            listings=[make_listing(f"F-{i}", buyer_fee_minor=50) for i in range(10)]
        )
        assert (
            engine().evaluate(high, moment=NOW, fee_schedule_id="t").ev_net
            < engine().evaluate(low, moment=NOW, fee_schedule_id="t").ev_net
        )

    def test_worst_case_uses_the_least_valuable_outcome(self) -> None:
        candidate = make_candidate(
            outcomes=[
                make_outcome("cheap", probability=Fraction(1, 2), net_minor=100),
                make_outcome("rich", probability=Fraction(1, 2), net_minor=100_000),
            ]
        )
        evaluation = engine().evaluate(candidate, moment=NOW, fee_schedule_id="t")
        assert evaluation.worst_case_pnl == usd(100) - evaluation.all_in_cost
        assert evaluation.best_case_pnl == usd(100_000) - evaluation.all_in_cost

    def test_probability_of_profit_counts_only_outcomes_above_all_in_cost(self) -> None:
        candidate = make_candidate(
            outcomes=[
                make_outcome("loser", probability=Fraction(3, 4), net_minor=1),
                make_outcome("winner", probability=Fraction(1, 4), net_minor=1_000_000),
            ]
        )
        evaluation = engine().evaluate(candidate, moment=NOW, fee_schedule_id="t")
        assert evaluation.probability_of_profit == Fraction(1, 4)
        assert evaluation.probability_of_profit_percent == Decimal("25.00")

    def test_unvaluable_mass_is_carried_through(self) -> None:
        from tradeup.domain.items import QualityType, WearCondition
        from tradeup.domain.valuation import OutputValuation

        unvaluable = OutputValuation.unvaluable(
            skin_id="unknown",
            market_hash_name="Weapon | Unknown (Field-Tested)",
            quality=QualityType.NORMAL,
            wear=WearCondition.FIELD_TESTED,
            output_float=Decimal("0.2"),
            currency_reference=usd(0),
            observed_at=NOW,
        )
        candidate = make_candidate(
            outcomes=[
                make_outcome("priced", probability=Fraction(1, 2)),
                make_outcome("unpriced", probability=Fraction(1, 2), valuation=unvaluable),
            ]
        )
        evaluation = engine().evaluate(candidate, moment=NOW, fee_schedule_id="t")
        assert evaluation.unvaluable_probability_mass == Fraction(1, 2)

    def test_input_permutations_produce_identical_economics(self) -> None:
        listings = [make_listing(f"P-{i}", price_minor=100 + i) for i in range(10)]
        forward = engine().evaluate(
            make_candidate(listings=listings), moment=NOW, fee_schedule_id="t"
        )
        reverse = engine().evaluate(
            make_candidate(listings=list(reversed(listings))), moment=NOW, fee_schedule_id="t"
        )
        assert forward.ev_net == reverse.ev_net
        assert forward.acquisition_cost == reverse.acquisition_cost
        assert forward.roi_net == reverse.roi_net


class TestSettlementCharge:
    def _engine_with_settlement(self, rate: Fraction) -> ExpectedValueEngine:
        settings = Settings(database_url="sqlite+pysqlite:///:memory:")  # type: ignore[call-arg]
        return ExpectedValueEngine(
            settings=settings,
            capital_model=CapitalModel(annual_rate=settings.annual_capital_cost_rate),
            partial_fill_model=PartialFillModel(base_currency=Currency.USD),
            settlement_charge_rate=rate,
        )

    def test_default_settlement_cost_is_an_explicit_zero(self) -> None:
        evaluation = engine().evaluate(make_candidate(), moment=NOW, fee_schedule_id="t")
        assert evaluation.settlement_cost == usd(0)

    def test_settlement_charge_is_proportional_to_acquisition_and_rounds_up(self) -> None:
        candidate = make_candidate()
        base = engine().evaluate(candidate, moment=NOW, fee_schedule_id="t")
        charged = self._engine_with_settlement(Fraction(1, 100)).evaluate(
            candidate, moment=NOW, fee_schedule_id="t"
        )
        expected = base.acquisition_cost.scaled_up(Fraction(1, 100))
        assert charged.settlement_cost == expected
        assert charged.all_in_cost == base.all_in_cost + expected
        assert charged.ev_net == base.ev_net - expected

    def test_negative_settlement_rate_is_refused(self) -> None:
        with pytest.raises(ValueError, match="cannot be negative"):
            self._engine_with_settlement(Fraction(-1, 100))


class TestInputGuards:
    def test_a_foreign_currency_listing_is_refused(self) -> None:
        """Mixing currencies without an FX quote is a category error, not a discount."""
        listing = make_listing("EUR-1")
        foreign = type(listing)(
            **{
                **{f: getattr(listing, f) for f in listing.__dataclass_fields__},
                "price": Money(100, Currency.EUR),
                "buyer_fee": Money(2, Currency.EUR),
                "deposit_fee": Money(0, Currency.EUR),
            }
        )
        candidate = make_candidate(listings=[foreign, *[make_listing(f"X-{i}") for i in range(9)]])
        with pytest.raises(ValueError, match="FX quote is required"):
            engine().evaluate(candidate, moment=NOW, fee_schedule_id="t")

    def test_a_non_cash_balance_listing_is_refused(self) -> None:
        listing = make_listing("W-1")
        wallet = type(listing)(
            **{
                **{f: getattr(listing, f) for f in listing.__dataclass_fields__},
                "price": Money(100, Currency.USD, BalanceType.STEAM_WALLET),
                "buyer_fee": Money(2, Currency.USD, BalanceType.STEAM_WALLET),
                "deposit_fee": Money(0, Currency.USD, BalanceType.STEAM_WALLET),
            }
        )
        candidate = make_candidate(listings=[wallet, *[make_listing(f"Y-{i}") for i in range(9)]])
        with pytest.raises(ValueError, match="documented conversion"):
            engine().evaluate(candidate, moment=NOW, fee_schedule_id="t")


class TestCapitalGuards:
    def test_negative_annual_rate_rejected(self) -> None:
        with pytest.raises(ValueError, match="cannot be negative"):
            CapitalModel(annual_rate=Decimal("-0.01"))

    def test_negative_principal_rejected(self) -> None:
        with pytest.raises(ValueError, match="non-negative principal"):
            CapitalModel(annual_rate=Decimal("0.12")).carry_cost(usd(-100), 5)

    def test_negative_timeline_stage_rejected(self) -> None:
        with pytest.raises(ValueError, match="cannot be negative"):
            CapitalTimeline(
                max_input_trade_lock_days=-1,
                transfer_days=0,
                contract_execution_days=0,
                expected_days_to_sale=0,
                settlement_days=0,
            )

    def test_a_trade_locked_input_extends_capital_days(self) -> None:
        from datetime import timedelta

        from tradeup.domain.listings import TradableStatus

        locked = [
            make_listing(
                f"L-{i}",
                tradable=TradableStatus.TRADE_LOCKED,
                trade_lock_until=NOW + timedelta(days=7),
            )
            for i in range(10)
        ]
        free = engine().evaluate(make_candidate(), moment=NOW, fee_schedule_id="t")
        held = engine().evaluate(make_candidate(listings=locked), moment=NOW, fee_schedule_id="t")
        assert held.expected_capital_days > free.expected_capital_days
        assert held.capital_carry_cost >= free.capital_carry_cost


def test_five_input_covert_candidate_evaluates() -> None:
    listings = [make_listing(f"C-{i}", rarity=Rarity.COVERT) for i in range(5)]
    candidate = make_candidate(listings=listings, input_rarity=Rarity.COVERT)
    evaluation = engine().evaluate(candidate, moment=NOW, fee_schedule_id="t")
    assert evaluation.acquisition_cost.is_positive
