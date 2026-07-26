"""SkinSnipe adapter -- cross-market price reference only.

Aggregator data is never execution truth. A price SkinSnipe reports for a venue is a
claim about that venue at some past moment; the only thing that can authorise a
purchase is a direct requery of the venue itself. So this adapter can produce
:class:`~tradeup.domain.valuation.PriceObservation` at the
``CROSS_MARKET_REFERENCE`` rung and nothing else -- it cannot fetch listings, cannot
verify one, and has no execution path at all.

**Unverified:** the SkinSnipe API base URL and endpoint shapes are shown only to
paying subscribers, so they could not be confirmed. ``base_url`` is therefore
required at construction rather than defaulted to a guess, and the adapter refuses
cleanly until one is supplied.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from tradeup.adapters.base import MarketAdapter
from tradeup.adapters.http import (
    RestClient,
    TransportAuthError,
    TransportError,
    TransportRateLimited,
    TransportSchemaError,
    TransportTimeout,
)
from tradeup.domain.execution import CapabilityResult, CapabilityStatus, ExecutionMode
from tradeup.domain.items import QualityType, WearCondition
from tradeup.domain.money import BalanceType, Currency, Money
from tradeup.domain.valuation import PriceObservation, ValuationSource

__all__ = ["SkinSnipeAdapter"]

VENUE = "skinsnipe"

_TRANSPORT_STATUS: Mapping[type[TransportError], CapabilityStatus] = {
    TransportAuthError: CapabilityStatus.AUTHENTICATION_REQUIRED,
    TransportRateLimited: CapabilityStatus.RATE_LIMITED,
    TransportTimeout: CapabilityStatus.TEMPORARILY_UNAVAILABLE,
    TransportSchemaError: CapabilityStatus.TEMPORARILY_UNAVAILABLE,
}


class SkinSnipeAdapter(MarketAdapter):
    """Price-reference-only aggregator client."""

    venue = VENUE
    execution_mode = ExecutionMode.UNSUPPORTED
    capability_note = (
        "Cross-market price reference only. Never execution truth; every price it "
        "reports must be reconfirmed directly at the venue before any purchase."
    )

    def __init__(
        self,
        client: RestClient,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
    ) -> None:
        super().__init__(live_execution_enabled=False)
        self._client = client
        self._api_key = api_key
        self._base_url = base_url.rstrip("/") if base_url else None

    @property
    def is_configured(self) -> bool:
        return bool(self._api_key) and bool(self._base_url)

    async def fetch_price_reference(
        self,
        market_hash_name: str,
        *,
        skin_id: str,
        quality: QualityType,
        wear: WearCondition,
        moment: datetime,
    ) -> CapabilityResult[Sequence[PriceObservation]]:
        if not self._base_url:
            return self._refuse(
                "fetch_price_reference",
                moment,
                CapabilityStatus.UNSUPPORTED,
                "SkinSnipe API base URL is not documented publicly and none was "
                "configured; refusing to guess an endpoint",
            )
        if not self._api_key:
            return self._refuse(
                "fetch_price_reference",
                moment,
                CapabilityStatus.AUTHENTICATION_REQUIRED,
                "SKINSNIPE_API_KEY is not configured",
            )
        try:
            response = await self._client.get(
                f"{self._base_url}/prices",
                params={"market_hash_name": market_hash_name},
                headers={"Authorization": f"Bearer {self._api_key}"},
            )
            observations = self._parse(response.json(), skin_id, quality, wear, moment)
        except TransportError as exc:
            status = _TRANSPORT_STATUS.get(type(exc), CapabilityStatus.TEMPORARILY_UNAVAILABLE)
            return self._refuse("fetch_price_reference", moment, status, str(exc))
        return CapabilityResult.succeeded(self.venue, "fetch_price_reference", moment, observations)

    @staticmethod
    def _parse(
        document: Any,
        skin_id: str,
        quality: QualityType,
        wear: WearCondition,
        moment: datetime,
    ) -> tuple[PriceObservation, ...]:
        rows = document.get("prices") if isinstance(document, Mapping) else document
        if not isinstance(rows, list):
            raise TransportSchemaError("skinsnipe response has no price list")
        observations: list[PriceObservation] = []
        for row in rows:
            if not isinstance(row, Mapping):
                raise TransportSchemaError("skinsnipe price entry is not an object")
            venue = row.get("market")
            raw_price = row.get("price")
            if not isinstance(venue, str) or raw_price is None:
                raise TransportSchemaError("skinsnipe price entry lacks market or price")
            try:
                minor = int(Decimal(str(raw_price)) * 100)
            except (InvalidOperation, ValueError) as exc:
                raise TransportSchemaError(f"skinsnipe price {raw_price!r} is not numeric") from exc
            observations.append(
                PriceObservation(
                    skin_id=skin_id,
                    quality=quality,
                    wear=wear,
                    venue=venue,
                    gross=Money(minor, Currency.USD, BalanceType.CASH_WITHDRAWABLE),
                    source=ValuationSource.CROSS_MARKET_REFERENCE,
                    observed_at=moment,
                    evidence_count=1,
                )
            )
        return tuple(observations)
