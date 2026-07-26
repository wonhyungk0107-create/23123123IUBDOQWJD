"""Normalised-float breakpoints.

Output value is not a smooth function of the input floats. It is a step function:
nothing changes as the average float creeps up, and then it crosses a wear boundary
and the output is worth materially less. Between two adjacent boundaries, the
cheapest feasible bundle is the only one worth considering.

So instead of sweeping the float budget continuously, we solve the optimizer once at
each breakpoint. The candidate budgets are the *only* places the answer can change,
which turns an infinite search into a handful of solves.

Breakpoints are computed per output skin from its own float range, because a wear
boundary sits at a different normalised position for every skin.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from decimal import Decimal
from fractions import Fraction

from tradeup.domain.items import WEAR_BOUNDS, FloatRange

__all__ = ["EPSILON", "candidate_float_budgets", "wear_breakpoints"]

#: Nudge used to sit *just below* a boundary rather than exactly on it. A float of
#: exactly 0.07 is Minimal Wear, not Factory New, so landing on the boundary gives up
#: the very value the breakpoint exists to capture.
EPSILON: Fraction = Fraction(1, 10**9)

#: Interior wear boundaries. 0.0 and 1.0 are range ends, not value cliffs.
_INTERIOR_BOUNDARIES: tuple[Decimal, ...] = tuple(
    low for _, low, _ in WEAR_BOUNDS if low > Decimal(0)
)


def wear_breakpoints(output_ranges: Iterable[FloatRange]) -> tuple[Fraction, ...]:
    """Normalised positions at which any output crosses a wear boundary.

    For a skin with range ``[a, b]`` and boundary ``w`` inside it, the crossing sits
    at ``z = (w - a) / (b - a)``.
    """
    breakpoints: set[Fraction] = set()
    for float_range in output_ranges:
        low = Fraction(float_range.minimum)
        high = Fraction(float_range.maximum)
        span = high - low
        if span <= 0:  # pragma: no cover - FloatRange already forbids this
            continue
        for boundary in _INTERIOR_BOUNDARIES:
            value = Fraction(boundary)
            if low < value <= high:
                breakpoints.add((value - low) / span)
    return tuple(sorted(b for b in breakpoints if 0 < b <= 1))


def candidate_float_budgets(
    output_ranges: Sequence[FloatRange],
    *,
    include_unconstrained: bool = True,
) -> tuple[Fraction, ...]:
    """Average-normalised-float budgets worth solving for.

    Each breakpoint is offered as "just below" it, so a bundle that squeezes under a
    wear boundary is found. ``1`` is included so the cheapest bundle overall is always
    considered, even when it lands in the worst wear band -- sometimes that contract
    is still the profitable one.
    """
    budgets: set[Fraction] = set()
    for breakpoint in wear_breakpoints(output_ranges):
        just_below = breakpoint - EPSILON
        if just_below > 0:
            budgets.add(just_below)
    if include_unconstrained:
        budgets.add(Fraction(1))
    return tuple(sorted(budgets))
