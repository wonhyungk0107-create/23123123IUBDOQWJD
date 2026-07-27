"""Automated acquisition. The only package allowed to submit a purchase.

Everything in here is gated: the live-execution switch, a ledger-backed
daily-spend account, per-intent price ceilings, the reservation state machine
and a full pre-buy revalidation all stand between an approved candidate and a
venue order. Every refusal is a typed :class:`~tradeup.domain.execution.ExecutionResult`,
because "why did the system not buy" is evidence, not noise.
"""

from __future__ import annotations

from tradeup.purchasing.auto_buyer import AutoBuyer

__all__ = ["AutoBuyer"]
