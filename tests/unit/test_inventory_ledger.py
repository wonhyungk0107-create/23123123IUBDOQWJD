"""Inventory state machine, reservations, and the immutable ledger."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from tests.factories import NOW, usd

from tradeup.domain.execution import (
    AssetReservation,
    InvalidReservationTransition,
    ReservationState,
)
from tradeup.domain.inventory import (
    InvalidInventoryTransition,
    InventoryItem,
    InventoryState,
)
from tradeup.domain.ledger import (
    LedgerEvent,
    LedgerEventType,
    LedgerSignError,
    make_event_id,
    settled_cash_pnl,
)
from tradeup.domain.listings import ListingIdentity
from tradeup.domain.money import BalanceType, Currency
from tradeup.execution.reservations import ReservationRegistry


def item(state: InventoryState = InventoryState.DISCOVERED, **overrides: object) -> InventoryItem:
    base: dict[str, object] = {
        "item_id": "item-1",
        "identity": ListingIdentity("v", "L-1"),
        "asset_id": "asset-1",
        "skin_id": "a-in-1",
        "market_hash_name": "Weapon | Thing (Field-Tested)",
        "state": state,
        "acquisition_cost": usd(1000),
        "acquired_at": NOW,
        "updated_at": NOW,
    }
    base.update(overrides)
    return InventoryItem(**base)  # type: ignore[arg-type]


class TestInventoryTransitions:
    def test_valid_transition_advances_and_records_history(self) -> None:
        start = item(InventoryState.DISCOVERED)
        moved = start.transition_to(InventoryState.SOFT_RESERVED, NOW + timedelta(minutes=1))
        assert moved.state is InventoryState.SOFT_RESERVED
        assert moved.history[-1] == (InventoryState.DISCOVERED, NOW)

    def test_invalid_transition_raises_and_lists_what_is_allowed(self) -> None:
        with pytest.raises(InvalidInventoryTransition, match="allowed:"):
            item(InventoryState.DISCOVERED).transition_to(InventoryState.SETTLED, NOW)

    def test_terminal_states_accept_nothing(self) -> None:
        for terminal in (
            InventoryState.CONSUMED,
            InventoryState.SETTLED,
            InventoryState.WRITTEN_OFF,
            InventoryState.RELEASED,
            InventoryState.STALE,
        ):
            assert terminal.is_terminal
            assert not item(terminal).allowed_transitions()

    def test_time_cannot_run_backwards(self) -> None:
        with pytest.raises(InvalidInventoryTransition, match="precedes"):
            item(InventoryState.DISCOVERED).transition_to(
                InventoryState.SOFT_RESERVED, NOW - timedelta(hours=1)
            )

    def test_the_full_happy_path_is_reachable(self) -> None:
        """Discovered through settled, one legal step at a time."""
        path = [
            InventoryState.SOFT_RESERVED,
            InventoryState.PURCHASE_REQUESTED,
            InventoryState.PURCHASED,
            InventoryState.TRADE_PROTECTED,
            InventoryState.PLATFORM_INVENTORY,
            InventoryState.WITHDRAWABLE,
            InventoryState.TRANSFER_PENDING,
            InventoryState.RECEIVED_IN_STEAM,
            InventoryState.TRADEUP_ELIGIBLE,
            InventoryState.CONSUMED,
        ]
        current = item(InventoryState.DISCOVERED)
        moment = NOW
        for target in path:
            moment = moment + timedelta(hours=1)
            current = current.transition_to(target, moment)
        assert current.state is InventoryState.CONSUMED

    def test_an_output_can_be_listed_sold_and_settled(self) -> None:
        current = item(InventoryState.OUTPUT_CREATED)
        moment = NOW
        for target in (
            InventoryState.OUTPUT_LISTED,
            InventoryState.SOLD_UNSETTLED,
            InventoryState.SETTLED,
        ):
            moment = moment + timedelta(days=1)
            current = current.transition_to(target, moment)
        assert current.state is InventoryState.SETTLED

    def test_a_sale_can_be_reversed_back_into_inventory(self) -> None:
        current = item(InventoryState.SOLD_UNSETTLED)
        reversed_item = current.transition_to(
            InventoryState.PLATFORM_INVENTORY, NOW + timedelta(days=1)
        )
        assert reversed_item.state is InventoryState.PLATFORM_INVENTORY

    def test_capital_holding_states_require_a_cost_basis(self) -> None:
        with pytest.raises(ValueError, match="without a cost basis"):
            item(InventoryState.PURCHASED, acquisition_cost=None)

    def test_capital_days_accrue_until_a_terminal_state(self) -> None:
        held = item(InventoryState.PLATFORM_INVENTORY)
        assert held.capital_days(NOW + timedelta(days=3)) == Decimal("3.0000")

    def test_capital_days_stop_at_settlement(self) -> None:
        settled = item(
            InventoryState.SETTLED,
            updated_at=NOW + timedelta(days=2),
        )
        assert settled.capital_days(NOW + timedelta(days=30)) == Decimal("2.0000")

    def test_discovered_items_hold_no_capital(self) -> None:
        assert not InventoryState.DISCOVERED.holds_capital
        assert InventoryState.TRADE_PROTECTED.holds_capital


class TestReservations:
    def _identities(self, count: int = 3) -> list[ListingIdentity]:
        return [ListingIdentity("v", f"L-{i}") for i in range(count)]

    def test_claiming_a_bundle_reserves_every_listing(self) -> None:
        registry = ReservationRegistry()
        result = registry.claim_bundle("TU-1", self._identities(), now=NOW)
        assert result.succeeded
        assert len(result.reserved) == 3

    def test_a_second_candidate_cannot_claim_the_same_listing(self) -> None:
        registry = ReservationRegistry()
        identities = self._identities()
        registry.claim_bundle("TU-1", identities, now=NOW)
        conflict = registry.claim_bundle("TU-2", identities[:1], now=NOW)
        assert not conflict.succeeded
        assert conflict.conflicts == (identities[0],)

    def test_a_conflicting_claim_reserves_nothing_at_all(self) -> None:
        """All-or-nothing: a half-reserved bundle blocks both candidates."""
        registry = ReservationRegistry()
        identities = self._identities(3)
        registry.claim_bundle("TU-1", identities[:1], now=NOW)
        result = registry.claim_bundle("TU-2", identities, now=NOW)
        assert not result.succeeded
        assert result.reserved == ()
        assert registry.holder(identities[2], NOW) is None

    def test_the_same_candidate_may_reclaim_its_own_listings(self) -> None:
        registry = ReservationRegistry()
        identities = self._identities()
        registry.claim_bundle("TU-1", identities, now=NOW)
        assert registry.claim_bundle("TU-1", identities, now=NOW).succeeded

    def test_releasing_frees_the_listings(self) -> None:
        registry = ReservationRegistry()
        identities = self._identities()
        registry.claim_bundle("TU-1", identities, now=NOW)
        assert registry.release_candidate("TU-1", now=NOW) == 3
        assert registry.claim_bundle("TU-2", identities, now=NOW).succeeded

    def test_expired_reservations_stop_blocking(self) -> None:
        registry = ReservationRegistry(ttl=timedelta(minutes=1))
        identities = self._identities(1)
        registry.claim_bundle("TU-1", identities, now=NOW)
        later = NOW + timedelta(minutes=5)
        assert registry.holder(identities[0], later) is None
        assert registry.expire_stale(later) == 1

    def test_illegal_state_transition_raises(self) -> None:
        reservation = AssetReservation(
            identity=ListingIdentity("v", "L-1"),
            candidate_id="TU-1",
            state=ReservationState.PURCHASED,
            reserved_at=NOW,
            expires_at=NOW + timedelta(minutes=10),
        )
        with pytest.raises(InvalidReservationTransition):
            reservation.transition_to(ReservationState.SOFT_RESERVED, NOW)

    def test_transition_on_an_unknown_listing_raises(self) -> None:
        registry = ReservationRegistry()
        with pytest.raises(InvalidReservationTransition, match="no reservation"):
            registry.transition(ListingIdentity("v", "nope"), ReservationState.RELEASED, now=NOW)

    def test_reservation_must_expire_after_it_is_taken(self) -> None:
        with pytest.raises(ValueError, match="must expire after"):
            AssetReservation(
                identity=ListingIdentity("v", "L-1"),
                candidate_id="TU-1",
                state=ReservationState.SOFT_RESERVED,
                reserved_at=NOW,
                expires_at=NOW,
            )


def event(
    event_type: LedgerEventType,
    minor: int,
    *,
    sequence: int = 0,
    contract_id: str | None = "TU-1",
    balance: BalanceType = BalanceType.CASH_WITHDRAWABLE,
) -> LedgerEvent:
    return LedgerEvent(
        event_id=make_event_id(event_type, NOW, sequence, contract_id or ""),
        event_type=event_type,
        amount=usd(minor, balance),
        occurred_at=NOW,
        recorded_at=NOW,
        sequence=sequence,
        contract_id=contract_id,
    )


class TestLedger:
    def test_outflows_must_be_negative(self) -> None:
        with pytest.raises(LedgerSignError, match="outflow but amount is positive"):
            event(LedgerEventType.PURCHASE, 1000)

    def test_inflows_must_be_positive(self) -> None:
        with pytest.raises(LedgerSignError, match="inflow but amount is negative"):
            event(LedgerEventType.SALE, -1000)

    def test_custody_moves_must_carry_zero(self) -> None:
        """A transfer moves an item, not money."""
        with pytest.raises(LedgerSignError, match="must carry a zero amount"):
            event(LedgerEventType.INPUT_TRANSFER, -1)

    def test_custody_moves_with_zero_are_accepted(self) -> None:
        assert event(LedgerEventType.TRADEUP_CONSUMPTION, 0).amount.is_zero

    def test_reversal_may_go_either_way(self) -> None:
        assert event(LedgerEventType.REVERSAL, -500).amount.is_negative
        assert event(LedgerEventType.REVERSAL, 500, sequence=1).amount.is_positive

    def test_event_ids_are_deterministic(self) -> None:
        first = make_event_id(LedgerEventType.PURCHASE, NOW, 0, "ref")
        second = make_event_id(LedgerEventType.PURCHASE, NOW, 0, "ref")
        assert first == second

    def test_sequence_disambiguates_identical_events(self) -> None:
        first = make_event_id(LedgerEventType.BUYER_FEE, NOW, 0, "ref")
        second = make_event_id(LedgerEventType.BUYER_FEE, NOW, 1, "ref")
        assert first != second

    def test_settled_pnl_nets_cash_movements(self) -> None:
        events = [
            event(LedgerEventType.PURCHASE, -1000, sequence=0),
            event(LedgerEventType.BUYER_FEE, -20, sequence=1),
            event(LedgerEventType.SALE, 2000, sequence=2),
            event(LedgerEventType.SELLER_FEE, -100, sequence=3),
        ]
        assert settled_cash_pnl(events, currency=Currency.USD) == usd(880)

    def test_steam_wallet_movements_are_excluded_from_cash_pnl(self) -> None:
        """Wallet credit is not profit, however large."""
        events = [
            event(LedgerEventType.PURCHASE, -1000, sequence=0),
            event(
                LedgerEventType.SALE,
                100_000,
                sequence=1,
                balance=BalanceType.STEAM_WALLET,
            ),
        ]
        assert settled_cash_pnl(events, currency=Currency.USD) == usd(-1000)

    def test_custody_moves_do_not_affect_pnl(self) -> None:
        events = [
            event(LedgerEventType.PURCHASE, -1000, sequence=0),
            event(LedgerEventType.INPUT_TRANSFER, 0, sequence=1),
            event(LedgerEventType.OUTPUT_CREATION, 0, sequence=2),
        ]
        assert settled_cash_pnl(events, currency=Currency.USD) == usd(-1000)

    def test_pnl_can_be_scoped_to_one_contract(self) -> None:
        events = [
            event(LedgerEventType.PURCHASE, -1000, sequence=0, contract_id="TU-1"),
            event(LedgerEventType.PURCHASE, -5000, sequence=1, contract_id="TU-2"),
        ]
        assert settled_cash_pnl(events, currency=Currency.USD, contract_id="TU-1") == usd(-1000)

    def test_empty_ledger_is_zero_not_undefined(self) -> None:
        assert settled_cash_pnl([], currency=Currency.USD) == usd(0)

    def test_negative_sequence_rejected(self) -> None:
        with pytest.raises(ValueError, match="sequence cannot be negative"):
            LedgerEvent(
                event_id="LE-x",
                event_type=LedgerEventType.PURCHASE,
                amount=usd(-100),
                occurred_at=NOW,
                recorded_at=NOW,
                sequence=-1,
            )
