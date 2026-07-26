"""CSFloat adapter -- exact float listings and direct revalidation. Read-only.

Capability, verified against https://docs.csfloat.com/ (see docs/source-matrix.md):
the documented API surface is three endpoints -- ``GET /api/v1/listings``,
``GET /api/v1/listings/{id}`` and ``POST /api/v1/listings``. The ``POST`` is a
*seller* operation that creates a listing of your own item. **There is no documented
purchase endpoint.**

So ``execution_mode`` is ``UNSUPPORTED``. Not "disabled pending a switch" --
unsupported, because there is nothing to switch on. If a buy path is ever confirmed
in the official documentation, that is a deliberate change with a citation, not a
configuration flag someone flips.

Authentication is a bare ``Authorization: <API-KEY>`` header. No rate limit is
published; community figures conflict, so none is encoded. The client's generic
backoff and ``Retry-After`` handling covers 429s without us inventing a number.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from tradeup.adapters.base import ListingQuery, ListingVerification, MarketAdapter
from tradeup.adapters.http import (
    HttpResponse,
    RestClient,
    TransportAuthError,
    TransportError,
    TransportRateLimited,
    TransportSchemaError,
    TransportTimeout,
)
from tradeup.domain.execution import CapabilityResult, CapabilityStatus, ExecutionMode
from tradeup.domain.items import QualityType, Rarity
from tradeup.domain.listings import (
    ListingIdentity,
    ListingStatus,
    MarketplaceListing,
    TradableStatus,
)
from tradeup.domain.money import BalanceType, Currency, Money

__all__ = ["CSFLOAT_BASE_URL", "CSFloatAdapter"]

CSFLOAT_BASE_URL = "https://csfloat.com/api/v1"
VENUE = "csfloat"

#: Transport failure -> capability status. Everything unmapped is treated as a
#: temporary outage, which is the conservative reading: we do not know the market
#: state, so the candidate must not proceed.
_TRANSPORT_STATUS: Mapping[type[TransportError], CapabilityStatus] = {
    TransportAuthError: CapabilityStatus.AUTHENTICATION_REQUIRED,
    TransportRateLimited: CapabilityStatus.RATE_LIMITED,
    TransportTimeout: CapabilityStatus.TEMPORARILY_UNAVAILABLE,
    TransportSchemaError: CapabilityStatus.TEMPORARILY_UNAVAILABLE,
}


def _decimal(value: Any, field: str) -> Decimal:
    if value is None:
        raise TransportSchemaError(f"csfloat payload is missing {field}")
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise TransportSchemaError(f"csfloat {field} is not numeric: {value!r}") from exc


class CSFloatAdapter(MarketAdapter):
    """Read-only CSFloat client."""

    venue = VENUE
    execution_mode = ExecutionMode.UNSUPPORTED
    capability_note = (
        "Read and verify only. No purchase endpoint exists in the documented API; "
        "acquisition here requires an operator card."
    )

    def __init__(
        self,
        client: RestClient,
        *,
        api_key: str | None = None,
        base_url: str = CSFLOAT_BASE_URL,
        rarity_by_name: Mapping[str, Rarity] | None = None,
    ) -> None:
        # live_execution_enabled is deliberately not accepted: no flag can enable a
        # purchase path that the venue does not document.
        super().__init__(live_execution_enabled=False)
        self._client = client
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._rarity_by_name = rarity_by_name or {}

    @property
    def has_credentials(self) -> bool:
        return bool(self._api_key)

    def _headers(self) -> dict[str, str]:
        return {"Authorization": self._api_key} if self._api_key else {}

    # -- parsing -------------------------------------------------------------

    def _parse_listing(
        self, payload: Mapping[str, Any], observed_at: datetime, payload_hash: str
    ) -> MarketplaceListing:
        """Strict parse. Any missing required field fails closed.

        A permissive parser that defaults a missing float to 0 or a missing price to
        zero would create a listing that looks like the bargain of the century.
        """
        if not isinstance(payload, Mapping):
            raise TransportSchemaError("csfloat listing entry is not an object")

        listing_id = payload.get("id")
        if not isinstance(listing_id, str) or not listing_id:
            raise TransportSchemaError("csfloat listing has no id")

        item = payload.get("item")
        if not isinstance(item, Mapping):
            raise TransportSchemaError(f"csfloat listing {listing_id} has no item object")

        asset_id = item.get("asset_id")
        market_hash_name = item.get("market_hash_name")
        if not isinstance(asset_id, str) or not asset_id:
            raise TransportSchemaError(f"csfloat listing {listing_id} has no asset_id")
        if not isinstance(market_hash_name, str) or not market_hash_name:
            raise TransportSchemaError(f"csfloat listing {listing_id} has no market_hash_name")

        raw_float = _decimal(item.get("float_value"), f"listing {listing_id} float_value")
        min_float = _decimal(item.get("min_float"), f"listing {listing_id} min_float")
        max_float = _decimal(item.get("max_float"), f"listing {listing_id} max_float")
        if max_float <= min_float:
            raise TransportSchemaError(f"csfloat listing {listing_id} has a degenerate float range")
        normalized = (raw_float - min_float) / (max_float - min_float)
        if not (Decimal(0) <= normalized <= Decimal(1)):
            raise TransportSchemaError(
                f"csfloat listing {listing_id} float {raw_float} is outside its stated range"
            )

        price_raw = payload.get("price")
        if not isinstance(price_raw, int):
            raise TransportSchemaError(
                f"csfloat listing {listing_id} price is not an integer minor-unit value"
            )

        collection_id = item.get("collection")
        if not isinstance(collection_id, str) or not collection_id:
            raise TransportSchemaError(
                f"csfloat listing {listing_id} states no collection; it cannot be "
                "used in a contract and must not be guessed at"
            )

        rarity_name = item.get("rarity_name")
        rarity = self._rarity_by_name.get(str(rarity_name).strip().lower()) if rarity_name else None
        if rarity is None:
            raise TransportSchemaError(
                f"csfloat listing {listing_id} has unmapped rarity {rarity_name!r}"
            )

        quality = QualityType.NORMAL
        if item.get("is_stattrak") is True:
            quality = QualityType.STATTRAK
        elif item.get("is_souvenir") is True:
            quality = QualityType.SOUVENIR

        return MarketplaceListing(
            identity=ListingIdentity(VENUE, listing_id),
            asset_id=asset_id,
            skin_id=f"{item.get('paint_index', 'unknown')}@{collection_id}",
            market_hash_name=market_hash_name,
            collection_id=collection_id,
            rarity=rarity,
            quality_type=quality,
            raw_float=raw_float,
            normalized_float=normalized,
            price=Money(price_raw, Currency.USD, BalanceType.CASH_WITHDRAWABLE),
            # CSFloat's buyer-side fee is not published as a per-listing field. It is
            # resolved from the versioned fee schedule, which fails closed if absent.
            buyer_fee=Money.zero(Currency.USD, BalanceType.CASH_WITHDRAWABLE),
            deposit_fee=Money.zero(Currency.USD, BalanceType.CASH_WITHDRAWABLE),
            observed_at=observed_at,
            listing_status=ListingStatus.ACTIVE,
            tradable_status=TradableStatus.TRADABLE,
            raw_payload_hash=payload_hash,
            paint_index=(
                int(item["paint_index"]) if isinstance(item.get("paint_index"), int) else None
            ),
            paint_seed=int(item["paint_seed"]) if isinstance(item.get("paint_seed"), int) else None,
        )

    def _failure[T](
        self, operation: str, moment: datetime, exc: TransportError
    ) -> CapabilityResult[T]:
        """Map a transport failure onto a typed capability refusal.

        A schema error maps to TEMPORARILY_UNAVAILABLE rather than being swallowed:
        a payload we cannot read means we do not know the market state, and not
        knowing must stop the candidate exactly like an outage does.
        """
        status = _TRANSPORT_STATUS.get(type(exc), CapabilityStatus.TEMPORARILY_UNAVAILABLE)
        retry_after = exc.retry_after_seconds if isinstance(exc, TransportRateLimited) else None
        result: CapabilityResult[T] = CapabilityResult(
            status=status,
            venue=self.venue,
            operation=operation,
            observed_at=moment,
            detail=str(exc) or type(exc).__name__,
            retry_after_seconds=retry_after,
        )
        return result

    # -- operations ----------------------------------------------------------

    async def fetch_listings(
        self, query: ListingQuery, *, moment: datetime
    ) -> CapabilityResult[Sequence[MarketplaceListing]]:
        if not self.has_credentials:
            return self._refuse(
                "fetch_listings",
                moment,
                CapabilityStatus.AUTHENTICATION_REQUIRED,
                "CSFLOAT_API_KEY is not configured",
            )
        params: dict[str, str] = {"limit": str(query.limit)}
        if query.market_hash_name:
            params["market_hash_name"] = query.market_hash_name
        if query.min_float is not None:
            params["min_float"] = str(query.min_float)
        if query.max_float is not None:
            params["max_float"] = str(query.max_float)

        try:
            response = await self._client.get(
                f"{self._base_url}/listings", params=params, headers=self._headers()
            )
            listings = self._parse_listings_response(response, moment)
        except TransportError as exc:
            return self._failure("fetch_listings", moment, exc)
        return CapabilityResult.succeeded(self.venue, "fetch_listings", moment, listings)

    def _parse_listings_response(
        self, response: HttpResponse, moment: datetime
    ) -> tuple[MarketplaceListing, ...]:
        document = response.json()
        rows = document.get("data") if isinstance(document, Mapping) else document
        if not isinstance(rows, list):
            raise TransportSchemaError("csfloat listings response is not a list")
        return tuple(self._parse_listing(row, moment, response.payload_sha256) for row in rows)

    async def fetch_listing_by_id(
        self, listing_id: str, *, moment: datetime
    ) -> CapabilityResult[MarketplaceListing]:
        if not self.has_credentials:
            return self._refuse(
                "fetch_listing_by_id",
                moment,
                CapabilityStatus.AUTHENTICATION_REQUIRED,
                "CSFLOAT_API_KEY is not configured",
            )
        try:
            response = await self._client.get(
                f"{self._base_url}/listings/{listing_id}", headers=self._headers()
            )
            payload = response.json()
            listing = self._parse_listing(payload, moment, response.payload_sha256)
        except TransportError as exc:
            return self._failure("fetch_listing_by_id", moment, exc)
        return CapabilityResult.succeeded(self.venue, "fetch_listing_by_id", moment, listing)

    async def verify_listing(
        self, identity: ListingIdentity, *, moment: datetime
    ) -> CapabilityResult[ListingVerification]:
        """Direct revalidation against the single-listing endpoint."""
        result = await self.fetch_listing_by_id(identity.listing_id, moment=moment)
        if result.ok:
            listing = result.unwrap()
            return CapabilityResult.succeeded(
                self.venue,
                "verify_listing",
                moment,
                ListingVerification(
                    identity=identity,
                    status=ListingStatus.ACTIVE,
                    verified_at=moment,
                    price=listing.price,
                    raw_float=listing.raw_float,
                    asset_id=listing.asset_id,
                    detail="revalidated via GET /listings/{id}",
                ),
            )
        if result.status is CapabilityStatus.TEMPORARILY_UNAVAILABLE:
            # A 404 on a single listing means it is gone -- a fact, not an outage.
            return CapabilityResult.succeeded(
                self.venue,
                "verify_listing",
                moment,
                ListingVerification(
                    identity=identity,
                    status=ListingStatus.UNKNOWN,
                    verified_at=moment,
                    detail=f"could not confirm listing: {result.detail}",
                ),
            )
        return self._refuse("verify_listing", moment, result.status, result.detail)
