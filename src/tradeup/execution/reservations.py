"""Listing reservations.

One listing can satisfy many candidate contracts. If two candidates both plan to buy
it, one of them is going to fail partway through a bundle and be left holding
orphans. So the lock is taken on the *listing*, not the contract.

The registry is the in-process authority. It enforces one active claim per listing
and refuses a partial bundle claim: either every listing in the candidate is reserved
or none is. A half-reserved bundle is worse than no reservation, because it looks
like progress while guaranteeing the other candidate cannot complete either.

Durability comes from the persistence layer's unique index on
``(venue, listing_id)`` for active reservations. This class is the fast path and the
single place the transition rules live.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from tradeup.domain.execution import (
    AssetReservation,
    InvalidReservationTransition,
    ReservationState,
)
from tradeup.domain.listings import ListingIdentity

__all__ = ["ReservationConflict", "ReservationRegistry", "ReservationResult"]

DEFAULT_RESERVATION_TTL = timedelta(minutes=10)


class ReservationConflict(Exception):
    """Raised when a listing is already claimed by another candidate."""


@dataclass(frozen=True)
class ReservationResult:
    """Outcome of attempting to claim a whole bundle."""

    candidate_id: str
    reserved: tuple[AssetReservation, ...]
    conflicts: tuple[ListingIdentity, ...]

    @property
    def succeeded(self) -> bool:
        return not self.conflicts

    @property
    def reserved_listing_ids(self) -> tuple[str, ...]:
        return tuple(r.identity.listing_id for r in self.reserved)


class ReservationRegistry:
    """Tracks which listings are claimed, by whom, and until when."""

    def __init__(self, *, ttl: timedelta = DEFAULT_RESERVATION_TTL) -> None:
        if ttl <= timedelta(0):
            raise ValueError("reservation ttl must be positive")
        self._ttl = ttl
        self._by_listing: dict[ListingIdentity, AssetReservation] = {}

    # -- queries -------------------------------------------------------------

    def active(self, now: datetime) -> tuple[AssetReservation, ...]:
        return tuple(
            r for r in self._by_listing.values() if r.state.is_active and not r.is_expired(now)
        )

    def holder(self, identity: ListingIdentity, now: datetime) -> str | None:
        """Candidate currently holding this listing, if any."""
        existing = self._by_listing.get(identity)
        if existing is None or not existing.state.is_active or existing.is_expired(now):
            return None
        return existing.candidate_id

    def is_available(self, identity: ListingIdentity, candidate_id: str, now: datetime) -> bool:
        holder = self.holder(identity, now)
        return holder is None or holder == candidate_id

    def state_of(self, identity: ListingIdentity) -> ReservationState:
        existing = self._by_listing.get(identity)
        return existing.state if existing else ReservationState.UNCLAIMED

    # -- mutation ------------------------------------------------------------

    def claim_bundle(
        self,
        candidate_id: str,
        identities: Sequence[ListingIdentity],
        *,
        now: datetime,
    ) -> ReservationResult:
        """Reserve every listing in a bundle, or none of them.

        All-or-nothing: a partially reserved bundle cannot complete and blocks the
        candidate that could have.
        """
        conflicts = [
            identity
            for identity in identities
            if not self.is_available(identity, candidate_id, now)
        ]
        if conflicts:
            return ReservationResult(candidate_id, (), tuple(sorted(conflicts)))

        reserved: list[AssetReservation] = []
        for identity in identities:
            reservation = AssetReservation(
                identity=identity,
                candidate_id=candidate_id,
                state=ReservationState.SOFT_RESERVED,
                reserved_at=now,
                expires_at=now + self._ttl,
            )
            self._by_listing[identity] = reservation
            reserved.append(reservation)
        return ReservationResult(candidate_id, tuple(reserved), ())

    def transition(
        self,
        identity: ListingIdentity,
        target: ReservationState,
        *,
        now: datetime,
    ) -> AssetReservation:
        """Advance one reservation, validating the transition."""
        existing = self._by_listing.get(identity)
        if existing is None:
            raise InvalidReservationTransition(f"{identity} has no reservation to transition")
        updated = existing.transition_to(target, now)
        self._by_listing[identity] = updated
        return updated

    def release_candidate(self, candidate_id: str, *, now: datetime) -> int:
        """Release every active claim held by a candidate. Returns how many."""
        released = 0
        for identity, reservation in list(self._by_listing.items()):
            if reservation.candidate_id != candidate_id or not reservation.state.is_active:
                continue
            if reservation.can_transition_to(ReservationState.RELEASED):
                self._by_listing[identity] = reservation.transition_to(
                    ReservationState.RELEASED, now
                )
                released += 1
        return released

    def expire_stale(self, now: datetime) -> int:
        """Mark expired active reservations stale so their listings free up."""
        expired = 0
        for identity, reservation in list(self._by_listing.items()):
            if not reservation.state.is_active or not reservation.is_expired(now):
                continue
            if reservation.can_transition_to(ReservationState.STALE):
                self._by_listing[identity] = reservation.transition_to(ReservationState.STALE, now)
                expired += 1
        return expired

    def snapshot(self) -> Mapping[ListingIdentity, AssetReservation]:
        return dict(self._by_listing)

    def __len__(self) -> int:
        return len(self._by_listing)
