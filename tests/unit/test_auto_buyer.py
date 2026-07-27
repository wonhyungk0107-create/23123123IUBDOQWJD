"""The auto-buy gate ladder, proven offline against a synthetic venue.

The stub venue documents purchase the way a real AUTOMATED adapter would --
including honouring the base class's policy-refusal ladder -- so these tests
exercise the engine's behaviour, not a bypass of it. No test touches the
network; every listing, price and venue here is a synthetic test constant.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path

import pytest
from tests.factories import make_candidate, make_listing, usd

from tradeup.adapters.base import ListingVerification, MarketAdapter
from tradeup.config import Settings
from tradeup.domain.execution import (
    CapabilityResult,
    CapabilityStatus,
    ExecutionMode,
    ExecutionResult,
    ExecutionStatus,
    PurchaseIntent,
    ReservationState,
)
from tradeup.domain.listings import ListingIdentity, ListingStatus, MarketplaceListing
from tradeup.execution.reservations import ReservationRegistry
from tradeup.purchasing import AutoBuyer

VENUE = "fixturemart"


class _Venue(MarketAdapter):
    """Synthetic venue with a documented, policy-gated purchase path."""

    venue = VENUE
    execution_mode = ExecutionMode.AUTOMATED
    capability_note = "synthetic test venue; purchase exists for tests only"

    def __init__(
        self,
        listings: list[MarketplaceListing],
        *,
        live: bool,
        gone: frozenset[str] = frozenset(),
        fail_at: int | None = None,
    ) -> None:
        super().__init__(live_execution_enabled=live)
        self._listings = {listing.identity: listing for listing in listings}
        self._gone = gone
        self._fail_at = fail_at
        self.verify_calls = 0
        self.purchase_calls = 0

    async def verify_listing(
        self, identity: ListingIdentity, *, moment: datetime
    ) -> CapabilityResult[ListingVerification]:
        self.verify_calls += 1
        listing = self._listings[identity]
        status = ListingStatus.SOLD if identity.listing_id in self._gone else ListingStatus.ACTIVE
        return CapabilityResult.succeeded(
            self.venue,
            "verify_listing",
            moment,
            ListingVerification(
                identity=identity, status=status, verified_at=moment, price=listing.price
            ),
        )

    async def create_purchase_intent(
        self, intent: PurchaseIntent, *, moment: datetime
    ) -> CapabilityResult[PurchaseIntent]:
        if self.execution_mode is not ExecutionMode.AUTOMATED or not self._live_execution_enabled:
            return self._execution_refusal("create_purchase_intent", moment)
        self.purchase_calls += 1
        if self._fail_at is not None and intent.sequence_position == self._fail_at:
            return CapabilityResult.refused(
                CapabilityStatus.TEMPORARILY_UNAVAILABLE,
                self.venue,
                "create_purchase_intent",
                moment,
                "synthetic venue outage",
            )
        return CapabilityResult.succeeded(self.venue, "create_purchase_intent", moment, intent)

    async def reconcile_execution(
        self, intent_id: str, *, moment: datetime
    ) -> CapabilityResult[ExecutionResult]:
        return CapabilityResult.succeeded(
            self.venue,
            "reconcile_execution",
            moment,
            ExecutionResult(
                intent_id=intent_id,
                status=ExecutionStatus.SUCCEEDED,
                recorded_at=moment,
                actual_price=usd(100),
                venue_reference=f"order-{intent_id}",
            ),
        )


def _listings() -> list[MarketplaceListing]:
    return [make_listing(f"L-{i:02d}", venue=VENUE) for i in range(10)]


def _live_settings(tmp_path: Path) -> Settings:
    return Settings(
        database_url=f"sqlite+pysqlite:///{(tmp_path / 'buy.db').as_posix()}",
        artifacts_dir=tmp_path / "artifacts",
        live_execution_enabled=True,
        max_daily_spend_minor=100_000,
    )


def _run(
    buyer: AutoBuyer, listings: list[MarketplaceListing], frozen_now: datetime
) -> tuple[ExecutionResult, ...]:
    candidate = make_candidate(listings=listings, created_at=frozen_now)
    return asyncio.run(buyer.execute_candidate(candidate, moment=frozen_now))


def test_disabled_switch_blocks_before_any_venue_contact(
    settings_obj: Settings, frozen_now: datetime
) -> None:
    listings = _listings()
    venue = _Venue(listings, live=True)
    buyer = AutoBuyer(
        settings=settings_obj,
        adapters={VENUE: venue},
        reservations=ReservationRegistry(),
        spent_today=usd(0),
    )
    results = _run(buyer, listings, frozen_now)
    assert all(r.status is ExecutionStatus.BLOCKED_BY_POLICY for r in results)
    assert venue.verify_calls == 0
    assert venue.purchase_calls == 0


def test_missing_spend_accounting_blocks(tmp_path: Path, frozen_now: datetime) -> None:
    listings = _listings()
    venue = _Venue(listings, live=True)
    buyer = AutoBuyer(
        settings=_live_settings(tmp_path),
        adapters={VENUE: venue},
        reservations=ReservationRegistry(),
        spent_today=None,
    )
    results = _run(buyer, listings, frozen_now)
    assert all(r.status is ExecutionStatus.BLOCKED_BY_POLICY for r in results)
    assert "daily-spend" in results[0].detail
    assert venue.purchase_calls == 0


def test_budget_ceiling_blocks(tmp_path: Path, frozen_now: datetime) -> None:
    # Bundle ceiling is 10 x 100 = 1000 minor; 99_500 already spent busts 100_000.
    listings = _listings()
    venue = _Venue(listings, live=True)
    buyer = AutoBuyer(
        settings=_live_settings(tmp_path),
        adapters={VENUE: venue},
        reservations=ReservationRegistry(),
        spent_today=usd(99_500),
    )
    results = _run(buyer, listings, frozen_now)
    assert all(r.status is ExecutionStatus.BLOCKED_BY_POLICY for r in results)
    assert venue.purchase_calls == 0


def test_missing_adapter_blocks(tmp_path: Path, frozen_now: datetime) -> None:
    listings = _listings()
    buyer = AutoBuyer(
        settings=_live_settings(tmp_path),
        adapters={},
        reservations=ReservationRegistry(),
        spent_today=usd(0),
    )
    results = _run(buyer, listings, frozen_now)
    assert all(r.status is ExecutionStatus.BLOCKED_BY_POLICY for r in results)
    assert "no adapter configured" in results[0].detail


def test_reservation_conflict_aborts(tmp_path: Path, frozen_now: datetime) -> None:
    listings = _listings()
    venue = _Venue(listings, live=True)
    registry = ReservationRegistry()
    registry.claim_bundle("TU-other", [listings[3].identity], now=frozen_now)
    buyer = AutoBuyer(
        settings=_live_settings(tmp_path),
        adapters={VENUE: venue},
        reservations=registry,
        spent_today=usd(0),
    )
    results = _run(buyer, listings, frozen_now)
    assert all(r.status is ExecutionStatus.NOT_ATTEMPTED for r in results)
    assert venue.purchase_calls == 0


def test_happy_path_buys_whole_bundle(tmp_path: Path, frozen_now: datetime) -> None:
    listings = _listings()
    venue = _Venue(listings, live=True)
    registry = ReservationRegistry()
    buyer = AutoBuyer(
        settings=_live_settings(tmp_path),
        adapters={VENUE: venue},
        reservations=registry,
        spent_today=usd(0),
    )
    results = _run(buyer, listings, frozen_now)
    assert [r.status for r in results] == [ExecutionStatus.SUCCEEDED] * 10
    assert all(r.actual_price == usd(100) for r in results)
    assert venue.purchase_calls == 10
    assert all(
        registry.state_of(listing.identity) is ReservationState.PURCHASED for listing in listings
    )


def test_gone_listing_aborts_before_any_purchase(tmp_path: Path, frozen_now: datetime) -> None:
    listings = _listings()
    venue = _Venue(listings, live=True, gone=frozenset({"L-03"}))
    registry = ReservationRegistry()
    buyer = AutoBuyer(
        settings=_live_settings(tmp_path),
        adapters={VENUE: venue},
        reservations=registry,
        spent_today=usd(0),
    )
    results = _run(buyer, listings, frozen_now)
    assert results[3].status is ExecutionStatus.LISTING_GONE
    assert all(r.status is ExecutionStatus.NOT_ATTEMPTED for i, r in enumerate(results) if i != 3)
    assert venue.purchase_calls == 0
    assert all(
        registry.state_of(listing.identity) is ReservationState.RELEASED for listing in listings
    )


def test_mid_bundle_failure_keeps_orphans_and_stops(tmp_path: Path, frozen_now: datetime) -> None:
    listings = _listings()
    venue = _Venue(listings, live=True, fail_at=1)
    registry = ReservationRegistry()
    buyer = AutoBuyer(
        settings=_live_settings(tmp_path),
        adapters={VENUE: venue},
        reservations=registry,
        spent_today=usd(0),
    )
    results = _run(buyer, listings, frozen_now)
    assert results[0].status is ExecutionStatus.SUCCEEDED
    assert results[1].status is ExecutionStatus.FAILED
    assert all(r.status is ExecutionStatus.NOT_ATTEMPTED for r in results[2:])
    # The bought input is orphan inventory -- exactly what the partial-fill
    # reserve prices -- and stays PURCHASED; everything else is released.
    assert registry.state_of(listings[0].identity) is ReservationState.PURCHASED
    assert all(
        registry.state_of(listing.identity) is ReservationState.RELEASED for listing in listings[1:]
    )


def test_settings_refuse_budget_without_switch(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        Settings(
            database_url=f"sqlite+pysqlite:///{(tmp_path / 'x.db').as_posix()}",
            artifacts_dir=tmp_path / "artifacts",
            live_execution_enabled=False,
            max_daily_spend_minor=1,
        )
