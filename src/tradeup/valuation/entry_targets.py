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

from decimal import Decimal
from fractions import Fraction

from tradeup.domain.money import Money

__all__ = ["Ratio", "max_acquisition_cost", "per_unit_entry_target"]

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
