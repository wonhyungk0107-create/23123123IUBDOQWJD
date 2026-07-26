"""Fixture-backed market adapter.

Serves listings, price references and revalidation results from an in-memory
scenario. This is what the offline demo and the integration tests run against, and
it is why no mandatory test needs the internet.

It is a real adapter, not a mock: it implements the same protocol, returns the same
:class:`~tradeup.domain.execution.CapabilityResult` types, and refuses execution
exactly as a live adapter would. The one thing it adds is a *scripted revalidation*
-- the ability to say "this listing will be gone when you check it, and that one will
have changed price" -- which is how the disappearing-listing and price-change gates
get exercised deterministically.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime

from tradeup.adapters.base import AccountState, ListingQuery, ListingVerification, MarketAdapter
from tradeup.domain.execution import CapabilityResult, CapabilityStatus, ExecutionMode
from tradeup.domain.items import QualityType, WearCondition
from tradeup.domain.listings import ListingIdentity, ListingStatus, MarketplaceListing
from tradeup.domain.money import BalanceType, Money
from tradeup.domain.valuation import PriceObservation

__all__ = ["FixtureMarketAdapter", "FixtureScenario"]


@dataclass
class FixtureScenario:
    """A deterministic market state, including how revalidation will behave."""

    listings: Sequence[MarketplaceListing] = field(default_factory=tuple)
    price_observations: Sequence[PriceObservation] = field(default_factory=tuple)
    balances: Mapping[BalanceType, Money] = field(default_factory=dict)
    #: Listings that will report as gone when revalidated.
    disappearing: frozenset[str] = frozenset()
    #: Listings whose price will differ on revalidation.
    price_changes: Mapping[str, Money] = field(default_factory=dict)
    #: Listings whose float will differ on revalidation (a metadata mismatch).
    float_changes: Mapping[str, object] = field(default_factory=dict)
    #: Listings whose asset id will differ on revalidation (an identity mismatch).
    identity_changes: Mapping[str, str] = field(default_factory=dict)

    def listing(self, listing_id: str) -> MarketplaceListing | None:
        for item in self.listings:
            if item.identity.listing_id == listing_id:
                return item
        return None


class FixtureMarketAdapter(MarketAdapter):
    """Read-only adapter over a :class:`FixtureScenario`."""

    execution_mode = ExecutionMode.OPERATOR_APPROVAL_REQUIRED
    capability_note = (
        "Offline fixture. Serves a scripted market state for the deterministic demo "
        "and integration tests. Never contacts a network."
    )

    def __init__(
        self,
        venue: str,
        scenario: FixtureScenario,
        *,
        live_execution_enabled: bool = False,
    ) -> None:
        super().__init__(live_execution_enabled=live_execution_enabled)
        self.venue = venue
        self._scenario = scenario

    @property
    def scenario(self) -> FixtureScenario:
        return self._scenario

    # -- reads ---------------------------------------------------------------

    async def fetch_listings(
        self, query: ListingQuery, *, moment: datetime
    ) -> CapabilityResult[Sequence[MarketplaceListing]]:
        results = [
            listing
            for listing in self._scenario.listings
            if listing.identity.venue == self.venue and self._matches(listing, query)
        ]
        ordered = sorted(results, key=lambda listing: listing.identity)[: query.limit]
        return CapabilityResult.succeeded(self.venue, "fetch_listings", moment, tuple(ordered))

    @staticmethod
    def _matches(listing: MarketplaceListing, query: ListingQuery) -> bool:
        if query.collection_id is not None and listing.collection_id != query.collection_id:
            return False
        if query.rarity is not None and listing.rarity is not query.rarity:
            return False
        if query.quality is not None and listing.quality_type is not query.quality:
            return False
        if (
            query.market_hash_name is not None
            and listing.market_hash_name != query.market_hash_name
        ):
            return False
        if query.max_float is not None and listing.raw_float > query.max_float:
            return False
        if query.min_float is not None and listing.raw_float < query.min_float:
            return False
        return not (query.max_price is not None and listing.price > query.max_price)

    async def fetch_listing_by_id(
        self, listing_id: str, *, moment: datetime
    ) -> CapabilityResult[MarketplaceListing]:
        listing = self._scenario.listing(listing_id)
        if listing is None:
            return self._refuse(
                "fetch_listing_by_id",
                moment,
                CapabilityStatus.TEMPORARILY_UNAVAILABLE,
                f"listing {listing_id} is not present in the fixture scenario",
            )
        return CapabilityResult.succeeded(self.venue, "fetch_listing_by_id", moment, listing)

    async def verify_listing(
        self, identity: ListingIdentity, *, moment: datetime
    ) -> CapabilityResult[ListingVerification]:
        """Re-check one listing, honouring the scenario's scripted changes."""
        listing_id = identity.listing_id
        listing = self._scenario.listing(listing_id)

        if listing is None or listing_id in self._scenario.disappearing:
            return CapabilityResult.succeeded(
                self.venue,
                "verify_listing",
                moment,
                ListingVerification(
                    identity=identity,
                    status=ListingStatus.WITHDRAWN,
                    verified_at=moment,
                    detail="listing is no longer available",
                ),
            )

        price = self._scenario.price_changes.get(listing_id, listing.price)
        raw_float = self._scenario.float_changes.get(listing_id, listing.raw_float)
        asset_id = self._scenario.identity_changes.get(listing_id, listing.asset_id)
        status = (
            ListingStatus.PRICE_CHANGED
            if listing_id in self._scenario.price_changes
            else ListingStatus.ACTIVE
        )
        return CapabilityResult.succeeded(
            self.venue,
            "verify_listing",
            moment,
            ListingVerification(
                identity=identity,
                status=status,
                verified_at=moment,
                price=price,
                raw_float=raw_float,  # type: ignore[arg-type]
                asset_id=asset_id,
                detail="revalidated against fixture scenario",
            ),
        )

    async def fetch_price_reference(
        self,
        market_hash_name: str,
        *,
        skin_id: str,
        quality: QualityType,
        wear: WearCondition,
        moment: datetime,
    ) -> CapabilityResult[Sequence[PriceObservation]]:
        matches = [
            obs
            for obs in self._scenario.price_observations
            if obs.skin_id == skin_id and obs.quality is quality and obs.wear is wear
        ]
        return CapabilityResult.succeeded(
            self.venue, "fetch_price_reference", moment, tuple(matches)
        )

    async def fetch_account_state(self, *, moment: datetime) -> CapabilityResult[AccountState]:
        return CapabilityResult.succeeded(
            self.venue,
            "fetch_account_state",
            moment,
            AccountState(
                venue=self.venue, balances=dict(self._scenario.balances), fetched_at=moment
            ),
        )
