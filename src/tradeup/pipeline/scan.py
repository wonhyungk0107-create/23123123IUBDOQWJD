"""The shadow-mode scan.

Ingest -> normalise -> enumerate -> optimise -> value -> gate -> revalidate -> gate
again -> operator card. Every stage records why candidates died, because the
rejection census is the actual output of a shadow run. A scan that surfaces nothing
is a useful result; a scan that cannot say *why* it surfaced nothing is not.

Two things are deliberate and load-bearing:

* **Normalised floats are recomputed from the registry, never trusted from a venue.**
  A marketplace's idea of a skin's float caps is not authoritative, and a wrong cap
  silently shifts every projected output float. Where the venue and the registry
  disagree the listing is dropped with ``METADATA_DISCREPANCY``.
* **Revalidation happens after the discovery gate and before the final gate**, and
  the candidate is re-evaluated on the revalidated data. Evaluating once and checking
  twice would let a price that moved during the scan pass a gate it no longer
  deserves.
"""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from fractions import Fraction

from tradeup.adapters.base import ListingQuery, ListingVerification, MarketAdapter
from tradeup.config import Settings
from tradeup.discovery.boundaries import candidate_float_budgets
from tradeup.discovery.enumerator import CompositionConstraints, enumerate_compositions
from tradeup.domain.contracts import (
    CandidateEvaluation,
    CandidateRejection,
    ContractComposition,
    TradeupCandidate,
    TradeupInput,
    TradeupOutcome,
)
from tradeup.domain.fees import UnknownFeeError
from tradeup.domain.items import QualityType, Rarity
from tradeup.domain.listings import ListingIdentity, MarketplaceListing
from tradeup.domain.mathematics import (
    MathematicsError,
    average_normalized_float,
    normalized_float_exact,
    outcome_probabilities,
    project_output_wear,
)
from tradeup.domain.rules import RuleViolation, TradeupRuleSet
from tradeup.domain.valuation import PriceObservation
from tradeup.execution.policy import GateDecision, RiskPolicy
from tradeup.execution.reservations import ReservationRegistry
from tradeup.metadata.registry import MetadataRegistry
from tradeup.optimizer.bundle import BundleOptimizer, BundleSolution
from tradeup.optimizer.pareto import CandidateItem
from tradeup.valuation.exit_prices import ExitPriceResolver
from tradeup.valuation.expected_value import ExpectedValueEngine

__all__ = ["ScanConfig", "ScanOutcome", "ScanPipeline", "ScanReport", "ScanStatistics"]

#: Tolerance when comparing a venue's normalised float against the registry's.
_FLOAT_AGREEMENT_TOLERANCE = Decimal("0.0001")


@dataclass(frozen=True, slots=True)
class ScanConfig:
    """What to scan and how hard to look."""

    input_rarity: Rarity
    quality: QualityType = QualityType.NORMAL
    composition_constraints: CompositionConstraints = field(default_factory=CompositionConstraints)
    candidate_ttl: timedelta = timedelta(minutes=10)
    #: Cap on solved bundles per composition, across float budgets.
    max_budgets_per_composition: int = 6
    #: Hard cap on candidates built, so a pathological market cannot run forever.
    max_candidates: int = 500


@dataclass(frozen=True)
class ScanOutcome:
    """One candidate and everything decided about it."""

    candidate: TradeupCandidate
    evaluation: CandidateEvaluation
    decision: GateDecision
    solution: BundleSolution
    verifications: Mapping[str, ListingVerification] = field(default_factory=dict)
    rejection: CandidateRejection | None = None

    @property
    def approved(self) -> bool:
        return self.decision.approved


@dataclass(frozen=True)
class ScanStatistics:
    """What the scan actually did. Written verbatim into the evidence artifact."""

    listings_ingested: int
    listings_usable: int
    listings_dropped: Mapping[str, int]
    compositions_enumerated: int
    bundles_solved: int
    candidates_built: int
    discovery_passed: int
    candidates_revalidated: int
    approved: int
    rejections_by_reason: Mapping[str, int]
    runtime_seconds: float

    def summary(self) -> dict[str, str]:
        return {
            "listings_ingested": str(self.listings_ingested),
            "listings_usable": str(self.listings_usable),
            "listings_dropped": ";".join(
                f"{k}={v}" for k, v in sorted(self.listings_dropped.items())
            )
            or "none",
            "compositions_enumerated": str(self.compositions_enumerated),
            "bundles_solved": str(self.bundles_solved),
            "candidates_built": str(self.candidates_built),
            "discovery_passed": str(self.discovery_passed),
            "candidates_revalidated": str(self.candidates_revalidated),
            "approved": str(self.approved),
            "rejections_by_reason": ";".join(
                f"{k}={v}" for k, v in sorted(self.rejections_by_reason.items())
            )
            or "none",
            "runtime_seconds": f"{self.runtime_seconds:.3f}",
        }


@dataclass(frozen=True)
class ScanReport:
    """Complete result of one scan."""

    scanned_at: datetime
    rule_version: str
    approved: tuple[ScanOutcome, ...]
    rejected: tuple[ScanOutcome, ...]
    statistics: ScanStatistics
    metadata_provenance: Mapping[str, str]
    settings_summary: Mapping[str, str]
    adapter_capabilities: tuple[Mapping[str, str], ...]

    @property
    def all_outcomes(self) -> tuple[ScanOutcome, ...]:
        return self.approved + self.rejected

    def ranked(self) -> tuple[ScanOutcome, ...]:
        """Approved candidates ranked by capital velocity, not raw ROI.

        8% over five days beats 12% over thirty, and ranking on ROI alone would
        systematically prefer the slower contract.
        """
        return tuple(
            sorted(
                self.approved,
                key=lambda o: (
                    -o.evaluation.profit_per_capital_day.minor_units,
                    -o.evaluation.lower_bound_ev.minor_units,
                    o.candidate.candidate_id,
                ),
            )
        )


class ScanPipeline:
    """Runs one complete shadow scan."""

    def __init__(
        self,
        *,
        settings: Settings,
        registry: MetadataRegistry,
        ruleset: TradeupRuleSet,
        optimizer: BundleOptimizer,
        exit_resolver: ExitPriceResolver,
        ev_engine: ExpectedValueEngine,
        policy: RiskPolicy,
        reservations: ReservationRegistry,
    ) -> None:
        self._settings = settings
        self._registry = registry
        self._ruleset = ruleset
        self._optimizer = optimizer
        self._exit = exit_resolver
        self._ev = ev_engine
        self._policy = policy
        self._reservations = reservations

    # -- ingestion -----------------------------------------------------------

    async def ingest(
        self,
        adapters: Sequence[MarketAdapter],
        config: ScanConfig,
        *,
        moment: datetime,
    ) -> tuple[list[MarketplaceListing], dict[str, int]]:
        """Collect listings from every adapter that can serve them.

        An adapter that refuses is not an error: its refusal is counted and the scan
        continues with the sources that did answer.
        """
        listings: list[MarketplaceListing] = []
        dropped: dict[str, int] = {}
        query = ListingQuery(rarity=config.input_rarity, quality=config.quality, limit=1000)
        for adapter in adapters:
            result = await adapter.fetch_listings(query, moment=moment)
            if not result.ok:
                key = f"adapter_{adapter.venue}_{result.status.value}"
                dropped[key] = dropped.get(key, 0) + 1
                continue
            listings.extend(result.unwrap())
        return listings, dropped

    def normalise(
        self,
        listings: Sequence[MarketplaceListing],
        config: ScanConfig,
        *,
        dropped: dict[str, int],
    ) -> dict[str, list[CandidateItem]]:
        """Validate listings against the registry and index them by collection.

        The registry is authoritative for float caps. A listing whose own normalised
        float disagrees is dropped rather than corrected: disagreement means one of
        the two sources is wrong about what the item *is*, and we do not know which.
        """
        by_collection: dict[str, list[CandidateItem]] = {}
        for listing in listings:
            if listing.rarity is not config.input_rarity:
                dropped["wrong_rarity"] = dropped.get("wrong_rarity", 0) + 1
                continue
            if listing.quality_type is not config.quality:
                dropped["wrong_quality"] = dropped.get("wrong_quality", 0) + 1
                continue
            if not listing.is_usable:
                dropped["not_purchasable"] = dropped.get("not_purchasable", 0) + 1
                continue
            if not self._registry.has_skin(listing.skin_id):
                dropped["unknown_skin"] = dropped.get("unknown_skin", 0) + 1
                continue

            skin = self._registry.skin(listing.skin_id)
            if skin.collection_id != listing.collection_id:
                dropped["collection_mismatch"] = dropped.get("collection_mismatch", 0) + 1
                continue
            try:
                exact = normalized_float_exact(listing.raw_float, skin.float_range)
            except MathematicsError:
                dropped["float_out_of_range"] = dropped.get("float_out_of_range", 0) + 1
                continue

            registry_normalized = Decimal(exact.numerator) / Decimal(exact.denominator)
            if abs(registry_normalized - listing.normalized_float) > _FLOAT_AGREEMENT_TOLERANCE:
                dropped["metadata_discrepancy"] = dropped.get("metadata_discrepancy", 0) + 1
                continue

            by_collection.setdefault(listing.collection_id, []).append(
                CandidateItem(
                    identity=listing.identity,
                    collection_id=listing.collection_id,
                    normalized=exact,
                    cost_minor=listing.venue_acquisition_cost.minor_units,
                )
            )
        return by_collection

    # -- candidate construction ----------------------------------------------

    def _build_candidate(
        self,
        solution: BundleSolution,
        listings_by_identity: Mapping[ListingIdentity, MarketplaceListing],
        price_observations: Sequence[PriceObservation],
        *,
        moment: datetime,
        config: ScanConfig,
    ) -> TradeupCandidate | None:
        """Turn a solved bundle into a fully valued candidate.

        Returns ``None`` when the contract is not well-defined -- an empty output
        pool, a quality mix the ruleset forbids, or an unresolvable fee. Those are
        counted by the caller rather than raised, because they are ordinary outcomes
        of scanning a real market.
        """
        composition = solution.composition
        inputs: list[TradeupInput] = []
        for identity in solution.selection:
            listing = listings_by_identity[identity]
            skin = self._registry.skin(listing.skin_id)
            inputs.append(
                TradeupInput(
                    listing=listing,
                    normalized=normalized_float_exact(listing.raw_float, skin.float_range),
                )
            )

        qualities = frozenset(i.listing.quality_type for i in inputs)
        try:
            output_quality = self._ruleset.output_quality_for(qualities)
        except RuleViolation:
            return None

        pools = self._registry.output_pools_for(
            composition.collection_ids, composition.input_rarity
        )
        try:
            probabilities = outcome_probabilities(
                dict(composition.counts_by_collection), pools, self._ruleset
            )
        except (MathematicsError, RuleViolation):
            return None

        average = average_normalized_float([i.normalized for i in inputs])

        outcomes: list[TradeupOutcome] = []
        for entry in probabilities:
            if not self._registry.has_skin(entry.output_skin_id):
                return None
            output_skin = self._registry.skin(entry.output_skin_id)
            try:
                output_float, wear = project_output_wear(
                    average, output_skin.float_range, self._ruleset
                )
            except (MathematicsError, RuleViolation):
                return None
            if not output_skin.supports(output_quality):
                # The ruleset says this contract yields a quality the output skin
                # does not exist in. That is a metadata or rule conflict, not a
                # cheap contract.
                return None
            market_hash_name = output_skin.market_hash_name(output_quality, wear)
            try:
                valuation = self._exit.resolve(
                    skin_id=entry.output_skin_id,
                    market_hash_name=market_hash_name,
                    quality=output_quality,
                    wear=wear,
                    output_float=output_float,
                    observations=price_observations,
                    moment=moment,
                )
            except UnknownFeeError:
                return None
            outcomes.append(
                TradeupOutcome(
                    collection_id=entry.collection_id,
                    skin_id=entry.output_skin_id,
                    probability=entry.probability,
                    output_float=output_float,
                    wear=wear,
                    quality=output_quality,
                    valuation=valuation,
                )
            )

        return TradeupCandidate(
            composition=composition,
            inputs=tuple(inputs),
            outcomes=tuple(outcomes),
            rule_version=self._ruleset.rule_version,
            output_quality=output_quality,
            average_normalized_float=average,
            created_at=moment,
            expires_at=moment + config.candidate_ttl,
        )

    # -- revalidation --------------------------------------------------------

    async def revalidate(
        self,
        candidate: TradeupCandidate,
        adapters_by_venue: Mapping[str, MarketAdapter],
        *,
        moment: datetime,
    ) -> dict[str, ListingVerification]:
        """Requery every listing directly at its own venue.

        Aggregator data never satisfies this step: the whole point is to ask the
        venue we would actually buy from.
        """
        verifications: dict[str, ListingVerification] = {}
        for item in candidate.inputs:
            adapter = adapters_by_venue.get(item.identity.venue)
            if adapter is None:
                continue
            result = await adapter.verify_listing(item.identity, moment=moment)
            if result.ok:
                verifications[item.identity.listing_id] = result.unwrap()
        return verifications

    # -- the scan ------------------------------------------------------------

    async def run(
        self,
        adapters: Sequence[MarketAdapter],
        config: ScanConfig,
        *,
        moment: datetime,
        price_observations: Sequence[PriceObservation] = (),
        fee_schedule_id: str = "default",
    ) -> ScanReport:
        started = time.perf_counter()
        adapters_by_venue = {a.venue: a for a in adapters}

        raw_listings, dropped = await self.ingest(adapters, config, moment=moment)
        listings_by_identity = {listing.identity: listing for listing in raw_listings}
        by_collection = self.normalise(raw_listings, config, dropped=dropped)
        usable = sum(len(v) for v in by_collection.values())

        input_count = self._ruleset.input_count_for(config.input_rarity)
        availability = {cid: len(items) for cid, items in by_collection.items()}

        compositions = list(
            enumerate_compositions(
                input_rarity=config.input_rarity,
                input_count=input_count,
                available_by_collection=availability,
                rule_version=self._ruleset.rule_version,
                constraints=config.composition_constraints,
            )
        )

        approved: list[ScanOutcome] = []
        rejected: list[ScanOutcome] = []
        rejection_counts: dict[str, int] = {}
        bundles_solved = 0
        candidates_built = 0
        discovery_passed = 0
        revalidated = 0
        seen_candidate_ids: set[str] = set()

        for composition in compositions:
            if candidates_built >= config.max_candidates:
                break
            budgets = self._budgets_for(composition, config)
            for budget in budgets:
                solution = self._optimizer.solve(
                    composition, by_collection, max_average_normalized=budget
                )
                if solution is None:
                    continue
                bundles_solved += 1

                candidate = self._build_candidate(
                    solution,
                    listings_by_identity,
                    price_observations,
                    moment=moment,
                    config=config,
                )
                if candidate is None:
                    continue
                if candidate.candidate_id in seen_candidate_ids:
                    # Different float budgets often select the same bundle.
                    continue
                seen_candidate_ids.add(candidate.candidate_id)
                candidates_built += 1

                evaluation = self._ev.evaluate(
                    candidate, moment=moment, fee_schedule_id=fee_schedule_id
                )
                discovery = self._policy.evaluate_discovery(candidate, evaluation, moment=moment)
                if not discovery.approved:
                    self._record(
                        rejected,
                        rejection_counts,
                        candidate,
                        evaluation,
                        discovery,
                        solution,
                        {},
                        moment,
                    )
                    continue
                discovery_passed += 1

                claim = self._reservations.claim_bundle(
                    candidate.candidate_id, list(candidate.input_identities), now=moment
                )

                verifications = await self.revalidate(candidate, adapters_by_venue, moment=moment)
                revalidated += 1

                final = self._policy.approves_for_operator_card(
                    candidate,
                    evaluation,
                    moment=moment,
                    ruleset=self._ruleset,
                    verifications=verifications,
                    reserved_listing_ids=claim.reserved_listing_ids,
                )
                if final.approved:
                    approved.append(
                        ScanOutcome(
                            candidate=candidate,
                            evaluation=evaluation,
                            decision=final,
                            solution=solution,
                            verifications=verifications,
                        )
                    )
                else:
                    self._reservations.release_candidate(candidate.candidate_id, now=moment)
                    self._record(
                        rejected,
                        rejection_counts,
                        candidate,
                        evaluation,
                        final,
                        solution,
                        verifications,
                        moment,
                    )

        statistics = ScanStatistics(
            listings_ingested=len(raw_listings),
            listings_usable=usable,
            listings_dropped=dropped,
            compositions_enumerated=len(compositions),
            bundles_solved=bundles_solved,
            candidates_built=candidates_built,
            discovery_passed=discovery_passed,
            candidates_revalidated=revalidated,
            approved=len(approved),
            rejections_by_reason=rejection_counts,
            runtime_seconds=time.perf_counter() - started,
        )

        return ScanReport(
            scanned_at=moment,
            rule_version=self._ruleset.rule_version,
            approved=tuple(approved),
            rejected=tuple(rejected),
            statistics=statistics,
            metadata_provenance=self._registry.provenance(),
            settings_summary=self._settings.gate_summary(),
            adapter_capabilities=tuple(a.describe() for a in adapters),
        )

    def _budgets_for(
        self, composition: ContractComposition, config: ScanConfig
    ) -> tuple[Fraction, ...]:
        """Float budgets worth solving, derived from the outputs' wear boundaries.

        Budgets sort ascending and the *loosest* end is kept when truncating.
        Found live (2026-07-26): collections with many output skins produce more
        breakpoints than the cap, and truncating from the tight end silently
        discarded the unconstrained budget — the only one a page of cheap,
        high-float listings can ever satisfy — so the optimizer solved nothing.
        The tightest budgets are the ones a real market rarely feeds anyway.
        """
        pools = self._registry.output_pools_for(
            composition.collection_ids, composition.input_rarity
        )
        ranges = [
            self._registry.skin(skin_id).float_range
            for pool in pools.values()
            for skin_id in pool.output_skin_ids
            if self._registry.has_skin(skin_id)
        ]
        if not ranges:
            return ()
        budgets = candidate_float_budgets(ranges)
        return budgets[-config.max_budgets_per_composition :]

    @staticmethod
    def _record(
        bucket: list[ScanOutcome],
        counts: dict[str, int],
        candidate: TradeupCandidate,
        evaluation: CandidateEvaluation,
        decision: GateDecision,
        solution: BundleSolution,
        verifications: Mapping[str, ListingVerification],
        moment: datetime,
    ) -> None:
        for reason in decision.reasons:
            counts[reason.value] = counts.get(reason.value, 0) + 1
        bucket.append(
            ScanOutcome(
                candidate=candidate,
                evaluation=evaluation,
                decision=decision,
                solution=solution,
                verifications=dict(verifications),
                rejection=decision.to_rejection(moment),
            )
        )
