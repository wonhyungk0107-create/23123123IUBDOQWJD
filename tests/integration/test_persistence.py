"""Persistence: migrations, idempotency, uniqueness, ledger immutability, replay."""

from __future__ import annotations

import subprocess
import sys
from datetime import timedelta
from fractions import Fraction
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from tests.factories import NOW, make_candidate, make_listing, make_registry, usd

from tradeup.domain.ledger import LedgerEvent, LedgerEventType, make_event_id
from tradeup.domain.listings import ListingIdentity
from tradeup.domain.money import Currency
from tradeup.persistence.database import Database, create_database
from tradeup.persistence.models import (
    ImmutableLedgerError,
    LedgerEventRow,
    ListingRow,
    ReservationRow,
)
from tradeup.persistence.repositories import (
    CandidateRepository,
    LedgerRepository,
    ListingRepository,
    MetadataRepository,
    ReservationRepository,
)


@pytest.fixture
def database(tmp_path: Path) -> Database:
    db = create_database(f"sqlite+pysqlite:///{(tmp_path / 'test.db').as_posix()}")
    db.create_all()
    yield db
    db.dispose()


class TestMigrations:
    def test_alembic_upgrade_and_downgrade_on_a_clean_database(self, tmp_path: Path) -> None:
        """G2: migrations apply to a fresh SQLite database, and roll back."""
        url = f"sqlite+pysqlite:///{(tmp_path / 'migrated.db').as_posix()}"
        env = {"TRADEUP_DATABASE_URL": url}
        root = Path(__file__).resolve().parent.parent.parent

        import os

        merged = {**os.environ, **env}
        upgrade = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=root,
            env=merged,
            capture_output=True,
            text=True,
            check=False,
        )
        assert upgrade.returncode == 0, upgrade.stderr

        downgrade = subprocess.run(
            [sys.executable, "-m", "alembic", "downgrade", "base"],
            cwd=root,
            env=merged,
            capture_output=True,
            text=True,
            check=False,
        )
        assert downgrade.returncode == 0, downgrade.stderr

    def test_sqlite_enforces_foreign_keys(self, database: Database) -> None:
        """Off by default in SQLite; the pragma is set on every connection."""
        with database.session() as session:
            result = session.execute(select(1)).scalar()
            assert result == 1
            pragma = session.connection().exec_driver_sql("PRAGMA foreign_keys").scalar()
            assert pragma == 1


class TestListingIdempotency:
    def test_reingesting_the_same_listing_updates_rather_than_duplicates(
        self, database: Database
    ) -> None:
        listing = make_listing("L-1", price_minor=1000)
        with database.session() as session:
            ListingRepository(session).upsert(listing)
        repriced = make_listing("L-1", price_minor=2000)
        with database.session() as session:
            ListingRepository(session).upsert(repriced)
        with database.session() as session:
            rows = list(session.scalars(select(ListingRow)))
            assert len(rows) == 1
            assert rows[0].price_minor == 2000

    def test_the_same_listing_id_at_a_different_venue_is_a_different_listing(
        self, database: Database
    ) -> None:
        with database.session() as session:
            repository = ListingRepository(session)
            repository.upsert(make_listing("SHARED", venue="venue-a"))
            repository.upsert(make_listing("SHARED", venue="venue-b"))
        with database.session() as session:
            assert ListingRepository(session).count() == 2

    def test_duplicate_identity_is_rejected_by_the_database(self, database: Database) -> None:
        """The unique constraint holds even if application code is bypassed."""
        with database.session() as session:
            ListingRepository(session).upsert(make_listing("L-1"))
        with pytest.raises(IntegrityError), database.session() as session:
            listing = make_listing("L-1")
            session.add(
                ListingRow(
                    venue=listing.identity.venue,
                    listing_id=listing.identity.listing_id,
                    asset_id="x",
                    skin_id="x",
                    market_hash_name="x",
                    collection_id="x",
                    rarity="MIL_SPEC",
                    quality_type="NORMAL",
                    raw_float="0.1",
                    normalized_float="0.1",
                    price_minor=1,
                    currency="USD",
                    balance_type="CASH_WITHDRAWABLE",
                    tradable_status="TRADABLE",
                    listing_status="ACTIVE",
                    observed_at=NOW,
                    raw_payload_hash="h",
                )
            )

    def test_stored_floats_round_trip_exactly(self, database: Database) -> None:
        """Exact decimals are stored as text; a REAL column would lose precision."""
        from decimal import Decimal

        listing = make_listing("L-precise", raw_float=Decimal("0.123456789012345"))
        with database.session() as session:
            ListingRepository(session).upsert(listing)
        with database.session() as session:
            row = ListingRepository(session).get(ListingIdentity("test-venue", "L-precise"))
            assert row is not None
            assert Decimal(row.raw_float) == Decimal("0.123456789012345")


class TestImmutableLedger:
    def _event(self, sequence: int = 0) -> LedgerEvent:
        return LedgerEvent(
            event_id=make_event_id(LedgerEventType.PURCHASE, NOW, sequence, "ref"),
            event_type=LedgerEventType.PURCHASE,
            amount=usd(-1000),
            occurred_at=NOW,
            recorded_at=NOW,
            sequence=sequence,
            contract_id="TU-1",
        )

    def test_events_append(self, database: Database) -> None:
        with database.session() as session:
            LedgerRepository(session).append(self._event())
        with database.session() as session:
            assert LedgerRepository(session).count() == 1

    def test_a_duplicate_event_id_is_refused(self, database: Database) -> None:
        with database.session() as session:
            LedgerRepository(session).append(self._event())
        with pytest.raises(ValueError, match="already exists"), database.session() as session:
            LedgerRepository(session).append(self._event())

    def test_updating_a_persisted_event_raises(self, database: Database) -> None:
        """Corrections are REVERSAL events. History is never rewritten."""
        with database.session() as session:
            LedgerRepository(session).append(self._event())
        with pytest.raises(ImmutableLedgerError), database.session() as session:
            row = session.scalar(select(LedgerEventRow))
            assert row is not None
            row.amount_minor = 0
            session.flush()

    def test_deleting_a_persisted_event_raises(self, database: Database) -> None:
        with database.session() as session:
            LedgerRepository(session).append(self._event())
        with pytest.raises(ImmutableLedgerError), database.session() as session:
            row = session.scalar(select(LedgerEventRow))
            assert row is not None
            session.delete(row)
            session.flush()

    def test_settled_cash_total_reconciles(self, database: Database) -> None:
        with database.session() as session:
            repository = LedgerRepository(session)
            repository.append(self._event(0))
            repository.append(
                LedgerEvent(
                    event_id=make_event_id(LedgerEventType.SALE, NOW, 1, "ref"),
                    event_type=LedgerEventType.SALE,
                    amount=usd(2500),
                    occurred_at=NOW,
                    recorded_at=NOW,
                    sequence=1,
                    contract_id="TU-1",
                )
            )
        with database.session() as session:
            total = LedgerRepository(session).settled_cash_total(Currency.USD)
            assert total == usd(1500)

    def test_an_empty_ledger_reconciles_to_zero(self, database: Database) -> None:
        with database.session() as session:
            assert LedgerRepository(session).settled_cash_total(Currency.USD) == usd(0)


class TestReservationUniqueness:
    def test_only_one_active_reservation_per_listing(self, database: Database) -> None:
        identity = ListingIdentity("v", "L-1")
        with database.session() as session:
            ReservationRepository(session).record(
                identity=identity,
                candidate_id="TU-1",
                state="SOFT_RESERVED",
                reserved_at=NOW,
                expires_at=NOW + timedelta(minutes=10),
            )
        with pytest.raises(IntegrityError), database.session() as session:
            ReservationRepository(session).record(
                identity=identity,
                candidate_id="TU-2",
                state="SOFT_RESERVED",
                reserved_at=NOW,
                expires_at=NOW + timedelta(minutes=10),
            )

    def test_a_listing_can_be_reserved_again_after_release(self, database: Database) -> None:
        """A plain unique constraint would forbid this forever; a partial index does not."""
        identity = ListingIdentity("v", "L-1")
        with database.session() as session:
            repository = ReservationRepository(session)
            repository.record(
                identity=identity,
                candidate_id="TU-1",
                state="SOFT_RESERVED",
                reserved_at=NOW,
                expires_at=NOW + timedelta(minutes=10),
            )
        with database.session() as session:
            assert ReservationRepository(session).deactivate(identity, released_at=NOW) == 1
        with database.session() as session:
            ReservationRepository(session).record(
                identity=identity,
                candidate_id="TU-2",
                state="SOFT_RESERVED",
                reserved_at=NOW,
                expires_at=NOW + timedelta(minutes=10),
            )
        with database.session() as session:
            rows = list(session.scalars(select(ReservationRow)))
            assert len(rows) == 2
            assert sum(1 for r in rows if r.is_active) == 1


class TestCandidateReproducibility:
    def test_a_persisted_candidate_reconstructs_to_the_same_identity(
        self, database: Database
    ) -> None:
        """G9: a stored candidate can be re-derived from its persisted inputs."""
        candidate = make_candidate()
        with database.session() as session:
            CandidateRepository(session).save(candidate, metadata_revision="test-registry")

        with database.session() as session:
            row = CandidateRepository(session).get(candidate.candidate_id)
            assert row is not None
            stored = [
                ListingIdentity(entry["venue"], entry["listing_id"])
                for entry in row.input_identities_json["identities"]
            ]
            assert tuple(sorted(stored)) == candidate.input_identities
            assert row.composition_signature == candidate.composition.signature
            assert row.rule_version == candidate.rule_version

    def test_the_exact_average_float_survives_a_round_trip(self, database: Database) -> None:
        candidate = make_candidate()
        with database.session() as session:
            CandidateRepository(session).save(candidate, metadata_revision="r")
        with database.session() as session:
            reloaded = CandidateRepository(session).average_normalized(candidate.candidate_id)
            assert reloaded == candidate.average_normalized_float
            assert isinstance(reloaded, Fraction)

    def test_saving_the_same_candidate_twice_updates_in_place(self, database: Database) -> None:
        candidate = make_candidate()
        with database.session() as session:
            repository = CandidateRepository(session)
            repository.save(candidate, metadata_revision="r")
            repository.save(candidate, metadata_revision="r")
        with database.session() as session:
            from tradeup.persistence.models import CandidateRow

            assert len(list(session.scalars(select(CandidateRow)))) == 1

    def test_metadata_snapshot_is_recorded_once(self, database: Database) -> None:
        registry = make_registry()
        with database.session() as session:
            repository = MetadataRepository(session)
            repository.record(registry)
            repository.record(registry)
        with database.session() as session:
            from tradeup.persistence.models import MetadataSnapshotRow

            assert len(list(session.scalars(select(MetadataSnapshotRow)))) == 1
