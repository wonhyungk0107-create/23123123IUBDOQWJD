"""Runs the offline demo end to end and writes the evidence artifacts.

The demo proves the software works. It proves nothing about the market, and it does
not pretend otherwise: the ledger section reports **zero events and $0.00 settled**,
because no purchase happened. Fabricating plausible purchase and sale events to make
the ledger section look alive would be manufacturing a successful result, which is
precisely the failure this project is built to avoid. Ledger mechanics are proven by
tests instead.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from tradeup.config import Settings
from tradeup.demo.scenario import (
    DEMO_FEE_SCHEDULE_ID,
    DemoScenario,
    build_demo_scenario,
    scenario_summary,
)
from tradeup.domain.items import Rarity
from tradeup.domain.rules import DEFAULT_RULE_REGISTRY, TradeupRuleSet
from tradeup.execution.policy import RiskPolicy
from tradeup.execution.reservations import ReservationRegistry
from tradeup.metadata.bymykel import load_pinned_snapshot
from tradeup.metadata.registry import MetadataRegistry
from tradeup.optimizer.bundle import BundleOptimizer
from tradeup.persistence.database import create_database
from tradeup.persistence.repositories import (
    CandidateRepository,
    LedgerRepository,
    ListingRepository,
    MetadataRepository,
    ScanRepository,
)
from tradeup.pipeline.scan import ScanConfig, ScanPipeline, ScanReport
from tradeup.reporting.operator_card import OperatorCard, build_operator_card
from tradeup.reporting.renderers import (
    render_card_console,
    render_ranked_table,
    render_scan_markdown,
    write_csv,
    write_json,
    write_parquet,
)
from tradeup.valuation.capital import CapitalModel
from tradeup.valuation.exit_prices import ExitPriceResolver
from tradeup.valuation.expected_value import ExpectedValueEngine
from tradeup.valuation.partial_fill import PartialFillModel

__all__ = ["DemoResult", "default_metadata_path", "run_demo"]


def default_metadata_path() -> Path:
    """The pinned snapshot shipped with the repository."""
    root = Path(__file__).resolve().parent.parent.parent.parent
    candidates = sorted((root / "data" / "metadata").glob("bymykel-*.json"))
    snapshots = [p for p in candidates if not p.name.endswith(".manifest.json")]
    if not snapshots:
        raise FileNotFoundError(
            f"no pinned metadata snapshot found under {root / 'data' / 'metadata'}"
        )
    return snapshots[0]


@dataclass(frozen=True)
class DemoResult:
    """Everything the demo produced."""

    report: ScanReport
    cards: tuple[OperatorCard, ...]
    scenario: DemoScenario
    registry: MetadataRegistry
    ruleset: TradeupRuleSet
    artifacts: dict[str, Path]
    settled_cash_minor: int
    ledger_event_count: int
    console_output: str

    @property
    def approved_count(self) -> int:
        return len(self.cards)

    @property
    def rejected_count(self) -> int:
        return len(self.report.rejected)


def run_demo(
    *,
    settings: Settings,
    now: datetime,
    metadata_path: Path | None = None,
    output_dir: Path | None = None,
    input_rarity: Rarity = Rarity.MIL_SPEC,
) -> DemoResult:
    """Initialise, import, scan, gate, report and persist. No network, no orders."""
    snapshot = metadata_path or default_metadata_path()
    evidence_dir = output_dir or settings.evidence_dir

    # 1. Metadata, from the pinned snapshot.
    import_result = load_pinned_snapshot(snapshot, imported_at=now)
    registry = import_result.registry
    registry.require_valid()

    # 2. Governing ruleset for this moment.
    ruleset = DEFAULT_RULE_REGISTRY.resolve(now)

    # 3. Synthetic market over that real metadata.
    scenario = build_demo_scenario(registry, now=now)

    # 4. Wire the economics.
    capital_model = CapitalModel(annual_rate=settings.annual_capital_cost_rate)
    exit_resolver = ExitPriceResolver(
        fee_schedule=scenario.fee_schedule,
        capital_model=capital_model,
        base_currency=settings.base_currency,
    )
    partial_fill = PartialFillModel(base_currency=settings.base_currency)
    ev_engine = ExpectedValueEngine(
        settings=settings, capital_model=capital_model, partial_fill_model=partial_fill
    )
    reservations = ReservationRegistry()
    pipeline = ScanPipeline(
        settings=settings,
        registry=registry,
        ruleset=ruleset,
        optimizer=BundleOptimizer(),
        exit_resolver=exit_resolver,
        ev_engine=ev_engine,
        policy=RiskPolicy(settings),
        reservations=reservations,
    )

    # 5. Scan.
    import asyncio

    report = asyncio.run(
        pipeline.run(
            list(scenario.adapters),
            ScanConfig(input_rarity=input_rarity),
            moment=now,
            price_observations=scenario.price_observations,
            fee_schedule_id=DEMO_FEE_SCHEDULE_ID,
        )
    )

    # 6. Operator cards for whatever survived.
    cards: list[OperatorCard] = []
    for outcome in report.ranked():
        assessment = partial_fill.assess(outcome.candidate.inputs, now)
        cards.append(
            build_operator_card(
                outcome.candidate,
                outcome.evaluation,
                assessment,
                metadata_revision=registry.revision,
                generated_at=now,
                extra_warnings=(
                    "SYNTHETIC DEMO DATA. Listings and prices are invented; only the "
                    "item metadata is real. This card must never be acted upon.",
                ),
            )
        )

    # 7. Persist.
    database = create_database(settings.database_url)
    database.create_all()
    with database.session() as session:
        MetadataRepository(session).record(registry, issue_count=len(import_result.issues))
        ListingRepository(session).upsert_many(scenario.listings)
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

    with database.session() as session:
        ledger = LedgerRepository(session)
        ledger_count = ledger.count()
        settled = ledger.settled_cash_total(settings.base_currency)
    database.dispose()

    # 8. Artifacts.
    console = "\n".join(
        [
            render_ranked_table(cards),
            "",
            *(render_card_console(card) for card in cards),
        ]
    )
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    artifacts: dict[str, Path] = {}
    artifacts["markdown"] = evidence_dir / f"demo-{stamp}.md"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    artifacts["markdown"].write_text(
        render_scan_markdown(report, cards, title="Offline demo scan (synthetic data)"),
        encoding="utf-8",
    )
    artifacts["json"] = write_json(
        evidence_dir / f"demo-{stamp}.json",
        {
            "scanned_at": now.isoformat(),
            "rule_version": ruleset.rule_version,
            "data_nature": "SYNTHETIC listings and prices over REAL pinned metadata",
            "metadata": dict(report.metadata_provenance),
            "scenario": scenario_summary(scenario),
            "settings": dict(report.settings_summary),
            "statistics": report.statistics.summary(),
            "adapters": [dict(c) for c in report.adapter_capabilities],
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
                "settled_fee_net_profit_minor": settled.minor_units,
                "ledger_event_count": ledger_count,
                "orders_placed": 0,
                "trade_ups_completed": 0,
                "sales_settled": 0,
                "note": (
                    "Zero across the board. No order was placed and no profit settled. "
                    "A passing candidate above is a software demonstration only."
                ),
            },
        },
    )
    rows = [card.summary_row() for card in cards]
    artifacts["csv"] = write_csv(evidence_dir / f"demo-{stamp}.csv", rows)
    parquet = write_parquet(evidence_dir / f"demo-{stamp}.parquet", rows)
    if parquet is not None:
        artifacts["parquet"] = parquet

    return DemoResult(
        report=report,
        cards=tuple(cards),
        scenario=scenario,
        registry=registry,
        ruleset=ruleset,
        artifacts=artifacts,
        settled_cash_minor=settled.minor_units,
        ledger_event_count=ledger_count,
        console_output=console,
    )
