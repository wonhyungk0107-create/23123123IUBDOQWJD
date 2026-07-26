"""Immutable domain models and pure economic mathematics.

Nothing in this package performs I/O, reads configuration, or knows what a
marketplace is. That keeps the money-critical logic testable without a network and
makes every value here reproducible from persisted inputs.
"""

from __future__ import annotations
