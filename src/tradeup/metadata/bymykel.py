"""Importer for ByMykel/CSGO-API-shaped skin metadata.

Import is from a *pinned snapshot* on disk, not a live fetch. The snapshot's SHA-256
becomes the registry's provenance, so every candidate is attributable to an exact
metadata payload, and a change in upstream data is a diffable event rather than a
silent shift in what the optimizer believes.

Two parsing details carry real weight:

* ``json.loads(..., parse_float=Decimal)``. The upstream payload encodes float caps
  as JSON numbers. Letting Python turn ``0.07`` into a binary float would poison
  every normalisation downstream, and it would do so invisibly.
* Skin identity is ``{upstream_id}@{collection_id}``. Upstream lists a skin's
  collections as an array, and the same paint can appear in more than one set with a
  different output pool. A trade-up cares about the (skin, collection) pair, so that
  pair is the identity.

Anything the payload does not state -- an unmapped rarity, a missing float cap, a
skin with no collection -- is skipped with an ERROR issue. Nothing is inferred from
display names.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Final

from tradeup.domain.items import Collection, FloatRange, QualityType, Rarity, Skin
from tradeup.metadata.registry import IssueSeverity, MetadataIssue, MetadataRegistry

__all__ = [
    "RARITY_BY_UPSTREAM_NAME",
    "ImportResult",
    "load_pinned_snapshot",
    "parse_payload",
]

#: Explicit rarity mapping. An upstream name that is not in this table is an ERROR
#: and the entry is skipped -- never mapped to a "closest" tier.
#:
#: Deliberately absent, with reasons:
#:
#: * ``Contraband`` (the Howl) -- has no trade-up semantics.
#: * ``Master`` -- verified against ``collections.json`` at the pinned revision to be
#:   the *agent/character* rarity (e.g. "Lt. Commander Ricksaw | NSWC SEAL"), not a
#:   knife tier. Mapping it to EXTRAORDINARY would have put agents into knife output
#:   pools.
#: * ``Exceedingly Rare`` -- not observed in the pinned payload; adding it would be a
#:   guess about a label we have never seen.
RARITY_BY_UPSTREAM_NAME: Final[Mapping[str, Rarity]] = {
    "consumer grade": Rarity.CONSUMER,
    "industrial grade": Rarity.INDUSTRIAL,
    "mil-spec grade": Rarity.MIL_SPEC,
    "restricted": Rarity.RESTRICTED,
    "classified": Rarity.CLASSIFIED,
    "covert": Rarity.COVERT,
    "extraordinary": Rarity.EXTRAORDINARY,
}


@dataclass(frozen=True)
class ImportResult:
    """A registry plus everything that went wrong building it."""

    registry: MetadataRegistry
    issues: tuple[MetadataIssue, ...]
    skipped_entries: int
    total_entries: int

    @property
    def errors(self) -> tuple[MetadataIssue, ...]:
        return tuple(i for i in self.issues if i.severity is IssueSeverity.ERROR)

    @property
    def warnings(self) -> tuple[MetadataIssue, ...]:
        return tuple(i for i in self.issues if i.severity is IssueSeverity.WARNING)

    def summary(self) -> dict[str, str]:
        return {
            **self.registry.provenance(),
            "total_entries": str(self.total_entries),
            "skipped_entries": str(self.skipped_entries),
            "errors": str(len(self.errors)),
            "warnings": str(len(self.warnings)),
        }


def _issue(severity: IssueSeverity, code: str, subject: str, message: str) -> MetadataIssue:
    return MetadataIssue(severity=severity, code=code, subject=subject, message=message)


def _coerce_rarity(raw: Any, subject: str) -> tuple[Rarity | None, MetadataIssue | None]:
    if isinstance(raw, Mapping):
        name = raw.get("name")
    else:
        name = raw
    if not isinstance(name, str) or not name.strip():
        return None, _issue(
            IssueSeverity.ERROR, "MISSING_RARITY", subject, "entry has no rarity name"
        )
    mapped = RARITY_BY_UPSTREAM_NAME.get(name.strip().lower())
    if mapped is None:
        return None, _issue(
            IssueSeverity.ERROR,
            "UNMAPPED_RARITY",
            subject,
            f"rarity {name!r} is not in the explicit mapping; refusing to guess a tier",
        )
    return mapped, None


def _coerce_float_range(
    payload: Mapping[str, Any], subject: str
) -> tuple[FloatRange | None, MetadataIssue | None]:
    minimum = payload.get("min_float")
    maximum = payload.get("max_float")
    if minimum is None or maximum is None:
        return None, _issue(
            IssueSeverity.ERROR,
            "MISSING_FLOAT_CAP",
            subject,
            "entry is missing min_float or max_float; float caps are never assumed",
        )
    try:
        low = Decimal(str(minimum))
        high = Decimal(str(maximum))
        return FloatRange(low, high), None
    except (InvalidOperation, ValueError) as exc:
        return None, _issue(
            IssueSeverity.ERROR,
            "INVALID_FLOAT_CAP",
            subject,
            f"float range {minimum}..{maximum} is not usable: {exc}",
        )


def _coerce_collections(payload: Mapping[str, Any]) -> list[tuple[str, str]]:
    """Return ``(collection_id, collection_name)`` pairs stated by the entry."""
    raw = payload.get("collections") or []
    if not isinstance(raw, Sequence) or isinstance(raw, str | bytes):
        return []
    pairs: list[tuple[str, str]] = []
    for item in raw:
        if isinstance(item, Mapping):
            cid = item.get("id")
            name = item.get("name")
            if isinstance(cid, str) and cid:
                pairs.append((cid, name if isinstance(name, str) and name else cid))
        elif isinstance(item, str) and item:
            pairs.append((item, item))
    return pairs


def _coerce_qualities(payload: Mapping[str, Any]) -> frozenset[QualityType]:
    qualities = {QualityType.NORMAL}
    if payload.get("stattrak") is True:
        qualities.add(QualityType.STATTRAK)
    if payload.get("souvenir") is True:
        qualities.add(QualityType.SOUVENIR)
    return frozenset(qualities)


def _coerce_paint_index(payload: Mapping[str, Any]) -> int | None:
    raw = payload.get("paint_index")
    if raw is None:
        return None
    try:
        return int(str(raw))
    except (TypeError, ValueError):
        return None


def parse_payload(
    raw: bytes,
    *,
    source: str,
    revision: str,
    imported_at: datetime,
) -> ImportResult:
    """Parse a pinned snapshot into a registry, collecting issues as it goes."""
    payload_sha256 = hashlib.sha256(raw).hexdigest()
    # parse_float=Decimal is load-bearing: it keeps float caps exact.
    document: Any = json.loads(raw.decode("utf-8"), parse_float=Decimal)

    if isinstance(document, Mapping):
        entries: list[Any] = list(document.values())
    elif isinstance(document, list):
        entries = document
    else:
        raise ValueError(f"unsupported snapshot shape: {type(document).__name__}")

    issues: list[MetadataIssue] = []
    skins: list[Skin] = []
    collection_names: dict[str, str] = {}
    collection_members: dict[str, set[str]] = {}
    skipped = 0

    for entry in entries:
        if not isinstance(entry, Mapping):
            skipped += 1
            issues.append(
                _issue(IssueSeverity.ERROR, "MALFORMED_ENTRY", "<unknown>", "entry is not an object")
            )
            continue

        upstream_id = entry.get("id")
        name = entry.get("name")
        if not isinstance(upstream_id, str) or not upstream_id:
            skipped += 1
            issues.append(
                _issue(IssueSeverity.ERROR, "MISSING_ID", str(name or "<unknown>"), "entry has no id")
            )
            continue
        if not isinstance(name, str) or not name:
            skipped += 1
            issues.append(_issue(IssueSeverity.ERROR, "MISSING_NAME", upstream_id, "entry has no name"))
            continue

        rarity, rarity_issue = _coerce_rarity(entry.get("rarity"), upstream_id)
        if rarity is None:
            skipped += 1
            if rarity_issue is not None:
                issues.append(rarity_issue)
            continue

        float_range, float_issue = _coerce_float_range(entry, upstream_id)
        if float_range is None:
            skipped += 1
            if float_issue is not None:
                issues.append(float_issue)
            continue

        collections = _coerce_collections(entry)
        if not collections:
            # Not an error: many cosmetics genuinely belong to no collection and
            # simply cannot participate in a contract.
            skipped += 1
            issues.append(
                _issue(
                    IssueSeverity.WARNING,
                    "NO_COLLECTION",
                    upstream_id,
                    f"{name!r} states no collection and cannot be used in a contract",
                )
            )
            continue

        qualities = _coerce_qualities(entry)
        paint_index = _coerce_paint_index(entry)

        for collection_id, collection_name in collections:
            collection_names.setdefault(collection_id, collection_name)
            skin_id = f"{upstream_id}@{collection_id}"
            collection_members.setdefault(collection_id, set()).add(skin_id)
            skins.append(
                Skin(
                    skin_id=skin_id,
                    name=name,
                    market_hash_base=name,
                    collection_id=collection_id,
                    rarity=rarity,
                    float_range=float_range,
                    paint_index=paint_index,
                    available_qualities=qualities,
                )
            )

    collections_out = [
        Collection(
            collection_id=collection_id,
            name=collection_names[collection_id],
            skin_ids=frozenset(members),
            yields_extraordinary=any(
                s.rarity is Rarity.EXTRAORDINARY for s in skins if s.collection_id == collection_id
            ),
        )
        for collection_id, members in sorted(collection_members.items())
    ]

    registry = MetadataRegistry(
        source=source,
        revision=revision,
        payload_sha256=payload_sha256,
        imported_at=imported_at,
        skins=sorted(skins, key=lambda s: s.skin_id),
        collections=collections_out,
    )
    issues.extend(registry.validate())

    return ImportResult(
        registry=registry,
        issues=tuple(issues),
        skipped_entries=skipped,
        total_entries=len(entries),
    )


def load_pinned_snapshot(
    path: Path,
    *,
    source: str = "ByMykel/CSGO-API",
    revision: str | None = None,
    imported_at: datetime,
) -> ImportResult:
    """Load and parse a snapshot file.

    ``revision`` defaults to the filename, which is why snapshots are named after
    the upstream commit they were taken from.
    """
    raw = path.read_bytes()
    return parse_payload(
        raw,
        source=source,
        revision=revision or path.stem,
        imported_at=imported_at,
    )
