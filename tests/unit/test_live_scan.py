"""Live-scan orchestration: name budgeting, prefetch wiring, and hard guards.

The network-facing body of ``run_live_scan`` is exercised by the opt-in live
suite; these tests cover everything that can be proven offline.
"""

from __future__ import annotations

import asyncio
from datetime import datetime

import pytest
from tests.factories import NOW, make_listing, make_registry

from tradeup.adapters.base import ListingQuery, ListingVerification
from tradeup.adapters.csfloat import CSFloatAdapter
from tradeup.adapters.http import FixtureTransport, RestClient
from tradeup.adapters.skinport import SkinportItemQuote
from tradeup.config import Settings
from tradeup.domain.execution import CapabilityResult
from tradeup.domain.items import QualityType, Rarity
from tradeup.domain.listings import ListingIdentity, ListingStatus
from tradeup.domain.money import BalanceType, Currency, Money
from tradeup.domain.valuation import AcquisitionReference
from tradeup.pipeline import live_scan
from tradeup.pipeline.live_scan import (
    LiveScanError,
    _acquisition_references,
    _PrefetchedListingsAdapter,
    _wanted_output_names,
    run_live_scan,
)
from tradeup.pipeline.scan import ScanReport, ScanStatistics
from tradeup.reporting.renderers import render_scan_markdown


def _usd(minor: int) -> Money:
    return Money(minor, Currency.USD, BalanceType.CASH_WITHDRAWABLE)


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


class TestAcquisitionReferences:
    def test_quoted_names_map_and_missing_names_get_zero_quantity_rows(self) -> None:
        quotes = {
            "Name A": SkinportItemQuote(
                market_hash_name="Name A",
                min_price=_usd(19),
                median_price=_usd(24),
                quantity=23,
            )
        }
        references = _acquisition_references(
            ["Name A", "Name B", "Name A"], quotes, venue="skinport", moment=NOW
        )
        # Duplicates collapse, order is preserved, and the unquoted name is still
        # a row: "the venue has none" is evidence, not an omission.
        assert [r.market_hash_name for r in references] == ["Name A", "Name B"]
        quoted, unquoted = references
        assert quoted.min_ask == _usd(19)
        assert quoted.median_ask == _usd(24)
        assert quoted.quantity == 23
        assert unquoted.min_ask is None
        assert unquoted.median_ask is None
        assert unquoted.quantity == 0
        assert all(r.venue == "skinport" for r in references)
        assert all(r.observed_at == NOW for r in references)

    def test_reference_rejects_naive_timestamps_and_negative_figures(self) -> None:
        with pytest.raises(ValueError, match="timezone-aware"):
            AcquisitionReference(
                market_hash_name="N",
                venue="skinport",
                min_ask=None,
                median_ask=None,
                quantity=0,
                observed_at=datetime(2026, 7, 26, 12, 0, 0),
            )
        with pytest.raises(ValueError, match="quantity"):
            AcquisitionReference(
                market_hash_name="N",
                venue="skinport",
                min_ask=None,
                median_ask=None,
                quantity=-1,
                observed_at=NOW,
            )
        with pytest.raises(ValueError, match="min_ask"):
            AcquisitionReference(
                market_hash_name="N",
                venue="skinport",
                min_ask=_usd(-1),
                median_ask=None,
                quantity=0,
                observed_at=NOW,
            )


class TestMarkdownAcquisitionSection:
    def _report(self) -> ScanReport:
        return ScanReport(
            scanned_at=NOW,
            rule_version="test-rules",
            approved=(),
            rejected=(),
            statistics=ScanStatistics(
                listings_ingested=0,
                listings_usable=0,
                listings_dropped={},
                compositions_enumerated=0,
                bundles_solved=0,
                candidates_built=0,
                discovery_passed=0,
                candidates_revalidated=0,
                approved=0,
                rejections_by_reason={},
                runtime_seconds=0.0,
            ),
            metadata_provenance={"revision": "rev", "payload_sha256": "0" * 64},
            settings_summary={},
            adapter_capabilities=(),
        )

    def test_section_renders_rows_detail_and_caveats(self) -> None:
        references = (
            AcquisitionReference(
                market_hash_name="Name A",
                venue="skinport",
                min_ask=_usd(19),
                median_ask=_usd(24),
                quantity=23,
                observed_at=NOW,
            ),
            AcquisitionReference(
                market_hash_name="Name B",
                venue="skinport",
                min_ask=None,
                median_ask=None,
                quantity=0,
                observed_at=NOW,
            ),
        )
        markdown = render_scan_markdown(
            self._report(),
            [],
            acquisition_reference=references,
            acquisition_reference_detail="2 items quoted",
        )
        assert "## Cross-market acquisition reference" in markdown
        assert "| Name A | skinport | 0.19 | 0.24 | 23 |" in markdown
        assert "| Name B | skinport | none listed | none listed | 0 |" in markdown
        assert "UNVERIFIED" in markdown
        assert "never feed EV or a gate" in markdown
        assert "_Fetch detail: 2 items quoted_" in markdown

    def test_section_is_absent_when_there_is_no_reference(self) -> None:
        markdown = render_scan_markdown(self._report(), [])
        assert "Cross-market acquisition reference" not in markdown


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
