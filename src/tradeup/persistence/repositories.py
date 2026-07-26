"""Repositories: the only place domain objects become rows.

Ingestion is idempotent. Re-running a scan against an unchanged market updates
observation timestamps rather than accumulating duplicate listings, which is what
makes "run the demo twice, get the same answer" (gate G12) achievable.

Ledger writes are append-only by construction: :meth:`LedgerRepository.append`
inserts and never updates, and the mapper-level guard in ``models`` turns any attempt
to modify a persisted event into an error.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from fractions import Fraction

from sqlalchemy import select
from sqlalchemy.orm import Session

from tradeup.domain.contracts import (
    CandidateEvaluation,
    CandidateRejection,
    TradeupCandidate,
)
from tradeup.domain.ledger import LedgerEvent
from tradeup.domain.listings import ListingIdentity, MarketplaceListing
from tradeup.domain.money import BalanceType, Currency, Money
from tradeup.metadata.registry import MetadataRegistry
from tradeup.optimizer.bundle import BundleSolution
from tradeup.persistence.models import (
    CandidateEvaluationRow,
    CandidateRejectionRow,
    CandidateRow,
    LedgerEventRow,
    ListingRow,
    MetadataSnapshotRow,
    ReservationRow,
    ScanRow,
)

__all__ = [
    "CandidateRepository",
    "LedgerRepository",
    "ListingRepository",
    "MetadataRepository",
    "ScanRepository",
]


class MetadataRepository:
    """Records which metadata snapshot backed a decision."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def record(self, registry: MetadataRegistry, *, issue_count: int = 0) -> MetadataSnapshotRow:
        existing = self._session.scalar(
            select(MetadataSnapshotRow).where(
                MetadataSnapshotRow.source == registry.source,
                MetadataSnapshotRow.revision == registry.revision,
                MetadataSnapshotRow.payload_sha256 == registry.payload_sha256,
            )
        )
        if existing is not None:
            return existing
        row = MetadataSnapshotRow(
            source=registry.source,
            revision=registry.revision,
            payload_sha256=registry.payload_sha256,
            imported_at=registry.imported_at,
            skin_count=registry.skin_count,
            collection_count=registry.collection_count,
            issue_count=issue_count,
        )
        self._session.add(row)
        self._session.flush()
        return row


class ListingRepository:
    """Idempotent listing storage keyed on (venue, listing_id)."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def upsert(self, listing: MarketplaceListing) -> ListingRow:
        row = self._session.scalar(
            select(ListingRow).where(
                ListingRow.venue == listing.identity.venue,
                ListingRow.listing_id == listing.identity.listing_id,
            )
        )
        if row is None:
            row = ListingRow(
                venue=listing.identity.venue,
                listing_id=listing.identity.listing_id,
            )
            self._session.add(row)

        row.asset_id = listing.asset_id
        row.skin_id = listing.skin_id
        row.market_hash_name = listing.market_hash_name
        row.collection_id = listing.collection_id
        row.rarity = listing.rarity.value
        row.quality_type = listing.quality_type.value
        row.raw_float = str(listing.raw_float)
        row.normalized_float = str(listing.normalized_float)
        row.price_minor = listing.price.minor_units
        row.currency = listing.price.currency.value
        row.balance_type = listing.price.balance_type.value
        row.buyer_fee_minor = listing.buyer_fee.minor_units
        row.deposit_fee_minor = listing.deposit_fee.minor_units
        row.paint_index = listing.paint_index
        row.paint_seed = listing.paint_seed
        row.seller_reliability = (
            str(listing.seller_reliability) if listing.seller_reliability is not None else None
        )
        row.trade_lock_until = listing.trade_lock_until
        row.tradable_status = listing.tradable_status.value
        row.listing_status = listing.listing_status.value
        row.observed_at = listing.observed_at
        row.verified_at = listing.verified_at
        row.raw_payload_hash = listing.raw_payload_hash
        self._session.flush()
        return row

    def upsert_many(self, listings: Sequence[MarketplaceListing]) -> int:
        for listing in listings:
            self.upsert(listing)
        return len(listings)

    def get(self, identity: ListingIdentity) -> ListingRow | None:
        return self._session.scalar(
            select(ListingRow).where(
                ListingRow.venue == identity.venue,
                ListingRow.listing_id == identity.listing_id,
            )
        )

    def count(self) -> int:
        return len(list(self._session.scalars(select(ListingRow))))


class ScanRepository:
    """One row per scan run, with its statistics and effective settings."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def record(
        self,
        *,
        scanned_at: datetime,
        rule_version: str,
        metadata_revision: str,
        settings_summary: dict[str, str],
        statistics: dict[str, str],
    ) -> ScanRow:
        row = ScanRow(
            scanned_at=scanned_at,
            rule_version=rule_version,
            metadata_revision=metadata_revision,
            settings_json=dict(settings_summary),
            statistics_json=dict(statistics),
        )
        self._session.add(row)
        self._session.flush()
        return row


class CandidateRepository:
    """Candidates, their economics and their rejections."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def save(
        self,
        candidate: TradeupCandidate,
        *,
        metadata_revision: str,
        scan_id: int | None = None,
        solution: BundleSolution | None = None,
    ) -> CandidateRow:
        row = self._session.get(CandidateRow, candidate.candidate_id)
        if row is None:
            row = CandidateRow(candidate_id=candidate.candidate_id)
            self._session.add(row)

        row.scan_id = scan_id
        row.rule_version = candidate.rule_version
        row.metadata_revision = metadata_revision
        row.composition_signature = candidate.composition.signature
        row.input_rarity = candidate.composition.input_rarity.value
        row.input_count = candidate.input_count
        row.output_quality = candidate.output_quality.value
        row.procurement_mode = candidate.procurement_mode.value
        row.average_normalized_numerator = candidate.average_normalized_float.numerator
        row.average_normalized_denominator = candidate.average_normalized_float.denominator
        row.created_at = candidate.created_at
        row.expires_at = candidate.expires_at
        row.input_identities_json = {
            "identities": [
                {"venue": i.venue, "listing_id": i.listing_id} for i in candidate.input_identities
            ]
        }
        row.outcomes_json = {
            "outcomes": [
                {
                    "collection_id": o.collection_id,
                    "skin_id": o.skin_id,
                    "probability_numerator": o.probability.numerator,
                    "probability_denominator": o.probability.denominator,
                    "output_float": str(o.output_float),
                    "wear": o.wear.value,
                    "quality": o.quality.value,
                    "net_proceeds_minor": o.valuation.net_proceeds.minor_units,
                    "valuation_source": o.valuation.source.value,
                    "confidence": o.valuation.confidence.value,
                    "evidence_count": o.valuation.evidence_count,
                    "exit_venue": o.valuation.exit_venue,
                }
                for o in candidate.outcomes
            ]
        }
        row.optimizer_explanation_json = (
            dict(solution.explanation.summary()) if solution is not None else {}
        )
        self._session.flush()
        return row

    def save_evaluation(self, evaluation: CandidateEvaluation) -> CandidateEvaluationRow:
        row = CandidateEvaluationRow(
            candidate_id=evaluation.candidate_id,
            evaluated_at=evaluation.evaluated_at,
            rule_version=evaluation.rule_version,
            fee_schedule_id=evaluation.fee_schedule_id,
            currency=evaluation.all_in_cost.currency.value,
            input_cost_minor=evaluation.input_cost.minor_units,
            buyer_fees_minor=evaluation.buyer_fees.minor_units,
            deposit_fees_minor=evaluation.deposit_fees.minor_units,
            fx_cost_minor=evaluation.fx_cost.minor_units,
            payment_surcharge_minor=evaluation.payment_surcharge.minor_units,
            acquisition_cost_minor=evaluation.acquisition_cost.minor_units,
            operational_cost_minor=evaluation.operational_cost.minor_units,
            capital_carry_cost_minor=evaluation.capital_carry_cost.minor_units,
            partial_fill_reserve_minor=evaluation.partial_fill_reserve.minor_units,
            all_in_cost_minor=evaluation.all_in_cost.minor_units,
            expected_output_value_minor=evaluation.expected_output_value.minor_units,
            ev_net_minor=evaluation.ev_net.minor_units,
            lower_bound_ev_minor=evaluation.lower_bound_ev.minor_units,
            worst_case_pnl_minor=evaluation.worst_case_pnl.minor_units,
            best_case_pnl_minor=evaluation.best_case_pnl.minor_units,
            profit_per_capital_day_minor=evaluation.profit_per_capital_day.minor_units,
            expected_orphan_loss_minor=evaluation.expected_orphan_loss.minor_units,
            roi_net=str(evaluation.roi_net),
            probability_of_profit=str(evaluation.probability_of_profit),
            expected_capital_days=str(evaluation.expected_capital_days),
            bundle_completion_probability=str(evaluation.bundle_completion_probability),
            unvaluable_probability_mass=str(evaluation.unvaluable_probability_mass),
            max_quote_age_seconds=str(evaluation.max_quote_age_seconds),
            output_confidence_floor=evaluation.output_confidence_floor,
        )
        self._session.add(row)
        self._session.flush()
        return row

    def save_rejection(self, rejection: CandidateRejection) -> CandidateRejectionRow:
        row = CandidateRejectionRow(
            candidate_id=rejection.candidate_id,
            stage=rejection.stage,
            rejected_at=rejection.rejected_at,
            reasons=",".join(r.value for r in rejection.reasons),
            details_json=dict(rejection.details),
        )
        self._session.add(row)
        self._session.flush()
        return row

    def get(self, candidate_id: str) -> CandidateRow | None:
        return self._session.get(CandidateRow, candidate_id)

    def average_normalized(self, candidate_id: str) -> Fraction | None:
        """Reload the exact average normalised float, losslessly."""
        row = self.get(candidate_id)
        if row is None:
            return None
        return Fraction(row.average_normalized_numerator, row.average_normalized_denominator)

    def latest_evaluation(self, candidate_id: str) -> CandidateEvaluationRow | None:
        return self._session.scalar(
            select(CandidateEvaluationRow)
            .where(CandidateEvaluationRow.candidate_id == candidate_id)
            .order_by(CandidateEvaluationRow.evaluated_at.desc())
            .limit(1)
        )


class LedgerRepository:
    """Append-only economic ledger."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def append(self, event: LedgerEvent) -> LedgerEventRow:
        """Insert one event. Never updates; a duplicate id is an error."""
        if self._session.get(LedgerEventRow, event.event_id) is not None:
            raise ValueError(
                f"ledger event {event.event_id} already exists; "
                "events are immutable and ids are content-derived"
            )
        row = LedgerEventRow(
            event_id=event.event_id,
            event_type=event.event_type.value,
            amount_minor=event.amount.minor_units,
            currency=event.amount.currency.value,
            balance_type=event.amount.balance_type.value,
            occurred_at=event.occurred_at,
            recorded_at=event.recorded_at,
            sequence=event.sequence,
            reference=event.reference,
            contract_id=event.contract_id,
            item_id=event.item_id,
            venue=event.venue,
            metadata_json=dict(event.metadata),
        )
        self._session.add(row)
        self._session.flush()
        return row

    def append_many(self, events: Sequence[LedgerEvent]) -> int:
        for event in events:
            self.append(event)
        return len(events)

    def settled_cash_total(self, currency: Currency, contract_id: str | None = None) -> Money:
        """Net settled, withdrawable cash. The only figure that is 'profit'."""
        statement = select(LedgerEventRow).where(
            LedgerEventRow.currency == currency.value,
            LedgerEventRow.balance_type == BalanceType.CASH_WITHDRAWABLE.value,
        )
        if contract_id is not None:
            statement = statement.where(LedgerEventRow.contract_id == contract_id)
        total = sum(row.amount_minor for row in self._session.scalars(statement))
        return Money(int(total), currency, BalanceType.CASH_WITHDRAWABLE)

    def count(self) -> int:
        return len(list(self._session.scalars(select(LedgerEventRow))))


class ReservationRepository:
    """Durable reservation records backed by the partial unique index."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def record(
        self,
        *,
        identity: ListingIdentity,
        candidate_id: str,
        state: str,
        reserved_at: datetime,
        expires_at: datetime,
        is_active: bool = True,
    ) -> ReservationRow:
        row = ReservationRow(
            venue=identity.venue,
            listing_id=identity.listing_id,
            candidate_id=candidate_id,
            state=state,
            is_active=is_active,
            reserved_at=reserved_at,
            expires_at=expires_at,
        )
        self._session.add(row)
        self._session.flush()
        return row

    def deactivate(self, identity: ListingIdentity, *, released_at: datetime) -> int:
        rows = list(
            self._session.scalars(
                select(ReservationRow).where(
                    ReservationRow.venue == identity.venue,
                    ReservationRow.listing_id == identity.listing_id,
                    ReservationRow.is_active.is_(True),
                )
            )
        )
        for row in rows:
            row.is_active = False
            row.released_at = released_at
        self._session.flush()
        return len(rows)


def decimal_or_none(value: str | None) -> Decimal | None:
    """Parse a stored exact-decimal string back to Decimal."""
    return Decimal(value) if value is not None else None
