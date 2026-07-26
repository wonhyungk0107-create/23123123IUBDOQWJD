"""Deterministic offline demonstration.

The scenario is **synthetic listings and synthetic prices over real metadata**. The
collections, skins, float caps and output pools come from the pinned
ByMykel/CSGO-API revision; the listings, prices and fees are invented and clearly
labelled as such.

This split is deliberate. Using real metadata means the demo exercises the same
identity, float-cap and output-pool code paths a live scan would. Using synthetic
prices means the demo proves *software behaviour* and nothing whatsoever about the
market. A passing candidate here is not evidence of an edge.
"""

from __future__ import annotations
