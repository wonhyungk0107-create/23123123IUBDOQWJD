"""Common adapter protocol.

The default implementation of every operation is a typed refusal. A subclass opts
in to a capability by overriding the method; it cannot opt in by accident.

Purchase-side operations additionally consult the live-execution policy, so even an
adapter that *could* buy something will refuse while the master switch is off. The
refusal is ``SUPPORTED_EXECUTION_DISABLED`` -- distinguishable in the evidence from
``UNSUPPORTED``, because "we chose not to" and "there is no such API" are very
different facts about a venue.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from tradeup.domain.execution import (
    CapabilityResult,
    CapabilityStatus,
    ExecutionMode,
    ExecutionResult,
    PurchaseIntent,
)
from tradeup.domain.fees import FeeOperation, FeeQuote
from tradeup.domain.items import QualityType, Rarity, WearCondition
from tradeup.domain.listings import ListingIdentity, ListingStatus, MarketplaceListing
from tradeup.domain.money import BalanceType, Money
from tradeup.domain.valuation import PriceObservation

__all__ = [
    "AccountState",
    "ListingQuery",
    "ListingVerification",
    "MarketAdapter",
]


@dataclass(frozen=True, slots=True)
class ListingQuery:
    """What to ask a venue for. All fields optional; adapters ignore what they cannot use."""

    market_hash_name: str | None = None
    collection_id: str | None = None
    rarity: Rarity | None = None
    quality: QualityType | None = None
    max_float: Decimal | None = None
    min_float: Decimal | None = None
    max_price: Money | None = None
    limit: int = 100

    def __post_init__(self) -> None:
        if self.limit < 1:
            raise ValueError("limit must be positive")


@dataclass(frozen=True, slots=True)
class ListingVerification:
    """Result of re-checking one listing directly against its venue.

    The three change flags are separate on purpose. "Gone", "cheaper than we thought"
    and "different item than we thought" are different failures with different
    implications, and collapsing them into a single boolean would destroy the
    revalidation statistics that tell us whether the edge is executable.
    """

    identity: ListingIdentity
    status: ListingStatus
    verified_at: datetime
    price: Money | None = None
    raw_float: Decimal | None = None
    asset_id: str | None = None
    detail: str = ""

    @property
    def is_active(self) -> bool:
        return self.status.is_purchasable

    def price_changed_from(self, expected: Money) -> bool:
        return self.price is not None and self.price != expected

    def float_changed_from(self, expected: Decimal) -> bool:
        return self.raw_float is not None and self.raw_float != expected

    def identity_changed_from(self, expected_asset_id: str) -> bool:
        return self.asset_id is not None and self.asset_id != expected_asset_id


@dataclass(frozen=True, slots=True)
class AccountState:
    """Balances held at a venue, by balance type."""

    venue: str
    balances: Mapping[BalanceType, Money]
    fetched_at: datetime

    def available(self, balance_type: BalanceType) -> Money | None:
        return self.balances.get(balance_type)


class MarketAdapter:
    """Base adapter. Every operation refuses unless a subclass implements it.

    Deliberately concrete rather than abstract. There is nothing a subclass *must*
    implement -- a venue we can only read from, or cannot legally touch at all, is a
    perfectly valid adapter that overrides nothing. Marking methods abstract would
    force stub implementations, and stubs are exactly the placeholder-shaped code
    that hides an unsupported operation behind something that looks finished.
    """

    #: Stable venue identifier used in listing identities, fees and evidence.
    venue: str = "unknown"

    #: How a purchase would happen here if policy allowed it.
    execution_mode: ExecutionMode = ExecutionMode.UNSUPPORTED

    #: Human-readable statement of what this adapter is allowed to do and why.
    capability_note: str = ""

    def __init__(self, *, live_execution_enabled: bool = False) -> None:
        self._live_execution_enabled = live_execution_enabled

    # -- refusal helpers -----------------------------------------------------

    def _refuse[T](
        self,
        operation: str,
        moment: datetime,
        status: CapabilityStatus = CapabilityStatus.UNSUPPORTED,
        detail: str = "",
    ) -> CapabilityResult[T]:
        result: CapabilityResult[T] = CapabilityResult(
            status=status,
            venue=self.venue,
            operation=operation,
            observed_at=moment,
            detail=detail or f"{self.venue} does not implement {operation}",
        )
        return result

    def _execution_refusal[T](self, operation: str, moment: datetime) -> CapabilityResult[T]:
        """Refusal appropriate to why a purchase cannot proceed."""
        if self.execution_mode is ExecutionMode.UNSUPPORTED:
            return self._refuse(
                operation,
                moment,
                CapabilityStatus.UNSUPPORTED,
                f"{self.venue} has no documented purchase API; "
                "an operator card is the only sanctioned path",
            )
        if self.execution_mode is ExecutionMode.OPERATOR_APPROVAL_REQUIRED:
            return self._refuse(
                operation,
                moment,
                CapabilityStatus.OPERATOR_ACTION_REQUIRED,
                f"{self.venue} purchases are performed by a human operator",
            )
        if not self._live_execution_enabled:
            return self._refuse(
                operation,
                moment,
                CapabilityStatus.SUPPORTED_EXECUTION_DISABLED,
                f"{self.venue} supports this operation but live execution is disabled",
            )
        return self._refuse(
            operation,
            moment,
            CapabilityStatus.POLICY_BLOCKED,
            f"{self.venue} execution is not permitted during groundwork",
        )

    # -- read operations -----------------------------------------------------

    async def fetch_listings(
        self, query: ListingQuery, *, moment: datetime
    ) -> CapabilityResult[Sequence[MarketplaceListing]]:
        return self._refuse("fetch_listings", moment)

    async def fetch_listing_by_id(
        self, listing_id: str, *, moment: datetime
    ) -> CapabilityResult[MarketplaceListing]:
        return self._refuse("fetch_listing_by_id", moment)

    async def verify_listing(
        self, identity: ListingIdentity, *, moment: datetime
    ) -> CapabilityResult[ListingVerification]:
        return self._refuse("verify_listing", moment)

    async def fetch_fee_quote(
        self, operation: FeeOperation, basis: Money, *, moment: datetime
    ) -> CapabilityResult[FeeQuote]:
        return self._refuse("fetch_fee_quote", moment)

    async def fetch_price_reference(
        self,
        market_hash_name: str,
        *,
        skin_id: str,
        quality: QualityType,
        wear: WearCondition,
        moment: datetime,
    ) -> CapabilityResult[Sequence[PriceObservation]]:
        return self._refuse("fetch_price_reference", moment)

    async def fetch_account_state(self, *, moment: datetime) -> CapabilityResult[AccountState]:
        return self._refuse("fetch_account_state", moment)

    # -- write operations ----------------------------------------------------
    # These have no default implementation that does anything. Both refuse, and a
    # subclass that wants to execute must override *and* pass the policy check.

    async def create_purchase_intent(
        self, intent: PurchaseIntent, *, moment: datetime
    ) -> CapabilityResult[PurchaseIntent]:
        return self._execution_refusal("create_purchase_intent", moment)

    async def reconcile_execution(
        self, intent_id: str, *, moment: datetime
    ) -> CapabilityResult[ExecutionResult]:
        return self._refuse("reconcile_execution", moment)

    # -- introspection -------------------------------------------------------

    def describe(self) -> dict[str, str]:
        """Machine-readable capability summary, written into evidence artifacts."""
        return {
            "venue": self.venue,
            "execution_mode": self.execution_mode.value,
            "live_execution_enabled": str(self._live_execution_enabled),
            "note": self.capability_note,
        }

    def __repr__(self) -> str:
        return f"{type(self).__name__}(venue={self.venue!r}, mode={self.execution_mode.value})"
