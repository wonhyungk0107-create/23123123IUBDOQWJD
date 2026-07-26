"""Marketplace adapters.

Every venue implements the same protocol and every operation returns a
:class:`~tradeup.domain.execution.CapabilityResult`. The base class's defaults are
*refusals*, so an adapter supports an operation only by explicitly implementing it.

That inversion is the safety property: there is no inherited behaviour that could
quietly do something a venue never documented, and an unsupported purchase path
returns ``UNSUPPORTED`` rather than falling through to some other mechanism.
"""

from __future__ import annotations
