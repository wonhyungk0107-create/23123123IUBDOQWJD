"""Revalidating standing buy orders against the current market sweep.

A standing buy order freezes an entry ceiling at placement time, but the exit
prices that justified that ceiling keep moving. Every batch already sweeps the
whole catalogue, so each order can be re-judged for free: recompute the
per-unit entry ceiling from the *current* best variant of the order's lead and
compare it with the standing price.

Three verdicts, none of them an instruction to spend:

* ``STILL_VALID`` — the standing price is at or below today's ceiling.
* ``CEILING_DECAYED`` — today's ceiling is below the standing price; fills at
  the standing price would no longer clear the ROI floor. The operator should
  reprice or cancel.
* ``LEAD_UNPRICEABLE`` — the current sweep cannot price the lead at all (no
  matching sketch survived; outcomes unpriced or depth gone). Unknown exits
  are a reason for caution, never reassurance.

Verdicts inherit every prior in the sweep estimate (ask haircut, assumed wear
point, name-level asks). A decay verdict is a prompt to re-confirm against
exact listings, not a measurement of loss.
"""

from __future__ import annotations

import enum
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from tradeup.discovery.prospects import Prospect
from tradeup.domain.items import QualityType, WearCondition
from tradeup.domain.money import Money
from tradeup.valuation.entry_targets import Ratio

__all__ = [
    "OrderVerdict",
    "OrderWatchStatus",
    "StandingOrder",
    "review_standing_orders",
]


class OrderWatchStatus(enum.StrEnum):
    STILL_VALID = "STILL_VALID"
    CEILING_DECAYED = "CEILING_DECAYED"
    LEAD_UNPRICEABLE = "LEAD_UNPRICEABLE"


@dataclass(frozen=True, slots=True)
class StandingOrder:
    """One operator-placed venue buy order, as recorded by the operator."""

    collection_id: str
    quality: QualityType
    input_wear: WearCondition
    market_hash_name: str
    units: int
    max_price: Money
    placed_at: datetime
    source_candidate_id: str | None = None

    def __post_init__(self) -> None:
        if self.units < 1:
            raise ValueError(f"a standing order needs at least one unit, got {self.units}")
        if self.max_price.is_negative:
            raise ValueError("a standing order cannot carry a negative price")
        if self.placed_at.tzinfo is None:
            raise ValueError("placed_at must be timezone-aware UTC")

    @property
    def lead_key(self) -> tuple[str, str, str]:
        return (self.collection_id, self.quality.value, self.input_wear.value)


@dataclass(frozen=True, slots=True)
class OrderVerdict:
    """One order judged against the current sweep."""

    order: StandingOrder
    status: OrderWatchStatus
    current_unit_ceiling: Money | None
    detail: str

    @property
    def needs_attention(self) -> bool:
        return self.status is not OrderWatchStatus.STILL_VALID


def _best_variant(prospects: Sequence[Prospect], key: tuple[str, str, str]) -> Prospect | None:
    """The best-ranked current sketch of this lead, mirroring lead selection."""
    matching = [
        prospect
        for prospect in prospects
        if (prospect.collection_id, prospect.quality.value, prospect.input_wear.value) == key
    ]
    if not matching:
        return None
    return max(matching, key=lambda p: (p.estimated_roi, p.collection_id))


def review_standing_orders(
    orders: Sequence[StandingOrder],
    prospects: Sequence[Prospect],
    *,
    target_roi: Ratio,
    confirmed_unit_ceilings: Mapping[tuple[str, str, str], Money] | None = None,
) -> tuple[OrderVerdict, ...]:
    """Judge every standing order against the current board. Order-preserving.

    ``confirmed_unit_ceilings`` carries per-lead ceilings measured from exact
    listings this batch. Exact evidence always beats the sweep estimate: the
    first live pilot showed an estimate-derived ceiling of $1.26/unit for a
    lead whose exact confirmation, in the same batch, put the ceiling at
    $0.13/unit — an optimistic verdict from priors when the truth was on hand.
    """
    confirmed = dict(confirmed_unit_ceilings or {})
    verdicts: list[OrderVerdict] = []
    for order in orders:
        exact_ceiling = confirmed.get(order.lead_key)
        if exact_ceiling is not None:
            verdicts.append(_judge(order, exact_ceiling, evidence="exact confirmation this batch"))
            continue
        prospect = _best_variant(prospects, order.lead_key)
        if prospect is None:
            verdicts.append(
                OrderVerdict(
                    order=order,
                    status=OrderWatchStatus.LEAD_UNPRICEABLE,
                    current_unit_ceiling=None,
                    detail=(
                        "the current sweep has no priceable sketch of this lead; "
                        "exits cannot be verified today — consider pausing the order"
                    ),
                )
            )
            continue
        ceiling = prospect.entry_target_unit_cost(target_roi)
        if ceiling.currency is not order.max_price.currency:
            verdicts.append(
                OrderVerdict(
                    order=order,
                    status=OrderWatchStatus.LEAD_UNPRICEABLE,
                    current_unit_ceiling=None,
                    detail=(
                        f"order priced in {order.max_price.currency} but the board "
                        f"prices in {ceiling.currency}; refusing to compare across "
                        "currencies"
                    ),
                )
            )
            continue
        verdicts.append(_judge(order, ceiling, evidence="sweep estimate under its priors"))
    return tuple(verdicts)


def _judge(order: StandingOrder, ceiling: Money, *, evidence: str) -> OrderVerdict:
    if order.max_price <= ceiling:
        headroom = ceiling - order.max_price
        return OrderVerdict(
            order=order,
            status=OrderWatchStatus.STILL_VALID,
            current_unit_ceiling=ceiling,
            detail=(
                f"standing {order.max_price} within today's ceiling {ceiling} "
                f"(headroom {headroom}; ceiling from {evidence})"
            ),
        )
    return OrderVerdict(
        order=order,
        status=OrderWatchStatus.CEILING_DECAYED,
        current_unit_ceiling=ceiling,
        detail=(
            f"today's entry ceiling is {ceiling} (from {evidence}), below the "
            f"standing {order.max_price}; fills at the standing price would no "
            "longer clear the floor — reprice or cancel"
        ),
    )
