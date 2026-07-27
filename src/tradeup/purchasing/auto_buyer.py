"""The gated auto-buy engine.

Turns an approved, entry-met candidate into venue purchase submissions -- or,
far more often, into a precise account of why it refused. The gate ladder, in
order, with no path around any rung:

1. ``live_execution_enabled`` is off -> every intent is ``BLOCKED_BY_POLICY``
   and **no venue is contacted**.
2. No ledger-backed daily-spend figure was supplied -> blocked. This is
   structural: the engine cannot be wired into a caller that does not account
   for money already spent today, so enabling live execution without ledger
   integration is impossible rather than merely discouraged.
3. The bundle's ceiling plus today's recorded spend would exceed
   ``max_daily_spend`` -> blocked.
4. A venue in the bundle has no configured adapter -> blocked.
5. The whole bundle is reserved atomically; a conflict aborts before anything
   is bought.
6. Every listing is re-verified against its venue *before the first purchase*.
   Gone or repriced means nothing is bought at all.
7. Purchases run sequentially under each intent's hard price ceiling. A
   failure mid-bundle stops everything: already-purchased inputs stay
   ``PURCHASED`` (they are the orphan inventory the partial-fill reserve
   priced), unpurchased reservations are released, and the remaining intents
   record ``NOT_ATTEMPTED``.

A submission whose reconciliation fails leaves its reservation in
``PURCHASE_PENDING`` deliberately: money may have moved, so the listing must
stay claimed until a human reconciles it. The engine never guesses.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime

from tradeup.adapters.base import MarketAdapter
from tradeup.config import Settings
from tradeup.domain.contracts import TradeupCandidate
from tradeup.domain.execution import (
    CapabilityResult,
    CapabilityStatus,
    ExecutionMode,
    ExecutionResult,
    ExecutionStatus,
    PurchaseIntent,
    ReservationState,
)
from tradeup.domain.money import Money, money_sum
from tradeup.execution.reservations import ReservationRegistry

__all__ = ["AutoBuyer"]

#: How a typed adapter refusal maps onto an execution outcome.
_REFUSAL_STATUS: dict[CapabilityStatus, ExecutionStatus] = {
    CapabilityStatus.OPERATOR_ACTION_REQUIRED: ExecutionStatus.AWAITING_OPERATOR,
    CapabilityStatus.UNSUPPORTED: ExecutionStatus.BLOCKED_BY_POLICY,
    CapabilityStatus.POLICY_BLOCKED: ExecutionStatus.BLOCKED_BY_POLICY,
    CapabilityStatus.SUPPORTED_EXECUTION_DISABLED: ExecutionStatus.BLOCKED_BY_POLICY,
    CapabilityStatus.AUTHENTICATION_REQUIRED: ExecutionStatus.FAILED,
    CapabilityStatus.RATE_LIMITED: ExecutionStatus.FAILED,
    CapabilityStatus.TEMPORARILY_UNAVAILABLE: ExecutionStatus.FAILED,
}


class AutoBuyer:
    """Executes one approved candidate's bundle, or records exactly why not."""

    def __init__(
        self,
        *,
        settings: Settings,
        adapters: Mapping[str, MarketAdapter],
        reservations: ReservationRegistry,
        spent_today: Money | None = None,
    ) -> None:
        self._settings = settings
        self._adapters = dict(adapters)
        self._reservations = reservations
        self._spent_today = spent_today

    # -- intent construction --------------------------------------------------

    def _intents(self, candidate: TradeupCandidate) -> tuple[PurchaseIntent, ...]:
        intents: list[PurchaseIntent] = []
        for position, item in enumerate(candidate.inputs):
            adapter = self._adapters.get(item.identity.venue)
            mode = adapter.execution_mode if adapter is not None else ExecutionMode.UNSUPPORTED
            intents.append(
                PurchaseIntent(
                    intent_id=f"{candidate.candidate_id}:{position:02d}",
                    candidate_id=candidate.candidate_id,
                    identity=item.identity,
                    max_price=item.listing.price,
                    execution_mode=mode,
                    created_at=candidate.created_at,
                    expires_at=candidate.expires_at,
                    sequence_position=position,
                )
            )
        return tuple(intents)

    @staticmethod
    def _all(
        intents: tuple[PurchaseIntent, ...],
        status: ExecutionStatus,
        moment: datetime,
        detail: str,
    ) -> tuple[ExecutionResult, ...]:
        return tuple(
            ExecutionResult(
                intent_id=intent.intent_id,
                status=status,
                recorded_at=moment,
                detail=detail,
            )
            for intent in intents
        )

    # -- execution ------------------------------------------------------------

    async def execute_candidate(
        self, candidate: TradeupCandidate, *, moment: datetime
    ) -> tuple[ExecutionResult, ...]:
        """Run the gate ladder and, only if every rung holds, buy the bundle."""
        intents = self._intents(candidate)

        if not self._settings.live_execution_enabled:
            return self._all(
                intents,
                ExecutionStatus.BLOCKED_BY_POLICY,
                moment,
                "live execution is disabled (live_execution_enabled=false); no venue was contacted",
            )
        if self._spent_today is None:
            return self._all(
                intents,
                ExecutionStatus.BLOCKED_BY_POLICY,
                moment,
                "no ledger-backed daily-spend figure was supplied; refusing to spend unaccountably",
            )
        ceiling = money_sum(
            (intent.max_price for intent in intents),
            currency=self._settings.base_currency,
        )
        budget = self._settings.max_daily_spend
        if self._spent_today + ceiling > budget:
            return self._all(
                intents,
                ExecutionStatus.BLOCKED_BY_POLICY,
                moment,
                f"bundle ceiling {ceiling} plus spent-today {self._spent_today} "
                f"exceeds max_daily_spend {budget}",
            )
        missing = sorted({intent.identity.venue for intent in intents} - self._adapters.keys())
        if missing:
            return self._all(
                intents,
                ExecutionStatus.BLOCKED_BY_POLICY,
                moment,
                f"no adapter configured for venue(s): {', '.join(missing)}",
            )

        claim = self._reservations.claim_bundle(
            candidate.candidate_id,
            [intent.identity for intent in intents],
            now=moment,
        )
        if not claim.succeeded:
            conflicts = ", ".join(str(identity) for identity in claim.conflicts)
            return self._all(
                intents,
                ExecutionStatus.NOT_ATTEMPTED,
                moment,
                f"bundle not reserved; listings held by another candidate: {conflicts}",
            )

        abort = await self._reverify(candidate, intents, moment)
        if abort is not None:
            self._reservations.release_candidate(candidate.candidate_id, now=moment)
            return abort

        return await self._buy_sequentially(candidate, intents, moment)

    async def _reverify(
        self,
        candidate: TradeupCandidate,
        intents: tuple[PurchaseIntent, ...],
        moment: datetime,
    ) -> tuple[ExecutionResult, ...] | None:
        """Re-check every listing before the first purchase. None means proceed."""
        for intent, item in zip(intents, candidate.inputs, strict=True):
            adapter = self._adapters[intent.identity.venue]
            verification = await adapter.verify_listing(intent.identity, moment=moment)
            if not verification.ok:
                return self._abort_on(
                    intents,
                    intent,
                    ExecutionStatus.FAILED,
                    moment,
                    f"revalidation refused: {verification.status.value} -- {verification.detail}",
                )
            checked = verification.unwrap()
            if not checked.is_present:
                return self._abort_on(
                    intents, intent, ExecutionStatus.LISTING_GONE, moment, "listing gone"
                )
            if not checked.is_active or checked.price_changed_from(item.listing.price):
                return self._abort_on(
                    intents,
                    intent,
                    ExecutionStatus.PRICE_CHANGED,
                    moment,
                    f"quoted {item.listing.price}, venue now reports {checked.price}",
                )
        return None

    @staticmethod
    def _abort_on(
        intents: tuple[PurchaseIntent, ...],
        failing: PurchaseIntent,
        status: ExecutionStatus,
        moment: datetime,
        detail: str,
    ) -> tuple[ExecutionResult, ...]:
        results: list[ExecutionResult] = []
        for intent in intents:
            if intent.intent_id == failing.intent_id:
                results.append(
                    ExecutionResult(
                        intent_id=intent.intent_id,
                        status=status,
                        recorded_at=moment,
                        detail=detail,
                    )
                )
            else:
                results.append(
                    ExecutionResult(
                        intent_id=intent.intent_id,
                        status=ExecutionStatus.NOT_ATTEMPTED,
                        recorded_at=moment,
                        detail=f"bundle aborted before purchase: {detail}",
                    )
                )
        return tuple(results)

    async def _buy_sequentially(
        self,
        candidate: TradeupCandidate,
        intents: tuple[PurchaseIntent, ...],
        moment: datetime,
    ) -> tuple[ExecutionResult, ...]:
        results: list[ExecutionResult] = []
        aborted = False
        abort_detail = ""
        for intent in intents:
            if aborted:
                results.append(
                    ExecutionResult(
                        intent_id=intent.intent_id,
                        status=ExecutionStatus.NOT_ATTEMPTED,
                        recorded_at=moment,
                        detail=f"bundle aborted after earlier failure: {abort_detail}",
                    )
                )
                self._release(intent, moment)
                continue

            adapter = self._adapters[intent.identity.venue]
            self._reservations.transition(
                intent.identity, ReservationState.PURCHASE_PENDING, now=moment
            )
            submitted: CapabilityResult[PurchaseIntent] = await adapter.create_purchase_intent(
                intent, moment=moment
            )
            if not submitted.ok:
                status = _REFUSAL_STATUS.get(submitted.status, ExecutionStatus.FAILED)
                abort_detail = f"{submitted.status.value}: {submitted.detail}"
                results.append(
                    ExecutionResult(
                        intent_id=intent.intent_id,
                        status=status,
                        recorded_at=moment,
                        detail=abort_detail,
                    )
                )
                self._release(intent, moment)
                aborted = True
                continue

            reconciled = await adapter.reconcile_execution(intent.intent_id, moment=moment)
            if not reconciled.ok:
                # Money may have moved. The reservation stays PURCHASE_PENDING
                # on purpose; only a human reconciliation may resolve it.
                abort_detail = (
                    "purchase submitted but reconciliation refused: "
                    f"{reconciled.status.value} -- {reconciled.detail}; "
                    "manual reconciliation required"
                )
                results.append(
                    ExecutionResult(
                        intent_id=intent.intent_id,
                        status=ExecutionStatus.FAILED,
                        recorded_at=moment,
                        detail=abort_detail,
                    )
                )
                aborted = True
                continue

            outcome = reconciled.unwrap()
            results.append(outcome)
            if outcome.status is ExecutionStatus.SUCCEEDED:
                self._reservations.transition(
                    intent.identity, ReservationState.PURCHASED, now=moment
                )
            else:
                abort_detail = f"venue reported {outcome.status.value}: {outcome.detail}"
                self._release(intent, moment)
                aborted = True
        return tuple(results)

    def _release(self, intent: PurchaseIntent, moment: datetime) -> None:
        state = self._reservations.state_of(intent.identity)
        if state.is_active and state is not ReservationState.PURCHASED:
            self._reservations.transition(intent.identity, ReservationState.RELEASED, now=moment)
