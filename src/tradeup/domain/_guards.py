"""Runtime type guards for the money and float paths.

Annotations say ``Decimal``; nothing enforces that at runtime, and a single ``float``
leaking into a float-range or fee calculation reintroduces exactly the binary
rounding error the whole design exists to avoid.

These helpers take ``object`` deliberately. That keeps the check genuinely reachable
from a static checker's point of view, so the guards do not have to be silenced with
per-line ignores that would eventually be copied onto a line that needed them.
"""

from __future__ import annotations

from decimal import Decimal

__all__ = ["ensure_decimal", "reject_float"]


def reject_float(value: object, context: str) -> None:
    """Raise if ``value`` is a binary float.

    ``bool`` and ``int`` are fine; only ``float`` is rejected.
    """
    if isinstance(value, float):
        raise TypeError(
            f"{context} must be Decimal, not float; "
            f"binary floats are not permitted on the money or float path"
        )


def ensure_decimal(value: object, context: str) -> Decimal:
    """Reject floats, then require a ``Decimal``."""
    reject_float(value, context)
    if not isinstance(value, Decimal):
        raise TypeError(f"{context} must be Decimal, got {type(value).__name__}")
    return value
