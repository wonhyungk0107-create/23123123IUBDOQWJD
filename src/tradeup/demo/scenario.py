"""Builds the deterministic demo scenario.

Everything here is derived, not hard-coded: collections are chosen from the pinned
registry by a stable rule (enough inputs, non-empty output pool, sorted by id), and
floats and prices come from fixed arithmetic sequences. No randomness, no wall clock.
Two runs produce byte-identical inputs, which is what makes acceptance gate G12
testable.

The scenario is constructed to exercise every rejection path the offline demo is
required to demonstrate:

* a **profitable** collection whose outputs are worth far more than its inputs
* a **negative-EV** collection whose outputs are nearly worthless
* a **stale** listing, observed hours ago, that breaches the quote-age gate
* a **disappearing** listing that revalidates as withdrawn
* a **price-changed** listing that revalidates at a different price
* **shared listings** across candidates, so the reservation conflict is real

Venue names are ``demo-market-a`` and ``demo-market-b`` rather than real venue names.
A synthetic fee schedule attached to ``csfloat`` would be indistinguishable from a
real one at a glance, and that is exactly the confusion this project cannot afford.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from tradeup.adapters.fixture import FixtureMarketAdapter, FixtureScenario
from tradeup.domain.conversion import ConversionQuote
from tradeup.domain.fees import FeeOperation, FeeRule, FeeSchedule
from tradeup.domain.items import QualityType, Rarity, Skin, WearCondition
from tradeup.domain.listings import (
    ListingIdentity,
    ListingStatus,
    MarketplaceListing,
    TradableStatus,
)
from tradeup.domain.money import BalanceType, Currency, Money
from tradeup.domain.valuation import PriceObservation, ValuationSource
from tradeup.metadata.registry import MetadataRegistry

__all__ = [
    "DEMO_CRYPTO_RAIL",
    "DEMO_FEE_SCHEDULE_ID",
    "DEMO_VENUE_A",
    "DEMO_VENUE_B",
    "DemoScenario",
    "build_demo_conversion_quote",
    "build_demo_scenario",
]

DEMO_VENUE_A = "demo-market-a"
DEMO_VENUE_B = "demo-market-b"
#: Synthetic crypto settlement rail: the fictional network the demo's BTC moves over.
DEMO_CRYPTO_RAIL = "demo-crypto-rail"
DEMO_FEE_SCHEDULE_ID = "demo-synthetic-v1"

_SYNTHETIC_SOURCE = (
    "SYNTHETIC demo fee -- invented for the offline demonstration. "
    "Not a real venue fee and must never be used for a live decision."
)


@dataclass(frozen=True)
class DemoScenario:
    """Everything the demo pipeline needs, plus a description of what it proves."""

    listings: tuple[MarketplaceListing, ...]
    price_observations: tuple[PriceObservation, ...]
    fee_schedule: FeeSchedule
    conversion_quote: ConversionQuote
    adapters: tuple[FixtureMarketAdapter, ...]
    collections: tuple[str, ...]
    disappearing: frozenset[str]
    price_changes: dict[str, Money]
    stale_listing_ids: tuple[str, ...]
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def listing_count(self) -> int:
        return len(self.listings)


def _fee_rule(
    rule_id: str, venue: str, operation: FeeOperation, percentage: str, fixed_minor: int = 0
) -> FeeRule:
    return FeeRule(
        rule_id=rule_id,
        venue=venue,
        operation=operation,
        balance_type=BalanceType.CASH_WITHDRAWABLE,
        currency=Currency.USD,
        percentage=Decimal(percentage),
        fixed_minor=fixed_minor,
        effective_from=datetime(2020, 1, 1, tzinfo=UTC),
        effective_until=None,
        source=_SYNTHETIC_SOURCE,
        last_verified=None,
    )


def build_demo_fee_schedule() -> FeeSchedule:
    """Synthetic, clearly-labelled fee schedule for the two demo venues."""
    rules: list[FeeRule] = []
    for venue, purchase, sale, withdrawal in (
        (DEMO_VENUE_A, "0.02", "0.05", "0.02"),
        (DEMO_VENUE_B, "0.03", "0.07", "0.03"),
    ):
        rules += [
            _fee_rule(f"{venue}:purchase", venue, FeeOperation.PURCHASE, purchase),
            _fee_rule(f"{venue}:deposit", venue, FeeOperation.DEPOSIT, "0.00"),
            _fee_rule(f"{venue}:sale", venue, FeeOperation.SALE, sale),
            _fee_rule(f"{venue}:withdrawal", venue, FeeOperation.WITHDRAWAL, withdrawal, 25),
            _fee_rule(f"{venue}:fx", venue, FeeOperation.FX_CONVERSION, "0.005"),
        ]
    # A flat 20,000-satoshi on-chain transfer fee for the synthetic BTC rail.
    rules.append(
        FeeRule(
            rule_id=f"{DEMO_CRYPTO_RAIL}:network",
            venue=DEMO_CRYPTO_RAIL,
            operation=FeeOperation.NETWORK_TRANSFER,
            balance_type=BalanceType.CASH_WITHDRAWABLE,
            currency=Currency.BTC,
            percentage=Decimal("0"),
            fixed_minor=20_000,
            effective_from=datetime(2020, 1, 1, tzinfo=UTC),
            effective_until=None,
            source=_SYNTHETIC_SOURCE,
            last_verified=None,
        )
    )
    return FeeSchedule(rules)


def build_demo_conversion_quote(now: datetime) -> ConversionQuote:
    """A fixed, clearly-synthetic BTC/USD quote, fresh relative to the demo instant."""
    return ConversionQuote(
        base=Currency.BTC,
        quote=Currency.USD,
        rate=Decimal("100000"),
        observed_at=now - timedelta(seconds=30),
        source=(
            "SYNTHETIC demo rate -- invented for the offline demonstration. "
            "Not a market observation and must never be used for a live decision."
        ),
        venue=DEMO_CRYPTO_RAIL,
    )


def _eligible_collections(registry: MetadataRegistry, *, minimum_inputs: int = 3) -> list[str]:
    """Collections usable at MIL_SPEC, in a stable order."""
    result = []
    for collection in sorted(registry.collections, key=lambda c: c.collection_id):
        inputs = registry.skins_at(collection.collection_id, Rarity.MIL_SPEC)
        pool = registry.output_pool(collection.collection_id, Rarity.MIL_SPEC)
        if len(inputs) >= minimum_inputs and not pool.is_empty:
            result.append(collection.collection_id)
    return result


def _float_at(skin: Skin, fraction: Decimal) -> Decimal:
    """A float ``fraction`` of the way through a skin's legal range."""
    span = skin.float_range.maximum - skin.float_range.minimum
    return (skin.float_range.minimum + span * fraction).quantize(Decimal("0.000000001"))


def _listing(
    *,
    venue: str,
    listing_id: str,
    skin: Skin,
    raw_float: Decimal,
    price_minor: int,
    buyer_fee_rate: Decimal,
    observed_at: datetime,
    trade_lock_until: datetime | None = None,
) -> MarketplaceListing:
    span = skin.float_range.maximum - skin.float_range.minimum
    normalized = ((raw_float - skin.float_range.minimum) / span).quantize(Decimal("0.000000001"))
    price = Money(price_minor, Currency.USD, BalanceType.CASH_WITHDRAWABLE)
    return MarketplaceListing(
        identity=ListingIdentity(venue, listing_id),
        asset_id=f"asset-{listing_id}",
        skin_id=skin.skin_id,
        market_hash_name=skin.market_hash_name(QualityType.NORMAL, WearCondition.FIELD_TESTED),
        collection_id=skin.collection_id,
        rarity=skin.rarity,
        quality_type=QualityType.NORMAL,
        raw_float=raw_float,
        normalized_float=normalized,
        price=price,
        buyer_fee=price.scaled_up(buyer_fee_rate),
        deposit_fee=Money.zero(Currency.USD, BalanceType.CASH_WITHDRAWABLE),
        observed_at=observed_at,
        listing_status=ListingStatus.ACTIVE,
        tradable_status=(
            TradableStatus.TRADE_LOCKED if trade_lock_until else TradableStatus.TRADABLE
        ),
        raw_payload_hash=f"demo-synthetic-{listing_id}",
        paint_index=skin.paint_index,
        trade_lock_until=trade_lock_until,
        seller_reliability=Decimal("0.98"),
    )


def _observations(
    skin: Skin, gross_minor: int, venue: str, observed_at: datetime, *, evidence: int = 4
) -> list[PriceObservation]:
    """Price evidence for every wear band this skin can actually reach.

    Covering all reachable wears means the valuation succeeds whatever float the
    contract lands on, without the scenario having to predict the optimizer's choice.
    """
    rows: list[PriceObservation] = []
    for wear in skin.float_range.reachable_wears:
        rows.append(
            PriceObservation(
                skin_id=skin.skin_id,
                quality=QualityType.NORMAL,
                wear=wear,
                venue=venue,
                gross=Money(gross_minor, Currency.USD, BalanceType.CASH_WITHDRAWABLE),
                source=ValuationSource.EXECUTABLE_CASH_BID,
                observed_at=observed_at,
                evidence_count=evidence,
                depth_units=5,
            )
        )
        rows.append(
            PriceObservation(
                skin_id=skin.skin_id,
                quality=QualityType.NORMAL,
                wear=wear,
                venue=venue,
                gross=Money(gross_minor + 150, Currency.USD, BalanceType.CASH_WITHDRAWABLE),
                source=ValuationSource.COMPLETED_SALE,
                observed_at=observed_at,
                evidence_count=evidence,
            )
        )
    return rows


def build_demo_scenario(
    registry: MetadataRegistry,
    *,
    now: datetime,
) -> DemoScenario:
    """Assemble the full deterministic scenario."""
    eligible = _eligible_collections(registry)
    if len(eligible) < 4:
        raise ValueError(f"demo needs at least 4 usable collections, registry has {len(eligible)}")
    profitable_id, worthless_id, unreliable_id, stale_id = eligible[:4]

    listings: list[MarketplaceListing] = []
    observations: list[PriceObservation] = []
    disappearing: set[str] = set()
    price_changes: dict[str, Money] = {}
    stale_ids: list[str] = []
    counter = 0

    # Each collection isolates one failure mode. Mixed into a single pool, the
    # cheapest-bundle solver would bury one mode behind an unrelated rejection, and
    # the demo is required to exhibit all of them.

    # -- A: cheap inputs, valuable outputs, all fresh. Should clear every gate.
    inputs_a = registry.skins_at(profitable_id, Rarity.MIL_SPEC)
    for index in range(14):
        skin = inputs_a[index % len(inputs_a)]
        counter += 1
        listing_id = f"A-{counter:03d}"
        listings.append(
            _listing(
                venue=DEMO_VENUE_A,
                listing_id=listing_id,
                skin=skin,
                raw_float=_float_at(skin, Decimal("0.01") + Decimal(index) * Decimal("0.004")),
                price_minor=95 + index * 4,
                buyer_fee_rate=Decimal("0.02"),
                observed_at=now - timedelta(seconds=30),
            )
        )
    for skin_id in registry.output_pool(profitable_id, Rarity.MIL_SPEC).output_skin_ids:
        observations.extend(
            _observations(registry.skin(skin_id), 2100, DEMO_VENUE_A, now - timedelta(minutes=2))
        )

    # -- B: near-worthless outputs. Fails the ROI gate at discovery.
    inputs_b = registry.skins_at(worthless_id, Rarity.MIL_SPEC)
    for index in range(12):
        skin = inputs_b[index % len(inputs_b)]
        counter += 1
        listing_id = f"B-{counter:03d}"
        listings.append(
            _listing(
                venue=DEMO_VENUE_B,
                listing_id=listing_id,
                skin=skin,
                raw_float=_float_at(skin, Decimal("0.02") + Decimal(index) * Decimal("0.003")),
                price_minor=110 + index * 3,
                buyer_fee_rate=Decimal("0.03"),
                observed_at=now - timedelta(seconds=45),
            )
        )
    for skin_id in registry.output_pool(worthless_id, Rarity.MIL_SPEC).output_skin_ids:
        observations.extend(
            _observations(registry.skin(skin_id), 180, DEMO_VENUE_B, now - timedelta(minutes=3))
        )

    # -- C: economics good enough to clear discovery, so the bundle actually reaches
    # revalidation -- where one listing has vanished and another has repriced. The two
    # doomed listings are the cheapest in the collection, so the solver is certain to
    # select them and the gate is certain to fire.
    inputs_c = registry.skins_at(unreliable_id, Rarity.MIL_SPEC)
    for index in range(12):
        skin = inputs_c[index % len(inputs_c)]
        counter += 1
        listing_id = f"C-{counter:03d}"
        doomed = index < 2
        listings.append(
            _listing(
                venue=DEMO_VENUE_B,
                listing_id=listing_id,
                skin=skin,
                raw_float=_float_at(skin, Decimal("0.015") + Decimal(index) * Decimal("0.005")),
                price_minor=(60 if doomed else 95) + index * 2,
                buyer_fee_rate=Decimal("0.03"),
                observed_at=now - timedelta(seconds=60),
                trade_lock_until=(now + timedelta(days=7)) if index == 4 else None,
            )
        )
        if index == 0:
            disappearing.add(listing_id)
        if index == 1:
            price_changes[listing_id] = Money(999, Currency.USD, BalanceType.CASH_WITHDRAWABLE)
    for skin_id in registry.output_pool(unreliable_id, Rarity.MIL_SPEC).output_skin_ids:
        observations.extend(
            _observations(registry.skin(skin_id), 1900, DEMO_VENUE_B, now - timedelta(minutes=4))
        )

    # -- D: profitable on paper, but every quote is hours old. Exists so the
    # quote-age gate is demonstrated rejecting on staleness rather than economics.
    inputs_d = registry.skins_at(stale_id, Rarity.MIL_SPEC)
    for index in range(12):
        skin = inputs_d[index % len(inputs_d)]
        counter += 1
        listing_id = f"D-{counter:03d}"
        stale_ids.append(listing_id)
        listings.append(
            _listing(
                venue=DEMO_VENUE_B,
                listing_id=listing_id,
                skin=skin,
                raw_float=_float_at(skin, Decimal("0.012") + Decimal(index) * Decimal("0.004")),
                price_minor=85 + index * 3,
                buyer_fee_rate=Decimal("0.03"),
                observed_at=now - timedelta(hours=3),
            )
        )
    for skin_id in registry.output_pool(stale_id, Rarity.MIL_SPEC).output_skin_ids:
        observations.extend(
            _observations(registry.skin(skin_id), 1900, DEMO_VENUE_B, now - timedelta(minutes=5))
        )

    scenario_a = FixtureScenario(
        listings=tuple(item for item in listings if item.identity.venue == DEMO_VENUE_A),
        price_observations=tuple(observations),
        balances={
            BalanceType.CASH_WITHDRAWABLE: Money(
                50_000, Currency.USD, BalanceType.CASH_WITHDRAWABLE
            )
        },
        disappearing=frozenset(),
        price_changes={},
    )
    scenario_b = FixtureScenario(
        listings=tuple(item for item in listings if item.identity.venue == DEMO_VENUE_B),
        price_observations=tuple(observations),
        balances={
            BalanceType.CASH_WITHDRAWABLE: Money(
                50_000, Currency.USD, BalanceType.CASH_WITHDRAWABLE
            )
        },
        disappearing=frozenset(disappearing),
        price_changes=dict(price_changes),
    )

    return DemoScenario(
        listings=tuple(listings),
        price_observations=tuple(observations),
        fee_schedule=build_demo_fee_schedule(),
        conversion_quote=build_demo_conversion_quote(now),
        adapters=(
            FixtureMarketAdapter(DEMO_VENUE_A, scenario_a),
            FixtureMarketAdapter(DEMO_VENUE_B, scenario_b),
        ),
        collections=(profitable_id, worthless_id, unreliable_id, stale_id),
        disappearing=frozenset(disappearing),
        price_changes=dict(price_changes),
        stale_listing_ids=tuple(stale_ids),
        notes=(
            "SYNTHETIC listings and prices over REAL pinned metadata.",
            f"Profitable collection: {profitable_id} (outputs priced far above inputs).",
            f"Negative-EV collection: {worthless_id} (outputs near-worthless).",
            f"Unreliable collection: {unreliable_id} (one vanishing listing and one "
            "that changes price on revalidation).",
            f"Stale collection: {stale_id} (economics fine, every quote 3 hours old).",
            "Crypto settlement rail is synthetic: a fixed BTC/USD rate and invented "
            "deposit/withdrawal/network fees, so the settlement charge is exercised "
            "deterministically.",
            "Nothing here is evidence about the real market.",
        ),
    )


def scenario_summary(scenario: DemoScenario) -> dict[str, str]:
    return {
        "listings": str(scenario.listing_count),
        "price_observations": str(len(scenario.price_observations)),
        "venues": f"{DEMO_VENUE_A},{DEMO_VENUE_B}",
        "collections": ",".join(scenario.collections),
        "disappearing_listings": ",".join(sorted(scenario.disappearing)) or "none",
        "price_changed_listings": ",".join(sorted(scenario.price_changes)) or "none",
        "stale_listings": ",".join(scenario.stale_listing_ids) or "none",
        "fee_schedule": DEMO_FEE_SCHEDULE_ID,
        "conversion_quote": (
            f"{scenario.conversion_quote.pair}@{scenario.conversion_quote.rate} "
            f"via {scenario.conversion_quote.venue} (SYNTHETIC)"
        ),
        "data_nature": "SYNTHETIC listings/prices over REAL pinned metadata",
    }


def all_price_observations(scenario: DemoScenario) -> Sequence[PriceObservation]:
    return scenario.price_observations
