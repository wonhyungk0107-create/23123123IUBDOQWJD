"""The live read-only shadow scan.

Same pipeline as the offline demo — ingest → enumerate → optimise → value → gate →
revalidate → operator card — but over the real market: CSFloat buy-now listings as
inputs, Skinport completed-sale medians as output evidence, and the sourced (dated,
un-re-verified) fee schedule from :mod:`tradeup.valuation.venue_fees`.

What this measures is the thing the whole system exists to measure: how many real
bundles clear the configured fee-net gates, and how often their listings survive to
direct revalidation. Zero approved candidates is a result.

Read-only, structurally: the only venue calls are documented GET endpoints, the
risk policy has no execution path here, and the ledger is never written.

Listings are fetched **once** and the same objects flow through the pipeline via a
prefetched wrapper; revalidation still goes to the live venue directly, because
"the listing is still there" is exactly the fact this scan exists to test.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import httpx

from tradeup.adapters.base import ListingQuery, ListingVerification, MarketAdapter
from tradeup.adapters.csfloat import CSFloatAdapter
from tradeup.adapters.http import HttpxTransport, RestClient, RetryPolicy
from tradeup.adapters.skinport import SkinportPriceSource, WantedName
from tradeup.config import Settings
from tradeup.demo.runner import default_metadata_path
from tradeup.domain.execution import CapabilityResult
from tradeup.domain.items import QualityType, Rarity
from tradeup.domain.listings import ListingIdentity, MarketplaceListing
from tradeup.domain.money import Money
from tradeup.domain.rules import DEFAULT_RULE_REGISTRY
from tradeup.domain.valuation import PriceObservation, ValuationSource
from tradeup.execution.policy import RiskPolicy
from tradeup.execution.reservations import ReservationRegistry
from tradeup.metadata.bymykel import load_pinned_snapshot
from tradeup.metadata.registry import MetadataRegistry
from tradeup.optimizer.bundle import BundleOptimizer
from tradeup.persistence.database import create_database
from tradeup.persistence.repositories import CandidateRepository, ScanRepository
from tradeup.pipeline.scan import ScanConfig, ScanPipeline, ScanReport
from tradeup.reporting.operator_card import OperatorCard, build_operator_card
from tradeup.reporting.renderers import render_scan_markdown, write_json
from tradeup.valuation.capital import CapitalModel
from tradeup.valuation.exit_prices import ExitPriceResolver
from tradeup.valuation.expected_value import ExpectedValueEngine
from tradeup.valuation.partial_fill import PartialFillModel
from tradeup.valuation.venue_fees import LIVE_FEE_SCHEDULE_ID, build_live_fee_schedule

__all__ = ["LiveScanError", "LiveScanResult", "run_live_scan"]

#: Skinport's request budget bounds how many output names can be priced per scan.
_MAX_WANTED_NAMES = 7 * 50

#: Targeted confirmations fetch CSFloat asks per output name; this caps the spend
#: against the measured 200-requests-per-window budget.
_EXIT_ASK_NAME_LIMIT = 25

_LIVE_CARD_WARNING = (
    "LIVE shadow scan. Prices and floats are real market reads, but the fee "
    "figures come from dated public statements (docs/source-matrix.md) that have "
    "NOT been re-verified at the venue, and the liquidity/fill numbers are stated "
    "priors. This card is evidence collection, not an instruction to buy."
)


class LiveScanError(Exception):
    """The scan could not run. The message states the exact blocker."""


class _PrefetchedListingsAdapter(MarketAdapter):
    """Serves one already-fetched listings result; everything else goes live.

    The pipeline's ingest and the collection survey must see the *same* listings
    or the survey's Skinport name budget would price outputs the scan never uses.
    Revalidation deliberately bypasses the prefetch: its entire purpose is to ask
    the venue again.
    """

    def __init__(
        self,
        live: CSFloatAdapter,
        prefetched: CapabilityResult[Sequence[MarketplaceListing]],
    ) -> None:
        super().__init__(live_execution_enabled=False)
        self._live = live
        self._prefetched = prefetched
        self.venue = live.venue
        self.execution_mode = live.execution_mode
        self.capability_note = live.capability_note

    async def fetch_listings(
        self, query: ListingQuery, *, moment: datetime
    ) -> CapabilityResult[Sequence[MarketplaceListing]]:
        return self._prefetched

    async def fetch_listing_by_id(
        self, listing_id: str, *, moment: datetime
    ) -> CapabilityResult[MarketplaceListing]:
        return await self._live.fetch_listing_by_id(listing_id, moment=moment)

    async def verify_listing(
        self, identity: ListingIdentity, *, moment: datetime
    ) -> CapabilityResult[ListingVerification]:
        return await self._live.verify_listing(identity, moment=moment)


@dataclass(frozen=True)
class LiveScanResult:
    """Everything one live shadow scan produced."""

    report: ScanReport
    cards: tuple[OperatorCard, ...]
    artifacts: dict[str, Path]
    listings_fetch_detail: str
    observations_detail: str
    observation_count: int
    wanted_name_count: int
    collections_priced: tuple[str, ...]
    collections_skipped: tuple[str, ...]

    @property
    def approved_count(self) -> int:
        return len(self.cards)

    @property
    def rejected_count(self) -> int:
        return len(self.report.rejected)


def _wanted_output_names(
    registry: MetadataRegistry,
    listings: Sequence[MarketplaceListing],
    input_rarity: Rarity,
    quality: QualityType,
) -> tuple[dict[str, WantedName], tuple[str, ...], tuple[str, ...]]:
    """Names to price, budgeted by collection liquidity.

    Collections with more live input listings get their output pools priced first;
    collections that fall outside the Skinport request budget are reported, not
    silently dropped — a candidate from an unpriced collection would be rejected
    on unvaluable outputs, and the report must say why.
    """
    counts: dict[str, int] = {}
    for listing in listings:
        counts[listing.collection_id] = counts.get(listing.collection_id, 0) + 1
    ordered = sorted(counts, key=lambda cid: (-counts[cid], cid))

    wanted: dict[str, WantedName] = {}
    priced: list[str] = []
    skipped: list[str] = []
    for collection_id in ordered:
        pool = registry.output_pool(collection_id, input_rarity)
        names: dict[str, WantedName] = {}
        for skin_id in sorted(pool.output_skin_ids):
            if not registry.has_skin(skin_id):
                continue
            skin = registry.skin(skin_id)
            if not skin.supports(quality):
                continue
            for wear in skin.float_range.reachable_wears:
                name = skin.market_hash_name(quality, wear)
                names[name] = (skin_id, quality, wear)
        if not names:
            skipped.append(collection_id)
            continue
        if len(wanted) + len(names) > _MAX_WANTED_NAMES:
            skipped.append(collection_id)
            continue
        wanted.update(names)
        priced.append(collection_id)
    return wanted, tuple(priced), tuple(skipped)


async def _gather_market_data(
    settings: Settings,
    registry: MetadataRegistry,
    input_rarity: Rarity,
    quality: QualityType,
    listing_limit: int,
    now: datetime,
    price_band: tuple[Money | None, Money | None],
    target_names: Sequence[str],
    per_name_limit: int,
) -> tuple[
    CapabilityResult[Sequence[MarketplaceListing]],
    CSFloatAdapter,
    tuple[PriceObservation, ...],
    str,
    tuple[str, ...],
    tuple[str, ...],
    int,
    httpx.AsyncClient,
]:
    """Fetch live listings and price evidence. The caller owns the http client."""
    api_key = settings.csfloat_api_key.get_secret_value() if settings.csfloat_api_key else None
    client = httpx.AsyncClient()
    rest = RestClient(
        transport=HttpxTransport(client, clock=lambda: datetime.now(tz=now.tzinfo)),
        user_agent=settings.http_user_agent,
        timeout_seconds=float(settings.http_timeout_seconds),
        retry=RetryPolicy(max_attempts=settings.http_max_retries),
        secrets={"csfloat_api_key": api_key or ""},
    )
    rarity_by_name = {
        "consumer grade": Rarity.CONSUMER,
        "industrial grade": Rarity.INDUSTRIAL,
        "mil-spec grade": Rarity.MIL_SPEC,
        "restricted": Rarity.RESTRICTED,
        "classified": Rarity.CLASSIFIED,
        "covert": Rarity.COVERT,
    }
    csfloat = CSFloatAdapter(
        rest, api_key=api_key, rarity_by_name=rarity_by_name, registry=registry
    )

    min_price, max_price = price_band
    if target_names:
        # Prospect-targeted mode: exact listings for exactly the input names the
        # sweep flagged, one documented market_hash_name query per name.
        collected: list[MarketplaceListing] = []
        details: list[str] = []
        unique_names = list(dict.fromkeys(target_names))
        for name in unique_names:
            per_name = await csfloat.fetch_listings(
                ListingQuery(market_hash_name=name, quality=quality, limit=per_name_limit),
                moment=now,
            )
            if not per_name.ok:
                await client.aclose()
                raise LiveScanError(
                    f"csfloat fetch for {name!r} refused: {per_name.status.value} "
                    f"({per_name.detail})"
                )
            collected.extend(per_name.unwrap())
            if per_name.detail:
                details.append(f"{name}: {per_name.detail}")
        listings_result: CapabilityResult[Sequence[MarketplaceListing]] = (
            CapabilityResult.succeeded(
                csfloat.venue,
                "fetch_listings",
                now,
                tuple(collected),
                detail=(
                    f"{len(collected)} listings across {len(unique_names)} targeted names; "
                    + " | ".join(details)
                ),
            )
        )
    else:
        listings_result = await csfloat.fetch_listings(
            ListingQuery(
                rarity=input_rarity,
                quality=quality,
                limit=listing_limit,
                min_price=min_price,
                max_price=max_price,
            ),
            moment=now,
        )
        if not listings_result.ok:
            await client.aclose()
            raise LiveScanError(
                f"csfloat listings fetch refused: {listings_result.status.value} "
                f"({listings_result.detail})"
            )

    wanted, priced, skipped = _wanted_output_names(
        registry, listings_result.unwrap(), input_rarity, quality
    )
    skinport = SkinportPriceSource(rest)
    observations_result = await skinport.fetch_sales_observations(wanted, moment=now)
    if not observations_result.ok:
        await client.aclose()
        raise LiveScanError(
            f"skinport sales-history fetch refused: {observations_result.status.value} "
            f"({observations_result.detail})"
        )
    observations = list(observations_result.unwrap())
    observations_detail = observations_result.detail

    # Targeted confirmations also read the *executable* side of the exit: the
    # lowest current buy-now asks on CSFloat per output name. These feed the
    # resolver's ask-floor cap, so a thin market's inflated sale median cannot
    # value an exit above the standing cheapest offer (venue: csfloat).
    if target_names:
        ask_names = list(wanted.items())[:_EXIT_ASK_NAME_LIMIT]
        fetched = 0
        stopped = ""
        for out_name, (skin_id, out_quality, out_wear) in ask_names:
            per_name = await csfloat.fetch_listings(
                ListingQuery(market_hash_name=out_name, quality=out_quality, limit=5),
                moment=now,
            )
            if not per_name.ok:
                stopped = f"; exit-ask fetch stopped at {fetched} names ({per_name.status.value})"
                break
            asks = per_name.unwrap()
            for listing in asks:
                observations.append(
                    PriceObservation(
                        skin_id=skin_id,
                        quality=out_quality,
                        wear=out_wear,
                        venue=csfloat.venue,
                        gross=listing.price,
                        source=ValuationSource.DEPTH_ADJUSTED_ASK,
                        observed_at=now,
                        evidence_count=1,
                        depth_units=len(asks),
                    )
                )
            fetched += 1
        observations_detail += (
            f"; csfloat exit asks for {fetched}/{len(wanted)} output names{stopped}"
        )
        if len(wanted) > _EXIT_ASK_NAME_LIMIT:
            observations_detail += (
                f" ({len(wanted) - _EXIT_ASK_NAME_LIMIT} names beyond the ask budget)"
            )

    return (
        listings_result,
        csfloat,
        tuple(observations),
        observations_detail,
        priced,
        skipped,
        len(wanted),
        client,
    )


def run_live_scan(
    *,
    settings: Settings,
    now: datetime,
    input_rarity: Rarity = Rarity.MIL_SPEC,
    quality: QualityType = QualityType.NORMAL,
    listing_limit: int = 150,
    max_candidates: int = 12,
    min_price: Money | None = None,
    max_price: Money | None = None,
    target_names: Sequence[str] = (),
    per_name_limit: int = 15,
    output_dir: Path | None = None,
) -> LiveScanResult:
    """One complete read-only shadow scan against the live market.

    ``max_candidates`` is deliberately small: every candidate that passes
    discovery costs one direct revalidation call per input against CSFloat's
    measured 200-requests-per-window budget, and a scan that burns the window
    mid-run produces INCOMPLETE_BUNDLE noise instead of survival evidence.
    """
    if settings.csfloat_api_key is None or not settings.csfloat_api_key.get_secret_value():
        raise LiveScanError("TRADEUP_CSFLOAT_API_KEY is not configured; the scan has no source")
    if settings.crypto_settlement_enabled:
        raise LiveScanError(
            "crypto settlement is enabled but no live conversion-quote source is wired; "
            "disable the rail for live scanning or supply a sourced quote first"
        )

    registry = load_pinned_snapshot(default_metadata_path(), imported_at=now).registry
    registry.require_valid()
    ruleset = DEFAULT_RULE_REGISTRY.resolve(now)
    fee_schedule = build_live_fee_schedule()

    async def _run() -> tuple[ScanReport, str, str, tuple[str, ...], tuple[str, ...], int, int]:
        (
            listings_result,
            csfloat,
            observations,
            observations_detail,
            priced,
            skipped,
            wanted_count,
            client,
        ) = await _gather_market_data(
            settings,
            registry,
            input_rarity,
            quality,
            listing_limit,
            now,
            (min_price, max_price),
            target_names,
            per_name_limit,
        )
        try:
            capital_model = CapitalModel(annual_rate=settings.annual_capital_cost_rate)
            exit_resolver = ExitPriceResolver(
                fee_schedule=fee_schedule,
                capital_model=capital_model,
                base_currency=settings.base_currency,
            )
            partial_fill = PartialFillModel(base_currency=settings.base_currency)
            ev_engine = ExpectedValueEngine(
                settings=settings,
                capital_model=capital_model,
                partial_fill_model=partial_fill,
            )
            pipeline = ScanPipeline(
                settings=settings,
                registry=registry,
                ruleset=ruleset,
                optimizer=BundleOptimizer(),
                exit_resolver=exit_resolver,
                ev_engine=ev_engine,
                policy=RiskPolicy(settings),
                reservations=ReservationRegistry(),
            )
            report = await pipeline.run(
                [_PrefetchedListingsAdapter(csfloat, listings_result)],
                ScanConfig(
                    input_rarity=input_rarity, quality=quality, max_candidates=max_candidates
                ),
                moment=now,
                price_observations=observations,
                fee_schedule_id=LIVE_FEE_SCHEDULE_ID,
            )
        finally:
            await client.aclose()
        return (
            report,
            listings_result.detail,
            observations_detail,
            priced,
            skipped,
            wanted_count,
            len(observations),
        )

    (
        report,
        listings_detail,
        observations_detail,
        priced,
        skipped,
        wanted_count,
        observation_count,
    ) = asyncio.run(_run())

    partial_fill = PartialFillModel(base_currency=settings.base_currency)
    cards = tuple(
        build_operator_card(
            outcome.candidate,
            outcome.evaluation,
            partial_fill.assess(outcome.candidate.inputs, now),
            metadata_revision=registry.revision,
            generated_at=now,
            extra_warnings=(_LIVE_CARD_WARNING,),
        )
        for outcome in report.ranked()
    )

    # Persist the scan so rejection statistics accumulate across runs.
    database = create_database(settings.database_url)
    database.create_all()
    with database.session() as session:
        scan_row = ScanRepository(session).record(
            scanned_at=now,
            rule_version=ruleset.rule_version,
            metadata_revision=registry.revision,
            settings_summary=dict(report.settings_summary),
            statistics=report.statistics.summary(),
        )
        candidates = CandidateRepository(session)
        for outcome in report.all_outcomes:
            candidates.save(
                outcome.candidate,
                metadata_revision=registry.revision,
                scan_id=scan_row.id,
                solution=outcome.solution,
            )
            candidates.save_evaluation(outcome.evaluation)
            if outcome.rejection is not None:
                candidates.save_rejection(outcome.rejection)
    database.dispose()

    evidence_dir = output_dir or settings.reports_dir
    evidence_dir.mkdir(parents=True, exist_ok=True)
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    artifacts: dict[str, Path] = {}
    artifacts["markdown"] = evidence_dir / f"live-scan-{stamp}.md"
    artifacts["markdown"].write_text(
        render_scan_markdown(report, list(cards), title="LIVE shadow scan (read-only)"),
        encoding="utf-8",
    )
    artifacts["json"] = write_json(
        evidence_dir / f"live-scan-{stamp}.json",
        {
            "scanned_at": now.isoformat(),
            "data_nature": (
                "LIVE read-only market data; fee figures from dated public statements, "
                "not re-verified at the venue"
            ),
            "rule_version": ruleset.rule_version,
            "fee_schedule": LIVE_FEE_SCHEDULE_ID,
            "input_rarity": input_rarity.value,
            "listings_fetch_detail": listings_detail,
            "output_pricing": {
                "wanted_names": wanted_count,
                "observation_count": observation_count,
                "observations_detail": observations_detail,
                "collections_priced": list(priced),
                "collections_skipped_beyond_budget": list(skipped),
            },
            "statistics": report.statistics.summary(),
            "settings": dict(report.settings_summary),
            "approved": [card.to_dict() for card in cards],
            "rejections": [
                {
                    "candidate_id": o.candidate.candidate_id,
                    "stage": o.rejection.stage if o.rejection else "",
                    "reasons": [r.value for r in o.rejection.reasons] if o.rejection else [],
                    "roi_net": str(o.evaluation.roi_net),
                    "acquisition_cost_minor": o.evaluation.acquisition_cost.minor_units,
                }
                for o in report.rejected
            ],
            "economic_evidence": {
                "orders_placed": 0,
                "trade_ups_completed": 0,
                "sales_settled": 0,
                "note": "Read-only scan. Nothing was bought, contracted, sold or settled.",
            },
        },
    )

    return LiveScanResult(
        report=report,
        cards=cards,
        artifacts=artifacts,
        listings_fetch_detail=listings_detail,
        observations_detail=observations_detail,
        observation_count=observation_count,
        wanted_name_count=wanted_count,
        collections_priced=priced,
        collections_skipped=skipped,
    )
