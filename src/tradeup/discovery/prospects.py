"""Catalog-wide trade-up prospect sweep.

Public trade-up sites surface "+EV" contracts by evaluating the entire item
catalogue against condition-level price references. There is nothing secret in
that: the mathematics are the game's public rules, which this repository already
implements exactly. What they add is a search strategy, and this module is that
strategy on licit data — the pinned metadata registry for identity and float
caps, and Skinport's documented keyless ``/v1/items`` endpoint for a one-request
price surface over every item. Nothing here touches an undocumented endpoint or a
path a robots.txt disallows.

A *prospect* is a (collection, quality, input-wear) contract sketch: ten of the
cheapest eligible inputs at that wear, exact outcome probabilities, and outputs
valued from Skinport *asks* less the sourced sale fee and an ask haircut. Two
honesty rules govern the output:

* **A prospect is a lead, not a candidate.** Asks are aspirations, the input
  float is an assumed point inside the wear band, and nothing has been checked
  against a purchasable listing. Every artifact says so, and the only sanctioned
  next step is the exact-listing scanner.
* **Unpriceable probability mass disqualifies.** If more than the configured
  share of outcomes has no quote, the prospect is dropped and counted rather
  than valued optimistically at whatever happened to have a price.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from fractions import Fraction

from tradeup.adapters.skinport import SkinportItemQuote
from tradeup.domain.fees import FeeOperation, FeeSchedule, UnknownFeeError
from tradeup.domain.items import WEAR_BOUNDS, QualityType, Rarity, Skin, WearCondition
from tradeup.domain.mathematics import (
    MathematicsError,
    outcome_probabilities,
    project_output_wear,
)
from tradeup.domain.money import BalanceType, Currency, Money
from tradeup.domain.rules import EligibleOutputPool, RuleViolation, TradeupRuleSet
from tradeup.metadata.registry import MetadataRegistry
from tradeup.valuation.expected_value import probability_weighted

__all__ = ["InputPlan", "Prospect", "ProspectPolicy", "SweepStatistics", "sweep_prospects"]

#: The venue whose asks price the sweep and whose sale fee nets the outputs.
_REFERENCE_VENUE = "skinport"


@dataclass(frozen=True)
class ProspectPolicy:
    """Stated sweep assumptions. Every one is a prior, not a measurement."""

    #: Discount on output asks: nobody has paid an ask. Matches the exit policy's
    #: DEPTH_ADJUSTED_ASK haircut so sweep and scanner speak the same dialect.
    ask_haircut: Decimal = Decimal("0.12")
    #: Where in the (wear band ∩ float range) interval inputs are assumed to sit.
    #: 1/2 is the midpoint; sourcing low-float inputs cheaply is not assumed.
    wear_point: Fraction = Fraction(1, 2)
    #: Maximum outcome probability mass allowed to have no price quote.
    max_unpriced_probability: Fraction = Fraction(1, 10)
    #: Mixed sketches pair every collection with this many of the cheapest-input
    #: collections. A profitable mix needs a cheap side, so pruning the expensive
    #: pairings bounds the search without hiding the economically plausible ones.
    mixed_filler_pool: int = 15

    def __post_init__(self) -> None:
        if not (Decimal(0) <= self.ask_haircut < Decimal(1)):
            raise ValueError("ask_haircut must be within [0, 1)")
        if not (0 <= self.wear_point <= 1):
            raise ValueError("wear_point must be within [0, 1]")
        if not (0 <= self.max_unpriced_probability <= 1):
            raise ValueError("max_unpriced_probability must be within [0, 1]")
        if self.mixed_filler_pool < 1:
            raise ValueError("mixed_filler_pool must be at least 1")


@dataclass(frozen=True, slots=True)
class InputPlan:
    """Units of one skin the sketch would buy, at the reference ask."""

    skin_id: str
    market_hash_name: str
    units: int
    unit_price: Money

    @property
    def cost(self) -> Money:
        return self.unit_price.scaled_up(self.units)


@dataclass(frozen=True)
class Prospect:
    """One ranked contract sketch. A lead for the exact-listing scanner."""

    collection_id: str
    collection_name: str
    input_rarity: Rarity
    quality: QualityType
    input_wear: WearCondition
    rule_version: str
    inputs: tuple[InputPlan, ...]
    average_normalized: Fraction
    estimated_cost: Money
    estimated_output_value: Money
    estimated_ev: Money
    estimated_roi: Decimal
    outcome_count: int
    unpriced_probability: Fraction
    observed_at: datetime
    #: Exact composition, e.g. ``(("col-a", 7), ("col-b", 3))``. The primary
    #: collection fields above name the side contributing the most inputs.
    counts_by_collection: tuple[tuple[str, int], ...] = ()
    filler_collection_name: str | None = None

    @property
    def is_mixed(self) -> bool:
        return len(self.counts_by_collection) > 1

    @property
    def roi_percent(self) -> Decimal:
        return (self.estimated_roi * 100).quantize(Decimal("0.01"))

    def summary_row(self) -> dict[str, str]:
        collection = self.collection_name
        if self.is_mixed and self.filler_collection_name:
            collection = f"{collection} + {self.filler_collection_name}"
        return {
            "collection": collection,
            "rarity": self.input_rarity.value,
            "quality": self.quality.value,
            "wear": self.input_wear.value,
            "est_cost": str(self.estimated_cost.as_major()),
            "est_ev": str(self.estimated_ev.as_major()),
            "est_roi": f"{self.roi_percent}%",
            "outcomes": str(self.outcome_count),
        }

    def to_dict(self) -> dict[str, object]:
        return {
            "collection_id": self.collection_id,
            "collection_name": self.collection_name,
            "counts_by_collection": [list(pair) for pair in self.counts_by_collection],
            "filler_collection_name": self.filler_collection_name,
            "input_rarity": self.input_rarity.value,
            "quality": self.quality.value,
            "input_wear": self.input_wear.value,
            "rule_version": self.rule_version,
            "estimated_cost_minor": self.estimated_cost.minor_units,
            "estimated_output_value_minor": self.estimated_output_value.minor_units,
            "estimated_ev_minor": self.estimated_ev.minor_units,
            "estimated_roi": str(self.estimated_roi),
            "average_normalized": str(self.average_normalized),
            "outcome_count": self.outcome_count,
            "unpriced_probability": str(self.unpriced_probability),
            "inputs": [
                {
                    "skin_id": plan.skin_id,
                    "market_hash_name": plan.market_hash_name,
                    "units": plan.units,
                    "unit_price_minor": plan.unit_price.minor_units,
                }
                for plan in self.inputs
            ],
            "data_nature": (
                "REFERENCE ESTIMATE from Skinport asks and an assumed in-band input "
                "float; not executable truth. Confirm with the exact-listing scanner."
            ),
        }


@dataclass(frozen=True)
class SweepStatistics:
    """What the sweep considered and why sketches fell out.

    ``combos_considered`` counts pure single-collection sketches;
    ``mixed_considered`` counts evaluated two-collection splits. The skip
    counters are shared by both kinds.
    """

    combos_considered: int
    mixed_considered: int
    prospects: int
    skipped_insufficient_depth: int
    skipped_unpriced_outcomes: int
    skipped_rule_or_metadata: int
    skipped_over_cost_cap: int

    def summary(self) -> dict[str, str]:
        return {
            "combos_considered": str(self.combos_considered),
            "mixed_considered": str(self.mixed_considered),
            "prospects": str(self.prospects),
            "skipped_insufficient_depth": str(self.skipped_insufficient_depth),
            "skipped_unpriced_outcomes": str(self.skipped_unpriced_outcomes),
            "skipped_rule_or_metadata": str(self.skipped_rule_or_metadata),
            "skipped_over_cost_cap": str(self.skipped_over_cost_cap),
        }


def _wear_band(wear: WearCondition) -> tuple[Decimal, Decimal]:
    for condition, low, high in WEAR_BOUNDS:
        if condition is wear:
            return low, high
    raise AssertionError(f"unknown wear {wear}")


def _assumed_normalized(skin: Skin, wear: WearCondition, wear_point: Fraction) -> Fraction | None:
    """Normalised float of an input assumed at ``wear_point`` of its band ∩ range."""
    band_low, band_high = _wear_band(wear)
    low = max(Fraction(band_low), Fraction(skin.float_range.minimum))
    high = min(Fraction(band_high), Fraction(skin.float_range.maximum))
    if high <= low:
        return None
    point = low + (high - low) * wear_point
    span = Fraction(skin.float_range.maximum) - Fraction(skin.float_range.minimum)
    return (point - Fraction(skin.float_range.minimum)) / span


@dataclass(frozen=True)
class _Component:
    """One collection's contribution options at a (rarity, quality, wear)."""

    collection_id: str
    collection_name: str
    pool: EligibleOutputPool
    outputs: tuple[Skin, ...]
    #: Cheapest purchasable units, expanded one entry per unit and capped at the
    #: contract size: (unit price, skin, market hash name, assumed normalised z).
    units: tuple[tuple[Money, Skin, str, Fraction], ...]

    @property
    def cheapest_unit_minor(self) -> int:
        return self.units[0][0].minor_units


def _build_component(
    *,
    collection_id: str,
    collection_name: str,
    pool: EligibleOutputPool,
    output_skins: Sequence[Skin],
    eligible: Sequence[Skin],
    quality: QualityType,
    wear: WearCondition,
    input_count: int,
    quotes: Mapping[str, SkinportItemQuote],
    policy: ProspectPolicy,
) -> _Component | None:
    """Expand the cheapest purchasable units for one collection, or ``None``."""
    priced: list[tuple[Money, Skin, str, int, Fraction]] = []
    for skin in eligible:
        if wear not in skin.float_range.reachable_wears:
            continue
        normalized = _assumed_normalized(skin, wear, policy.wear_point)
        if normalized is None:
            continue
        name = skin.market_hash_name(quality, wear)
        quote = quotes.get(name)
        if quote is None or quote.min_price is None or quote.quantity < 1:
            continue
        priced.append((quote.min_price, skin, name, quote.quantity, normalized))
    if not priced:
        return None
    priced.sort(key=lambda entry: (entry[0].minor_units, entry[1].skin_id))
    units: list[tuple[Money, Skin, str, Fraction]] = []
    for unit_price, skin, name, quantity, normalized in priced:
        for _ in range(min(quantity, input_count - len(units))):
            units.append((unit_price, skin, name, normalized))
        if len(units) >= input_count:
            break
    return _Component(
        collection_id=collection_id,
        collection_name=collection_name,
        pool=pool,
        outputs=tuple(output_skins),
        units=tuple(units),
    )


def sweep_prospects(
    *,
    registry: MetadataRegistry,
    ruleset: TradeupRuleSet,
    quotes: Mapping[str, SkinportItemQuote],
    fee_schedule: FeeSchedule,
    base_currency: Currency,
    moment: datetime,
    qualities: Sequence[QualityType] = (QualityType.NORMAL, QualityType.STATTRAK),
    policy: ProspectPolicy | None = None,
    max_cost: Money | None = None,
    include_mixed: bool = True,
) -> tuple[tuple[Prospect, ...], SweepStatistics]:
    """Evaluate every contract sketch: pure collections, and two-way mixes.

    Mixing follows the game's real weighting — ``k`` inputs from one collection
    shift ``k/N`` of the outcome probability onto that collection's pool — so a
    valuable pool can be diluted with cheap filler exactly the way public
    calculators surface. Pairings are pruned to the cheapest fillers per
    :class:`ProspectPolicy`; the census reports what was evaluated. Returns
    prospects ranked by estimated ROI plus the skip census — a sweep that
    silently dropped half the space would present thin results as a market fact.
    """
    policy = policy or ProspectPolicy()
    zero = Money.zero(base_currency, BalanceType.CASH_WITHDRAWABLE)

    prospects: list[Prospect] = []
    considered = 0
    mixed_considered = 0
    skipped_depth = 0
    skipped_unpriced = 0
    skipped_rules = 0
    skipped_cost = 0

    def record(sketch: Prospect | str) -> None:
        nonlocal skipped_depth, skipped_unpriced, skipped_rules, skipped_cost
        if isinstance(sketch, Prospect):
            if max_cost is not None and sketch.estimated_cost > max_cost:
                skipped_cost += 1
                return
            prospects.append(sketch)
        elif sketch == "depth":
            skipped_depth += 1
        elif sketch == "unpriced":
            skipped_unpriced += 1
        else:
            skipped_rules += 1

    for rarity in sorted(ruleset.eligible_input_rarities, key=lambda r: r.value):
        try:
            input_count = ruleset.input_count_for(rarity)
        except RuleViolation:
            continue
        for quality in qualities:
            for wear, _low, _high in WEAR_BOUNDS:
                components: list[_Component] = []
                for collection in sorted(registry.collections, key=lambda c: c.collection_id):
                    pool = registry.output_pool(collection.collection_id, rarity)
                    if pool.is_empty:
                        continue
                    input_skins = registry.skins_at(collection.collection_id, rarity)
                    if not input_skins:
                        continue
                    output_skins = [
                        registry.skin(skin_id)
                        for skin_id in pool.output_skin_ids
                        if registry.has_skin(skin_id)
                    ]
                    if not output_skins:
                        continue
                    if not all(skin.supports(quality) for skin in output_skins):
                        skipped_rules += 1
                        continue
                    eligible = [s for s in input_skins if s.supports(quality)]
                    if not eligible:
                        continue

                    considered += 1
                    component = _build_component(
                        collection_id=collection.collection_id,
                        collection_name=collection.name,
                        pool=pool,
                        output_skins=output_skins,
                        eligible=eligible,
                        quality=quality,
                        wear=wear,
                        input_count=input_count,
                        quotes=quotes,
                        policy=policy,
                    )
                    if component is None or len(component.units) < input_count:
                        skipped_depth += 1
                        if component is not None:
                            components.append(component)
                        continue
                    components.append(component)
                    record(
                        _evaluate(
                            parts=((component, input_count),),
                            rarity=rarity,
                            quality=quality,
                            wear=wear,
                            input_count=input_count,
                            ruleset=ruleset,
                            quotes=quotes,
                            fee_schedule=fee_schedule,
                            base_currency=base_currency,
                            policy=policy,
                            moment=moment,
                            zero=zero,
                        )
                    )

                if not include_mixed or len(components) < 2:
                    continue
                fillers = sorted(
                    components, key=lambda c: (c.cheapest_unit_minor, c.collection_id)
                )[: policy.mixed_filler_pool]
                seen_pairs: set[tuple[str, str]] = set()
                for value_side in components:
                    for filler in fillers:
                        if filler.collection_id == value_side.collection_id:
                            continue
                        pair_key = (
                            min(value_side.collection_id, filler.collection_id),
                            max(value_side.collection_id, filler.collection_id),
                        )
                        if pair_key in seen_pairs:
                            continue
                        seen_pairs.add(pair_key)
                        first, second = sorted((value_side, filler), key=lambda c: c.collection_id)
                        for first_count in range(1, input_count):
                            second_count = input_count - first_count
                            if first_count > len(first.units):
                                continue
                            if second_count > len(second.units):
                                continue
                            mixed_considered += 1
                            record(
                                _evaluate(
                                    parts=((first, first_count), (second, second_count)),
                                    rarity=rarity,
                                    quality=quality,
                                    wear=wear,
                                    input_count=input_count,
                                    ruleset=ruleset,
                                    quotes=quotes,
                                    fee_schedule=fee_schedule,
                                    base_currency=base_currency,
                                    policy=policy,
                                    moment=moment,
                                    zero=zero,
                                )
                            )

    prospects.sort(
        key=lambda p: (
            -p.estimated_roi,
            -p.estimated_ev.minor_units,
            p.collection_id,
            p.quality.value,
            p.input_wear.value,
        )
    )
    statistics = SweepStatistics(
        combos_considered=considered,
        mixed_considered=mixed_considered,
        prospects=len(prospects),
        skipped_insufficient_depth=skipped_depth,
        skipped_unpriced_outcomes=skipped_unpriced,
        skipped_rule_or_metadata=skipped_rules,
        skipped_over_cost_cap=skipped_cost,
    )
    return tuple(prospects), statistics


def _evaluate(
    *,
    parts: tuple[tuple[_Component, int], ...],
    rarity: Rarity,
    quality: QualityType,
    wear: WearCondition,
    input_count: int,
    ruleset: TradeupRuleSet,
    quotes: Mapping[str, SkinportItemQuote],
    fee_schedule: FeeSchedule,
    base_currency: Currency,
    policy: ProspectPolicy,
    moment: datetime,
    zero: Money,
) -> Prospect | str:
    """Economics of one composition over its components' cheapest units."""
    plans: list[InputPlan] = []
    weighted_z = Fraction(0)
    cost = zero
    for component, count in parts:
        taken = component.units[:count]
        grouped: dict[tuple[str, int], tuple[InputPlan, int]] = {}
        for unit_price, skin, name, normalized in taken:
            weighted_z += normalized
            key = (skin.skin_id, unit_price.minor_units)
            if key in grouped:
                plan, units = grouped[key]
                grouped[key] = (plan, units + 1)
            else:
                grouped[key] = (
                    InputPlan(
                        skin_id=skin.skin_id,
                        market_hash_name=name,
                        units=0,
                        unit_price=unit_price,
                    ),
                    1,
                )
        for plan, units in grouped.values():
            final = InputPlan(
                skin_id=plan.skin_id,
                market_hash_name=plan.market_hash_name,
                units=units,
                unit_price=plan.unit_price,
            )
            plans.append(final)
            cost = cost + final.cost

    average_normalized = weighted_z / input_count

    counts = {component.collection_id: count for component, count in parts}
    pools = {component.collection_id: component.pool for component, count in parts}
    try:
        probabilities = outcome_probabilities(counts, pools, ruleset)
    except (MathematicsError, RuleViolation):
        return "rules"

    outputs_by_id = {
        skin.skin_id: skin for component, _count in parts for skin in component.outputs
    }
    terms: list[tuple[Fraction, Money]] = []
    unpriced = Fraction(0)
    for entry in probabilities:
        out_skin = outputs_by_id.get(entry.output_skin_id)
        if out_skin is None:
            return "rules"
        try:
            _output_float, out_wear = project_output_wear(
                average_normalized, out_skin.float_range, ruleset
            )
        except (MathematicsError, RuleViolation):
            return "rules"
        out_name = out_skin.market_hash_name(quality, out_wear)
        quote = quotes.get(out_name)
        if quote is None or quote.min_price is None:
            unpriced += entry.probability
            terms.append((entry.probability, zero))
            continue
        gross = quote.min_price
        try:
            sale_fee = fee_schedule.quote(_REFERENCE_VENUE, FeeOperation.SALE, gross, moment).fee
        except UnknownFeeError:
            return "rules"
        haircut = gross.scaled_up(policy.ask_haircut)
        net = gross - sale_fee - haircut
        if net.is_negative:
            net = zero
        terms.append((entry.probability, net))
    if unpriced > policy.max_unpriced_probability:
        return "unpriced"

    value = probability_weighted(terms, base_currency)
    ev = value - cost
    roi = ev.ratio_to(cost) if not cost.is_zero else Decimal(0)

    ordered = sorted(parts, key=lambda part: (-part[1], part[0].collection_id))
    primary = ordered[0][0]
    filler = ordered[1][0] if len(ordered) > 1 else None
    return Prospect(
        collection_id=primary.collection_id,
        collection_name=primary.collection_name,
        input_rarity=rarity,
        quality=quality,
        input_wear=wear,
        rule_version=ruleset.rule_version,
        inputs=tuple(plans),
        average_normalized=average_normalized,
        estimated_cost=cost,
        estimated_output_value=value,
        estimated_ev=ev,
        estimated_roi=roi,
        outcome_count=len(probabilities),
        unpriced_probability=unpriced,
        observed_at=moment,
        counts_by_collection=tuple(
            sorted((component.collection_id, count) for component, count in parts)
        ),
        filler_collection_name=filler.collection_name if filler is not None else None,
    )
