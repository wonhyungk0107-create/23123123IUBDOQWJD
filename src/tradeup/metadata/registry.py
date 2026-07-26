"""Item metadata registry.

Owns identity: which skins exist, what collection and rarity they belong to, their
float caps, and -- critically -- which outputs each collection can produce at the
next rarity. Output pools are derived from *stated* collection membership and
rarity, never from parsing display names, so a knife can only appear in a pool
because the metadata source put it in that collection.

Every registry carries the source revision and a hash of the exact payload it was
built from. A candidate is therefore always attributable to a specific metadata
state, which is what makes a later disagreement diagnosable instead of mysterious.
"""

from __future__ import annotations

import enum
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from tradeup.domain.items import Collection, QualityType, Rarity, Skin
from tradeup.domain.rules import EligibleOutputPool

__all__ = [
    "IssueSeverity",
    "MetadataIssue",
    "MetadataRegistry",
    "MetadataValidationError",
    "UnknownCollectionError",
    "UnknownSkinError",
]


class UnknownSkinError(KeyError):
    """Raised for a skin id the registry has never seen."""


class UnknownCollectionError(KeyError):
    """Raised for a collection id the registry has never seen."""


class MetadataValidationError(Exception):
    """Raised when a registry with ERROR-level issues is used for money decisions."""


class IssueSeverity(enum.StrEnum):
    ERROR = "ERROR"
    """Blocks promotion. The registry cannot back a purchase decision."""

    WARNING = "WARNING"
    """Recorded and surfaced, but does not block."""


@dataclass(frozen=True, slots=True)
class MetadataIssue:
    """One problem found during validation, with a machine-readable code."""

    severity: IssueSeverity
    code: str
    subject: str
    message: str

    def __str__(self) -> str:
        return f"[{self.severity.value}] {self.code} {self.subject}: {self.message}"


class MetadataRegistry:
    """Immutable view of item metadata at one source revision."""

    def __init__(
        self,
        *,
        source: str,
        revision: str,
        payload_sha256: str,
        imported_at: datetime,
        skins: Sequence[Skin],
        collections: Sequence[Collection],
    ) -> None:
        if imported_at.tzinfo is None:
            raise ValueError("imported_at must be timezone-aware UTC")
        if not payload_sha256:
            raise ValueError("a registry requires the hash of the payload it came from")

        skin_ids = [s.skin_id for s in skins]
        duplicate_skins = {s for s in skin_ids if skin_ids.count(s) > 1}
        if duplicate_skins:
            raise ValueError(f"duplicate skin ids: {sorted(duplicate_skins)}")
        collection_ids = [c.collection_id for c in collections]
        duplicate_collections = {c for c in collection_ids if collection_ids.count(c) > 1}
        if duplicate_collections:
            raise ValueError(f"duplicate collection ids: {sorted(duplicate_collections)}")

        self.source = source
        self.revision = revision
        self.payload_sha256 = payload_sha256
        self.imported_at = imported_at
        self._skins: Mapping[str, Skin] = {s.skin_id: s for s in skins}
        self._collections: Mapping[str, Collection] = {c.collection_id: c for c in collections}

        # Index skins by (collection, rarity) once; pool lookups are hot in the
        # composition enumerator.
        index: dict[tuple[str, Rarity], list[Skin]] = {}
        for skin in skins:
            index.setdefault((skin.collection_id, skin.rarity), []).append(skin)
        self._by_collection_rarity: Mapping[tuple[str, Rarity], tuple[Skin, ...]] = {
            key: tuple(sorted(value, key=lambda s: s.skin_id)) for key, value in index.items()
        }

    # -- lookups -------------------------------------------------------------

    def __len__(self) -> int:
        return len(self._skins)

    @property
    def skin_count(self) -> int:
        return len(self._skins)

    @property
    def collection_count(self) -> int:
        return len(self._collections)

    @property
    def skins(self) -> tuple[Skin, ...]:
        return tuple(self._skins.values())

    @property
    def collections(self) -> tuple[Collection, ...]:
        return tuple(self._collections.values())

    def skin(self, skin_id: str) -> Skin:
        try:
            return self._skins[skin_id]
        except KeyError:
            raise UnknownSkinError(f"unknown skin id {skin_id!r} in {self.revision}") from None

    def has_skin(self, skin_id: str) -> bool:
        return skin_id in self._skins

    def collection(self, collection_id: str) -> Collection:
        try:
            return self._collections[collection_id]
        except KeyError:
            raise UnknownCollectionError(
                f"unknown collection id {collection_id!r} in {self.revision}"
            ) from None

    def has_collection(self, collection_id: str) -> bool:
        return collection_id in self._collections

    def skins_at(self, collection_id: str, rarity: Rarity) -> tuple[Skin, ...]:
        return self._by_collection_rarity.get((collection_id, rarity), ())

    def eligible_inputs(
        self, collection_id: str, rarity: Rarity, quality: QualityType
    ) -> tuple[Skin, ...]:
        """Skins in a collection at a rarity that exist in the requested quality."""
        return tuple(s for s in self.skins_at(collection_id, rarity) if s.supports(quality))

    # -- output pools --------------------------------------------------------

    def output_pool(self, collection_id: str, input_rarity: Rarity) -> EligibleOutputPool:
        """Outputs a collection can produce one rarity above ``input_rarity``.

        Returns an *empty* pool rather than raising when the collection has no
        outputs at the next tier. Emptiness is a legitimate, common state (most
        collections top out below Covert), and the probability engine already fails
        closed on it -- so the caller that tried to build a contract gets the error,
        not the caller that was merely enumerating.
        """
        if not self.has_collection(collection_id):
            raise UnknownCollectionError(f"unknown collection id {collection_id!r}")
        output_rarity = input_rarity.next_rarity
        if output_rarity is None:
            return EligibleOutputPool(
                collection_id=collection_id,
                input_rarity=input_rarity,
                output_rarity=input_rarity,
                output_skin_ids=(),
            )
        outputs = self.skins_at(collection_id, output_rarity)
        return EligibleOutputPool(
            collection_id=collection_id,
            input_rarity=input_rarity,
            output_rarity=output_rarity,
            output_skin_ids=tuple(s.skin_id for s in outputs),
        )

    def output_pools_for(
        self, collection_ids: Sequence[str], input_rarity: Rarity
    ) -> dict[str, EligibleOutputPool]:
        return {cid: self.output_pool(cid, input_rarity) for cid in collection_ids}

    def collections_with_inputs_at(self, rarity: Rarity) -> tuple[str, ...]:
        """Collections that have at least one skin at ``rarity`` *and* an output pool."""
        result = []
        for collection_id in sorted(self._collections):
            if not self.skins_at(collection_id, rarity):
                continue
            if self.output_pool(collection_id, rarity).is_empty:
                continue
            result.append(collection_id)
        return tuple(result)

    # -- validation ----------------------------------------------------------

    def validate(self) -> tuple[MetadataIssue, ...]:
        """Structural checks. Returns issues rather than raising, so callers can log."""
        issues: list[MetadataIssue] = []

        for skin in self.skins:
            if not self.has_collection(skin.collection_id):
                issues.append(
                    MetadataIssue(
                        severity=IssueSeverity.ERROR,
                        code="UNKNOWN_COLLECTION_REFERENCE",
                        subject=skin.skin_id,
                        message=(
                            f"skin references collection {skin.collection_id!r} "
                            "which is not in this snapshot"
                        ),
                    )
                )

        for collection in self.collections:
            for skin_id in sorted(collection.skin_ids):
                if not self.has_skin(skin_id):
                    issues.append(
                        MetadataIssue(
                            severity=IssueSeverity.ERROR,
                            code="UNKNOWN_SKIN_REFERENCE",
                            subject=collection.collection_id,
                            message=f"collection lists unknown skin {skin_id!r}",
                        )
                    )
            members = {s.skin_id for s in self.skins if s.collection_id == collection.collection_id}
            missing = members - set(collection.skin_ids)
            if missing:
                issues.append(
                    MetadataIssue(
                        severity=IssueSeverity.ERROR,
                        code="COLLECTION_MEMBERSHIP_DISAGREEMENT",
                        subject=collection.collection_id,
                        message=(
                            "skins claim membership but are absent from the collection roster: "
                            f"{sorted(missing)}"
                        ),
                    )
                )

        # A collection that can supply inputs but produces nothing is a dead end.
        # Worth surfacing, but not an error: it is the normal state of most sets.
        for collection in self.collections:
            for rarity in Rarity:
                if rarity is Rarity.EXTRAORDINARY:
                    continue
                if not self.skins_at(collection.collection_id, rarity):
                    continue
                if self.output_pool(collection.collection_id, rarity).is_empty:
                    issues.append(
                        MetadataIssue(
                            severity=IssueSeverity.WARNING,
                            code="EMPTY_OUTPUT_POOL",
                            subject=f"{collection.collection_id}/{rarity.value}",
                            message=(
                                "collection has inputs at this rarity but no outputs one "
                                "tier higher; contracts using it will be rejected"
                            ),
                        )
                    )

        for collection in self.collections:
            if collection.yields_extraordinary:
                extraordinary = self.skins_at(collection.collection_id, Rarity.EXTRAORDINARY)
                if not extraordinary:
                    issues.append(
                        MetadataIssue(
                            severity=IssueSeverity.ERROR,
                            code="MISSING_EXTRAORDINARY_MAPPING",
                            subject=collection.collection_id,
                            message=(
                                "collection is declared to yield knives/gloves but the "
                                "snapshot contains no Extraordinary skins for it; these "
                                "must be stated explicitly, never inferred from names"
                            ),
                        )
                    )

        return tuple(issues)

    def errors(self) -> tuple[MetadataIssue, ...]:
        return tuple(i for i in self.validate() if i.severity is IssueSeverity.ERROR)

    def require_valid(self) -> None:
        """Raise unless the registry is free of ERROR-level issues."""
        errors = self.errors()
        if errors:
            rendered = "\n  ".join(str(e) for e in errors)
            raise MetadataValidationError(
                f"metadata registry {self.revision} has {len(errors)} error(s):\n  {rendered}"
            )

    def provenance(self) -> dict[str, str]:
        return {
            "source": self.source,
            "revision": self.revision,
            "payload_sha256": self.payload_sha256,
            "imported_at": self.imported_at.isoformat(),
            "skin_count": str(self.skin_count),
            "collection_count": str(self.collection_count),
        }

    def __repr__(self) -> str:
        return (
            f"MetadataRegistry(source={self.source!r}, revision={self.revision!r}, "
            f"skins={self.skin_count}, collections={self.collection_count})"
        )
