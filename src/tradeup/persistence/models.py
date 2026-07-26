"""SQLAlchemy models.

Design rules that show up throughout:

* **Money is stored as integer minor units plus currency plus balance type.** There
  is no ``Float`` or ``Numeric`` money column anywhere. Decimals that are genuinely
  ratios (ROI, probabilities, floats) are stored as exact strings, because SQLite has
  no exact decimal type and a REAL column would reintroduce binary error at the point
  we can least afford it.
* **Timestamps are timezone-aware UTC.**
* **The ledger is append-only, enforced at the mapper level.** An ``UPDATE`` or
  ``DELETE`` against ``ledger_events`` raises. The whole value of the ledger is that
  it cannot be quietly rewritten once a number becomes inconvenient.
* **Listing identity is unique per (venue, listing_id)**, and an active reservation is
  unique per listing, so duplicate allocation is impossible at the storage layer and
  not merely discouraged in application code.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, ClassVar

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    event,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

__all__ = [
    "Base",
    "CandidateEvaluationRow",
    "CandidateRejectionRow",
    "CandidateRow",
    "ImmutableLedgerError",
    "InventoryItemRow",
    "LedgerEventRow",
    "ListingRow",
    "MetadataSnapshotRow",
    "RawPayloadRow",
    "RealizedContractResultRow",
    "ReservationRow",
    "ScanRow",
]


class ImmutableLedgerError(Exception):
    """Raised on any attempt to modify or delete a persisted ledger event."""


class Base(DeclarativeBase):
    """Declarative base with a JSON type usable on both SQLite and PostgreSQL."""

    # SQLAlchemy reads this off the class; ClassVar keeps it out of the dataclass-like
    # field machinery and satisfies the mutable-class-attribute check.
    type_annotation_map: ClassVar[dict[Any, Any]] = {dict[str, Any]: JSON}


def _utc_column(nullable: bool = False) -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), nullable=nullable)


class MetadataSnapshotRow(Base):
    """One pinned metadata import, with its payload hash."""

    __tablename__ = "metadata_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(128))
    revision: Mapped[str] = mapped_column(String(128))
    payload_sha256: Mapped[str] = mapped_column(String(64))
    imported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    skin_count: Mapped[int] = mapped_column(Integer)
    collection_count: Mapped[int] = mapped_column(Integer)
    issue_count: Mapped[int] = mapped_column(Integer, default=0)

    __table_args__ = (
        UniqueConstraint("source", "revision", "payload_sha256", name="uq_metadata_snapshot"),
    )


class RawPayloadRow(Base):
    """Content-addressed archive of what a venue actually returned.

    Stored separately from parsed rows so a later disagreement can be settled against
    the bytes we saw, not against our interpretation of them.
    """

    __tablename__ = "raw_payloads"

    payload_sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    venue: Mapped[str] = mapped_column(String(64))
    operation: Mapped[str] = mapped_column(String(64))
    url: Mapped[str] = mapped_column(String(1024), default="")
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    byte_length: Mapped[int] = mapped_column(Integer)
    content: Mapped[bytes | None] = mapped_column(nullable=True)


class ScanRow(Base):
    """One scan run."""

    __tablename__ = "scans"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    scanned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    rule_version: Mapped[str] = mapped_column(String(128))
    metadata_revision: Mapped[str] = mapped_column(String(128))
    settings_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    statistics_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    candidates: Mapped[list[CandidateRow]] = relationship(back_populates="scan")


class ListingRow(Base):
    """An exact listing as observed. Ingestion is idempotent on (venue, listing_id)."""

    __tablename__ = "listings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    venue: Mapped[str] = mapped_column(String(64), index=True)
    listing_id: Mapped[str] = mapped_column(String(128))
    asset_id: Mapped[str] = mapped_column(String(128), index=True)
    skin_id: Mapped[str] = mapped_column(String(256), index=True)
    market_hash_name: Mapped[str] = mapped_column(String(512))
    collection_id: Mapped[str] = mapped_column(String(256), index=True)
    rarity: Mapped[str] = mapped_column(String(32))
    quality_type: Mapped[str] = mapped_column(String(32))

    # Exact decimal values as text. Never REAL.
    raw_float: Mapped[str] = mapped_column(String(64))
    normalized_float: Mapped[str] = mapped_column(String(64))

    price_minor: Mapped[int] = mapped_column(BigInteger)
    currency: Mapped[str] = mapped_column(String(8))
    balance_type: Mapped[str] = mapped_column(String(32))
    buyer_fee_minor: Mapped[int] = mapped_column(BigInteger, default=0)
    deposit_fee_minor: Mapped[int] = mapped_column(BigInteger, default=0)

    paint_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    paint_seed: Mapped[int | None] = mapped_column(Integer, nullable=True)
    seller_reliability: Mapped[str | None] = mapped_column(String(32), nullable=True)

    trade_lock_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    tradable_status: Mapped[str] = mapped_column(String(32))
    listing_status: Mapped[str] = mapped_column(String(32))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    raw_payload_hash: Mapped[str] = mapped_column(String(64))

    __table_args__ = (
        UniqueConstraint("venue", "listing_id", name="uq_listing_identity"),
        Index("ix_listing_collection_rarity", "collection_id", "rarity"),
    )


class CandidateRow(Base):
    """A candidate contract, reproducible from its stored inputs."""

    __tablename__ = "candidates"

    candidate_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    scan_id: Mapped[int | None] = mapped_column(ForeignKey("scans.id"), nullable=True)
    rule_version: Mapped[str] = mapped_column(String(128))
    metadata_revision: Mapped[str] = mapped_column(String(128))
    composition_signature: Mapped[str] = mapped_column(String(512))
    input_rarity: Mapped[str] = mapped_column(String(32))
    input_count: Mapped[int] = mapped_column(Integer)
    output_quality: Mapped[str] = mapped_column(String(32))
    procurement_mode: Mapped[str] = mapped_column(String(64))

    # Exact rational, stored as numerator/denominator so it round-trips without loss.
    average_normalized_numerator: Mapped[int] = mapped_column(BigInteger)
    average_normalized_denominator: Mapped[int] = mapped_column(BigInteger)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    input_identities_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    outcomes_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    optimizer_explanation_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    scan: Mapped[ScanRow | None] = relationship(back_populates="candidates")
    evaluations: Mapped[list[CandidateEvaluationRow]] = relationship(back_populates="candidate")
    rejections: Mapped[list[CandidateRejectionRow]] = relationship(back_populates="candidate")


class CandidateEvaluationRow(Base):
    """Economics of a candidate at one moment under one fee schedule."""

    __tablename__ = "candidate_evaluations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    candidate_id: Mapped[str] = mapped_column(ForeignKey("candidates.candidate_id"), index=True)
    evaluated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    rule_version: Mapped[str] = mapped_column(String(128))
    fee_schedule_id: Mapped[str] = mapped_column(String(128))
    currency: Mapped[str] = mapped_column(String(8))

    input_cost_minor: Mapped[int] = mapped_column(BigInteger)
    buyer_fees_minor: Mapped[int] = mapped_column(BigInteger)
    deposit_fees_minor: Mapped[int] = mapped_column(BigInteger)
    fx_cost_minor: Mapped[int] = mapped_column(BigInteger)
    payment_surcharge_minor: Mapped[int] = mapped_column(BigInteger)
    acquisition_cost_minor: Mapped[int] = mapped_column(BigInteger)
    operational_cost_minor: Mapped[int] = mapped_column(BigInteger)
    settlement_cost_minor: Mapped[int] = mapped_column(BigInteger, default=0)
    capital_carry_cost_minor: Mapped[int] = mapped_column(BigInteger)
    partial_fill_reserve_minor: Mapped[int] = mapped_column(BigInteger)
    all_in_cost_minor: Mapped[int] = mapped_column(BigInteger)

    expected_output_value_minor: Mapped[int] = mapped_column(BigInteger)
    ev_net_minor: Mapped[int] = mapped_column(BigInteger)
    lower_bound_ev_minor: Mapped[int] = mapped_column(BigInteger)
    worst_case_pnl_minor: Mapped[int] = mapped_column(BigInteger)
    best_case_pnl_minor: Mapped[int] = mapped_column(BigInteger)
    profit_per_capital_day_minor: Mapped[int] = mapped_column(BigInteger)
    expected_orphan_loss_minor: Mapped[int] = mapped_column(BigInteger)

    roi_net: Mapped[str] = mapped_column(String(64))
    probability_of_profit: Mapped[str] = mapped_column(String(64))
    expected_capital_days: Mapped[str] = mapped_column(String(32))
    bundle_completion_probability: Mapped[str] = mapped_column(String(32))
    unvaluable_probability_mass: Mapped[str] = mapped_column(String(64))
    max_quote_age_seconds: Mapped[str] = mapped_column(String(32))
    output_confidence_floor: Mapped[str] = mapped_column(String(16))

    candidate: Mapped[CandidateRow] = relationship(back_populates="evaluations")


class CandidateRejectionRow(Base):
    """Why a candidate died, with machine-readable codes."""

    __tablename__ = "candidate_rejections"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    candidate_id: Mapped[str] = mapped_column(ForeignKey("candidates.candidate_id"), index=True)
    stage: Mapped[str] = mapped_column(String(32), index=True)
    rejected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    reasons: Mapped[str] = mapped_column(String(1024))
    details_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    candidate: Mapped[CandidateRow] = relationship(back_populates="rejections")


class ReservationRow(Base):
    """A claim on a listing.

    ``is_active`` is a stored flag rather than a derived one purely so a partial
    unique index can enforce "one active claim per listing" in the database. Two
    concurrent scanners cannot both claim the same asset even if application-level
    checks race.
    """

    __tablename__ = "reservations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    venue: Mapped[str] = mapped_column(String(64))
    listing_id: Mapped[str] = mapped_column(String(128))
    candidate_id: Mapped[str] = mapped_column(String(32), index=True)
    state: Mapped[str] = mapped_column(String(32))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    reserved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# Partial unique index: at most one *active* claim per listing, while still allowing
# the same listing to be reserved again after a release. A plain unique constraint
# would forbid that second claim forever; a non-unique index would not prevent the
# duplicate allocation this exists to stop.
#
# Declared after the class body because the predicate must reference the mapped
# columns, which do not exist until the class is built.
Index(
    "uq_active_reservation_per_listing",
    ReservationRow.venue,
    ReservationRow.listing_id,
    unique=True,
    sqlite_where=ReservationRow.is_active.is_(True),
    postgresql_where=ReservationRow.is_active.is_(True),
)


class InventoryItemRow(Base):
    """An asset we hold or intend to hold."""

    __tablename__ = "inventory_items"

    item_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    venue: Mapped[str | None] = mapped_column(String(64), nullable=True)
    listing_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    asset_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    skin_id: Mapped[str] = mapped_column(String(256), index=True)
    market_hash_name: Mapped[str] = mapped_column(String(512))
    state: Mapped[str] = mapped_column(String(32), index=True)
    contract_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    acquisition_cost_minor: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    currency: Mapped[str | None] = mapped_column(String(8), nullable=True)
    balance_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    raw_float: Mapped[str | None] = mapped_column(String(64), nullable=True)
    acquired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    trade_lock_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    history_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class LedgerEventRow(Base):
    """An immutable economic fact. Update and delete are blocked at the mapper."""

    __tablename__ = "ledger_events"

    event_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    event_type: Mapped[str] = mapped_column(String(48), index=True)
    amount_minor: Mapped[int] = mapped_column(BigInteger)
    currency: Mapped[str] = mapped_column(String(8))
    balance_type: Mapped[str] = mapped_column(String(32))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    sequence: Mapped[int] = mapped_column(BigInteger)
    reference: Mapped[str] = mapped_column(String(256), default="")
    contract_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    item_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    venue: Mapped[str | None] = mapped_column(String(64), nullable=True)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


@event.listens_for(LedgerEventRow, "before_update", propagate=True)
def _block_ledger_update(mapper: object, connection: object, target: LedgerEventRow) -> None:
    raise ImmutableLedgerError(
        f"ledger event {target.event_id} cannot be modified; "
        "corrections are recorded as REVERSAL events, never as edits"
    )


@event.listens_for(LedgerEventRow, "before_delete", propagate=True)
def _block_ledger_delete(mapper: object, connection: object, target: LedgerEventRow) -> None:
    raise ImmutableLedgerError(
        f"ledger event {target.event_id} cannot be deleted; the ledger is append-only"
    )


class RealizedContractResultRow(Base):
    """Predicted versus actual, for calibration."""

    __tablename__ = "realized_contract_results"

    contract_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    rule_version: Mapped[str] = mapped_column(String(128))
    currency: Mapped[str] = mapped_column(String(8))

    predicted_ev_minor: Mapped[int] = mapped_column(BigInteger)
    predicted_roi: Mapped[str] = mapped_column(String(64))
    predicted_sale_value_minor: Mapped[int] = mapped_column(BigInteger)
    predicted_capital_days: Mapped[str] = mapped_column(String(32))
    predicted_distribution_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    actual_output_skin_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    actual_output_float: Mapped[str | None] = mapped_column(String(64), nullable=True)
    actual_gross_sale_minor: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    actual_fees_minor: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    actual_settled_proceeds_minor: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    actual_capital_days: Mapped[str | None] = mapped_column(String(32), nullable=True)

    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
