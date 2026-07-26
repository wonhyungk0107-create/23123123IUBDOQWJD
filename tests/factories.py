"""Builders for test fixtures.

Every builder produces a *valid* object by default and takes overrides, so a test
that cares about one field does not have to restate twenty. Defaults are chosen to
be economically boring — cheap inputs, plausible floats — so that when a test asserts
a rejection, the reason is the thing the test changed.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from fractions import Fraction

from tradeup.domain.contracts import (
    ContractComposition,
    TradeupCandidate,
    TradeupInput,
    TradeupOutcome,
)
from tradeup.domain.items import (
    Collection,
    FloatRange,
    QualityType,
    Rarity,
    Skin,
    WearCondition,
)
from tradeup.domain.listings import (
    ListingIdentity,
    ListingStatus,
    MarketplaceListing,
    TradableStatus,
)
from tradeup.domain.money import BalanceType, Currency, Money
from tradeup.domain.valuation import (
    OutputValuation,
    PriceObservation,
    ValuationConfidence,
    ValuationSource,
)
from tradeup.metadata.registry import MetadataRegistry

NOW = datetime(2026, 7, 25, 12, 0, 0, tzinfo=UTC)
FULL_RANGE = FloatRange(Decimal("0.00"), Decimal("1.00"))


def usd(minor: int, balance: BalanceType = BalanceType.CASH_WITHDRAWABLE) -> Money:
    return Money(minor, Currency.USD, balance)


def make_skin(
    skin_id: str,
    *,
    collection_id: str = "col-a",
    rarity: Rarity = Rarity.MIL_SPEC,
    float_range: FloatRange | None = None,
    qualities: frozenset[QualityType] | None = None,
) -> Skin:
    return Skin(
        skin_id=skin_id,
        name=f"Weapon | {skin_id}",
        market_hash_base=f"Weapon | {skin_id}",
        collection_id=collection_id,
        rarity=rarity,
        float_range=float_range or FULL_RANGE,
        paint_index=1,
        available_qualities=qualities or frozenset({QualityType.NORMAL}),
    )


def make_registry(
    *,
    collections: dict[str, dict[Rarity, list[str]]] | None = None,
    revision: str = "test-registry",
) -> MetadataRegistry:
    """Build a registry from ``{collection: {rarity: [skin_id, ...]}}``."""
    layout = collections or {
        "col-a": {Rarity.MIL_SPEC: ["a-in-1", "a-in-2"], Rarity.RESTRICTED: ["a-out-1", "a-out-2"]},
        "col-b": {Rarity.MIL_SPEC: ["b-in-1"], Rarity.RESTRICTED: ["b-out-1"]},
    }
    skins: list[Skin] = []
    collection_rows: list[Collection] = []
    for collection_id, by_rarity in layout.items():
        members: list[str] = []
        for rarity, skin_ids in by_rarity.items():
            for skin_id in skin_ids:
                skins.append(make_skin(skin_id, collection_id=collection_id, rarity=rarity))
                members.append(skin_id)
        collection_rows.append(
            Collection(
                collection_id=collection_id,
                name=collection_id,
                skin_ids=frozenset(members),
            )
        )
    return MetadataRegistry(
        source="tests",
        revision=revision,
        payload_sha256="0" * 64,
        imported_at=NOW,
        skins=skins,
        collections=collection_rows,
    )


def make_listing(
    listing_id: str,
    *,
    venue: str = "test-venue",
    skin_id: str = "a-in-1",
    collection_id: str = "col-a",
    rarity: Rarity = Rarity.MIL_SPEC,
    quality: QualityType = QualityType.NORMAL,
    raw_float: Decimal = Decimal("0.10"),
    float_range: FloatRange | None = None,
    price_minor: int = 100,
    buyer_fee_minor: int = 2,
    deposit_fee_minor: int = 0,
    observed_at: datetime | None = None,
    status: ListingStatus = ListingStatus.ACTIVE,
    tradable: TradableStatus = TradableStatus.TRADABLE,
    trade_lock_until: datetime | None = None,
    seller_reliability: Decimal | None = Decimal("0.99"),
    asset_id: str | None = None,
) -> MarketplaceListing:
    span = float_range or FULL_RANGE
    normalized = (raw_float - span.minimum) / (span.maximum - span.minimum)
    return MarketplaceListing(
        identity=ListingIdentity(venue, listing_id),
        asset_id=asset_id or f"asset-{listing_id}",
        skin_id=skin_id,
        market_hash_name=f"Weapon | {skin_id} (Field-Tested)",
        collection_id=collection_id,
        rarity=rarity,
        quality_type=quality,
        raw_float=raw_float,
        normalized_float=normalized,
        price=usd(price_minor),
        buyer_fee=usd(buyer_fee_minor),
        deposit_fee=usd(deposit_fee_minor),
        observed_at=observed_at or NOW,
        listing_status=status,
        tradable_status=tradable,
        raw_payload_hash=f"hash-{listing_id}",
        trade_lock_until=trade_lock_until,
        seller_reliability=seller_reliability,
    )


def make_valuation(
    skin_id: str = "a-out-1",
    *,
    net_minor: int = 2000,
    gross_minor: int = 2400,
    source: ValuationSource = ValuationSource.EXECUTABLE_CASH_BID,
    confidence: ValuationConfidence = ValuationConfidence.HIGH,
    evidence_count: int = 5,
    days_to_sale: int = 3,
    wear: WearCondition = WearCondition.FIELD_TESTED,
) -> OutputValuation:
    return OutputValuation(
        skin_id=skin_id,
        market_hash_name=f"Weapon | {skin_id} ({wear.market_suffix})",
        quality=QualityType.NORMAL,
        wear=wear,
        output_float=Decimal("0.20"),
        exit_venue="test-venue",
        source=source,
        observed_at=NOW,
        gross_reference=usd(gross_minor),
        seller_fee=usd(max(gross_minor - net_minor, 0)),
        withdrawal_fee=usd(0),
        fx_cost=usd(0),
        liquidity_haircut=usd(0),
        float_adjustment=usd(0),
        carry_cost=usd(0),
        net_proceeds=usd(net_minor),
        confidence=confidence,
        evidence_count=evidence_count,
        expected_days_to_sale=days_to_sale,
    )


def make_outcome(
    skin_id: str = "a-out-1",
    *,
    probability: Fraction = Fraction(1, 2),
    collection_id: str = "col-a",
    net_minor: int = 2000,
    valuation: OutputValuation | None = None,
) -> TradeupOutcome:
    return TradeupOutcome(
        collection_id=collection_id,
        skin_id=skin_id,
        probability=probability,
        output_float=Decimal("0.20"),
        wear=WearCondition.FIELD_TESTED,
        quality=QualityType.NORMAL,
        valuation=valuation or make_valuation(skin_id, net_minor=net_minor),
    )


def make_candidate(
    *,
    listings: list[MarketplaceListing] | None = None,
    outcomes: list[TradeupOutcome] | None = None,
    rule_version: str = "2026-05-souvenir-covert",
    created_at: datetime | None = None,
    ttl: timedelta = timedelta(minutes=10),
    input_rarity: Rarity = Rarity.MIL_SPEC,
) -> TradeupCandidate:
    """A valid 10-input, single-collection candidate by default."""
    created = created_at or NOW
    items = listings or [
        make_listing(f"L-{i:02d}", raw_float=Decimal("0.10") + Decimal(i) / 1000) for i in range(10)
    ]
    counts: dict[str, int] = {}
    for listing in items:
        counts[listing.collection_id] = counts.get(listing.collection_id, 0) + 1

    inputs = [
        TradeupInput(listing=listing, normalized=Fraction(listing.normalized_float))
        for listing in items
    ]
    resolved_outcomes = outcomes or [
        make_outcome("a-out-1", probability=Fraction(1, 2)),
        make_outcome("a-out-2", probability=Fraction(1, 2)),
    ]
    return TradeupCandidate(
        composition=ContractComposition(
            input_rarity=input_rarity,
            counts_by_collection=counts,
            rule_version=rule_version,
        ),
        inputs=tuple(inputs),
        outcomes=tuple(resolved_outcomes),
        rule_version=rule_version,
        output_quality=QualityType.NORMAL,
        average_normalized_float=Fraction(1, 10),
        created_at=created,
        expires_at=created + ttl,
    )


def make_observation(
    skin_id: str = "a-out-1",
    *,
    # Defaults to a venue the shared `fee_schedule` fixture actually prices, so a
    # test about valuation does not fail on a missing fee rule it never meant to
    # exercise.
    venue: str = "csfloat",
    gross_minor: int = 2400,
    source: ValuationSource = ValuationSource.EXECUTABLE_CASH_BID,
    wear: WearCondition = WearCondition.FIELD_TESTED,
    observed_at: datetime | None = None,
    evidence_count: int = 4,
    balance: BalanceType = BalanceType.CASH_WITHDRAWABLE,
    currency: Currency = Currency.USD,
) -> PriceObservation:
    return PriceObservation(
        skin_id=skin_id,
        quality=QualityType.NORMAL,
        wear=wear,
        venue=venue,
        gross=Money(gross_minor, currency, balance),
        source=source,
        observed_at=observed_at or NOW,
        evidence_count=evidence_count,
    )
