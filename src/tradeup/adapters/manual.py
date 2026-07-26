"""Manual-link and policy-blocked venues.

Three kinds of venue live here, and the difference between them is recorded in the
type system rather than in a comment:

* **Manual-link** (CS.MONEY, SkinSwap) -- inventory exists and a human can transact
  there, but we have not verified a documented partner API. Every operation returns
  ``OPERATOR_ACTION_REQUIRED`` with a link for a person to open. The temptation these
  adapters exist to resist is reaching for the internal endpoints a browser uses;
  those are undocumented, and using them is exactly the practice this project
  refuses to inherit from its reference implementations.

* **Unidentified** ("Skins Money") -- named in the project brief, never identified.
  ``skins.money`` returns an HTTP 522 from a Cloudflare edge with a dead origin,
  which is not evidence that a service exists. It stays ``POLICY_BLOCKED`` until
  someone identifies the actual service and its authorised interface. It is
  deliberately not resolved by guessing at CS.MONEY or SkinsMonkey.

* **Steam** -- the trade-up itself happens here, performed by a human. Every
  operation is ``POLICY_BLOCKED``. See :class:`SteamManualAdapter`.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from tradeup.adapters.base import AccountState, ListingQuery, ListingVerification, MarketAdapter
from tradeup.domain.execution import CapabilityResult, CapabilityStatus, ExecutionMode
from tradeup.domain.listings import ListingIdentity, MarketplaceListing

__all__ = [
    "CSMoneyManualAdapter",
    "DisabledAdapter",
    "ManualLinkAdapter",
    "SkinSwapManualAdapter",
    "SteamManualAdapter",
]


class ManualLinkAdapter(MarketAdapter):
    """A venue a human can use and an agent cannot.

    Reads refuse with ``OPERATOR_ACTION_REQUIRED`` rather than ``UNSUPPORTED``,
    because the data does exist -- it is reachable by a person, just not by us.
    """

    execution_mode = ExecutionMode.OPERATOR_APPROVAL_REQUIRED

    def __init__(self, venue: str, *, browse_url: str, reason: str) -> None:
        super().__init__(live_execution_enabled=False)
        self.venue = venue
        self._browse_url = browse_url
        self.capability_note = reason

    @property
    def browse_url(self) -> str:
        """URL for an operator to open. Never requested by this process."""
        return self._browse_url

    def _operator_required[T](self, operation: str, moment: datetime) -> CapabilityResult[T]:
        return self._refuse(
            operation,
            moment,
            CapabilityStatus.OPERATOR_ACTION_REQUIRED,
            f"{self.capability_note} Operator link: {self._browse_url}",
        )

    async def fetch_listings(
        self, query: ListingQuery, *, moment: datetime
    ) -> CapabilityResult[Sequence[MarketplaceListing]]:
        return self._operator_required("fetch_listings", moment)

    async def verify_listing(
        self, identity: ListingIdentity, *, moment: datetime
    ) -> CapabilityResult[ListingVerification]:
        return self._operator_required("verify_listing", moment)

    async def fetch_account_state(self, *, moment: datetime) -> CapabilityResult[AccountState]:
        return self._operator_required("fetch_account_state", moment)


class CSMoneyManualAdapter(ManualLinkAdapter):
    """CS.MONEY. Manual link only."""

    def __init__(self) -> None:
        super().__init__(
            "csmoney",
            browse_url="https://cs.money/csgo/trade/",
            reason=(
                "No verified partner API. Browser-internal endpoints are undocumented "
                "and must not be called; acquisition here is an operator action."
            ),
        )


class SkinSwapManualAdapter(ManualLinkAdapter):
    """SkinSwap. Manual link only until a documented API is verified."""

    def __init__(self) -> None:
        super().__init__(
            "skinswap",
            browse_url="https://skinswap.com/",
            reason=(
                "No documented public API verified. Treated as manual-link inventory "
                "until one is confirmed."
            ),
        )


class DisabledAdapter(MarketAdapter):
    """A venue that is switched off, with the reason attached.

    Every operation returns ``POLICY_BLOCKED``. Registering a disabled adapter is
    better than omitting the venue: the refusal is visible in evidence artifacts, so
    a reader can see the source was considered and deliberately excluded.
    """

    execution_mode = ExecutionMode.UNSUPPORTED

    def __init__(self, venue: str, *, reason: str) -> None:
        super().__init__(live_execution_enabled=False)
        self.venue = venue
        self.capability_note = reason

    def _blocked[T](self, operation: str, moment: datetime) -> CapabilityResult[T]:
        return self._refuse(
            operation, moment, CapabilityStatus.POLICY_BLOCKED, self.capability_note
        )

    async def fetch_listings(
        self, query: ListingQuery, *, moment: datetime
    ) -> CapabilityResult[Sequence[MarketplaceListing]]:
        return self._blocked("fetch_listings", moment)

    async def verify_listing(
        self, identity: ListingIdentity, *, moment: datetime
    ) -> CapabilityResult[ListingVerification]:
        return self._blocked("verify_listing", moment)

    async def fetch_account_state(self, *, moment: datetime) -> CapabilityResult[AccountState]:
        return self._blocked("fetch_account_state", moment)


class SteamManualAdapter(DisabledAdapter):
    """Steam. Human-only, permanently.

    The Steam Subscriber Agreement §4.C ("Automation") states: "You may not use any
    form of scripts, bots, macros, or other non-human-controlled systems
    ('Automation') to interact with Content and Services on Steam in any manner,
    including but not limited to..." -- a broad prohibition whose enumerated examples
    concern account creation, stats, rewards and Overwatch, and which reaches
    Market interaction through the lead-in clause rather than a named bullet.

    This adapter is the enforcement point: there is no method here that can be
    overridden into contacting Steam, and the trade-up itself is performed by a
    person following an operator card.
    """

    def __init__(self) -> None:
        super().__init__(
            "steam",
            reason=(
                "Steam interaction is human-only under SSA section 4.C (Automation). "
                "The trade-up contract is executed manually by the operator; this "
                "system emits instructions and records the result."
            ),
        )


def skins_money_adapter() -> DisabledAdapter:
    """The unidentified 'Skins Money' source from the project brief."""
    return DisabledAdapter(
        "skins_money",
        reason=(
            "Service not identified. skins.money returns HTTP 522 (Cloudflare edge, "
            "dead origin), which is not evidence a service exists. Disabled until the "
            "actual service and an authorised interface are identified; deliberately "
            "not resolved by assuming CS.MONEY or SkinsMonkey."
        ),
    )
