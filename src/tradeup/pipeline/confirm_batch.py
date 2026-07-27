"""Confirm the sweep's best leads against exact listings, and keep the score.

One batch: sweep the catalogue, deduplicate the leaderboard into distinct leads,
confirm each lead against exact purchasable listings with exact floats, and
append an estimate-versus-executable row to the calibration dataset whatever the
outcome — a lead that found no listings is as much a data point as one that
built a candidate. The batch then reports the calibration over the *entire*
recorded history, not just this run.

Budget behaviour is explicit: Skinport allows 8 requests per 5 minutes and each
confirmation spends one or two, so a batch that trips the limit sleeps out the
window once and retries; a second refusal records the lead as FAILED and moves
on. Nothing is silently skipped.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Protocol

import httpx

from tradeup.adapters.http import HttpxTransport, RestClient, RetryPolicy
from tradeup.adapters.skinport import SkinportItemQuote, SkinportPriceSource
from tradeup.config import Settings
from tradeup.demo.runner import default_metadata_path
from tradeup.discovery.calibration import (
    CalibrationPair,
    CalibrationReport,
    applied_haircuts,
    build_calibration_report,
)
from tradeup.discovery.prospects import Prospect, ProspectPolicy, SweepStatistics, sweep_prospects
from tradeup.domain.items import QualityType, Rarity
from tradeup.domain.rules import DEFAULT_RULE_REGISTRY
from tradeup.metadata.bymykel import load_pinned_snapshot
from tradeup.persistence.database import create_database
from tradeup.persistence.models import ProspectConfirmationRow
from tradeup.persistence.repositories import ConfirmationRepository
from tradeup.pipeline.live_scan import LiveScanError, LiveScanResult, run_live_scan
from tradeup.valuation.entry_targets import EntryAssessment, assess_entry
from tradeup.valuation.venue_fees import build_live_fee_schedule

__all__ = ["BatchResult", "ConfirmationOutcome", "run_confirmation_batch", "select_leads"]


def select_leads(
    prospects: Sequence[Prospect],
    *,
    top: int,
    min_estimated_roi: Decimal,
) -> tuple[Prospect, ...]:
    """Distinct leads worth spending confirmation budget on.

    The ranked board contains near-duplicates — a pure sketch and its filler
    dilutions share the same market reality — so leads deduplicate on
    (primary collection, rarity, quality, wear), keeping the best-ranked variant.
    """
    seen: set[tuple[str, str, str, str]] = set()
    leads: list[Prospect] = []
    for prospect in prospects:
        if prospect.estimated_roi < min_estimated_roi:
            continue
        key = (
            prospect.collection_id,
            prospect.input_rarity.value,
            prospect.quality.value,
            prospect.input_wear.value,
        )
        if key in seen:
            continue
        seen.add(key)
        leads.append(prospect)
        if len(leads) >= top:
            break
    return tuple(leads)


@dataclass(frozen=True)
class ConfirmationOutcome:
    """One lead's confirmation, exactly as persisted."""

    prospect: Prospect
    status: str
    listings_found: int
    candidate_id: str | None
    exact_cost_minor: int | None
    exact_output_value_minor: int | None
    exact_ev_minor: int | None
    exact_roi: Decimal | None
    rejection_reasons: str | None
    detail: str | None
    #: Entry-threshold judgement for the best candidate, when one was built.
    assessment: EntryAssessment | None = None
    #: True when the best candidate cleared every gate — the alert condition.
    approved: bool = False
    #: Human-readable exact listings of the best bundle, for the alert card.
    input_listings: tuple[str, ...] = ()

    def summary_row(self) -> dict[str, str]:
        exact = (
            f"{(self.exact_roi * 100).quantize(Decimal('0.01'))}%"
            if (self.exact_roi is not None)
            else "-"
        )
        return {
            "collection": self.prospect.collection_name,
            "quality": self.prospect.quality.value,
            "wear": self.prospect.input_wear.value,
            "est_roi": f"{self.prospect.roi_percent}%",
            "exact_roi": exact,
            "status": self.status,
            "reasons": self.rejection_reasons or "",
        }


@dataclass(frozen=True)
class BatchResult:
    """Everything one confirm-and-calibrate batch produced."""

    swept_at: datetime
    sweep_statistics: SweepStatistics
    leads: tuple[Prospect, ...]
    outcomes: tuple[ConfirmationOutcome, ...]
    calibration: CalibrationReport
    history_rows: int
    #: The per-quality haircuts this batch's sweep actually ran under.
    applied_ask_haircuts: tuple[tuple[QualityType, Decimal], ...] = ()
    #: Operator card written when an approved candidate met the entry target.
    alert_path: Path | None = None


def _fetch_catalog_quotes(settings: Settings, now: datetime) -> dict[str, SkinportItemQuote]:
    async def _fetch() -> dict[str, SkinportItemQuote]:
        async with httpx.AsyncClient() as client:
            rest = RestClient(
                transport=HttpxTransport(client, clock=lambda: datetime.now(UTC)),
                user_agent=settings.http_user_agent,
                timeout_seconds=float(settings.http_timeout_seconds),
                retry=RetryPolicy(max_attempts=settings.http_max_retries),
            )
            result = await SkinportPriceSource(rest).fetch_item_quotes(moment=now)
            if not result.ok:
                raise LiveScanError(
                    f"skinport catalogue fetch refused: {result.status.value} ({result.detail})"
                )
            return dict(result.unwrap())

    return asyncio.run(_fetch())


def _confirm_one(
    prospect: Prospect,
    *,
    settings: Settings,
    now: datetime,
    per_name_limit: int,
    max_candidates: int,
    output_dir: Path | None,
) -> tuple[str, LiveScanResult | None, str | None]:
    """Run one targeted confirmation. Returns (status, result, failure detail)."""
    names = [plan.market_hash_name for plan in prospect.inputs]
    try:
        result = run_live_scan(
            settings=settings,
            now=now,
            input_rarity=prospect.input_rarity,
            quality=prospect.quality,
            target_names=names,
            per_name_limit=per_name_limit,
            max_candidates=max_candidates,
            output_dir=output_dir,
        )
    except LiveScanError as exc:
        return "FAILED", None, str(exc)
    if result.report.statistics.listings_ingested == 0:
        return "NO_LISTINGS", result, None
    if result.report.statistics.candidates_built == 0:
        return "NO_BUNDLE", result, None
    return "CONFIRMED", result, None


class _Confirmer(Protocol):
    """The exact-listing confirmation step, injectable so tests stay offline."""

    def __call__(
        self,
        prospect: Prospect,
        *,
        settings: Settings,
        now: datetime,
        per_name_limit: int,
        max_candidates: int,
        output_dir: Path | None,
    ) -> tuple[str, LiveScanResult | None, str | None]: ...


def run_confirmation_batch(
    *,
    settings: Settings,
    clock: Callable[[], datetime],
    top: int = 5,
    min_estimated_roi: Decimal = Decimal("0"),
    per_name_limit: int = 15,
    max_candidates: int = 5,
    qualities: Sequence[QualityType] = (QualityType.NORMAL, QualityType.STATTRAK),
    input_rarities: Sequence[Rarity] | None = None,
    policy: ProspectPolicy | None = None,
    retry_wait_seconds: int = 310,
    sleeper: Callable[[float], None] = time.sleep,
    output_dir: Path | None = None,
    quotes_fetcher: Callable[[Settings, datetime], dict[str, SkinportItemQuote]] | None = None,
    confirm: _Confirmer | None = None,
) -> BatchResult:
    """Sweep, confirm the distinct top leads, persist the pairs, report the fit.

    The loop is closed: reliable per-quality haircut recommendations from the
    recorded history steer this batch's sweep automatically, and an approved
    candidate at or below the entry target produces an alert artifact. Nothing
    here can spend; acquisition remains a human following the operator card.
    """
    policy = policy or ProspectPolicy()
    fetch_quotes = quotes_fetcher if quotes_fetcher is not None else _fetch_catalog_quotes
    confirm_lead: _Confirmer = confirm if confirm is not None else _confirm_one
    swept_at = clock()

    # Feed the recorded history back into the sweep: a reliable per-quality
    # recommendation is applied; an anecdote or empty history leaves the
    # configured prior in place.
    database = create_database(settings.database_url)
    database.create_all()
    with database.session() as session:
        prior_rows = ConfirmationRepository(session).all_rows()
    prior_calibration = build_calibration_report(calibration_pairs(prior_rows))
    haircuts = applied_haircuts(prior_calibration, default=policy.ask_haircut)
    policy = replace(
        policy,
        ask_haircut_by_quality=tuple(sorted(haircuts.items(), key=lambda pair: pair[0].value)),
    )

    registry = load_pinned_snapshot(default_metadata_path(), imported_at=swept_at).registry
    registry.require_valid()
    ruleset = DEFAULT_RULE_REGISTRY.resolve(swept_at)
    quotes = fetch_quotes(settings, swept_at)

    prospects, statistics = sweep_prospects(
        registry=registry,
        ruleset=ruleset,
        quotes=quotes,
        fee_schedule=build_live_fee_schedule(),
        base_currency=settings.base_currency,
        moment=swept_at,
        qualities=tuple(qualities),
        policy=policy,
        max_cost=settings.max_contract_cost,
    )
    del quotes
    leads = select_leads(prospects, top=top, min_estimated_roi=min_estimated_roi)
    if input_rarities is not None:
        allowed = set(input_rarities)
        leads = tuple(lead for lead in leads if lead.input_rarity in allowed)

    outcomes: list[ConfirmationOutcome] = []
    for lead in leads:
        status, result, failure = confirm_lead(
            lead,
            settings=settings,
            now=clock(),
            per_name_limit=per_name_limit,
            max_candidates=max_candidates,
            output_dir=output_dir,
        )
        if status == "FAILED" and failure is not None and "RATE_LIMITED" in failure:
            # One respectful wait for the documented window, then one retry.
            sleeper(float(retry_wait_seconds))
            status, result, failure = confirm_lead(
                lead,
                settings=settings,
                now=clock(),
                per_name_limit=per_name_limit,
                max_candidates=max_candidates,
                output_dir=output_dir,
            )

        candidate_id = None
        exact_cost = exact_value = exact_ev = None
        exact_roi: Decimal | None = None
        reasons: str | None = None
        listings_found = 0
        assessment: EntryAssessment | None = None
        approved = False
        input_listings: tuple[str, ...] = ()
        if result is not None:
            listings_found = result.report.statistics.listings_ingested
            all_outcomes = result.report.all_outcomes
            if all_outcomes:
                best = max(
                    all_outcomes,
                    key=lambda o: (o.evaluation.roi_net, o.candidate.candidate_id),
                )
                candidate_id = best.candidate.candidate_id
                evaluation = best.evaluation
                exact_cost = evaluation.acquisition_cost.minor_units
                exact_value = evaluation.expected_output_value.minor_units
                exact_ev = evaluation.ev_net.minor_units
                exact_roi = evaluation.roi_net
                approved = best.approved
                input_listings = tuple(str(item.listing) for item in best.candidate.inputs)
                if best.rejection is not None:
                    reasons = ",".join(r.value for r in best.rejection.reasons)
                if evaluation.acquisition_cost.minor_units >= 1:
                    # Overheads held at their evaluated magnitudes: pessimistic
                    # for any cheaper bundle, per assess_entry's contract.
                    assessment = assess_entry(
                        expected_net_output_value=evaluation.expected_output_value,
                        observed_acquisition_cost=evaluation.acquisition_cost,
                        input_count=best.candidate.input_count,
                        target_roi=settings.final_min_net_roi,
                        fixed_overhead=(
                            evaluation.operational_cost
                            + evaluation.settlement_cost
                            + evaluation.capital_carry_cost
                            + evaluation.partial_fill_reserve
                        ),
                    )

        outcomes.append(
            ConfirmationOutcome(
                prospect=lead,
                status=status,
                listings_found=listings_found,
                candidate_id=candidate_id,
                exact_cost_minor=exact_cost,
                exact_output_value_minor=exact_value,
                exact_ev_minor=exact_ev,
                exact_roi=exact_roi,
                rejection_reasons=reasons,
                detail=failure,
                assessment=assessment,
                approved=approved,
                input_listings=input_listings,
            )
        )

    # Persist every outcome, then calibrate over the full recorded history.
    recorded_at = clock()
    with database.session() as session:
        repository = ConfirmationRepository(session)
        for outcome in outcomes:
            prospect = outcome.prospect
            repository.record(
                ProspectConfirmationRow(
                    confirmed_at=recorded_at,
                    rule_version=prospect.rule_version,
                    collection_id=prospect.collection_id,
                    counts_json={cid: count for cid, count in prospect.counts_by_collection},
                    input_rarity=prospect.input_rarity.value,
                    quality=prospect.quality.value,
                    input_wear=prospect.input_wear.value,
                    estimated_cost_minor=prospect.estimated_cost.minor_units,
                    estimated_output_value_minor=prospect.estimated_output_value.minor_units,
                    estimated_ev_minor=prospect.estimated_ev.minor_units,
                    estimated_roi=str(prospect.estimated_roi),
                    # The haircut this estimate actually ran under -- with
                    # per-quality calibration these differ by lead.
                    estimated_ask_haircut=str(prospect.ask_haircut),
                    status=outcome.status,
                    listings_found=outcome.listings_found,
                    candidate_id=outcome.candidate_id,
                    exact_cost_minor=outcome.exact_cost_minor,
                    exact_output_value_minor=outcome.exact_output_value_minor,
                    exact_ev_minor=outcome.exact_ev_minor,
                    exact_roi=str(outcome.exact_roi) if outcome.exact_roi is not None else None,
                    rejection_reasons=outcome.rejection_reasons,
                    detail=outcome.detail,
                )
            )
        history = repository.all_rows()
    database.dispose()

    entry_hits = tuple(
        outcome
        for outcome in outcomes
        if outcome.approved and outcome.assessment is not None and outcome.assessment.entry_met
    )
    alert_path = (
        _write_entry_alert(entry_hits, settings=settings, moment=recorded_at)
        if entry_hits
        else None
    )

    calibration = build_calibration_report(calibration_pairs(history))
    return BatchResult(
        swept_at=swept_at,
        sweep_statistics=statistics,
        leads=leads,
        outcomes=tuple(outcomes),
        calibration=calibration,
        history_rows=len(history),
        applied_ask_haircuts=policy.ask_haircut_by_quality,
        alert_path=alert_path,
    )


def _write_entry_alert(
    hits: Sequence[ConfirmationOutcome],
    *,
    settings: Settings,
    moment: datetime,
) -> Path:
    """Write the operator card for approved candidates at the entry target.

    The card is evidence of a moment, not a standing offer: it states what was
    measured, when, and that nothing was bought. Acquisition stays human.
    """
    alert_dir = settings.artifacts_dir / "alerts"
    alert_dir.mkdir(parents=True, exist_ok=True)
    path = alert_dir / f"entry-alert-{moment.strftime('%Y%m%dT%H%M%SZ')}.md"
    lines = [
        "# ENTRY ALERT -- approved trade-up candidate at the entry target",
        "",
        f"Generated {moment.isoformat()} by the confirm batch.",
        "Live read-only measurement of purchasable listings; **nothing was bought**.",
        "Acquisition is human-only. Verify every price and fee at the venue before",
        "paying: sourced fees have not been confirmed against an account screen.",
        "",
    ]
    for outcome in hits:
        prospect = outcome.prospect
        assessment = outcome.assessment
        roi = (
            f"{(outcome.exact_roi * 100).quantize(Decimal('0.01'))}%"
            if outcome.exact_roi is not None
            else "unknown"
        )
        lines.append(
            f"## {prospect.collection_name} -- {prospect.quality.value} {prospect.input_wear.value}"
        )
        lines.append("")
        lines.append(f"- candidate: `{outcome.candidate_id}`")
        lines.append(f"- exact fee-net ROI at evaluation: {roi}")
        if assessment is not None:
            lines.append(
                f"- bundle acquisition {assessment.observed_total} at or below entry cap "
                f"{assessment.target_total} (per-unit cap {assessment.target_unit})"
            )
        lines.append("- purchasable inputs at evaluation time:")
        lines.extend(f"    - {listing}" for listing in outcome.input_listings)
        lines.append("")
    lines.append("Listings move. Re-confirm (`tradeup candidates confirm --rank 1`) before")
    lines.append("acting; this alert is a measurement, not an instruction to spend.")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def calibration_pairs(rows: Sequence[ProspectConfirmationRow]) -> tuple[CalibrationPair, ...]:
    """History rows that carry a usable estimate/exact value pair."""
    pairs: list[CalibrationPair] = []
    for row in rows:
        if row.status != "CONFIRMED":
            continue
        if row.exact_output_value_minor is None or row.exact_roi is None:
            continue
        if row.estimated_output_value_minor <= 0:
            continue
        pairs.append(
            CalibrationPair(
                quality=QualityType(row.quality),
                estimated_output_value_minor=row.estimated_output_value_minor,
                exact_output_value_minor=row.exact_output_value_minor,
                estimated_roi=Decimal(row.estimated_roi),
                exact_roi=Decimal(row.exact_roi),
                ask_haircut_at_estimate=Decimal(row.estimated_ask_haircut),
            )
        )
    return tuple(pairs)
