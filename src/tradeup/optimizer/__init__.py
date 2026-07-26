"""Exact-asset bundle selection.

The optimizer answers one question: given a collection composition and a float
budget, what is the *cheapest* set of exact listings that satisfies it?

It never enumerates raw listing combinations. Choosing 10 of 400 listings is
2.6e19 subsets; the same problem solved as a per-collection dynamic program over a
cost-versus-float Pareto frontier is milliseconds.
"""

from __future__ import annotations
