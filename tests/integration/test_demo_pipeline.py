"""End-to-end offline demo.

Covers acceptance gates G10 (the vertical slice runs from fixture ingestion through
operator-card generation) and G12 (two runs from a clean state agree).

The scenario is required to *exhibit* each failure mode, not merely to be capable of
it. A demo that only ever shows the happy path proves nothing about the gates.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tradeup.config import Settings
from tradeup.demo.runner import default_metadata_path, run_demo
from tradeup.demo.scenario import DEMO_VENUE_A, DEMO_VENUE_B, build_demo_scenario
from tradeup.domain.contracts import RejectionReason
from tradeup.domain.items import Rarity
from tradeup.metadata.bymykel import load_pinned_snapshot

DEMO_NOW = datetime(2026, 7, 25, 12, 0, 0, tzinfo=UTC)


def demo_settings(tmp_path: Path) -> Settings:
    return Settings(
        database_url=f"sqlite+pysqlite:///{(tmp_path / 'demo.db').as_posix()}",
        artifacts_dir=tmp_path / "artifacts",
    )


@pytest.fixture(scope="module")
def demo_result_factory():  # type: ignore[no-untyped-def]
    def _run(tmp_path: Path):  # type: ignore[no-untyped-def]
        return run_demo(
            settings=demo_settings(tmp_path),
            now=DEMO_NOW,
            output_dir=tmp_path / "artifacts" / "evidence",
        )

    return _run


class TestMetadataStage:
    def test_the_pinned_snapshot_imports_without_errors(self) -> None:
        result = load_pinned_snapshot(default_metadata_path(), imported_at=DEMO_NOW)
        assert not result.errors
        assert result.registry.skin_count > 1000
        assert result.registry.collection_count > 50

    def test_the_registry_carries_its_payload_hash(self) -> None:
        result = load_pinned_snapshot(default_metadata_path(), imported_at=DEMO_NOW)
        assert len(result.registry.payload_sha256) == 64

    def test_the_registry_validates(self) -> None:
        result = load_pinned_snapshot(default_metadata_path(), imported_at=DEMO_NOW)
        result.registry.require_valid()


class TestScenarioConstruction:
    def test_the_scenario_spans_two_marketplaces(self) -> None:
        registry = load_pinned_snapshot(default_metadata_path(), imported_at=DEMO_NOW).registry
        scenario = build_demo_scenario(registry, now=DEMO_NOW)
        venues = {listing.identity.venue for listing in scenario.listings}
        assert venues == {DEMO_VENUE_A, DEMO_VENUE_B}

    def test_the_scenario_uses_real_collections_from_the_pinned_registry(self) -> None:
        registry = load_pinned_snapshot(default_metadata_path(), imported_at=DEMO_NOW).registry
        scenario = build_demo_scenario(registry, now=DEMO_NOW)
        for collection_id in scenario.collections:
            assert registry.has_collection(collection_id)
            assert registry.skins_at(collection_id, Rarity.MIL_SPEC)

    def test_the_scenario_declares_its_failure_modes(self) -> None:
        registry = load_pinned_snapshot(default_metadata_path(), imported_at=DEMO_NOW).registry
        scenario = build_demo_scenario(registry, now=DEMO_NOW)
        assert scenario.disappearing
        assert scenario.price_changes
        assert scenario.stale_listing_ids

    def test_it_is_deterministic(self) -> None:
        registry = load_pinned_snapshot(default_metadata_path(), imported_at=DEMO_NOW).registry
        first = build_demo_scenario(registry, now=DEMO_NOW)
        second = build_demo_scenario(registry, now=DEMO_NOW)
        assert [listing.identity for listing in first.listings] == [
            listing.identity for listing in second.listings
        ]
        assert [listing.price for listing in first.listings] == [
            listing.price for listing in second.listings
        ]


class TestEndToEnd:
    def test_the_slice_runs_and_produces_at_least_one_operator_card(self, tmp_path: Path) -> None:
        result = run_demo(
            settings=demo_settings(tmp_path), now=DEMO_NOW, output_dir=tmp_path / "evidence"
        )
        assert result.approved_count >= 1
        card = result.cards[0]
        assert card.input_count == 10
        assert card.inputs
        assert card.outcomes
        assert card.purchase_sequence
        assert card.manual_steam_steps
        assert card.assumptions

    def test_every_required_rejection_mode_is_exercised(self, tmp_path: Path) -> None:
        result = run_demo(
            settings=demo_settings(tmp_path), now=DEMO_NOW, output_dir=tmp_path / "evidence"
        )
        seen = set(result.report.statistics.rejections_by_reason)
        for required in (
            RejectionReason.BELOW_DISCOVERY_ROI,
            RejectionReason.QUOTE_TOO_OLD,
            RejectionReason.LISTING_DISAPPEARED,
            RejectionReason.LISTING_PRICE_CHANGED,
            RejectionReason.ASSET_ALREADY_RESERVED,
        ):
            assert required.value in seen, f"{required.value} was never exercised"

    def test_no_listing_is_allocated_to_two_approved_candidates(self, tmp_path: Path) -> None:
        result = run_demo(
            settings=demo_settings(tmp_path), now=DEMO_NOW, output_dir=tmp_path / "evidence"
        )
        allocated: set[str] = set()
        for outcome in result.report.approved:
            for identity in outcome.candidate.input_identities:
                key = str(identity)
                assert key not in allocated
                allocated.add(key)

    def test_the_card_carries_the_synthetic_data_warning(self, tmp_path: Path) -> None:
        """A synthetic card must never be mistakable for a real recommendation."""
        result = run_demo(
            settings=demo_settings(tmp_path), now=DEMO_NOW, output_dir=tmp_path / "evidence"
        )
        assert result.cards[0].is_synthetic_warning_present

    def test_settled_profit_is_zero_and_says_so(self, tmp_path: Path) -> None:
        result = run_demo(
            settings=demo_settings(tmp_path), now=DEMO_NOW, output_dir=tmp_path / "evidence"
        )
        assert result.settled_cash_minor == 0
        assert result.ledger_event_count == 0

    def test_all_artifacts_are_written(self, tmp_path: Path) -> None:
        result = run_demo(
            settings=demo_settings(tmp_path), now=DEMO_NOW, output_dir=tmp_path / "evidence"
        )
        for kind in ("json", "markdown", "csv"):
            assert kind in result.artifacts
            assert result.artifacts[kind].exists()
            assert result.artifacts[kind].stat().st_size > 0

    def test_the_json_artifact_reports_zero_economic_evidence(self, tmp_path: Path) -> None:
        result = run_demo(
            settings=demo_settings(tmp_path), now=DEMO_NOW, output_dir=tmp_path / "evidence"
        )
        payload = json.loads(result.artifacts["json"].read_text(encoding="utf-8"))
        evidence = payload["economic_evidence"]
        assert evidence["settled_fee_net_profit_minor"] == 0
        assert evidence["orders_placed"] == 0
        assert evidence["trade_ups_completed"] == 0
        assert evidence["sales_settled"] == 0

    def test_the_json_artifact_records_metadata_provenance(self, tmp_path: Path) -> None:
        result = run_demo(
            settings=demo_settings(tmp_path), now=DEMO_NOW, output_dir=tmp_path / "evidence"
        )
        payload = json.loads(result.artifacts["json"].read_text(encoding="utf-8"))
        assert len(payload["metadata"]["payload_sha256"]) == 64
        assert payload["data_nature"].startswith("SYNTHETIC")

    def test_candidates_and_rejections_are_persisted(self, tmp_path: Path) -> None:
        from sqlalchemy import select

        from tradeup.persistence.database import create_database
        from tradeup.persistence.models import (
            CandidateEvaluationRow,
            CandidateRejectionRow,
            CandidateRow,
        )

        settings = demo_settings(tmp_path)
        result = run_demo(settings=settings, now=DEMO_NOW, output_dir=tmp_path / "evidence")
        database = create_database(settings.database_url)
        with database.session() as session:
            candidates = list(session.scalars(select(CandidateRow)))
            evaluations = list(session.scalars(select(CandidateEvaluationRow)))
            rejections = list(session.scalars(select(CandidateRejectionRow)))
        database.dispose()

        total = result.approved_count + result.rejected_count
        assert len(candidates) == total
        assert len(evaluations) == total
        assert len(rejections) == result.rejected_count

    def test_rejections_persist_machine_readable_reason_codes(self, tmp_path: Path) -> None:
        from sqlalchemy import select

        from tradeup.persistence.database import create_database
        from tradeup.persistence.models import CandidateRejectionRow

        settings = demo_settings(tmp_path)
        run_demo(settings=settings, now=DEMO_NOW, output_dir=tmp_path / "evidence")
        database = create_database(settings.database_url)
        with database.session() as session:
            rows = list(session.scalars(select(CandidateRejectionRow)))
        database.dispose()
        assert rows
        for row in rows:
            assert row.reasons
            for code in row.reasons.split(","):
                RejectionReason(code)  # raises if it is not a known code


class TestReproducibility:
    def test_two_runs_from_clean_state_agree(self, tmp_path: Path) -> None:
        """G12: identical inputs, identical results."""
        first = run_demo(
            settings=demo_settings(tmp_path / "a"), now=DEMO_NOW, output_dir=tmp_path / "a" / "e"
        )
        second = run_demo(
            settings=demo_settings(tmp_path / "b"), now=DEMO_NOW, output_dir=tmp_path / "b" / "e"
        )

        assert first.approved_count == second.approved_count
        assert first.rejected_count == second.rejected_count
        assert [c.contract_id for c in first.cards] == [c.contract_id for c in second.cards]
        assert (
            first.report.statistics.rejections_by_reason
            == second.report.statistics.rejections_by_reason
        )

    def test_the_markdown_artifact_is_identical_across_runs(self, tmp_path: Path) -> None:
        """Every line agrees except the measured runtime.

        Wall-clock duration is an observation about this machine, not a result of the
        scan, so it is normalised out rather than removed from the report -- knowing
        a scan took 0.2s or 40s is worth keeping. Everything that feeds a decision
        must match exactly.
        """
        first = run_demo(
            settings=demo_settings(tmp_path / "a"), now=DEMO_NOW, output_dir=tmp_path / "a" / "e"
        )
        second = run_demo(
            settings=demo_settings(tmp_path / "b"), now=DEMO_NOW, output_dir=tmp_path / "b" / "e"
        )

        def normalise(path: Path) -> list[str]:
            return [
                "| runtime_seconds | <measured> |"
                if line.startswith("| runtime_seconds |")
                else line
                for line in path.read_text(encoding="utf-8").splitlines()
            ]

        left = normalise(first.artifacts["markdown"])
        right = normalise(second.artifacts["markdown"])
        assert left == right
        # And the normalisation must not be doing all the work.
        assert len(left) > 50
        assert any(line.startswith("| runtime_seconds |") for line in left)

    def test_candidate_ids_are_content_derived(self, tmp_path: Path) -> None:
        result = run_demo(
            settings=demo_settings(tmp_path), now=DEMO_NOW, output_dir=tmp_path / "evidence"
        )
        for outcome in result.report.all_outcomes:
            assert outcome.candidate.candidate_id == outcome.candidate.compute_id()
