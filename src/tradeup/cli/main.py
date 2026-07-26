"""``tradeup`` command line.

Every command that could conceivably spend money prints the execution boundary
before doing anything, and there is no command that places an order. ``doctor``
exists so the first question after a fresh checkout -- "is any of this actually
wired up?" -- has a one-line answer.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

import typer

from tradeup.clock import SystemClock
from tradeup.config import Settings, load_settings
from tradeup.demo.runner import default_metadata_path, run_demo
from tradeup.domain.items import Rarity
from tradeup.domain.money import Currency
from tradeup.domain.rules import DEFAULT_RULE_REGISTRY
from tradeup.metadata.bymykel import load_pinned_snapshot
from tradeup.metadata.registry import IssueSeverity
from tradeup.persistence.database import create_database
from tradeup.persistence.repositories import LedgerRepository, ListingRepository

app = typer.Typer(
    name="tradeup",
    help="Exact-asset CS2 trade-up procurement agent (shadow mode).",
    no_args_is_help=True,
    add_completion=False,
)
metadata_app = typer.Typer(help="Pinned item metadata.", no_args_is_help=True)
listings_app = typer.Typer(help="Marketplace listings.", no_args_is_help=True)
rules_app = typer.Typer(help="Trade-up rule registry.", no_args_is_help=True)
candidates_app = typer.Typer(help="Candidate contracts.", no_args_is_help=True)
ledger_app = typer.Typer(help="Economic ledger.", no_args_is_help=True)

app.add_typer(metadata_app, name="metadata")
app.add_typer(listings_app, name="listings")
app.add_typer(rules_app, name="rules")
app.add_typer(candidates_app, name="candidates")
app.add_typer(ledger_app, name="ledger")


def _settings() -> Settings:
    return load_settings()


def _now() -> datetime:
    return SystemClock().now()


def _echo_boundary(settings: Settings) -> None:
    typer.echo(
        f"execution: live={settings.live_execution_enabled} "
        f"daily_spend_limit={settings.max_daily_spend} "
        "(no command in this CLI places an order)"
    )


# ---------------------------------------------------------------------------
# metadata
# ---------------------------------------------------------------------------


@metadata_app.command("import")
def metadata_import(
    path: Annotated[Path | None, typer.Option(help="Pinned snapshot to import.")] = None,
) -> None:
    """Import the pinned metadata snapshot and report its provenance."""
    snapshot = path or default_metadata_path()
    result = load_pinned_snapshot(snapshot, imported_at=_now())
    for key, value in result.summary().items():
        typer.echo(f"{key:<24} {value}")
    if result.errors:
        typer.echo(f"\n{len(result.errors)} error(s):")
        for issue in result.errors[:20]:
            typer.echo(f"  {issue}")
        raise typer.Exit(1)


@metadata_app.command("validate")
def metadata_validate(
    path: Annotated[Path | None, typer.Option(help="Pinned snapshot to validate.")] = None,
    show_warnings: Annotated[bool, typer.Option(help="Print warnings too.")] = False,
) -> None:
    """Validate the registry. Exits non-zero on any ERROR-level issue."""
    snapshot = path or default_metadata_path()
    result = load_pinned_snapshot(snapshot, imported_at=_now())
    errors = result.errors
    warnings = result.warnings
    typer.echo(
        f"{result.registry.skin_count} skins, {result.registry.collection_count} collections, "
        f"{len(errors)} errors, {len(warnings)} warnings"
    )
    if show_warnings:
        for issue in warnings[:50]:
            typer.echo(f"  {issue}")
    for issue in errors:
        typer.echo(f"  {issue}")
    if errors:
        raise typer.Exit(1)
    typer.echo("metadata registry is valid")


# ---------------------------------------------------------------------------
# rules
# ---------------------------------------------------------------------------


@rules_app.command("validate")
def rules_validate() -> None:
    """Show every rule version, its validity window and its validation status."""
    for ruleset in DEFAULT_RULE_REGISTRY.all_rulesets:
        until = ruleset.effective_until.isoformat() if ruleset.effective_until else "open"
        typer.echo(f"{ruleset.rule_version}")
        typer.echo(f"  effective       {ruleset.effective_from.isoformat()} -> {until}")
        typer.echo(f"  validation      {ruleset.validation_status.value}")
        typer.echo(
            "  input counts    "
            + ", ".join(
                f"{r.value}={ruleset.input_count_by_rarity[r]}"
                for r in ruleset.eligible_input_rarities
            )
        )
        typer.echo(f"  float method    {ruleset.float_method.value}")
        typer.echo(f"  prob method     {ruleset.probability_method.value}")
        typer.echo(f"  sources         {'; '.join(ruleset.source_references) or 'none'}")
    active = DEFAULT_RULE_REGISTRY.resolve(_now())
    typer.echo(f"\nactive now: {active.rule_version}")
    if not active.validation_status.approvable:
        typer.echo("WARNING: the active ruleset is UNVERIFIED and cannot approve candidates")
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# listings
# ---------------------------------------------------------------------------


@listings_app.command("ingest")
def listings_ingest(
    demo: Annotated[bool, typer.Option(help="Ingest the offline demo scenario.")] = True,
) -> None:
    """Ingest listings into the database. Offline demo scenario by default."""
    settings = _settings()
    _echo_boundary(settings)
    if not demo:
        typer.echo(
            "live ingestion requires credentials and is exercised by 'make live-smoke'; "
            "no live source is configured here"
        )
        raise typer.Exit(0)

    from tradeup.demo.scenario import build_demo_scenario

    now = _now()
    registry = load_pinned_snapshot(default_metadata_path(), imported_at=now).registry
    scenario = build_demo_scenario(registry, now=now)
    database = create_database(settings.database_url)
    database.create_all()
    with database.session() as session:
        count = ListingRepository(session).upsert_many(scenario.listings)
    database.dispose()
    typer.echo(f"ingested {count} synthetic demo listings (idempotent on venue+listing_id)")


@listings_app.command("verify")
def listings_verify() -> None:
    """Revalidate stored listings against the demo scenario's scripted state."""
    settings = _settings()
    now = _now()
    from tradeup.demo.scenario import build_demo_scenario

    registry = load_pinned_snapshot(default_metadata_path(), imported_at=now).registry
    scenario = build_demo_scenario(registry, now=now)

    async def _verify() -> tuple[int, int]:
        active = gone = 0
        for adapter in scenario.adapters:
            for listing in adapter.scenario.listings:
                result = await adapter.verify_listing(listing.identity, moment=now)
                if result.ok and result.unwrap().is_active:
                    active += 1
                else:
                    gone += 1
        return active, gone

    active, gone = asyncio.run(_verify())
    typer.echo(f"revalidated: {active} active, {gone} unavailable")
    typer.echo(f"database: {settings.database_url}")


# ---------------------------------------------------------------------------
# candidates
# ---------------------------------------------------------------------------


@candidates_app.command("scan")
def candidates_scan(
    rarity: Annotated[str, typer.Option(help="Input rarity to scan.")] = "MIL_SPEC",
) -> None:
    """Run a shadow scan over the offline scenario and print the ranked table."""
    settings = _settings()
    _echo_boundary(settings)
    result = run_demo(settings=settings, now=_now(), input_rarity=Rarity(rarity))
    typer.echo(result.console_output)
    typer.echo(
        f"\n{result.approved_count} approved, {result.rejected_count} rejected. "
        f"Settled fee-net profit: $0.00 (nothing was bought)."
    )


@candidates_app.command("prospects")
def candidates_prospects(
    top: Annotated[int, typer.Option(help="How many ranked prospects to print.")] = 15,
    ask_haircut: Annotated[
        str | None,
        typer.Option(help="Override the ask haircut (e.g. from 'candidates calibration')."),
    ] = None,
    output: Annotated[Path | None, typer.Option(help="Directory for artifacts.")] = None,
) -> None:
    """Catalog-wide trade-up prospect sweep from documented, keyless price data.

    One Skinport /v1/items request prices the whole search space; every
    (collection, rarity, quality, wear) contract sketch is evaluated with exact
    probabilities and fee-net estimates over those asks. Prospects are LEADS for
    the exact-listing scanner, never executable truth.
    """
    from decimal import Decimal

    import httpx

    from tradeup.adapters.http import HttpxTransport, RestClient, RetryPolicy
    from tradeup.adapters.skinport import SkinportPriceSource
    from tradeup.discovery.prospects import ProspectPolicy, sweep_prospects
    from tradeup.reporting.renderers import write_json
    from tradeup.valuation.venue_fees import build_live_fee_schedule

    settings = _settings()
    _echo_boundary(settings)
    typer.echo(
        "REFERENCE-LEVEL sweep over Skinport asks. Estimates assume in-band input "
        "floats and unconfirmed prices; nothing here is executable truth.\n"
    )
    now = _now()
    registry = load_pinned_snapshot(default_metadata_path(), imported_at=now).registry
    ruleset = DEFAULT_RULE_REGISTRY.resolve(now)

    async def _fetch() -> Any:
        async with httpx.AsyncClient() as client:
            rest = RestClient(
                transport=HttpxTransport(client, clock=lambda: datetime.now(UTC)),
                user_agent=settings.http_user_agent,
                timeout_seconds=float(settings.http_timeout_seconds),
                retry=RetryPolicy(max_attempts=settings.http_max_retries),
            )
            return await SkinportPriceSource(rest).fetch_item_quotes(moment=now)

    result = asyncio.run(_fetch())
    if not result.ok:
        typer.echo(f"sweep blocked: {result.status.value} ({result.detail})")
        raise typer.Exit(1)
    quotes = result.unwrap()
    typer.echo(f"priced catalogue: {result.detail}")

    policy = ProspectPolicy(ask_haircut=Decimal(ask_haircut)) if ask_haircut is not None else None
    if policy is not None:
        typer.echo(f"ask haircut override: {policy.ask_haircut} (from calibration)")
    prospects, statistics = sweep_prospects(
        registry=registry,
        ruleset=ruleset,
        quotes=quotes,
        fee_schedule=build_live_fee_schedule(),
        base_currency=settings.base_currency,
        moment=now,
        policy=policy,
        max_cost=settings.max_contract_cost,
    )

    typer.echo("\nSWEEP CENSUS")
    for key, value in statistics.summary().items():
        typer.echo(f"  {key:<28} {value}")

    typer.echo(f"\nTOP {min(top, len(prospects))} PROSPECTS (by estimated ROI)")
    header = (
        f"{'Collection':<38}{'Rarity':<12}{'Quality':<10}{'Wear':<16}"
        f"{'Cost':>10}{'EV':>10}{'ROI':>9}"
    )
    typer.echo(header)
    typer.echo("-" * len(header))
    for prospect in prospects[:top]:
        typer.echo(
            f"{prospect.collection_name[:36]:<38}"
            f"{prospect.input_rarity.value:<12}"
            f"{prospect.quality.value:<10}"
            f"{prospect.input_wear.value:<16}"
            f"{prospect.estimated_cost.as_major()!s:>10}"
            f"{prospect.estimated_ev.as_major()!s:>10}"
            f"{str(prospect.roi_percent) + '%':>9}"
        )

    evidence_dir = output or settings.reports_dir
    evidence_dir.mkdir(parents=True, exist_ok=True)
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    artifact = write_json(
        evidence_dir / f"prospects-{stamp}.json",
        {
            "swept_at": now.isoformat(),
            "data_nature": (
                "REFERENCE ESTIMATES from Skinport asks; assumed in-band input floats; "
                "not executable truth. Confirm with candidates scan-live."
            ),
            "rule_version": ruleset.rule_version,
            "census": statistics.summary(),
            "prospects": [p.to_dict() for p in prospects[:100]],
        },
    )
    typer.echo(f"\nartifact   {artifact}")
    if prospects:
        best = prospects[0]
        cheapest = min(plan.unit_price.minor_units for plan in best.inputs)
        dearest = max(plan.unit_price.minor_units for plan in best.inputs)
        typer.echo(
            "\nConfirm the top prospect against exact listings, for example:\n"
            f"  tradeup candidates scan-live --rarity {best.input_rarity.value} "
            f"--min-price-minor {max(cheapest - 50, 1)} "
            f"--max-price-minor {dearest + 100} --limit 120"
        )


def _echo_calibration(calibration: Any, history_rows: int) -> None:
    from tradeup.discovery.calibration import MIN_RELIABLE_SAMPLES

    typer.echo(f"\nCALIBRATION (over {history_rows} recorded confirmations)")
    if not calibration.has_data:
        typer.echo("  no CONFIRMED estimate/exact pairs recorded yet")
        return
    header = (
        f"  {'scope':<12}{'samples':>8}{'value_ratio':>13}{'roi_gap':>10}"
        f"{'rec_haircut':>13}{'reliable':>10}"
    )
    typer.echo(header)
    rows = [("ALL", calibration.overall)] + [
        (entry.quality.value, entry) for entry in calibration.by_quality
    ]
    for label, entry in rows:
        summary = entry.summary()
        typer.echo(
            f"  {label:<12}{summary['samples']:>8}{summary['median_value_ratio']:>13}"
            f"{summary['median_roi_gap']:>10}{summary['recommended_ask_haircut']:>13}"
            f"{summary['reliable']:>10}"
        )
    typer.echo(
        f"  recommendations with fewer than {MIN_RELIABLE_SAMPLES} samples are anecdotes; "
        "apply one explicitly via 'candidates prospects --ask-haircut <value>'"
    )


@candidates_app.command("confirm-batch")
def candidates_confirm_batch(
    top: Annotated[int, typer.Option(help="Distinct leads to confirm this batch.")] = 5,
    min_roi: Annotated[
        str, typer.Option(help="Only confirm leads at or above this estimated ROI.")
    ] = "0",
    per_name_limit: Annotated[
        int, typer.Option(help="Exact listings to fetch per input name.")
    ] = 15,
    max_candidates: Annotated[int, typer.Option(help="Cap on candidates per lead.")] = 5,
    output: Annotated[Path | None, typer.Option(help="Directory for artifacts.")] = None,
) -> None:
    """Sweep, confirm the top distinct leads against exact listings, calibrate.

    Every lead's estimate/exact pair is persisted append-only; the calibration
    report at the end covers the entire recorded history, not just this batch.
    """
    from decimal import Decimal

    from tradeup.pipeline.confirm_batch import run_confirmation_batch
    from tradeup.pipeline.live_scan import LiveScanError

    settings = _settings()
    _echo_boundary(settings)
    typer.echo(
        "LIVE read-only confirm-and-calibrate batch. Estimates come from asks; "
        "confirmations run on exact purchasable listings and exact floats.\n"
    )
    try:
        result = run_confirmation_batch(
            settings=settings,
            clock=_now,
            top=top,
            min_estimated_roi=Decimal(min_roi),
            per_name_limit=per_name_limit,
            max_candidates=max_candidates,
            output_dir=output,
        )
    except LiveScanError as exc:
        typer.echo(f"batch blocked: {exc}")
        raise typer.Exit(1) from exc

    typer.echo("SWEEP CENSUS")
    for key, value in result.sweep_statistics.summary().items():
        typer.echo(f"  {key:<28} {value}")

    typer.echo(f"\nCONFIRMATIONS ({len(result.outcomes)} leads)")
    header = (
        f"  {'Collection':<38}{'Quality':<10}{'Wear':<16}{'Est ROI':>10}"
        f"{'Exact ROI':>11}  {'Status':<12}{'Reasons'}"
    )
    typer.echo(header)
    typer.echo("  " + "-" * (len(header) + 8))
    for outcome in result.outcomes:
        row = outcome.summary_row()
        typer.echo(
            f"  {row['collection'][:36]:<38}{row['quality']:<10}{row['wear']:<16}"
            f"{row['est_roi']:>10}{row['exact_roi']:>11}  {row['status']:<12}{row['reasons']}"
        )

    _echo_calibration(result.calibration, result.history_rows)
    typer.echo("\nNothing was bought; every confirmation is a read-only measurement.")


@candidates_app.command("calibration")
def candidates_calibration() -> None:
    """Report the recorded estimate-versus-executable gap distribution."""
    from tradeup.discovery.calibration import build_calibration_report
    from tradeup.persistence.repositories import ConfirmationRepository
    from tradeup.pipeline.confirm_batch import calibration_pairs

    settings = _settings()
    database = create_database(settings.database_url)
    database.create_all()
    with database.session() as session:
        rows = ConfirmationRepository(session).all_rows()
    database.dispose()
    report = build_calibration_report(calibration_pairs(rows))
    _echo_calibration(report, len(rows))


@candidates_app.command("confirm")
def candidates_confirm(
    rank: Annotated[int, typer.Option(help="1-based rank in the prospects artifact.")] = 1,
    artifact: Annotated[
        Path | None, typer.Option(help="Prospects JSON; defaults to the newest one.")
    ] = None,
    per_name_limit: Annotated[
        int, typer.Option(help="Exact listings to fetch per input name.")
    ] = 15,
    max_candidates: Annotated[int, typer.Option(help="Cap on candidates evaluated.")] = 10,
    output: Annotated[Path | None, typer.Option(help="Directory for artifacts.")] = None,
    quiet: Annotated[bool, typer.Option(help="Only print the summary.")] = False,
) -> None:
    """Confirm one swept prospect against exact live listings with exact floats.

    Fetches real buy-now listings for exactly the prospect's input names (mixed
    collections included), then runs the full pipeline: exact floats, exact
    probabilities, fee-net EV, risk gates and direct revalidation.
    """
    from tradeup.domain.items import QualityType
    from tradeup.pipeline.live_scan import LiveScanError, run_live_scan
    from tradeup.reporting.renderers import render_card_console, render_ranked_table

    settings = _settings()
    _echo_boundary(settings)

    source = artifact
    if source is None:
        candidates_files = sorted(settings.reports_dir.glob("prospects-*.json"))
        if not candidates_files:
            typer.echo("no prospects artifact found; run 'tradeup candidates prospects' first")
            raise typer.Exit(1)
        source = candidates_files[-1]
    payload = json.loads(source.read_text(encoding="utf-8"))
    prospects = payload.get("prospects", [])
    if not 1 <= rank <= len(prospects):
        typer.echo(f"artifact {source.name} has {len(prospects)} prospects; rank {rank} invalid")
        raise typer.Exit(1)
    prospect = prospects[rank - 1]

    names = [entry["market_hash_name"] for entry in prospect["inputs"]]
    quality = QualityType(prospect["quality"])
    rarity = Rarity(prospect["input_rarity"])
    typer.echo(
        f"confirming prospect #{rank} from {source.name}: "
        f"{prospect['collection_name']}"
        + (
            f" + {prospect['filler_collection_name']}"
            if prospect.get("filler_collection_name")
            else ""
        )
        + f" ({quality.value}, {prospect['input_wear']}, "
        f"estimated ROI {prospect['estimated_roi'][:8]})"
    )
    typer.echo(
        "LIVE read-only confirmation. The estimate above came from asks; the gates "
        "below run on exact purchasable listings and exact floats.\n"
    )
    try:
        result = run_live_scan(
            settings=settings,
            now=_now(),
            input_rarity=rarity,
            quality=quality,
            target_names=names,
            per_name_limit=per_name_limit,
            max_candidates=max_candidates,
            output_dir=output,
        )
    except LiveScanError as exc:
        typer.echo(f"confirmation blocked: {exc}")
        raise typer.Exit(1) from exc

    if not quiet:
        typer.echo(render_ranked_table(list(result.cards)))
        for card in result.cards:
            typer.echo("")
            typer.echo(render_card_console(card))

    if result.acquisition_reference:
        typer.echo(
            "\nCROSS-MARKET ACQUISITION REFERENCE (name-level asks; floats unknown; "
            "purchase is operator-manual; buy-side payment fees UNVERIFIED)"
        )
        for ref in result.acquisition_reference:
            min_ask = (
                f"{ref.min_ask.as_major()} {ref.min_ask.currency.value}"
                if ref.min_ask is not None
                else "none listed"
            )
            median = (
                f"{ref.median_ask.as_major()} {ref.median_ask.currency.value}"
                if ref.median_ask is not None
                else "none listed"
            )
            typer.echo(
                f"  {ref.market_hash_name}  [{ref.venue}]  "
                f"min ask {min_ask}  median {median}  x{ref.quantity} available"
            )
        typer.echo(f"  ({result.acquisition_reference_detail}; reference only, not gate input)")

    stats = result.report.statistics
    typer.echo("\nSCAN STATISTICS")
    for key, value in stats.summary().items():
        typer.echo(f"  {key:<26} {value}")
    typer.echo(f"  listings_fetch             {result.listings_fetch_detail[:160]}")
    typer.echo(f"  output_pricing             {result.observations_detail}")

    typer.echo("\nARTIFACTS")
    for kind, path in sorted(result.artifacts.items()):
        typer.echo(f"  {kind:<10} {path}")

    typer.echo(
        f"\n{result.approved_count} candidate(s) cleared all gates, "
        f"{result.rejected_count} rejected. Nothing was bought; this is a measurement."
    )


@candidates_app.command("scan-live")
def candidates_scan_live(
    rarity: Annotated[str, typer.Option(help="Input rarity to scan.")] = "MIL_SPEC",
    limit: Annotated[int, typer.Option(help="Maximum live listings to ingest.")] = 150,
    max_candidates: Annotated[
        int, typer.Option(help="Cap on candidates; each revalidation costs API budget.")
    ] = 12,
    min_price_minor: Annotated[
        int | None, typer.Option(help="Only ingest listings at or above this price (cents).")
    ] = None,
    max_price_minor: Annotated[
        int | None, typer.Option(help="Only ingest listings at or below this price (cents).")
    ] = None,
    output: Annotated[Path | None, typer.Option(help="Directory for artifacts.")] = None,
    quiet: Annotated[bool, typer.Option(help="Only print the summary.")] = False,
) -> None:
    """Read-only shadow scan against the live market. Requires a CSFloat API key."""
    from tradeup.domain.money import Money
    from tradeup.pipeline.live_scan import LiveScanError, run_live_scan
    from tradeup.reporting.renderers import render_card_console, render_ranked_table

    settings = _settings()
    _echo_boundary(settings)
    typer.echo(
        "LIVE read-only shadow scan. Fee figures come from dated public statements "
        "and are NOT re-verified at the venue; nothing here is a buy instruction.\n"
    )
    try:
        result = run_live_scan(
            settings=settings,
            now=_now(),
            input_rarity=Rarity(rarity),
            listing_limit=limit,
            max_candidates=max_candidates,
            min_price=(
                Money(min_price_minor, settings.base_currency)
                if min_price_minor is not None
                else None
            ),
            max_price=(
                Money(max_price_minor, settings.base_currency)
                if max_price_minor is not None
                else None
            ),
            output_dir=output,
        )
    except LiveScanError as exc:
        typer.echo(f"scan blocked: {exc}")
        raise typer.Exit(1) from exc

    if not quiet:
        typer.echo(render_ranked_table(list(result.cards)))
        for card in result.cards:
            typer.echo("")
            typer.echo(render_card_console(card))

    stats = result.report.statistics
    typer.echo("\nSCAN STATISTICS")
    for key, value in stats.summary().items():
        typer.echo(f"  {key:<26} {value}")
    typer.echo(f"  listings_fetch             {result.listings_fetch_detail}")
    typer.echo(f"  output_pricing             {result.observations_detail}")
    typer.echo(
        f"  collections priced/skipped {len(result.collections_priced)}"
        f"/{len(result.collections_skipped)}"
    )

    typer.echo("\nARTIFACTS")
    for kind, path in sorted(result.artifacts.items()):
        typer.echo(f"  {kind:<10} {path}")

    typer.echo(
        f"\n{result.approved_count} candidate(s) cleared all gates, "
        f"{result.rejected_count} rejected. Nothing was bought; this is a measurement."
    )


@candidates_app.command("explain")
def candidates_explain(contract_id: str) -> None:
    """Explain one candidate: selection, constraints and search space."""
    settings = _settings()
    result = run_demo(settings=settings, now=_now())
    for outcome in result.report.all_outcomes:
        if outcome.candidate.candidate_id != contract_id:
            continue
        explanation = outcome.solution.explanation
        typer.echo(f"contract {contract_id}")
        for key, value in explanation.summary().items():
            typer.echo(f"  {key:<26} {value}")
        typer.echo(f"  decision                   {'APPROVED' if outcome.approved else 'REJECTED'}")
        if outcome.rejection is not None:
            typer.echo(
                f"  reasons                    "
                f"{', '.join(r.value for r in outcome.rejection.reasons)}"
            )
        typer.echo("  selected listings:")
        for item in outcome.candidate.inputs:
            typer.echo(
                f"    {item.identity}  float={item.listing.raw_float} "
                f"z={item.normalized}  cost={item.acquisition_cost}"
            )
        return
    typer.echo(f"no candidate {contract_id} in this scan")
    raise typer.Exit(1)


@candidates_app.command("export")
def candidates_export(
    output: Annotated[Path | None, typer.Option(help="Directory for artifacts.")] = None,
) -> None:
    """Run a scan and write JSON, CSV, Parquet and Markdown artifacts."""
    settings = _settings()
    result = run_demo(settings=settings, now=_now(), output_dir=output)
    for kind, path in sorted(result.artifacts.items()):
        typer.echo(f"{kind:<10} {path}")


# ---------------------------------------------------------------------------
# ledger
# ---------------------------------------------------------------------------


@ledger_app.command("reconcile")
def ledger_reconcile() -> None:
    """Report settled, withdrawable cash. The only number that counts as profit."""
    settings = _settings()
    database = create_database(settings.database_url)
    database.create_all()
    with database.session() as session:
        repository = LedgerRepository(session)
        count = repository.count()
        settled = repository.settled_cash_total(settings.base_currency)
    database.dispose()
    typer.echo(f"ledger events        {count}")
    typer.echo(f"settled cash P&L     {settled}")
    if count == 0:
        typer.echo("No economic events recorded. Nothing has been bought, contracted or sold.")


# ---------------------------------------------------------------------------
# demo and doctor
# ---------------------------------------------------------------------------


@app.command("demo")
def demo(
    output: Annotated[Path | None, typer.Option(help="Directory for artifacts.")] = None,
    quiet: Annotated[bool, typer.Option(help="Only print the summary.")] = False,
) -> None:
    """Deterministic offline end-to-end run. No network, no orders."""
    # The demo always exercises the crypto settlement rail on synthetic values,
    # regardless of the environment, so the offline slice covers the whole money
    # path and stays byte-comparable between runs.
    settings = load_settings(
        crypto_settlement_enabled=True,
        settlement_currency=Currency.BTC,
    )
    _echo_boundary(settings)
    typer.echo("SYNTHETIC demo data over REAL pinned metadata. Not market evidence.\n")

    # A fixed instant so repeated runs are byte-comparable apart from filenames.
    now = datetime(2026, 7, 25, 12, 0, 0, tzinfo=UTC)
    result = run_demo(settings=settings, now=now, output_dir=output)

    if not quiet:
        typer.echo(result.console_output)

    stats = result.report.statistics
    typer.echo("\nSCAN STATISTICS")
    for key, value in stats.summary().items():
        typer.echo(f"  {key:<26} {value}")

    typer.echo("\nECONOMIC EVIDENCE")
    typer.echo(f"  settled fee-net profit     $0.00 ({result.ledger_event_count} ledger events)")
    typer.echo("  orders placed              0")
    typer.echo("  trade-ups completed        0")
    typer.echo("  sales settled              0")

    typer.echo("\nARTIFACTS")
    for kind, path in sorted(result.artifacts.items()):
        typer.echo(f"  {kind:<10} {path}")

    typer.echo(
        f"\n{result.approved_count} candidate(s) cleared all gates, "
        f"{result.rejected_count} rejected."
    )


@app.command("doctor")
def doctor() -> None:
    """Check that the environment, metadata, rules and database are wired up."""
    settings = _settings()
    problems: list[str] = []

    typer.echo("EXECUTION BOUNDARY")
    typer.echo(f"  live_execution_enabled     {settings.live_execution_enabled}")
    typer.echo(f"  max_daily_spend            {settings.max_daily_spend}")
    if settings.live_execution_enabled:
        problems.append("live execution is ENABLED; groundwork expects it off")

    typer.echo("\nMETADATA")
    try:
        snapshot = default_metadata_path()
        result = load_pinned_snapshot(snapshot, imported_at=_now())
        typer.echo(f"  snapshot                   {snapshot.name}")
        typer.echo(f"  revision                   {result.registry.revision}")
        typer.echo(f"  payload sha256             {result.registry.payload_sha256[:16]}...")
        typer.echo(
            f"  skins / collections        {result.registry.skin_count} / "
            f"{result.registry.collection_count}"
        )
        errors = [i for i in result.issues if i.severity is IssueSeverity.ERROR]
        typer.echo(f"  errors                     {len(errors)}")
        if errors:
            problems.append(f"{len(errors)} metadata errors")
    except (FileNotFoundError, ValueError) as exc:
        typer.echo(f"  UNAVAILABLE: {exc}")
        problems.append("metadata snapshot unavailable")

    typer.echo("\nRULES")
    try:
        active = DEFAULT_RULE_REGISTRY.resolve(_now())
        typer.echo(f"  active version             {active.rule_version}")
        typer.echo(f"  validation status          {active.validation_status.value}")
        if not active.validation_status.approvable:
            problems.append("active ruleset is UNVERIFIED")
    except Exception as exc:
        typer.echo(f"  UNRESOLVED: {exc}")
        problems.append("no ruleset covers the current time")

    typer.echo("\nDATABASE")
    try:
        database = create_database(settings.database_url)
        database.create_all()
        with database.session() as session:
            listings = ListingRepository(session).count()
            events = LedgerRepository(session).count()
        database.dispose()
        typer.echo(f"  url                        {settings.database_url}")
        typer.echo(f"  listings / ledger events   {listings} / {events}")
    except Exception as exc:
        typer.echo(f"  UNAVAILABLE: {exc}")
        problems.append("database unavailable")

    typer.echo("\nCREDENTIALS (presence only; values are never printed)")
    for name, present in sorted(settings.available_credentials().items()):
        typer.echo(f"  {name:<26} {'present' if present else 'absent'}")

    typer.echo("\nGATES")
    for key, value in sorted(settings.gate_summary().items()):
        typer.echo(f"  {key:<26} {value}")

    if problems:
        typer.echo("\nPROBLEMS")
        for problem in problems:
            typer.echo(f"  - {problem}")
        raise typer.Exit(1)
    typer.echo("\nall checks passed")


@app.command("capabilities")
def capabilities() -> None:
    """Print what each adapter is permitted to do, as JSON."""
    from tradeup.adapters.manual import (
        CSMoneyManualAdapter,
        SkinSwapManualAdapter,
        SteamManualAdapter,
        skins_money_adapter,
    )

    adapters = [
        CSMoneyManualAdapter(),
        SkinSwapManualAdapter(),
        SteamManualAdapter(),
        skins_money_adapter(),
    ]
    typer.echo(json.dumps([a.describe() for a in adapters], indent=2))


if __name__ == "__main__":  # pragma: no cover
    app()
