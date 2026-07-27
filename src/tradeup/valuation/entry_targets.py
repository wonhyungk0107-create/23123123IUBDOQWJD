"""Entry-price targets: the exact inverse of the fee-net ROI floor.

Forward valuation answers "at this acquisition cost, what is the ROI?".
Procurement screening needs the inverse: "given what the outputs are worth net
of exit fees and the ask haircut, what is the most that may leave the wallet
for inputs while still clearing the floor?" The answer is a *spending cap*, so
every ambiguity resolves downward: the cap is the largest integer minor-unit
amount whose forward economics clear the floor, never a rounded quotient that
might overshoot it.

The proportional overhead is charged the way the expected-value engine charges
it -- ``Money.scaled_up``, rounded toward more cost -- so a cap computed here
and a candidate evaluated there cannot disagree at the boundary. That ceiling
is also why the closed form ``(V - F) / (1 + r + q)`` is not used directly:
rounding ``q * A`` up makes the true cap up to one minor unit lower than the
quotient, and an entry alert firing one cent above the real threshold is
exactly the kind of plausible wrong number this package exists to prevent.

A target derived from a sweep estimate inherits every prior in that estimate:
the ask haircut, the assumed in-band wear point, name-level asks. It is a
screening threshold for deciding when exact confirmation is worth spending
request budget on, never an executable price.
"""

from __future__ import annotations

import itertools
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from fractions import Fraction

from tradeup.domain.fees import FeeOperation, FeeRule, FeeSchedule, UnknownFeeError
from tradeup.domain.money import BalanceType, Currency, Money

__all__ = [
    "EntryAssessment",
    "Ratio",
    "assess_entry",
    "max_acquisition_cost",
    "max_sticker_price",
    "per_unit_entry_target",
]

#: Exact ratio inputs. ``float`` is rejected at runtime, as everywhere on the
#: money path.
Ratio = Decimal | Fraction | int


def _as_fraction(value: Ratio, label: str) -> Fraction:
    if isinstance(value, float):
        raise TypeError(f"{label} does not accept float; pass Decimal, int or Fraction.")
    return Fraction(value)


def _clears(
    acquisition_minor: int,
    *,
    headroom_minor: int,
    one_plus_target: Fraction,
    proportional_overhead: Fraction,
) -> bool:
    """Forward check: does spending ``acquisition_minor`` clear the floor?

    The overhead ceiling mirrors ``Money.scaled_up``: a fractional minor unit
    is never charged in the candidate's favour.
    """
    q = proportional_overhead
    overhead_minor = -((-q.numerator * acquisition_minor) // q.denominator)
    return one_plus_target * acquisition_minor + overhead_minor <= headroom_minor


def max_acquisition_cost(
    *,
    expected_net_output_value: Money,
    target_roi: Ratio,
    fixed_overhead: Money | None = None,
    proportional_overhead: Ratio = 0,
) -> Money:
    """The largest acquisition spend that still clears ``target_roi``.

    Returns the greatest amount ``A`` in the value's currency and balance type
    such that::

        (V - F - ceil(q * A) - A) / A  >=  target_roi

    where ``V`` is ``expected_net_output_value`` (already net of exit fees and
    haircut), ``F`` is ``fixed_overhead`` (operational cost, a partial-fill
    reserve -- anything charged regardless of spend) and ``q`` is
    ``proportional_overhead`` (a settlement-rail rate, a carry rate times a
    holding period -- anything charged per unit of spend). With both overheads
    zero this inverts the sweep prospect model; with the engine's rates it
    inverts the loaded gate model.

    A zero result means no positive spend clears the floor. ``target_roi``
    must exceed -1; ``proportional_overhead`` must not be negative. The search
    is exact integer arithmetic: the result clears the inequality and the
    result plus one minor unit does not.
    """
    target = _as_fraction(target_roi, "target_roi")
    overhead_rate = _as_fraction(proportional_overhead, "proportional_overhead")
    one_plus_target = 1 + target
    if one_plus_target <= 0:
        raise ValueError(f"target_roi must exceed -1, got {target_roi}")
    if overhead_rate < 0:
        raise ValueError(f"proportional_overhead cannot be negative, got {proportional_overhead}")

    value = expected_net_output_value
    zero = Money.zero(value.currency, value.balance_type)
    headroom = value - (fixed_overhead if fixed_overhead is not None else zero)
    if headroom.minor_units <= 0:
        return zero
    headroom_minor = headroom.minor_units
    if not _clears(
        1,
        headroom_minor=headroom_minor,
        one_plus_target=one_plus_target,
        proportional_overhead=overhead_rate,
    ):
        return zero

    # Invariant: ``low`` clears, ``high`` does not. The seed for ``high`` is
    # safe because past it (1 + r) * A alone exceeds the headroom, before any
    # overhead is added.
    low = 1
    high = headroom_minor * one_plus_target.denominator // one_plus_target.numerator + 1
    while high - low > 1:
        mid = (low + high) // 2
        if _clears(
            mid,
            headroom_minor=headroom_minor,
            one_plus_target=one_plus_target,
            proportional_overhead=overhead_rate,
        ):
            low = mid
        else:
            high = mid
    return Money(low, value.currency, value.balance_type)


def per_unit_entry_target(total_cap: Money, input_count: int) -> Money:
    """Even per-input share of a total spending cap, rounded down.

    The guarantee is on the average: if the average unit cost of
    ``input_count`` inputs is at or below this figure, the bundle total is at
    or below ``total_cap``. The comparison basis must match the cap's --
    per-unit *acquisition* cost (sticker plus venue buyer-side fees) against a
    cap on acquisition spend.
    """
    if input_count < 1:
        raise ValueError(f"input_count must be at least 1, got {input_count}")
    if total_cap.is_negative:
        raise ValueError("a spending cap cannot be negative")
    return Money(total_cap.minor_units // input_count, total_cap.currency, total_cap.balance_type)


def _loaded_cost_minor(
    sticker_minor: int,
    *,
    rules: Sequence[FeeRule],
    currency: Currency,
    balance_type: BalanceType,
    moment: datetime,
) -> int:
    """Sticker plus every resolved fee on that sticker, in minor units.

    Each rule's ``quote`` rounds its fee up, so this is the same loaded cost
    the acquisition side would be charged.
    """
    basis = Money(sticker_minor, currency, balance_type)
    return sticker_minor + sum(rule.quote(basis, moment).fee.minor_units for rule in rules)


def max_sticker_price(
    *,
    unit_acquisition_cap: Money,
    venue: str,
    fee_schedule: FeeSchedule,
    moment: datetime,
    operations: tuple[FeeOperation, ...] = (FeeOperation.PURCHASE,),
) -> Money:
    """The largest sticker price whose fee-loaded cost fits under the cap.

    :func:`per_unit_entry_target` caps what may leave the wallet per input; an
    operator browsing a venue sees stickers. This inverts the venue's fee
    schedule: the result is the greatest sticker ``s`` such that every
    operation in ``operations`` resolves exactly one fee rule on basis ``s``
    and ``s`` plus the resolved fees is at or below ``unit_acquisition_cap``.

    Tiered fees make affordability non-monotone in the sticker -- a volume
    tier with a lower rate can make a higher sticker cheaper all-in than a
    lower one -- so the search runs per tier segment, inside which the rule
    set is fixed and the loaded cost strictly increases, and takes the best
    across segments.

    Failing closed: when no sticker in range has fee coverage the answer is
    not zero, it is unknowable -- ``UnknownFeeError``. Overlapping tiers raise
    the same way; ambiguity is as dangerous as absence. A zero result means
    coverage exists but no positive sticker fits under the cap.
    """
    cap = unit_acquisition_cap
    zero = Money.zero(cap.currency, cap.balance_type)
    if cap.minor_units < 1:
        return zero

    candidates: dict[FeeOperation, tuple[FeeRule, ...]] = {
        operation: tuple(
            rule
            for rule in fee_schedule.rules
            if rule.venue == venue
            and rule.operation is operation
            and rule.balance_type is cap.balance_type
            and rule.currency is cap.currency
            and rule.covers_time(moment)
        )
        for operation in operations
    }

    # Segment the sticker range at every tier boundary: within a segment the
    # matching rule set cannot change, which is what makes the loaded cost
    # monotone and the per-segment binary search sound.
    boundaries = {1, cap.minor_units + 1}
    for rules in candidates.values():
        for rule in rules:
            boundaries.add(max(rule.tier_lower_minor, 1))
            if rule.tier_upper_minor is not None:
                boundaries.add(rule.tier_upper_minor)
    points = sorted(b for b in boundaries if 1 <= b <= cap.minor_units + 1)

    best = 0
    covered_anywhere = False
    for start, stop in itertools.pairwise(points):
        probe = Money(start, cap.currency, cap.balance_type)
        segment_rules: list[FeeRule] = []
        uncovered = False
        for operation in operations:
            matched = [rule for rule in candidates[operation] if rule.covers_amount(probe)]
            if len(matched) > 1:
                ids = ", ".join(sorted(rule.rule_id for rule in matched))
                raise UnknownFeeError(f"ambiguous fee rules for {venue}/{operation.value}: {ids}")
            if not matched:
                uncovered = True
                break
            segment_rules.append(matched[0])
        if uncovered:
            continue
        covered_anywhere = True

        def cost(sticker_minor: int, rules: Sequence[FeeRule]) -> int:
            return _loaded_cost_minor(
                sticker_minor,
                rules=rules,
                currency=cap.currency,
                balance_type=cap.balance_type,
                moment=moment,
            )

        top = stop - 1
        if cost(start, segment_rules) > cap.minor_units:
            continue
        if cost(top, segment_rules) <= cap.minor_units:
            best = max(best, top)
            continue
        low, high = start, top
        while high - low > 1:
            mid = (low + high) // 2
            if cost(mid, segment_rules) <= cap.minor_units:
                low = mid
            else:
                high = mid
        best = max(best, low)

    if not covered_anywhere:
        raise UnknownFeeError(
            f"no fee coverage for {venue} on any sticker up to {cap}; "
            "refusing to treat unknown fees as unaffordable"
        )
    return Money(best, cap.currency, cap.balance_type)


@dataclass(frozen=True, slots=True)
class EntryAssessment:
    """Where a confirmed bundle sits relative to the entry threshold.

    ``entry_met`` is exact: the observed spend is at or below the largest
    spend that clears ``target_roi`` under the stated overheads. ``shortfall``
    is how much cheaper the whole bundle must get before entry is met — zero
    when it already is. All amounts share the observation's currency and
    balance type.
    """

    target_roi: Fraction
    target_total: Money
    target_unit: Money
    observed_total: Money
    input_count: int
    entry_met: bool
    shortfall: Money


def assess_entry(
    *,
    expected_net_output_value: Money,
    observed_acquisition_cost: Money,
    input_count: int,
    target_roi: Ratio,
    fixed_overhead: Money | None = None,
) -> EntryAssessment:
    """Judge a confirmed bundle against the entry threshold.

    ``fixed_overhead`` carries the charges beyond acquisition (operational
    cost, settlement share, capital carry, partial-fill reserve) *at their
    evaluated magnitudes*. Holding spend-dependent charges fixed at the level
    evaluated for the observed bundle overstates them for any cheaper bundle,
    so the resulting target errs low — the pessimistic direction for a
    spending threshold.

    A bundle with a non-positive observed cost is a data fault, not a bargain.
    """
    if observed_acquisition_cost.minor_units < 1:
        raise ValueError(
            f"observed acquisition cost must be positive, got {observed_acquisition_cost}"
        )
    target_total = max_acquisition_cost(
        expected_net_output_value=expected_net_output_value,
        target_roi=target_roi,
        fixed_overhead=fixed_overhead,
    )
    shortfall = observed_acquisition_cost - target_total
    if shortfall.is_negative:
        shortfall = Money.zero(shortfall.currency, shortfall.balance_type)
    return EntryAssessment(
        target_roi=_as_fraction(target_roi, "target_roi"),
        target_total=target_total,
        target_unit=per_unit_entry_target(target_total, input_count),
        observed_total=observed_acquisition_cost,
        input_count=input_count,
        entry_met=observed_acquisition_cost <= target_total,
        shortfall=shortfall,
    )
