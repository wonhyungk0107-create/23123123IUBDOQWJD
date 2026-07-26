"""Live-scan orchestration: name budgeting, prefetch wiring, and hard guards.

The network-facing body of ``run_live_scan`` is exercised by the opt-in live
suite; these tests cover everything that can be proven offline.
"""

from __future__ import annotations

import asyncio

import pytest
from tests.factories import NOW, make_listing, make_registry

from tradeup.adapters.base import ListingQuery, ListingVerification
from tradeup.adapters.csfloat import CSFloatAdapter
from tradeup.adapters.http import FixtureTransport, RestClient
from tradeup.config import Settings
from tradeup.domain.execution import CapabilityResult
from tradeup.domain.items import QualityType, Rarity
from tradeup.domain.listings import ListingIdentity, ListingStatus
from tradeup.pipeline import live_scan
from tradeup.pipeline.live_scan import (
    LiveScanError,
    _PrefetchedListingsAdapter,
    _wanted_output_names,
    run_live_scan,
)


class TestWantedOutputNames:
    def test_composes_output_names_from_the_registry(self) -> None:
        registry = make_registry()
        listings = [make_listing(f"A-{i}") for i in range(3)] + [
            make_listing("B-1", skin_id="b-in-1", collection_id="col-b")
        ]
        wanted, priced, skipped = _wanted_output_names(
            registry, listings, Rarity.MIL_SPEC, QualityType.NORMAL
        )
        assert priced == ("col-a", "col-b")
        assert skipped == ()
        # col-a has two RESTRICTED outputs, col-b one; full range reaches all 5 wears.
        assert len(wanted) == 15
        skin_ids = {identity[0] for identity in wanted.values()}
        assert skin_ids == {"a-out-1", "a-out-2", "b-out-1"}
        for _name, (_skin, quality, _wear) in wanted.items():
            assert quality is QualityType.NORMAL

    def test_liquidity_orders_collections_and_budget_skips_are_reported(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(live_scan, "_MAX_WANTED_NAMES", 10)
        registry = make_registry()
        listings = [make_listing(f"A-{i}") for i in range(3)] + [
            make_listing("B-1", skin_id="b-in-1", collection_id="col-b")
        ]
        wanted, priced, skipped = _wanted_output_names(
            registry, listings, Rarity.MIL_SPEC, QualityType.NORMAL
        )
        # col-a (3 live listings) fills the budget; col-b is skipped and SAID so.
        assert priced == ("col-a",)
        assert skipped == ("col-b",)
        assert len(wanted) == 10


class _RecordingLiveAdapter(CSFloatAdapter):
    def __init__(self) -> None:
        super().__init__(
            RestClient(transport=FixtureTransport({}), user_agent="test"),
            api_key="test-key",
        )
        self.verified: list[ListingIdentity] = []

    async def verify_listing(
        self, identity: ListingIdentity, *, moment: object
    ) -> CapabilityResult[ListingVerification]:
        self.verified.append(identity)
        return CapabilityResult.succeeded(
            self.venue,
            "verify_listing",
            NOW,
            ListingVerification(identity=identity, status=ListingStatus.ACTIVE, verified_at=NOW),
        )


class TestPrefetchedAdapter:
    def test_listings_come_from_the_prefetch_and_verification_goes_live(self) -> None:
        live = _RecordingLiveAdapter()
        listing = make_listing("L-1", venue="csfloat")
        prefetched = CapabilityResult.succeeded("csfloat", "fetch_listings", NOW, (listing,))
        wrapper = _PrefetchedListingsAdapter(live, prefetched)

        fetched = asyncio.run(wrapper.fetch_listings(ListingQuery(), moment=NOW))
        assert fetched is prefetched

        identity = ListingIdentity("csfloat", "L-1")
        verification = asyncio.run(wrapper.verify_listing(identity, moment=NOW))
        assert verification.ok
        assert live.verified == [identity]

    def test_wrapper_mirrors_the_live_adapter_capability(self) -> None:
        live = _RecordingLiveAdapter()
        wrapper = _PrefetchedListingsAdapter(
            live, CapabilityResult.succeeded("csfloat", "fetch_listings", NOW, ())
        )
        assert wrapper.venue == "csfloat"
        assert wrapper.execution_mode is live.execution_mode


class TestGuards:
    def test_missing_csfloat_key_is_a_stated_blocker(self) -> None:
        settings = Settings(  # type: ignore[call-arg]
            database_url="sqlite+pysqlite:///:memory:", csfloat_api_key=None
        )
        with pytest.raises(LiveScanError, match="TRADEUP_CSFLOAT_API_KEY"):
            run_live_scan(settings=settings, now=NOW)

    def test_blank_csfloat_key_is_a_stated_blocker(self) -> None:
        settings = Settings(  # type: ignore[call-arg]
            database_url="sqlite+pysqlite:///:memory:", csfloat_api_key=""
        )
        with pytest.raises(LiveScanError, match="TRADEUP_CSFLOAT_API_KEY"):
            run_live_scan(settings=settings, now=NOW)

    def test_crypto_settlement_without_a_quote_source_is_refused(self) -> None:
        from tradeup.domain.money import Currency

        settings = Settings(  # type: ignore[call-arg]
            database_url="sqlite+pysqlite:///:memory:",
            csfloat_api_key="k",
            crypto_settlement_enabled=True,
            settlement_currency=Currency.BTC,
        )
        with pytest.raises(LiveScanError, match="conversion-quote"):
            run_live_scan(settings=settings, now=NOW)
