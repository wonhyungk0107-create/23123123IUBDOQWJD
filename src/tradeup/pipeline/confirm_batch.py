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
import json
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
from tradeup.discovery.order_watch import (
    OrderVerdict,
    StandingOrder,
    review_standing_orders,
)
from tradeup.discovery.prospects import Prospect, ProspectPolicy, SweepStatistics, sweep_prospects
from tradeup.domain.contracts import TradeupCandidate
from tradeup.domain.execution import ExecutionResult
from tradeup.domain.items import WEAR_BOUNDS, QualityType, Rarity, WearCondition
from tradeup.domain.listings import MarketplaceListing
from tradeup.domain.money import BalanceType, Currency, Money
from tradeup.domain.rules import DEFAULT_RULE_REGISTRY
from tradeup.execution.reservations import ReservationRegistry
from tradeup.metadata.bymykel import load_pinned_snapshot
from tradeup.persistence.database import create_database
from tradeup.persistence.models import ProspectConfirmationRow
from tradeup.persistence.repositories import ConfirmationRepository
from tradeup.pipeline.live_scan import LiveScanError, LiveScanResult, run_live_scan
from tradeup.purchasing import AutoBuyer
from tradeup.reporting.operator_card import venue_listing_url
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
    #: The best candidate itself, so the execution engine can act on it.
    candidate: TradeupCandidate | None = None

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
    #: (candidate_id, result) for every automated-execution attempt, including
    #: typed refusals — zero buys is a result and is reported as one.
    executions: tuple[tuple[str, ExecutionResult], ...] = ()
    #: Every recorded standing buy order judged against this batch's board.
    order_verdicts: tuple[OrderVerdict, ...] = ()
    #: Written when any standing order needs operator attention.
    order_alert_path: Path | None = None
    #: Set when the standing-orders file exists but could not be parsed —
    #: surfaced loudly because unreviewed orders are unpriced risk.
    order_watch_error: str | None = None
    #: Machine-readable order parameters written alongside an entry alert.
    proposed_orders_path: Path | None = None


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
    auto_buyer: AutoBuyer | None = None,
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
        best_candidate: TradeupCandidate | None = None
        if result is not None:
            listings_found = result.report.statistics.listings_ingested
            all_outcomes = result.report.all_outcomes
            if all_outcomes:
                best = max(
                    all_outcomes,
                    key=lambda o: (o.evaluation.roi_net, o.candidate.candidate_id),
                )
                candidate_id = best.candidate.candidate_id
                best_candidate = best.candidate
                evaluation = best.evaluation
                exact_cost = evaluation.acquisition_cost.minor_units
                exact_value = evaluation.expected_output_value.minor_units
                exact_ev = evaluation.ev_net.minor_units
                exact_roi = evaluation.roi_net
                approved = best.approved
                input_listings = tuple(
                    _listing_line(item.listing) for item in best.candidate.inputs
                )
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
                candidate=best_candidate,
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

    standing_orders, order_watch_error = _load_standing_orders(settings)
    # Ceilings measured from exact listings this batch beat sweep estimates.
    # Two confirmations of one lead keep the lower ceiling — the pessimistic one.
    confirmed_unit_ceilings: dict[tuple[str, str, str], Money] = {}
    for outcome in outcomes:
        if outcome.assessment is None:
            continue
        lead_key = (
            outcome.prospect.collection_id,
            outcome.prospect.quality.value,
            outcome.prospect.input_wear.value,
        )
        unit_ceiling = outcome.assessment.target_unit
        existing = confirmed_unit_ceilings.get(lead_key)
        if existing is None or unit_ceiling < existing:
            confirmed_unit_ceilings[lead_key] = unit_ceiling
    order_verdicts = (
        review_standing_orders(
            standing_orders,
            prospects,
            target_roi=settings.final_min_net_roi,
            confirmed_unit_ceilings=confirmed_unit_ceilings,
        )
        if standing_orders
        else ()
    )
    attention = tuple(verdict for verdict in order_verdicts if verdict.needs_attention)
    order_alert_path = (
        _write_order_alert(attention, settings=settings, moment=recorded_at) if attention else None
    )

    entry_hits = tuple(
        outcome
        for outcome in outcomes
        if outcome.approved and outcome.assessment is not None and outcome.assessment.entry_met
    )

    # Attempt automated execution on every entry hit. With no buyer configured
    # (or live execution off) each attempt yields typed refusals, and those
    # refusals are reported: zero buys is a result, never a silence.
    executions: list[tuple[str, ExecutionResult]] = []
    if entry_hits:
        buyer = auto_buyer or AutoBuyer(
            settings=settings, adapters={}, reservations=ReservationRegistry()
        )
        for hit in entry_hits:
            if hit.candidate is None:
                continue
            attempt = asyncio.run(buyer.execute_candidate(hit.candidate, moment=recorded_at))
            executions.extend((hit.candidate.candidate_id, result) for result in attempt)

    proposed_orders_path = (
        _write_proposed_orders(entry_hits, settings=settings, moment=recorded_at)
        if entry_hits
        else None
    )
    alert_path = (
        _write_entry_alert(
            entry_hits,
            executions=tuple(executions),
            proposed_orders=proposed_orders_path,
            settings=settings,
            moment=recorded_at,
        )
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
        executions=tuple(executions),
        order_verdicts=order_verdicts,
        order_alert_path=order_alert_path,
        order_watch_error=order_watch_error,
        proposed_orders_path=proposed_orders_path,
    )


def _load_standing_orders(
    settings: Settings,
) -> tuple[tuple[StandingOrder, ...], str | None]:
    """Operator-recorded standing orders, or a loud error. Never a silent skip.

    The file is operator-maintained state: the batch proposes parameters, the
    operator places the orders at the venue by hand and copies the proposal to
    ``artifacts/orders/standing-orders.json`` to enable revalidation. A file
    that exists but cannot be parsed is reported as an error — an unreviewed
    standing order is unpriced risk, not a detail to skip quietly.
    """
    path = settings.artifacts_dir / "orders" / "standing-orders.json"
    if not path.exists():
        return (), None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        orders = tuple(
            StandingOrder(
                collection_id=entry["collection_id"],
                quality=QualityType(entry["quality"]),
                input_wear=WearCondition(entry["input_wear"]),
                market_hash_name=entry["market_hash_name"],
                units=int(entry["units"]),
                max_price=Money(
                    int(entry["max_price_minor"]),
                    Currency(entry["currency"]),
                    BalanceType.CASH_WITHDRAWABLE,
                ),
                placed_at=datetime.fromisoformat(entry["placed_at"]),
                source_candidate_id=entry.get("source_candidate_id"),
            )
            for entry in payload["orders"]
        )
    except (KeyError, TypeError, ValueError) as exc:
        return (), f"{path}: {exc!r} — standing orders were NOT reviewed this batch"
    return orders, None


def _write_proposed_orders(
    hits: Sequence[ConfirmationOutcome],
    *,
    settings: Settings,
    moment: datetime,
) -> Path | None:
    """Machine-readable buy-order parameters matching the alert card."""
    entries: list[dict[str, object]] = []
    for outcome in hits:
        assessment = outcome.assessment
        if assessment is None:
            continue
        for plan in outcome.prospect.inputs:
            entries.append(
                {
                    "collection_id": outcome.prospect.collection_id,
                    "quality": outcome.prospect.quality.value,
                    "input_wear": outcome.prospect.input_wear.value,
                    "market_hash_name": plan.market_hash_name,
                    "units": plan.units,
                    "max_price_minor": assessment.target_unit.minor_units,
                    "currency": assessment.target_unit.currency.value,
                    "placed_at": moment.isoformat(),
                    "source_candidate_id": outcome.candidate_id,
                }
            )
    if not entries:
        return None
    alert_dir = settings.artifacts_dir / "alerts"
    alert_dir.mkdir(parents=True, exist_ok=True)
    path = alert_dir / f"standing-orders-proposed-{moment.strftime('%Y%m%dT%H%M%SZ')}.json"
    payload = {
        "note": (
            "PROPOSED buy-order parameters, not placed orders. Place them at the "
            "venue by hand, adjust placed_at, and copy this file to "
            "artifacts/orders/standing-orders.json so every batch revalidates "
            "the standing prices against fresh exit evidence."
        ),
        "orders": entries,
    }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


def _write_order_alert(
    verdicts: Sequence[OrderVerdict],
    *,
    settings: Settings,
    moment: datetime,
) -> Path:
    """Operator notice: standing orders whose economics no longer hold."""
    alert_dir = settings.artifacts_dir / "alerts"
    alert_dir.mkdir(parents=True, exist_ok=True)
    path = alert_dir / f"order-alert-{moment.strftime('%Y%m%dT%H%M%SZ')}.md"
    lines = [
        "# ORDER ALERT -- standing buy orders need attention",
        "",
        f"Generated {moment.isoformat()} from the current catalogue sweep.",
        "Estimate-derived (the sweep's priors apply); re-confirm against exact",
        "listings before acting. Nothing was bought or cancelled here -- order",
        "changes are operator actions at the venue.",
        "",
    ]
    for verdict in verdicts:
        order = verdict.order
        lines.append(
            f"- `{order.market_hash_name}` x{order.units} (standing {order.max_price}): "
            f"{verdict.status.value} -- {verdict.detail}"
        )
    lines.append("")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _buy_order_lines(outcome: ConfirmationOutcome) -> list[str]:
    """Ready-to-enter standing buy-order parameters for this lead.

    CSFloat executes buy orders venue-side, before public listing; *placing*
    them is a one-time manual step because no placement API is documented.
    The per-unit ceiling is the entry cap: if every fill lands at or below
    it, the bundle total clears the ROI floor. Prices assume the adapter's
    CSFloat fee model (buyer pays sticker; the seller pays the sale fee) —
    re-verify at the venue before funding orders.
    """
    assessment = outcome.assessment
    if assessment is None:
        return []
    prospect = outcome.prospect
    band = next(
        ((low, high) for wear, low, high in WEAR_BOUNDS if wear is prospect.input_wear),
        None,
    )
    band_note = f", float {band[0]}-{band[1]}" if band is not None else ""
    lines = [
        "### Standing buy orders (venue-executed acquisition)",
        "",
        "Place once by hand on the venue; the venue then fills automatically",
        "before public listing. Enter **at most** the ceiling below — fills at",
        "any price up to it keep the bundle inside the entry cap:",
        "",
    ]
    for plan in prospect.inputs:
        lines.append(
            f"- `{plan.market_hash_name}` x{plan.units}: "
            f"max price {assessment.target_unit}{band_note}"
        )
    lines.append("")
    lines.append("Fills accumulate as inventory (TARGET_ACCUMULATION); accept each trade")
    lines.append("offer promptly. Expect fills anywhere in the float band — the estimate")
    lines.append("assumed mid-band. Ceiling assumes buyer pays sticker only;")
    lines.append("re-verify the buyer-side fee at the venue before funding orders.")
    lines.append("")
    return lines


def _listing_line(listing: MarketplaceListing) -> str:
    """Listing summary plus a direct venue link where one is documented.

    The link turns the operator flow into notification -> click -> buy; the
    purchase click and the Steam trade-offer acceptance stay human.
    """
    url = venue_listing_url(listing.identity.venue, listing.identity.listing_id)
    return f"{listing} -> {url}" if url else str(listing)


def _write_entry_alert(
    hits: Sequence[ConfirmationOutcome],
    *,
    executions: tuple[tuple[str, ExecutionResult], ...] = (),
    proposed_orders: Path | None = None,
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
        if assessment is not None:
            lines.extend(_buy_order_lines(outcome))
    if executions:
        lines.append("## Automated execution attempt")
        lines.append("")
        for candidate_id, execution in executions:
            lines.append(
                f"- `{candidate_id}` intent `{execution.intent_id}`: "
                f"{execution.status.value} -- {execution.detail}"
            )
        lines.append("")
    if proposed_orders is not None:
        active = settings.artifacts_dir / "orders" / "standing-orders.json"
        lines.append(f"Machine-readable order parameters: `{proposed_orders}`.")
        lines.append(f"After placing the orders at the venue, copy that file to `{active}`")
        lines.append("so every future batch revalidates your standing prices against")
        lines.append("fresh exit evidence and alerts you on decay.")
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
