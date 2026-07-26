"""Renderers: console, Markdown, JSON, CSV and Parquet.

Output is plain deterministic text rather than styled terminal output. Two reasons:
a byte-comparable artifact is what acceptance gate G12 (reproducibility) actually
tests, and an operator card is a document someone may paste into a ticket or read
six months later, not a transient console flourish.

Money is rendered from integer minor units at the last possible moment.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from tradeup.pipeline.scan import ScanReport
from tradeup.reporting.operator_card import OperatorCard

__all__ = [
    "render_card_console",
    "render_card_markdown",
    "render_ranked_table",
    "render_scan_markdown",
    "write_csv",
    "write_json",
    "write_parquet",
]

_RULE = "=" * 78
_THIN = "-" * 78


def _money(value: object) -> str:
    return str(getattr(value, "as_major", lambda: value)())


def render_ranked_table(cards: Sequence[OperatorCard]) -> str:
    """The first deliverable: a ranked, timestamped candidate table."""
    header = (
        f"{'Contract':<16}{'In':>4}{'Cost':>12}{'Net EV':>10}{'Net ROI':>10}"
        f"{'P(profit)':>11}{'Worst':>12}{'Cap-days':>10}{'Fill P':>9}"
    )
    lines = [header, _THIN]
    if not cards:
        lines.append("(no candidates cleared the configured gates)")
        return "\n".join(lines)
    for card in cards:
        lines.append(
            f"{card.contract_id:<16}"
            f"{card.input_count:>4}"
            f"{_money(card.acquisition_cost):>12}"
            f"{_money(card.net_ev):>10}"
            f"{(card.net_roi * 100).quantize(Decimal('0.01')):>9}%"
            f"{card.probability_of_profit_percent:>10}%"
            f"{_money(card.worst_case_pnl):>12}"
            f"{card.expected_capital_days:>10}"
            f"{card.bundle_completion_probability:>9}"
        )
    return "\n".join(lines)


def render_card_console(card: OperatorCard) -> str:
    """Full operator card as plain text."""
    lines: list[str] = [
        _RULE,
        f"OPERATOR ACTION CARD  {card.contract_id}",
        _RULE,
        f"generated       {card.generated_at.isoformat()}",
        f"expires         {card.expires_at.isoformat()}",
        f"rule version    {card.rule_version}",
        f"fee schedule    {card.fee_schedule_id}",
        f"metadata rev    {card.metadata_revision}",
        "",
        "ECONOMICS",
        _THIN,
        f"  inputs                    {card.input_count}",
        f"  acquisition cost          {card.acquisition_cost}",
        f"  all-in cost               {card.all_in_cost}",
        f"  settlement-rail charge    {card.settlement_cost}",
        f"  expected output value     {card.expected_output_value}",
        f"  net EV                    {card.net_ev}",
        f"  net ROI                   {(card.net_roi * 100).quantize(Decimal('0.01'))}%",
        f"  lower-confidence-bound EV {card.lower_bound_ev}",
        f"  probability of profit     {card.probability_of_profit_percent}%",
        f"  worst case                {card.worst_case_pnl}",
        f"  best case                 {card.best_case_pnl}",
        f"  expected capital-days     {card.expected_capital_days}",
        f"  profit per capital-day    {card.profit_per_capital_day}",
        f"  bundle completion P       {card.bundle_completion_probability}",
        f"  max orphan exposure       {card.max_orphan_exposure}",
        "",
        "INPUTS TO BUY",
        _THIN,
    ]
    for item in card.inputs:
        lines.append(
            f"  [{item.position:>2}] {item.venue}:{item.listing_id}  {item.market_hash_name}"
        )
        lines.append(
            f"       float={item.raw_float}  normalised={item.normalized_float}  "
            f"price={item.price}  all-in={item.all_in_cost}"
        )
        lines.append(
            f"       collection={item.collection_id}  trade-lock={item.trade_lock_days}d  "
            f"asset={item.asset_id}"
        )
        lines.append(f"       {item.link_or_instruction}")

    lines += ["", "PURCHASE SEQUENCE (most fragile first)", _THIN]
    for step in card.purchase_sequence:
        lines.append(
            f"  {step.order:>2}. {step.venue}:{step.listing_id}  cost={step.cost}  "
            f"exposure={step.cumulative_exposure}  survival={step.survival_probability}"
        )
        lines.append(f"      loss if this one is gone: {step.orphan_loss_if_next_fails}")

    lines += ["", "OUTCOME DISTRIBUTION", _THIN]
    for outcome in card.outcomes:
        lines.append(f"  {outcome.probability_percent:>8}%  {outcome.market_hash_name}")
        lines.append(
            f"             float={outcome.output_float} ({outcome.wear})  "
            f"net={outcome.net_proceeds}  via {outcome.valuation_source} "
            f"[{outcome.confidence}, n={outcome.evidence_count}]  "
            f"~{outcome.expected_days_to_sale}d on {outcome.exit_venue}"
        )

    lines += [
        "",
        "RECOMMENDED EXIT",
        _THIN,
        f"  venue: {card.recommended_exit_venue}",
        f"  {card.exit_rationale}",
        "",
        "MANUAL STEAM ACTIONS (human only -- never automated)",
        _THIN,
    ]
    lines.extend(f"  {i}. {step}" for i, step in enumerate(card.manual_steam_steps, start=1))

    lines += ["", "ASSUMPTIONS", _THIN]
    lines.extend(f"  - {a}" for a in card.assumptions)

    lines += ["", "WARNINGS", _THIN]
    lines.extend(f"  ! {w}" for w in card.warnings)

    lines += [
        "",
        "OPERATOR RECORD",
        _THIN,
        "  Record one of: APPROVED REJECTED PURCHASED LISTING_GONE PRICE_CHANGED",
        "                 RECEIVED_IN_STEAM TRADEUP_COMPLETED OUTPUT_RECORDED",
        "                 LISTED_FOR_SALE SOLD SETTLED",
        "  There is no automated purchase. Live execution is disabled.",
        _RULE,
    ]
    return "\n".join(lines)


def render_card_markdown(card: OperatorCard) -> str:
    """Operator card as Markdown, for tickets and archived reports."""
    lines = [
        f"## Operator action card `{card.contract_id}`",
        "",
        f"- **Generated** {card.generated_at.isoformat()}",
        f"- **Expires** {card.expires_at.isoformat()}",
        f"- **Rule version** `{card.rule_version}`",
        f"- **Fee schedule** `{card.fee_schedule_id}`",
        f"- **Metadata revision** `{card.metadata_revision}`",
        "",
        "### Economics",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| Inputs | {card.input_count} |",
        f"| Acquisition cost | {card.acquisition_cost} |",
        f"| All-in cost | {card.all_in_cost} |",
        f"| Settlement-rail charge | {card.settlement_cost} |",
        f"| Expected output value | {card.expected_output_value} |",
        f"| Net EV | {card.net_ev} |",
        f"| Net ROI | {(card.net_roi * 100).quantize(Decimal('0.01'))}% |",
        f"| Lower-confidence-bound EV | {card.lower_bound_ev} |",
        f"| Probability of profit | {card.probability_of_profit_percent}% |",
        f"| Worst case | {card.worst_case_pnl} |",
        f"| Best case | {card.best_case_pnl} |",
        f"| Expected capital-days | {card.expected_capital_days} |",
        f"| Profit per capital-day | {card.profit_per_capital_day} |",
        f"| Bundle completion probability | {card.bundle_completion_probability} |",
        f"| Max orphan exposure | {card.max_orphan_exposure} |",
        "",
        "### Inputs",
        "",
        "| # | Venue | Listing | Item | Float | Price | All-in | Lock |",
        "|---|---|---|---|---|---|---|---|",
    ]
    lines.extend(
        f"| {i.position} | {i.venue} | `{i.listing_id}` | {i.market_hash_name} | "
        f"{i.raw_float} | {i.price.as_major()} | {i.all_in_cost.as_major()} | {i.trade_lock_days}d |"
        for i in card.inputs
    )
    lines += [
        "",
        "### Purchase sequence",
        "",
        "| Order | Listing | Cost | Cumulative exposure | Loss if unavailable | Survival |",
        "|---|---|---|---|---|---|",
    ]
    lines.extend(
        f"| {s.order} | `{s.venue}:{s.listing_id}` | {s.cost.as_major()} | "
        f"{s.cumulative_exposure.as_major()} | {s.orphan_loss_if_next_fails.as_major()} | "
        f"{s.survival_probability} |"
        for s in card.purchase_sequence
    )
    lines += [
        "",
        "### Outcome distribution",
        "",
        "| P | Output | Float | Wear | Net | Evidence | Confidence | Days | Exit |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    lines.extend(
        f"| {o.probability_percent}% | {o.market_hash_name} | {o.output_float} | {o.wear} | "
        f"{o.net_proceeds.as_major()} | {o.valuation_source} (n={o.evidence_count}) | "
        f"{o.confidence} | {o.expected_days_to_sale} | {o.exit_venue} |"
        for o in card.outcomes
    )
    lines += [
        "",
        "### Recommended exit",
        "",
        f"**{card.recommended_exit_venue}** — {card.exit_rationale}",
        "",
        "### Manual Steam actions",
        "",
        "> Performed by a human. No part of this may be automated.",
        "",
    ]
    lines.extend(f"{i}. {s}" for i, s in enumerate(card.manual_steam_steps, start=1))
    lines += ["", "### Assumptions", ""]
    lines.extend(f"- {a}" for a in card.assumptions)
    lines += ["", "### Warnings", ""]
    lines.extend(f"- **{w}**" for w in card.warnings)
    return "\n".join(lines)


def render_scan_markdown(
    report: ScanReport, cards: Sequence[OperatorCard], *, title: str = "Shadow scan report"
) -> str:
    """Full scan report: ranked table, statistics, rejection census, then cards."""
    stats = report.statistics
    lines = [
        f"# {title}",
        "",
        f"**Scanned at** {report.scanned_at.isoformat()}  ",
        f"**Rule version** `{report.rule_version}`  ",
        f"**Metadata** `{report.metadata_provenance.get('revision', 'unknown')}` "
        f"(sha256 `{report.metadata_provenance.get('payload_sha256', '')[:16]}…`)",
        "",
        "> Shadow mode. No order was placed, no trade-up performed, no profit settled.",
        "",
        "## Ranked candidates",
        "",
    ]
    if cards:
        lines += [
            "| Contract | Inputs | Cost | Net EV | Net ROI | P(profit) | Worst case | "
            "Capital-days | Fill P | Verified |",
            "|---|---|---|---|---|---|---|---|---|---|",
        ]
        lines.extend(
            f"| {c.contract_id} | {c.input_count} | {c.acquisition_cost.as_major()} | "
            f"{c.net_ev.as_major()} | {(c.net_roi * 100).quantize(Decimal('0.01'))}% | "
            f"{c.probability_of_profit_percent}% | {c.worst_case_pnl.as_major()} | "
            f"{c.expected_capital_days} | {c.bundle_completion_probability} | Yes |"
            for c in cards
        )
    else:
        lines.append("No candidate cleared the configured gates. That is a result, not a failure.")

    lines += [
        "",
        "## Scan statistics",
        "",
        "| Metric | Value |",
        "|---|---|",
    ]
    lines.extend(f"| {k} | {v} |" for k, v in stats.summary().items())

    lines += ["", "## Rejection census", ""]
    if stats.rejections_by_reason:
        lines += ["| Reason | Count |", "|---|---|"]
        lines.extend(
            f"| `{reason}` | {count} |"
            for reason, count in sorted(
                stats.rejections_by_reason.items(), key=lambda kv: (-kv[1], kv[0])
            )
        )
    else:
        lines.append("No candidate was rejected.")

    lines += [
        "",
        "## Adapter capabilities",
        "",
        "| Venue | Execution mode | Note |",
        "|---|---|---|",
    ]
    lines.extend(
        f"| {c.get('venue')} | `{c.get('execution_mode')}` | {c.get('note', '')} |"
        for c in report.adapter_capabilities
    )

    lines += ["", "## Effective gates", "", "| Setting | Value |", "|---|---|"]
    lines.extend(f"| `{k}` | {v} |" for k, v in sorted(report.settings_summary.items()))

    if cards:
        lines += ["", "## Operator cards", ""]
        for card in cards:
            lines += [render_card_markdown(card), ""]

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Machine-readable output
# ---------------------------------------------------------------------------


def write_json(path: Path, payload: Mapping[str, Any] | Sequence[Any]) -> Path:
    """Write deterministic, sorted, UTF-8 JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return path


def write_csv(path: Path, rows: Sequence[Mapping[str, object]]) -> Path:
    """Write rows as CSV. An empty result still writes a header-only file.

    A zero-row artifact is meaningful evidence -- it records that the scan ran and
    found nothing, which is different from the scan not having run.
    """
    import csv

    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = sorted({key for row in rows for key in row}) or ["contract"]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in fieldnames})
    return path


def write_parquet(path: Path, rows: Sequence[Mapping[str, object]]) -> Path | None:
    """Write rows as Parquet. Returns ``None`` when there is nothing to write.

    Parquet has no meaningful empty-with-schema form here, so an empty scan produces
    the CSV and JSON artifacts only, and says so rather than writing a file that
    would fail to read back.
    """
    if not rows:
        return None
    import polars as pl

    path.parent.mkdir(parents=True, exist_ok=True)
    frame = pl.DataFrame([{k: str(v) for k, v in row.items()} for row in rows])
    frame.write_parquet(path)
    return path


def utc_stamp(moment: datetime) -> str:
    """Filesystem-safe UTC timestamp for artifact names."""
    return moment.strftime("%Y%m%dT%H%M%SZ")
