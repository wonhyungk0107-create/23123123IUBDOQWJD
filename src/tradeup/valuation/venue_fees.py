"""Sourced venue fee rules for live shadow scanning.

Fees are data, and these are the best figures the public record supports, each with
its provenance in the rule's ``source`` field and a fuller account in
``docs/source-matrix.md``. None has been confirmed against a venue account screen,
so ``last_verified`` is ``None`` on every rule and **every figure must be re-read at
the venue before any purchase decision** — these exist so a shadow scan can measure
opportunity frequency fee-net, not so anyone can spend against them.

Where a public range exists rather than a number, the pessimistic end is encoded
(CSFloat withdrawal: 0.5-2.5% by volume -> 2.5%), because understating a cost is
the failure mode this system exists to avoid.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from tradeup.domain.fees import FeeOperation, FeeRule, FeeSchedule
from tradeup.domain.money import BalanceType, Currency

__all__ = ["LIVE_FEE_SCHEDULE_ID", "build_live_fee_schedule"]

LIVE_FEE_SCHEDULE_ID = "live-sourced-2026-07-26"

_CSFLOAT_FEE_SOURCE = (
    "blog.csfloat.com/changes-to-fees-on-csgofloat-market/ (official, 2021-08-22): "
    "static 2% seller sale fee; withdrawals 0.5-2.5% by volume. DATED and not "
    "re-confirmed against an account screen; re-verify before any purchase."
)

_SKINPORT_SALE_SOURCE = (
    "skinport.com/blog/lower-fees-for-everybody and the official @Skinport "
    "announcement (2025-07): standard selling fee lowered 12% -> 8% (6% above "
    "EUR 1000, not encoded; the pessimistic 8% applies). SECONDARY: the pages "
    "render client-side and were confirmed via their published summaries."
)

_SKINPORT_PAYOUT_SOURCE = (
    "skinport.com/faq/payout-fees (official FAQ): payouts to a linked bank account "
    "carry no fee. SECONDARY: page renders client-side; confirmed via its "
    "published summary. A stated zero, not a failed lookup."
)


def _rule(
    rule_id: str,
    venue: str,
    operation: FeeOperation,
    percentage: str,
    source: str,
) -> FeeRule:
    return FeeRule(
        rule_id=rule_id,
        venue=venue,
        operation=operation,
        balance_type=BalanceType.CASH_WITHDRAWABLE,
        currency=Currency.USD,
        percentage=Decimal(percentage),
        fixed_minor=0,
        effective_from=datetime(2026, 7, 26, tzinfo=UTC),
        effective_until=None,
        source=source,
        last_verified=None,
    )


def build_live_fee_schedule() -> FeeSchedule:
    """The fee rules a live shadow scan may price exits against."""
    return FeeSchedule(
        [
            _rule("live:csfloat:sale", "csfloat", FeeOperation.SALE, "0.02", _CSFLOAT_FEE_SOURCE),
            _rule(
                "live:csfloat:withdrawal",
                "csfloat",
                FeeOperation.WITHDRAWAL,
                "0.025",
                _CSFLOAT_FEE_SOURCE,
            ),
            _rule(
                "live:skinport:sale",
                "skinport",
                FeeOperation.SALE,
                "0.08",
                _SKINPORT_SALE_SOURCE,
            ),
            _rule(
                "live:skinport:withdrawal",
                "skinport",
                FeeOperation.WITHDRAWAL,
                "0",
                _SKINPORT_PAYOUT_SOURCE,
            ),
        ]
    )
