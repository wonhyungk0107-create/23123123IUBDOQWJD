"""Reduce a pinned ByMykel/CSGO-API ``skins.json`` to the fields this project consumes.

The upstream payload is ~5.5 MB and mostly fields we never read (image URLs,
localised descriptions, weapon/team objects). Committing it whole would bloat every
clone for no analytical gain, but committing *nothing* would make the metadata
un-pinned and the demo non-deterministic.

So we commit a projection plus a manifest that records the upstream commit and the
SHA-256 of the original payload. Anyone can re-download that exact commit, re-run
this script, and byte-compare the result -- which is what makes "pin commit, diff
every update" an actual procedure rather than an intention.

Usage::

    uv run python tools/project_bymykel.py <raw-skins.json> <upstream-commit-sha>

Decimal handling is load-bearing: float caps are read with ``parse_float=Decimal``
and re-emitted as their exact original text, so no binary float ever exists.
"""

from __future__ import annotations

import hashlib
import json
import sys
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

OUTPUT_DIR = Path(__file__).resolve().parent.parent / "data" / "metadata"
UPSTREAM_PATH = "public/api/en/skins.json"
UPSTREAM_REPO = "ByMykel/CSGO-API"

#: Only these keys survive the projection.
KEPT_FIELDS = (
    "id",
    "name",
    "rarity",
    "collections",
    "min_float",
    "max_float",
    "stattrak",
    "souvenir",
    "paint_index",
)


class DecimalEncoder(json.JSONEncoder):
    """Emit Decimal as an exact JSON number, preserving the original text."""

    def default(self, o: Any) -> Any:
        if isinstance(o, Decimal):
            return float(o)  # pragma: no cover - replaced by the raw-text pass below
        return super().default(o)


def _encode(value: Any) -> str:
    """Serialise with Decimals rendered as their exact literal text."""
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, bool) or value is None:
        return json.dumps(value)
    if isinstance(value, int | float):
        return json.dumps(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, Mapping):
        inner = ",".join(f"{json.dumps(k, ensure_ascii=False)}:{_encode(v)}" for k, v in value.items())
        return "{" + inner + "}"
    if isinstance(value, Sequence):
        return "[" + ",".join(_encode(v) for v in value) + "]"
    raise TypeError(f"cannot encode {type(value).__name__}")


def project(entries: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Keep consumed fields, and only entries that state a collection."""
    projected: list[dict[str, Any]] = []
    for entry in entries:
        collections = entry.get("collections") or []
        if not collections:
            continue
        row: dict[str, Any] = {}
        for field in KEPT_FIELDS:
            if field not in entry:
                continue
            value = entry[field]
            if field == "rarity" and isinstance(value, Mapping):
                value = {"name": value.get("name")}
            elif field == "collections" and isinstance(value, Sequence):
                value = [
                    {"id": c.get("id"), "name": c.get("name")}
                    for c in value
                    if isinstance(c, Mapping)
                ]
            row[field] = value
        projected.append(row)
    projected.sort(key=lambda r: str(r.get("id", "")))
    return projected


def main(argv: Sequence[str]) -> int:
    if len(argv) != 3:
        print(__doc__)
        return 2
    raw_path = Path(argv[1])
    commit_sha = argv[2]

    raw = raw_path.read_bytes()
    upstream_sha256 = hashlib.sha256(raw).hexdigest()
    document = json.loads(raw.decode("utf-8"), parse_float=Decimal)
    entries = list(document.values()) if isinstance(document, Mapping) else list(document)

    projected = project([e for e in entries if isinstance(e, Mapping)])
    payload = _encode(projected).encode("utf-8")
    projection_sha256 = hashlib.sha256(payload).hexdigest()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    short = commit_sha[:7]
    projection_path = OUTPUT_DIR / f"bymykel-{short}.json"
    manifest_path = OUTPUT_DIR / f"bymykel-{short}.manifest.json"

    projection_path.write_bytes(payload)
    manifest = {
        "upstream_repo": UPSTREAM_REPO,
        "upstream_path": UPSTREAM_PATH,
        "upstream_commit": commit_sha,
        "upstream_url": (
            f"https://raw.githubusercontent.com/{UPSTREAM_REPO}/{commit_sha}/{UPSTREAM_PATH}"
        ),
        "upstream_bytes": len(raw),
        "upstream_sha256": upstream_sha256,
        "upstream_entry_count": len(entries),
        "projection_file": projection_path.name,
        "projection_bytes": len(payload),
        "projection_sha256": projection_sha256,
        "projection_entry_count": len(projected),
        "projected_fields": list(KEPT_FIELDS),
        "projected_at": datetime.now(UTC).isoformat(),
        "tool": "tools/project_bymykel.py",
        "note": (
            "Entries without a stated collection are dropped: they cannot participate "
            "in a trade-up contract."
        ),
    }
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    print(f"upstream   {len(raw):>10,} bytes  sha256={upstream_sha256}")
    print(f"projection {len(payload):>10,} bytes  sha256={projection_sha256}")
    print(f"entries    {len(entries):>10,} -> {len(projected):,}")
    print(f"wrote {projection_path}")
    print(f"wrote {manifest_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
