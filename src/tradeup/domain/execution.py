"""Execution capability, reservations, purchase intents and results.

The central type is :class:`CapabilityResult`. Every adapter operation returns one,
and an adapter that cannot do something returns a *typed* refusal rather than
raising or, far worse, silently falling back to some other mechanism. That is the
structural guarantee behind "never automate Steam": there is no code path from
``UNSUPPORTED`` to an alternative implementation.

Reservations exist because one listing can satisfy many candidate contracts. The
lock is taken on the underlying listing, not the contract, so two concurrent scans
cannot both plan to buy the same asset.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime
from typing import Self

from tradeup.domain.listings import ListingIdentity
from tradeup.domain.money import Money

__all__ = [
    "AssetReservation",
    "CapabilityResult",
    "CapabilityStatus",
    "ExecutionMode",
    "ExecutionResult",
    "ExecutionStatus",
    "InvalidReservationTransition",
    "PurchaseIntent",
    "ReservationState",
]


class CapabilityStatus(enum.StrEnum):
    """Outcome of an adapter operation."""

    SUPPORTED_READ_ONLY = "SUPPORTED_READ_ONLY"
    """Succeeded. Data returned. No market state changed."""

    SUPPORTED_EXECUTION_DISABLED = "SUPPORTED_EXECUTION_DISABLED"
    """The venue documents this operation, but policy forbids running it."""

    OPERATOR_ACTION_REQUIRED = "OPERATOR_ACTION_REQUIRED"
    """A human must perform this step. The agent emits instructions and waits."""

    AUTHENTICATION_REQUIRED = "AUTHENTICATION_REQUIRED"
    """Credentials absent or rejected. Not an error condition during groundwork."""

    RATE_LIMITED = "RATE_LIMITED"
    TEMPORARILY_UNAVAILABLE = "TEMPORARILY_UNAVAILABLE"

    UNSUPPORTED = "UNSUPPORTED"
    """No documented interface exists. Never a cue to invent one."""

    POLICY_BLOCKED = "POLICY_BLOCKED"
    """Forbidden by our own boundaries (e.g. anything that automates Steam)."""

    @property
    def has_data(self) -> bool:
        return self is CapabilityStatus.SUPPORTED_READ_ONLY

    @property
    def is_retryable(self) -> bool:
        return self in {
            CapabilityStatus.RATE_LIMITED,
            CapabilityStatus.TEMPORARILY_UNAVAILABLE,
        }

    @property
    def is_permanent_refusal(self) -> bool:
        return self in {CapabilityStatus.UNSUPPORTED, CapabilityStatus.POLICY_BLOCKED}


class CapabilityError(Exception):
    """Raised by :meth:`CapabilityResult.unwrap` on a non-data result."""


@dataclass(frozen=True, slots=True)
class CapabilityResult[T]:
    """A typed adapter outcome carrying either data or a specific refusal."""

    status: CapabilityStatus
    venue: str
    operation: str
    observed_at: datetime
    value: T | None = None
    detail: str = ""
    #: Set when the venue tells us when to come back.
    retry_after_seconds: int | None = None

    def __post_init__(self) -> None:
        if self.observed_at.tzinfo is None:
            raise ValueError("observed_at must be timezone-aware UTC")
        if self.status.has_data and self.value is None:
            raise ValueError(
                f"{self.venue}/{self.operation} reported success with no value; "
                "a successful read must carry data"
            )
        if not self.status.has_data and self.value is not None:
            raise ValueError(
                f"{self.venue}/{self.operation} returned {self.status} with a value; "
                "non-success results must not smuggle data past the caller"
            )
        if not self.status.has_data and not self.detail:
            raise ValueError(
                f"{self.venue}/{self.operation} returned {self.status} without a reason"
            )

    @property
    def ok(self) -> bool:
        return self.status.has_data

    def unwrap(self) -> T:
        """Return the value or raise. Use only where a refusal is genuinely fatal."""
        if not self.ok or self.value is None:
            raise CapabilityError(
                f"{self.venue}/{self.operation}: {self.status.value} -- {self.detail}"
            )
        return self.value

    def or_none(self) -> T | None:
        return self.value if self.ok else None

    # -- constructors --------------------------------------------------------

    @classmethod
    def succeeded(
        cls, venue: str, operation: str, observed_at: datetime, value: T, detail: str = ""
    ) -> Self:
        return cls(
            status=CapabilityStatus.SUPPORTED_READ_ONLY,
            venue=venue,
            operation=operation,
            observed_at=observed_at,
            value=value,
            detail=detail,
        )

    @classmethod
    def refused(
        cls,
        status: CapabilityStatus,
        venue: str,
        operation: str,
        observed_at: datetime,
        detail: str,
        retry_after_seconds: int | None = None,
    ) -> Self:
        if status.has_data:
            raise ValueError("refused() requires a non-success status")
        return cls(
            status=status,
            venue=venue,
            operation=operation,
            observed_at=observed_at,
            detail=detail,
            retry_after_seconds=retry_after_seconds,
        )


class ExecutionMode(enum.StrEnum):
    """How a purchase would actually happen at a venue."""

    AUTOMATED = "AUTOMATED"
    """A documented purchase API exists and policy permits using it."""

    OPERATOR_APPROVAL_REQUIRED = "OPERATOR_APPROVAL_REQUIRED"
    """A human buys it. The agent produces exact instructions."""

    UNSUPPORTED = "UNSUPPORTED"
    """No sanctioned purchase path. The candidate cannot be executed here."""


class ReservationState(enum.StrEnum):
    UNCLAIMED = "UNCLAIMED"
    SOFT_RESERVED = "SOFT_RESERVED"
    PURCHASE_PENDING = "PURCHASE_PENDING"
    PURCHASED = "PURCHASED"
    RELEASED = "RELEASED"
    STALE = "STALE"

    @property
    def is_active(self) -> bool:
        """States that block another candidate from claiming the same listing."""
        return self in {
            ReservationState.SOFT_RESERVED,
            ReservationState.PURCHASE_PENDING,
            ReservationState.PURCHASED,
        }

    @property
    def is_terminal(self) -> bool:
        return self in {
            ReservationState.PURCHASED,
            ReservationState.RELEASED,
            ReservationState.STALE,
        }


_RESERVATION_TRANSITIONS: dict[ReservationState, frozenset[ReservationState]] = {
    ReservationState.UNCLAIMED: frozenset({ReservationState.SOFT_RESERVED}),
    ReservationState.SOFT_RESERVED: frozenset(
        {
            ReservationState.PURCHASE_PENDING,
            ReservationState.RELEASED,
            ReservationState.STALE,
        }
    ),
    ReservationState.PURCHASE_PENDING: frozenset(
        {
            ReservationState.PURCHASED,
            ReservationState.RELEASED,
            ReservationState.STALE,
        }
    ),
    ReservationState.PURCHASED: frozenset(),
    ReservationState.RELEASED: frozenset(),
    ReservationState.STALE: frozenset(),
}


class InvalidReservationTransition(Exception):
    """Raised on an illegal reservation state change."""


@dataclass(frozen=True, slots=True)
class AssetReservation:
    """A claim on one listing by one candidate."""

    identity: ListingIdentity
    candidate_id: str
    state: ReservationState
    reserved_at: datetime
    expires_at: datetime
    released_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.reserved_at.tzinfo is None or self.expires_at.tzinfo is None:
            raise ValueError("reservation timestamps must be timezone-aware UTC")
        if self.expires_at <= self.reserved_at:
            raise ValueError("reservation must expire after it was taken")

    def can_transition_to(self, target: ReservationState) -> bool:
        return target in _RESERVATION_TRANSITIONS[self.state]

    def transition_to(self, target: ReservationState, moment: datetime) -> AssetReservation:
        if not self.can_transition_to(target):
            raise InvalidReservationTransition(
                f"{self.identity}: {self.state.value} -> {target.value} is not permitted"
            )
        released = moment if target in {ReservationState.RELEASED, ReservationState.STALE} else None
        return AssetReservation(
            identity=self.identity,
            candidate_id=self.candidate_id,
            state=target,
            reserved_at=self.reserved_at,
            expires_at=self.expires_at,
            released_at=released,
        )

    def is_expired(self, now: datetime) -> bool:
        return now >= self.expires_at


class ExecutionStatus(enum.StrEnum):
    NOT_ATTEMPTED = "NOT_ATTEMPTED"
    BLOCKED_BY_POLICY = "BLOCKED_BY_POLICY"
    AWAITING_OPERATOR = "AWAITING_OPERATOR"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    LISTING_GONE = "LISTING_GONE"
    PRICE_CHANGED = "PRICE_CHANGED"


@dataclass(frozen=True, slots=True)
class PurchaseIntent:
    """A recorded intention to acquire one listing, with a hard price ceiling.

    During groundwork every intent is created with
    ``execution_mode=OPERATOR_APPROVAL_REQUIRED`` and is never executed.
    """

    intent_id: str
    candidate_id: str
    identity: ListingIdentity
    max_price: Money
    execution_mode: ExecutionMode
    created_at: datetime
    expires_at: datetime
    sequence_position: int

    def __post_init__(self) -> None:
        if self.created_at.tzinfo is None or self.expires_at.tzinfo is None:
            raise ValueError("intent timestamps must be timezone-aware UTC")
        if self.expires_at <= self.created_at:
            raise ValueError("intent must expire after creation")
        if self.max_price.is_negative:
            raise ValueError("max_price cannot be negative")
        if self.sequence_position < 0:
            raise ValueError("sequence_position cannot be negative")


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    """What actually happened to a purchase intent."""

    intent_id: str
    status: ExecutionStatus
    recorded_at: datetime
    actual_price: Money | None = None
    venue_reference: str = ""
    detail: str = ""
    recorded_by: str = "system"
    metadata: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.recorded_at.tzinfo is None:
            raise ValueError("recorded_at must be timezone-aware UTC")
        if self.status is ExecutionStatus.SUCCEEDED and self.actual_price is None:
            raise ValueError("a successful execution must record the price actually paid")
        if self.status is not ExecutionStatus.SUCCEEDED and self.actual_price is not None:
            raise ValueError("only a successful execution may record a paid price")
