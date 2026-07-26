"""Inventory lifecycle.

Capital is tied up from the moment an input is bought until the output's sale
proceeds settle. The states below are the checkpoints of that journey, and the
transition table is what stops the system from modelling same-day recycling of
capital that is in fact sitting in a ten-day trade hold.

Every transition is validated and produces a ledger event. There is no method that
sets a state directly.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from tradeup.domain.listings import ListingIdentity
from tradeup.domain.money import Money

__all__ = [
    "InvalidInventoryTransition",
    "InventoryItem",
    "InventoryState",
]


class InventoryState(enum.StrEnum):
    """Where an asset is in its lifecycle."""

    DISCOVERED = "DISCOVERED"
    SOFT_RESERVED = "SOFT_RESERVED"
    PURCHASE_REQUESTED = "PURCHASE_REQUESTED"
    PURCHASED = "PURCHASED"
    TRADE_PROTECTED = "TRADE_PROTECTED"
    """Held by the venue and invisible to third parties (DMarket ~10 days)."""

    PLATFORM_INVENTORY = "PLATFORM_INVENTORY"
    WITHDRAWABLE = "WITHDRAWABLE"
    TRANSFER_PENDING = "TRANSFER_PENDING"
    RECEIVED_IN_STEAM = "RECEIVED_IN_STEAM"
    TRADEUP_ELIGIBLE = "TRADEUP_ELIGIBLE"
    CONSUMED = "CONSUMED"
    """Destroyed by the contract. Terminal for an input."""

    OUTPUT_CREATED = "OUTPUT_CREATED"
    OUTPUT_LISTED = "OUTPUT_LISTED"
    SOLD_UNSETTLED = "SOLD_UNSETTLED"
    SETTLED = "SETTLED"
    """Proceeds received and attributable. Terminal, and the only profitable end."""

    RELEASED = "RELEASED"
    STALE = "STALE"
    WRITTEN_OFF = "WRITTEN_OFF"

    @property
    def is_terminal(self) -> bool:
        return not _INVENTORY_TRANSITIONS[self]

    @property
    def holds_capital(self) -> bool:
        """States in which money is committed but not yet recovered."""
        return self in {
            InventoryState.PURCHASE_REQUESTED,
            InventoryState.PURCHASED,
            InventoryState.TRADE_PROTECTED,
            InventoryState.PLATFORM_INVENTORY,
            InventoryState.WITHDRAWABLE,
            InventoryState.TRANSFER_PENDING,
            InventoryState.RECEIVED_IN_STEAM,
            InventoryState.TRADEUP_ELIGIBLE,
            InventoryState.OUTPUT_CREATED,
            InventoryState.OUTPUT_LISTED,
            InventoryState.SOLD_UNSETTLED,
        }

    @property
    def is_orphan_risk(self) -> bool:
        """Bought, but not yet committed to a contract that can complete."""
        return self in {
            InventoryState.PURCHASED,
            InventoryState.TRADE_PROTECTED,
            InventoryState.PLATFORM_INVENTORY,
            InventoryState.WITHDRAWABLE,
            InventoryState.TRANSFER_PENDING,
            InventoryState.RECEIVED_IN_STEAM,
            InventoryState.TRADEUP_ELIGIBLE,
        }


_INVENTORY_TRANSITIONS: dict[InventoryState, frozenset[InventoryState]] = {
    InventoryState.DISCOVERED: frozenset(
        {InventoryState.SOFT_RESERVED, InventoryState.RELEASED, InventoryState.STALE}
    ),
    InventoryState.SOFT_RESERVED: frozenset(
        {
            InventoryState.PURCHASE_REQUESTED,
            InventoryState.RELEASED,
            InventoryState.STALE,
        }
    ),
    InventoryState.PURCHASE_REQUESTED: frozenset(
        {InventoryState.PURCHASED, InventoryState.RELEASED, InventoryState.STALE}
    ),
    # A purchase lands either in a hold period or straight into platform inventory.
    InventoryState.PURCHASED: frozenset(
        {
            InventoryState.TRADE_PROTECTED,
            InventoryState.PLATFORM_INVENTORY,
            InventoryState.WRITTEN_OFF,
        }
    ),
    InventoryState.TRADE_PROTECTED: frozenset(
        {InventoryState.PLATFORM_INVENTORY, InventoryState.WRITTEN_OFF}
    ),
    InventoryState.PLATFORM_INVENTORY: frozenset(
        {InventoryState.WITHDRAWABLE, InventoryState.OUTPUT_LISTED, InventoryState.WRITTEN_OFF}
    ),
    InventoryState.WITHDRAWABLE: frozenset(
        {InventoryState.TRANSFER_PENDING, InventoryState.OUTPUT_LISTED, InventoryState.WRITTEN_OFF}
    ),
    InventoryState.TRANSFER_PENDING: frozenset(
        {
            InventoryState.RECEIVED_IN_STEAM,
            # A reversed or failed transfer returns the asset to the venue.
            InventoryState.PLATFORM_INVENTORY,
            InventoryState.WRITTEN_OFF,
        }
    ),
    InventoryState.RECEIVED_IN_STEAM: frozenset(
        {InventoryState.TRADEUP_ELIGIBLE, InventoryState.WRITTEN_OFF}
    ),
    InventoryState.TRADEUP_ELIGIBLE: frozenset(
        {
            InventoryState.CONSUMED,
            # Liquidating an input we chose not to contract after all.
            InventoryState.TRANSFER_PENDING,
            InventoryState.WRITTEN_OFF,
        }
    ),
    InventoryState.CONSUMED: frozenset(),
    InventoryState.OUTPUT_CREATED: frozenset(
        {
            InventoryState.TRANSFER_PENDING,
            InventoryState.OUTPUT_LISTED,
            InventoryState.WRITTEN_OFF,
        }
    ),
    InventoryState.OUTPUT_LISTED: frozenset(
        {
            InventoryState.SOLD_UNSETTLED,
            # Delisting returns it to inventory.
            InventoryState.PLATFORM_INVENTORY,
            InventoryState.WRITTEN_OFF,
        }
    ),
    InventoryState.SOLD_UNSETTLED: frozenset(
        {
            InventoryState.SETTLED,
            # A reversal puts the item back in our hands.
            InventoryState.PLATFORM_INVENTORY,
            InventoryState.WRITTEN_OFF,
        }
    ),
    InventoryState.SETTLED: frozenset(),
    InventoryState.RELEASED: frozenset(),
    InventoryState.STALE: frozenset(),
    InventoryState.WRITTEN_OFF: frozenset(),
}

#: The output of a completed contract enters here. It has no purchase of its own,
#: so it is seeded rather than transitioned into.
OUTPUT_ENTRY_STATE = InventoryState.OUTPUT_CREATED


class InvalidInventoryTransition(Exception):
    """Raised on an illegal inventory state change."""


@dataclass(frozen=True, slots=True)
class InventoryItem:
    """One asset we hold or intend to hold, with its cost basis and history.

    ``acquisition_cost`` is the all-in cost actually paid, which is what capital-day
    and orphan-loss calculations charge against.
    """

    item_id: str
    identity: ListingIdentity | None
    asset_id: str | None
    skin_id: str
    market_hash_name: str
    state: InventoryState
    acquisition_cost: Money | None
    acquired_at: datetime | None
    updated_at: datetime
    contract_id: str | None = None
    raw_float: Decimal | None = None
    trade_lock_until: datetime | None = None
    history: tuple[tuple[InventoryState, datetime], ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if self.updated_at.tzinfo is None:
            raise ValueError("updated_at must be timezone-aware UTC")
        if self.acquired_at is not None and self.acquired_at.tzinfo is None:
            raise ValueError("acquired_at must be timezone-aware UTC")
        if self.state.holds_capital and self.acquisition_cost is None:
            raise ValueError(
                f"item {self.item_id} is in {self.state.value} without a cost basis; "
                "capital-days cannot be computed"
            )

    def can_transition_to(self, target: InventoryState) -> bool:
        return target in _INVENTORY_TRANSITIONS[self.state]

    def allowed_transitions(self) -> frozenset[InventoryState]:
        return _INVENTORY_TRANSITIONS[self.state]

    def transition_to(self, target: InventoryState, moment: datetime) -> InventoryItem:
        """Move to ``target`` or raise. Appends to the item's own history."""
        if not self.can_transition_to(target):
            allowed = ", ".join(sorted(s.value for s in self.allowed_transitions())) or "<terminal>"
            raise InvalidInventoryTransition(
                f"{self.item_id}: {self.state.value} -> {target.value} is not permitted; "
                f"allowed: {allowed}"
            )
        if moment < self.updated_at:
            raise InvalidInventoryTransition(
                f"{self.item_id}: transition at {moment.isoformat()} precedes "
                f"last update {self.updated_at.isoformat()}"
            )
        return InventoryItem(
            item_id=self.item_id,
            identity=self.identity,
            asset_id=self.asset_id,
            skin_id=self.skin_id,
            market_hash_name=self.market_hash_name,
            state=target,
            acquisition_cost=self.acquisition_cost,
            acquired_at=self.acquired_at,
            updated_at=moment,
            contract_id=self.contract_id,
            raw_float=self.raw_float,
            trade_lock_until=self.trade_lock_until,
            history=(*self.history, (self.state, self.updated_at)),
        )

    def capital_days(self, now: datetime) -> Decimal:
        """Days of capital committed so far. Zero before acquisition."""
        if self.acquired_at is None:
            return Decimal(0)
        end = self.updated_at if self.state.is_terminal else now
        seconds = max((end - self.acquired_at).total_seconds(), 0.0)
        return (Decimal(str(seconds)) / Decimal(86400)).quantize(Decimal("0.0001"))
