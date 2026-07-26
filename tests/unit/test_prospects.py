"""Prospect sweep: golden economics, depth handling, and honest skip accounting.

Golden case, hand-computed (never copied from the implementation):

Full-range skins, Field-Tested band [0.15, 0.38): assumed input float is the band
midpoint 0.265, so z = 53/200. Inputs: 6 units at $1.00 + 4 at $1.20 = $10.80.
Pure two-output pool -> 1/2 each; output float 0.265 -> Field-Tested. Outputs at
$10.00 and $4.00 asks, less 8% sale fee and 12% ask haircut:
  10.00 -> 1000 - 80 - 120 = 800    4.00 -> 400 - 32 - 48 = 320
Value = 800/2 + 320/2 = 560 minor. EV = 560 - 1080 = -520. ROI = -520/1080.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from fractions import Fraction

import pytest
from tests.factories import make_registry

from tradeup.adapters.skinport import SkinportItemQuote
from tradeup.discovery.prospects import ProspectPolicy, sweep_prospects
from tradeup.domain.items import QualityType, Rarity, WearCondition
from tradeup.domain.money import BalanceType, Currency, Money
from tradeup.domain.rules import RULESET_2026_05
from tradeup.valuation.venue_fees import build_live_fee_schedule

#: After the live fee schedule's effective_from (2026-07-26); the sweep must be
#: able to resolve the skinport sale fee or it correctly fails closed.
SWEEP_NOW = datetime(2026, 7, 26, 12, 0, 0, tzinfo=UTC)


def usd(minor: int) -> Money:
    return Money(minor, Currency.USD, BalanceType.CASH_WITHDRAWABLE)


def quote(name: str, price_minor: int | None, quantity: int) -> SkinportItemQuote:
    return SkinportItemQuote(
        market_hash_name=name,
        min_price=usd(price_minor) if price_minor is not None else None,
        median_price=None,
        quantity=quantity,
    )


def quotes_fixture() -> dict[str, SkinportItemQuote]:
    return {
        "Weapon | a-in-1 (Field-Tested)": quote("Weapon | a-in-1 (Field-Tested)", 100, 6),
        "Weapon | a-in-2 (Field-Tested)": quote("Weapon | a-in-2 (Field-Tested)", 120, 10),
        "Weapon | a-out-1 (Field-Tested)": quote("Weapon | a-out-1 (Field-Tested)", 1_000, 3),
        "Weapon | a-out-2 (Field-Tested)": quote("Weapon | a-out-2 (Field-Tested)", 400, 5),
        # col-b: inputs quoted, outputs not -> the whole sketch must be dropped.
        "Weapon | b-in-1 (Field-Tested)": quote("Weapon | b-in-1 (Field-Tested)", 50, 10),
    }


def sweep(**overrides: object):  # type: ignore[no-untyped-def]
    base: dict[str, object] = {
        "registry": make_registry(),
        "ruleset": RULESET_2026_05,
        "quotes": quotes_fixture(),
        "fee_schedule": build_live_fee_schedule(),
        "base_currency": Currency.USD,
        "moment": SWEEP_NOW,
        "qualities": (QualityType.NORMAL,),
        "include_mixed": False,
    }
    base.update(overrides)
    return sweep_prospects(**base)  # type: ignore[arg-type]


class TestGoldenSweep:
    def test_the_priced_collection_yields_the_hand_computed_prospect(self) -> None:
        prospects, statistics = sweep()
        assert statistics.prospects == 1
        best = prospects[0]
        assert best.collection_id == "col-a"
        assert best.input_rarity is Rarity.MIL_SPEC
        assert best.input_wear is WearCondition.FIELD_TESTED
        assert best.average_normalized == Fraction(53, 200)
        assert best.estimated_cost == usd(1_080)
        assert best.estimated_output_value == usd(560)
        assert best.estimated_ev == usd(-520)
        assert best.estimated_roi == Decimal(-520) / Decimal(1080)
        assert best.outcome_count == 2
        assert best.unpriced_probability == Fraction(0)

    def test_the_greedy_fill_respects_depth_and_price_order(self) -> None:
        prospects, _ = sweep()
        plans = prospects[0].inputs
        assert [(p.skin_id, p.units, p.unit_price.minor_units) for p in plans] == [
            ("a-in-1", 6, 100),
            ("a-in-2", 4, 120),
        ]

    def test_the_census_accounts_for_every_combo(self) -> None:
        _, statistics = sweep()
        # 2 collections x 5 wears, NORMAL only.
        assert statistics.combos_considered == 10
        # col-a: 4 unquoted wears; col-b: 4 unquoted wears.
        assert statistics.skipped_insufficient_depth == 8
        # col-b Field-Tested: inputs quoted, outputs entirely unpriced.
        assert statistics.skipped_unpriced_outcomes == 1
        assert statistics.skipped_over_cost_cap == 0

    def test_the_cost_cap_is_applied_and_counted(self) -> None:
        prospects, statistics = sweep(max_cost=usd(500))
        assert prospects == ()
        assert statistics.skipped_over_cost_cap == 1

    def test_insufficient_depth_never_fabricates_a_bundle(self) -> None:
        quotes = quotes_fixture()
        quotes["Weapon | a-in-1 (Field-Tested)"] = quote("Weapon | a-in-1 (Field-Tested)", 100, 6)
        del quotes["Weapon | a-in-2 (Field-Tested)"]  # only 6 of 10 units available
        prospects, statistics = sweep(quotes=quotes)
        assert all(p.collection_id != "col-a" for p in prospects)
        assert statistics.skipped_insufficient_depth >= 1


class TestMixedSweep:
    """Hand-computed mix: 9 col-a units dilute 1 col-b filler unit.

    P(col-a pool) = 9/10 split over two outputs -> 9/20 each; P(col-b) = 1/10
    with an unpriced output (tolerated at exactly the 1/10 policy limit, valued
    zero). Cost = 6x1.00 + 3x1.20 + 1x0.50 = $10.10.
    Value = 9/20*800 + 9/20*320 = 504 minor. EV = 504 - 1010 = -506.
    """

    def test_the_mixed_split_is_weighted_exactly_as_the_game_weights_it(self) -> None:
        prospects, statistics = sweep(include_mixed=True)
        mixed = [p for p in prospects if p.is_mixed]
        assert len(mixed) == 1
        mix = mixed[0]
        assert mix.counts_by_collection == (("col-a", 9), ("col-b", 1))
        assert mix.collection_id == "col-a"  # primary side: most inputs
        assert mix.filler_collection_name == "col-b"
        assert mix.estimated_cost == usd(1_010)
        assert mix.estimated_output_value == usd(504)
        assert mix.estimated_ev == usd(-506)
        assert mix.outcome_count == 3
        assert mix.unpriced_probability == Fraction(1, 10)
        # Splits k_a=1..8 put >= 2/10 probability on the unpriced col-b pool and
        # are dropped as unpriced, never valued optimistically.
        assert statistics.mixed_considered == 9

    def test_mixing_does_not_change_the_pure_results(self) -> None:
        pure_only, _ = sweep()
        with_mixed, _ = sweep(include_mixed=True)
        pure_from_mixed = [p for p in with_mixed if not p.is_mixed]
        assert [p.estimated_ev for p in pure_from_mixed] == [p.estimated_ev for p in pure_only]
        assert all(p.counts_by_collection == ((p.collection_id, 10),) for p in pure_only)


class TestPolicyGuards:
    def test_policy_bounds_are_validated(self) -> None:
        with pytest.raises(ValueError, match="ask_haircut"):
            ProspectPolicy(ask_haircut=Decimal("1"))
        with pytest.raises(ValueError, match="wear_point"):
            ProspectPolicy(wear_point=Fraction(3, 2))
        with pytest.raises(ValueError, match="max_unpriced_probability"):
            ProspectPolicy(max_unpriced_probability=Fraction(2, 1))

    def test_partial_unpriced_mass_within_tolerance_contributes_zero(self) -> None:
        """A tolerated unpriced outcome counts as zero value, never as a guess."""
        quotes = quotes_fixture()
        del quotes["Weapon | a-out-2 (Field-Tested)"]
        prospects, _ = sweep(
            quotes=quotes,
            policy=ProspectPolicy(max_unpriced_probability=Fraction(1, 2)),
        )
        best = next(p for p in prospects if p.collection_id == "col-a")
        assert best.unpriced_probability == Fraction(1, 2)
        # Only out-1 contributes: 800/2 = 400 minor.
        assert best.estimated_output_value == usd(400)
