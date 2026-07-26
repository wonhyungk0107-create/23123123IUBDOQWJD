"""Item identity: rarity, quality, wear, float ranges, skins and collections.

Nothing here is inferred from a display name. Knife and glove mappings, quality
eligibility and float caps all come from the metadata registry, because guessing
them from strings is how a procurement system silently buys the wrong asset.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Final

__all__ = [
    "Collection",
    "FloatRange",
    "MAX_FLOAT",
    "MIN_FLOAT",
    "QualityType",
    "Rarity",
    "Skin",
    "WEAR_BOUNDS",
    "WearCondition",
    "classify_wear",
]

MIN_FLOAT: Final[Decimal] = Decimal("0")
MAX_FLOAT: Final[Decimal] = Decimal("1")


class Rarity(enum.StrEnum):
    """Weapon-skin rarity ladder, lowest first.

    ``EXTRAORDINARY`` covers knives and gloves -- the output tier of a Covert
    contract. It has no successor: there is nothing to trade an Extraordinary up
    into, so it can never be a contract input tier.
    """

    CONSUMER = "CONSUMER"
    INDUSTRIAL = "INDUSTRIAL"
    MIL_SPEC = "MIL_SPEC"
    RESTRICTED = "RESTRICTED"
    CLASSIFIED = "CLASSIFIED"
    COVERT = "COVERT"
    EXTRAORDINARY = "EXTRAORDINARY"

    @property
    def ladder_index(self) -> int:
        return _RARITY_ORDER.index(self)

    @property
    def next_rarity(self) -> Rarity | None:
        """The tier a contract of this rarity produces, or ``None`` if terminal."""
        index = self.ladder_index
        if index + 1 >= len(_RARITY_ORDER):
            return None
        return _RARITY_ORDER[index + 1]

    @property
    def can_be_contract_input(self) -> bool:
        return self.next_rarity is not None


_RARITY_ORDER: Final[tuple[Rarity, ...]] = (
    Rarity.CONSUMER,
    Rarity.INDUSTRIAL,
    Rarity.MIL_SPEC,
    Rarity.RESTRICTED,
    Rarity.CLASSIFIED,
    Rarity.COVERT,
    Rarity.EXTRAORDINARY,
)


class QualityType(enum.StrEnum):
    """Orthogonal item quality.

    Whether Souvenir items may be used as contract inputs, and what they produce,
    is a *rule-registry* question -- never a constant in this module.
    """

    NORMAL = "NORMAL"
    STATTRAK = "STATTRAK"
    SOUVENIR = "SOUVENIR"


class WearCondition(enum.StrEnum):
    FACTORY_NEW = "FACTORY_NEW"
    MINIMAL_WEAR = "MINIMAL_WEAR"
    FIELD_TESTED = "FIELD_TESTED"
    WELL_WORN = "WELL_WORN"
    BATTLE_SCARRED = "BATTLE_SCARRED"

    @property
    def market_suffix(self) -> str:
        return _WEAR_SUFFIX[self]


_WEAR_SUFFIX: Final[dict[WearCondition, str]] = {
    WearCondition.FACTORY_NEW: "Factory New",
    WearCondition.MINIMAL_WEAR: "Minimal Wear",
    WearCondition.FIELD_TESTED: "Field-Tested",
    WearCondition.WELL_WORN: "Well-Worn",
    WearCondition.BATTLE_SCARRED: "Battle-Scarred",
}

#: Half-open ``[low, high)`` wear bands, except the final band which is closed at
#: 1.0 so that a float of exactly 1.0 classifies.
WEAR_BOUNDS: Final[tuple[tuple[WearCondition, Decimal, Decimal], ...]] = (
    (WearCondition.FACTORY_NEW, Decimal("0.00"), Decimal("0.07")),
    (WearCondition.MINIMAL_WEAR, Decimal("0.07"), Decimal("0.15")),
    (WearCondition.FIELD_TESTED, Decimal("0.15"), Decimal("0.38")),
    (WearCondition.WELL_WORN, Decimal("0.38"), Decimal("0.45")),
    (WearCondition.BATTLE_SCARRED, Decimal("0.45"), Decimal("1.00")),
)


def classify_wear(value: Decimal) -> WearCondition:
    """Map an absolute float value to its wear band.

    Uses exact Decimal comparisons; a value sitting precisely on a boundary belongs
    to the *higher-wear* band, matching in-game behaviour (0.07 is Minimal Wear).
    """
    if isinstance(value, float):
        raise TypeError("classify_wear does not accept float; pass Decimal.")
    if not (MIN_FLOAT <= value <= MAX_FLOAT):
        raise ValueError(f"float value {value} outside [0, 1]")
    for condition, low, high in WEAR_BOUNDS:
        if low <= value < high:
            return condition
    # Only reachable at exactly 1.0.
    return WearCondition.BATTLE_SCARRED


@dataclass(frozen=True, slots=True)
class FloatRange:
    """A skin's permitted float interval, as published by the metadata source.

    Both bounds are inclusive for membership purposes. A zero-width range is
    rejected: normalisation would divide by zero, and a skin with a single legal
    float does not exist in practice.
    """

    minimum: Decimal
    maximum: Decimal

    def __post_init__(self) -> None:
        for name, value in (("minimum", self.minimum), ("maximum", self.maximum)):
            if isinstance(value, float):
                raise TypeError(f"FloatRange.{name} must be Decimal, not float")
            if not (MIN_FLOAT <= value <= MAX_FLOAT):
                raise ValueError(f"FloatRange.{name}={value} outside [0, 1]")
        if self.minimum >= self.maximum:
            raise ValueError(f"FloatRange requires minimum < maximum, got {self.minimum}..{self.maximum}")

    @property
    def width(self) -> Decimal:
        return self.maximum - self.minimum

    def contains(self, value: Decimal) -> bool:
        return self.minimum <= value <= self.maximum

    def clamp(self, value: Decimal) -> Decimal:
        return min(max(value, self.minimum), self.maximum)

    @property
    def reachable_wears(self) -> tuple[WearCondition, ...]:
        """Wear bands this skin can actually occupy.

        A skin capped at 0.50 can never be Factory New if its minimum is 0.45, and
        an output valuation that assumes otherwise is valuing an item that cannot
        exist.
        """
        found: list[WearCondition] = []
        for condition, low, high in WEAR_BOUNDS:
            band_high = high if condition is not WearCondition.BATTLE_SCARRED else MAX_FLOAT
            overlaps = self.minimum < band_high and self.maximum >= low
            if condition is WearCondition.BATTLE_SCARRED:
                overlaps = self.maximum >= low
            if overlaps and condition not in found:
                found.append(condition)
        return tuple(found)


@dataclass(frozen=True, slots=True)
class Skin:
    """A distinct paint on a distinct weapon, at a distinct rarity.

    Identity is ``skin_id`` from the pinned metadata source. ``market_hash_base`` is
    the name *without* the wear suffix and StatTrak/Souvenir prefix, so market hash
    names are composed rather than parsed.
    """

    skin_id: str
    name: str
    market_hash_base: str
    collection_id: str
    rarity: Rarity
    float_range: FloatRange
    paint_index: int | None = None
    available_qualities: frozenset[QualityType] = field(
        default_factory=lambda: frozenset({QualityType.NORMAL})
    )

    def __post_init__(self) -> None:
        if not self.skin_id:
            raise ValueError("skin_id is required")
        if not self.available_qualities:
            raise ValueError(f"skin {self.skin_id} must have at least one quality")

    def supports(self, quality: QualityType) -> bool:
        return quality in self.available_qualities

    def market_hash_name(self, quality: QualityType, wear: WearCondition) -> str:
        """Compose the canonical market hash name. Never parse one to get here."""
        if not self.supports(quality):
            raise ValueError(f"skin {self.skin_id} does not exist in {quality}")
        prefix = {
            QualityType.NORMAL: "",
            QualityType.STATTRAK: "StatTrak™ ",
            QualityType.SOUVENIR: "Souvenir ",
        }[quality]
        return f"{prefix}{self.market_hash_base} ({wear.market_suffix})"


@dataclass(frozen=True, slots=True)
class Collection:
    """A case or collection: the unit that determines a contract's output pool."""

    collection_id: str
    name: str
    skin_ids: frozenset[str]
    #: Set when a collection is known not to contribute Extraordinary outputs.
    yields_extraordinary: bool = False

    def __post_init__(self) -> None:
        if not self.collection_id:
            raise ValueError("collection_id is required")
