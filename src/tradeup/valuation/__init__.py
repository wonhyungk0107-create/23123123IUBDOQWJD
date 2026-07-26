"""Economics: fee application, exit valuation, capital cost, partial-fill risk, EV.

This package turns a bundle of exact listings into a defensible number. Everything
here is deterministic and injectable -- no clocks, no network, no configuration read
from the environment -- so an evaluation can be reproduced exactly from persisted
inputs months later.
"""

from __future__ import annotations
