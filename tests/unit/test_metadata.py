"""Metadata import and registry validation. Mostly about failing closed."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from tests.factories import make_registry, make_skin

from tradeup.domain.items import Collection, QualityType, Rarity
from tradeup.metadata.bymykel import RARITY_BY_UPSTREAM_NAME, parse_payload
from tradeup.metadata.registry import (
    IssueSeverity,
    MetadataRegistry,
    MetadataValidationError,
    UnknownCollectionError,
    UnknownSkinError,
)

NOW = datetime(2026, 7, 25, tzinfo=UTC)


def entry(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": "skin-1",
        "name": "AK-47 | Redline",
        "rarity": {"name": "Mil-Spec Grade"},
        "collections": [{"id": "col-a", "name": "Collection A"}],
        "min_float": 0.1,
        "max_float": 0.7,
        "stattrak": True,
        "souvenir": False,
        "paint_index": "282",
    }
    base.update(overrides)
    return base


def load(entries: list[Any]) -> Any:
    return parse_payload(
        json.dumps(entries).encode("utf-8"),
        source="test",
        revision="rev-1",
        imported_at=NOW,
    )


class TestImportSuccess:
    def test_parses_a_well_formed_entry(self) -> None:
        result = load([entry()])
        assert result.registry.skin_count == 1
        skin = result.registry.skin("skin-1@col-a")
        assert skin.rarity is Rarity.MIL_SPEC
        assert skin.float_range.minimum == Decimal("0.1")
        assert skin.paint_index == 282

    def test_float_caps_are_decimal_not_binary_float(self) -> None:
        """The whole money discipline dies here if the JSON decoder wins."""
        result = load([entry(min_float=0.07, max_float=0.15)])
        skin = result.registry.skin("skin-1@col-a")
        assert isinstance(skin.float_range.minimum, Decimal)
        assert skin.float_range.minimum == Decimal("0.07")

    def test_stattrak_and_souvenir_flags_become_qualities(self) -> None:
        result = load([entry(stattrak=True, souvenir=True)])
        skin = result.registry.skin("skin-1@col-a")
        assert skin.available_qualities == frozenset(
            {QualityType.NORMAL, QualityType.STATTRAK, QualityType.SOUVENIR}
        )

    def test_a_skin_in_two_collections_becomes_two_entries(self) -> None:
        """Identity is (skin, collection): the output pool differs per set."""
        result = load(
            [
                entry(
                    collections=[
                        {"id": "col-a", "name": "A"},
                        {"id": "col-b", "name": "B"},
                    ]
                )
            ]
        )
        assert result.registry.has_skin("skin-1@col-a")
        assert result.registry.has_skin("skin-1@col-b")
        assert result.registry.collection_count == 2

    def test_payload_hash_is_recorded(self) -> None:
        assert len(load([entry()]).registry.payload_sha256) == 64

    def test_a_dict_shaped_payload_is_accepted(self) -> None:
        payload = json.dumps({"skin-1": entry()}).encode("utf-8")
        result = parse_payload(payload, source="t", revision="r", imported_at=NOW)
        assert result.registry.skin_count == 1


class TestImportFailsClosed:
    def test_unmapped_rarity_is_skipped_with_an_error(self) -> None:
        result = load([entry(rarity={"name": "Contraband"})])
        assert result.skipped_entries == 1
        assert any(i.code == "UNMAPPED_RARITY" for i in result.errors)

    def test_master_rarity_is_not_treated_as_a_knife_tier(self) -> None:
        """Verified to be the agent rarity; mapping it would poison knife pools."""
        assert "master" not in RARITY_BY_UPSTREAM_NAME
        result = load([entry(rarity={"name": "Master"})])
        assert result.skipped_entries == 1

    def test_missing_float_cap_is_skipped_with_an_error(self) -> None:
        payload = entry()
        del payload["min_float"]
        result = load([payload])
        assert any(i.code == "MISSING_FLOAT_CAP" for i in result.errors)

    def test_inverted_float_range_is_skipped(self) -> None:
        result = load([entry(min_float=0.9, max_float=0.1)])
        assert any(i.code == "INVALID_FLOAT_CAP" for i in result.errors)

    def test_missing_id_is_skipped(self) -> None:
        payload = entry()
        del payload["id"]
        result = load([payload])
        assert any(i.code == "MISSING_ID" for i in result.errors)

    def test_missing_name_is_skipped(self) -> None:
        payload = entry()
        del payload["name"]
        assert any(i.code == "MISSING_NAME" for i in load([payload]).errors)

    def test_missing_rarity_is_skipped(self) -> None:
        payload = entry()
        del payload["rarity"]
        assert any(i.code == "MISSING_RARITY" for i in load([payload]).errors)

    def test_an_entry_without_a_collection_is_a_warning_not_an_error(self) -> None:
        """Plenty of cosmetics have no collection. They just cannot be contracted."""
        result = load([entry(collections=[])])
        assert result.skipped_entries == 1
        assert not result.errors
        assert any(i.code == "NO_COLLECTION" for i in result.warnings)

    def test_a_non_object_entry_is_an_error(self) -> None:
        assert any(i.code == "MALFORMED_ENTRY" for i in load(["not-an-object"]).errors)

    def test_an_unsupported_top_level_shape_raises(self) -> None:
        with pytest.raises(ValueError, match="unsupported snapshot shape"):
            parse_payload(b'"just a string"', source="t", revision="r", imported_at=NOW)


class TestRegistryQueries:
    def test_unknown_skin_raises(self) -> None:
        with pytest.raises(UnknownSkinError):
            make_registry().skin("nope")

    def test_unknown_collection_raises(self) -> None:
        with pytest.raises(UnknownCollectionError):
            make_registry().collection("nope")

    def test_output_pool_for_a_terminal_rarity_is_empty(self) -> None:
        registry = make_registry()
        pool = registry.output_pool("col-a", Rarity.EXTRAORDINARY)
        assert pool.is_empty

    def test_output_pool_for_an_unknown_collection_raises(self) -> None:
        with pytest.raises(UnknownCollectionError):
            make_registry().output_pool("nope", Rarity.MIL_SPEC)

    def test_collections_with_inputs_excludes_dead_ends(self) -> None:
        registry = make_registry(
            collections={
                "usable": {
                    Rarity.MIL_SPEC: ["u-in"],
                    Rarity.RESTRICTED: ["u-out"],
                },
                "dead-end": {Rarity.MIL_SPEC: ["d-in"]},
            }
        )
        assert registry.collections_with_inputs_at(Rarity.MIL_SPEC) == ("usable",)

    def test_eligible_inputs_filters_by_quality(self) -> None:
        registry = MetadataRegistry(
            source="t",
            revision="r",
            payload_sha256="0" * 64,
            imported_at=NOW,
            skins=[
                make_skin("normal-only", qualities=frozenset({QualityType.NORMAL})),
                make_skin(
                    "with-st",
                    qualities=frozenset({QualityType.NORMAL, QualityType.STATTRAK}),
                ),
            ],
            collections=[
                Collection(
                    collection_id="col-a",
                    name="A",
                    skin_ids=frozenset({"normal-only", "with-st"}),
                )
            ],
        )
        stattrak = registry.eligible_inputs("col-a", Rarity.MIL_SPEC, QualityType.STATTRAK)
        assert [s.skin_id for s in stattrak] == ["with-st"]

    def test_provenance_is_reported(self) -> None:
        provenance = make_registry().provenance()
        assert provenance["revision"] == "test-registry"
        # Default layout: col-a has 2 inputs + 2 outputs, col-b has 1 + 1.
        assert provenance["skin_count"] == "6"
        assert provenance["collection_count"] == "2"


class TestRegistryValidation:
    def test_a_skin_referencing_an_unknown_collection_is_an_error(self) -> None:
        registry = MetadataRegistry(
            source="t",
            revision="r",
            payload_sha256="0" * 64,
            imported_at=NOW,
            skins=[make_skin("orphan", collection_id="missing")],
            collections=[],
        )
        codes = {i.code for i in registry.errors()}
        assert "UNKNOWN_COLLECTION_REFERENCE" in codes

    def test_a_collection_listing_an_unknown_skin_is_an_error(self) -> None:
        registry = MetadataRegistry(
            source="t",
            revision="r",
            payload_sha256="0" * 64,
            imported_at=NOW,
            skins=[],
            collections=[
                Collection(collection_id="col-a", name="A", skin_ids=frozenset({"ghost"}))
            ],
        )
        assert "UNKNOWN_SKIN_REFERENCE" in {i.code for i in registry.errors()}

    def test_membership_disagreement_is_an_error(self) -> None:
        registry = MetadataRegistry(
            source="t",
            revision="r",
            payload_sha256="0" * 64,
            imported_at=NOW,
            skins=[make_skin("member", collection_id="col-a")],
            collections=[Collection(collection_id="col-a", name="A", skin_ids=frozenset())],
        )
        assert "COLLECTION_MEMBERSHIP_DISAGREEMENT" in {i.code for i in registry.errors()}

    def test_a_declared_knife_collection_without_knives_is_an_error(self) -> None:
        registry = MetadataRegistry(
            source="t",
            revision="r",
            payload_sha256="0" * 64,
            imported_at=NOW,
            skins=[make_skin("only-covert", rarity=Rarity.COVERT)],
            collections=[
                Collection(
                    collection_id="col-a",
                    name="A",
                    skin_ids=frozenset({"only-covert"}),
                    yields_extraordinary=True,
                )
            ],
        )
        assert "MISSING_EXTRAORDINARY_MAPPING" in {i.code for i in registry.errors()}

    def test_require_valid_raises_on_errors(self) -> None:
        registry = MetadataRegistry(
            source="t",
            revision="r",
            payload_sha256="0" * 64,
            imported_at=NOW,
            skins=[make_skin("orphan", collection_id="missing")],
            collections=[],
        )
        with pytest.raises(MetadataValidationError, match="error"):
            registry.require_valid()

    def test_require_valid_passes_on_a_sound_registry(self) -> None:
        make_registry().require_valid()

    def test_a_dead_end_collection_is_only_a_warning(self) -> None:
        registry = make_registry(collections={"dead": {Rarity.MIL_SPEC: ["x"]}})
        issues = registry.validate()
        assert not registry.errors()
        assert any(
            i.code == "EMPTY_OUTPUT_POOL" and i.severity is IssueSeverity.WARNING for i in issues
        )

    def test_duplicate_skin_ids_are_rejected_at_construction(self) -> None:
        with pytest.raises(ValueError, match="duplicate skin ids"):
            MetadataRegistry(
                source="t",
                revision="r",
                payload_sha256="0" * 64,
                imported_at=NOW,
                skins=[make_skin("same"), make_skin("same")],
                collections=[],
            )

    def test_a_registry_requires_a_payload_hash(self) -> None:
        with pytest.raises(ValueError, match="requires the hash"):
            MetadataRegistry(
                source="t",
                revision="r",
                payload_sha256="",
                imported_at=NOW,
                skins=[],
                collections=[],
            )

    def test_issue_renders_readably(self) -> None:
        registry = make_registry(collections={"dead": {Rarity.MIL_SPEC: ["x"]}})
        rendered = str(registry.validate()[0])
        assert "WARNING" in rendered and "EMPTY_OUTPUT_POOL" in rendered
