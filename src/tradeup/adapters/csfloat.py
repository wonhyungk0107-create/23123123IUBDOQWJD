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

Live-verified payload shape (authenticated read, 2026-07-26, recorded in
docs/source-matrix.md): a listing row carries ``type`` (``buy_now`` or
``auction``), ``state``, ``price`` (integer cents) and an ``item`` object with
``item_name``, ``float_value``, ``paint_index``, ``def_index``, ``rarity_name``
and quality flags. The item does **not** state its collection or float caps, so
identity, collection and float range are resolved from the pinned metadata
registry — which the architecture already treats as the only authority for those
facts. A row that cannot be resolved unambiguously is excluded and counted, never
guessed at, and only ``buy_now``/``listed`` rows count as purchasable listings:
an auction's current price is a bid, not an ask.
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
from tradeup.domain.items import QualityType, Rarity, Skin
from tradeup.domain.listings import (
    ListingIdentity,
    ListingStatus,
    MarketplaceListing,
    TradableStatus,
)
from tradeup.domain.money import BalanceType, Currency, Money
from tradeup.metadata.registry import MetadataRegistry

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


#: Documented ``category`` filter values (1 normal, 2 stattrak, 3 souvenir).
_CATEGORY_PARAM: Mapping[QualityType, int] = {
    QualityType.NORMAL: 1,
    QualityType.STATTRAK: 2,
    QualityType.SOUVENIR: 3,
}

#: ``rarity`` filter values. The parameter is documented but its values are not;
#: these are the game's standard rarity indices, of which only 6=Covert has been
#: confirmed against a live response (docs/source-matrix.md). A wrong value here
#: biases recall, never correctness: every returned row is still resolved against
#: the registry and cross-checked on rarity_name.
_RARITY_PARAM: Mapping[Rarity, int] = {
    Rarity.CONSUMER: 1,
    Rarity.INDUSTRIAL: 2,
    Rarity.MIL_SPEC: 3,
    Rarity.RESTRICTED: 4,
    Rarity.CLASSIFIED: 5,
    Rarity.COVERT: 6,
}

#: Measured live (2026-07-26): the documented maximum ``limit=50`` draws a 429
#: from a limiter that carries no rate headers, while 40 succeeds against the
#: measured 200-requests-per-window budget. 40 is the safe page size.
_PAGE_LIMIT = 40


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
        registry: MetadataRegistry | None = None,
    ) -> None:
        # live_execution_enabled is deliberately not accepted: no flag can enable a
        # purchase path that the venue does not document.
        super().__init__(live_execution_enabled=False)
        self._client = client
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._rarity_by_name = rarity_by_name or {}
        self._registry = registry

    @property
    def has_credentials(self) -> bool:
        return bool(self._api_key)

    def _headers(self) -> dict[str, str]:
        return {"Authorization": self._api_key} if self._api_key else {}

    # -- parsing -------------------------------------------------------------

    #: Quality prefixes CSFloat bakes into display names and the registry does not.
    #: Literal removal only — this is a dictionary-key adjustment, not name parsing.
    _NAME_PREFIXES = ("StatTrak™ ", "Souvenir ")

    def _resolve_skin(
        self,
        listing_id: str,
        item_name: str,
        paint_index: int | None,
        rarity: Rarity,
        quality: QualityType,
    ) -> Skin:
        """Resolve a venue row to exactly one registry skin, or fail closed.

        The registry is the only authority for collection membership and float
        caps, so identity must land on a registry entry. Candidates come from an
        exact base-name lookup, then must agree on paint index (when both sides
        state one), rarity and quality support. Anything other than exactly one
        surviving candidate is a refusal to guess.
        """
        if self._registry is None:
            raise TransportSchemaError(
                f"csfloat listing {listing_id} cannot be resolved: no metadata "
                "registry configured, and identity is registry-authoritative"
            )
        candidates = self._registry.skins_named(item_name)
        if not candidates:
            for prefix in self._NAME_PREFIXES:
                if item_name.startswith(prefix):
                    candidates = self._registry.skins_named(item_name[len(prefix) :])
                    break
        if paint_index is not None:
            stated = [s for s in candidates if s.paint_index is not None]
            if stated:
                candidates = tuple(s for s in stated if s.paint_index == paint_index)
        candidates = tuple(s for s in candidates if s.rarity is rarity and s.supports(quality))
        if len(candidates) != 1:
            raise TransportSchemaError(
                f"csfloat listing {listing_id} identity {item_name!r} "
                f"(paint {paint_index}, {rarity.value}, {quality.value}) resolved to "
                f"{len(candidates)} registry skins; refusing to guess"
            )
        return candidates[0]

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

        listing_type = payload.get("type")
        if listing_type != "buy_now":
            raise TransportSchemaError(
                f"csfloat listing {listing_id} type {listing_type!r} is not purchasable "
                "at its stated price; an auction's current price is a bid, not an ask"
            )
        state = payload.get("state")
        if state != "listed":
            raise TransportSchemaError(
                f"csfloat listing {listing_id} state {state!r} is not an active listing"
            )

        item = payload.get("item")
        if not isinstance(item, Mapping):
            raise TransportSchemaError(f"csfloat listing {listing_id} has no item object")

        asset_id = item.get("asset_id")
        market_hash_name = item.get("market_hash_name")
        item_name = item.get("item_name")
        if not isinstance(asset_id, str) or not asset_id:
            raise TransportSchemaError(f"csfloat listing {listing_id} has no asset_id")
        if not isinstance(market_hash_name, str) or not market_hash_name:
            raise TransportSchemaError(f"csfloat listing {listing_id} has no market_hash_name")
        if not isinstance(item_name, str) or not item_name:
            raise TransportSchemaError(f"csfloat listing {listing_id} has no item_name")

        raw_float = _decimal(item.get("float_value"), f"listing {listing_id} float_value")

        price_raw = payload.get("price")
        if not isinstance(price_raw, int):
            raise TransportSchemaError(
                f"csfloat listing {listing_id} price is not an integer minor-unit value"
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

        paint_index = int(item["paint_index"]) if isinstance(item.get("paint_index"), int) else None
        skin = self._resolve_skin(listing_id, item_name, paint_index, rarity, quality)

        span = skin.float_range.maximum - skin.float_range.minimum
        normalized = (raw_float - skin.float_range.minimum) / span
        if not (Decimal(0) <= normalized <= Decimal(1)):
            raise TransportSchemaError(
                f"csfloat listing {listing_id} float {raw_float} is outside the "
                f"registry's stated range for {skin.skin_id}"
            )

        return MarketplaceListing(
            identity=ListingIdentity(VENUE, listing_id),
            asset_id=asset_id,
            skin_id=skin.skin_id,
            market_hash_name=market_hash_name,
            collection_id=skin.collection_id,
            rarity=skin.rarity,
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
            paint_index=paint_index,
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

    def _listing_params(self, query: ListingQuery) -> dict[str, str]:
        """Documented filters only. ``type=buy_now`` is unconditional: an auction's
        current price is a bid, and this system only models purchasable asks."""
        params: dict[str, str] = {
            "type": "buy_now",
            "sort_by": "lowest_price",
        }
        if query.market_hash_name:
            params["market_hash_name"] = query.market_hash_name
        if query.min_float is not None:
            params["min_float"] = str(query.min_float)
        if query.max_float is not None:
            params["max_float"] = str(query.max_float)
        if query.max_price is not None:
            params["max_price"] = str(query.max_price.minor_units)
        if query.min_price is not None:
            params["min_price"] = str(query.min_price.minor_units)
        if query.rarity is not None:
            params["rarity"] = str(_RARITY_PARAM[query.rarity])
        if query.quality is not None:
            params["category"] = str(_CATEGORY_PARAM[query.quality])
        return params

    async def fetch_listings(
        self, query: ListingQuery, *, moment: datetime
    ) -> CapabilityResult[Sequence[MarketplaceListing]]:
        """Fetch up to ``query.limit`` listings, following documented cursor pages.

        Rows are parsed strictly and individually: an unparseable row is excluded
        and counted, never defaulted. Only when *no* row across the whole fetch
        parses is the result a refusal — an empty success there would hide a
        breaking API change behind "the market was quiet".
        """
        if not self.has_credentials:
            return self._refuse(
                "fetch_listings",
                moment,
                CapabilityStatus.AUTHENTICATION_REQUIRED,
                "CSFLOAT_API_KEY is not configured",
            )
        base_params = self._listing_params(query)
        collected: list[MarketplaceListing] = []
        drop_reasons: list[str] = []
        total_rows = 0
        pages = 0
        max_pages = -(-query.limit // _PAGE_LIMIT)
        cursor: str | None = None

        try:
            while len(collected) < query.limit and pages < max_pages:
                params = dict(base_params)
                params["limit"] = str(min(_PAGE_LIMIT, query.limit - len(collected)))
                if cursor:
                    params["cursor"] = cursor
                response = await self._client.get(
                    f"{self._base_url}/listings", params=params, headers=self._headers()
                )
                listings, page_drops, cursor = self._parse_listings_page(response, moment)
                collected.extend(listings)
                drop_reasons.extend(page_drops)
                total_rows += len(listings) + len(page_drops)
                pages += 1
                if not cursor or (not listings and not page_drops):
                    break
        except TransportError as exc:
            return self._failure("fetch_listings", moment, exc)

        if total_rows and not collected:
            return self._failure(
                "fetch_listings",
                moment,
                TransportSchemaError(
                    f"csfloat returned {total_rows} rows across {pages} page(s) and none "
                    f"parsed; first fault: {drop_reasons[0]}"
                ),
            )
        detail = f"{pages} page(s)"
        if drop_reasons:
            detail += (
                f"; {len(drop_reasons)}/{total_rows} rows dropped by strict parse; "
                f"first: {drop_reasons[0]}"
            )
        return CapabilityResult.succeeded(
            self.venue, "fetch_listings", moment, tuple(collected), detail=detail
        )

    def _parse_listings_page(
        self, response: HttpResponse, moment: datetime
    ) -> tuple[tuple[MarketplaceListing, ...], list[str], str | None]:
        """One page: parsed listings, per-row drop reasons, and the next cursor."""
        document = response.json()
        if isinstance(document, Mapping):
            rows = document.get("data")
            raw_cursor = document.get("cursor")
            next_cursor = raw_cursor if isinstance(raw_cursor, str) and raw_cursor else None
        else:
            rows = document
            next_cursor = None
        if not isinstance(rows, list):
            raise TransportSchemaError("csfloat listings response is not a list")
        listings: list[MarketplaceListing] = []
        drop_reasons: list[str] = []
        for row in rows:
            try:
                listings.append(self._parse_listing(row, moment, response.payload_sha256))
            except TransportSchemaError as exc:
                drop_reasons.append(str(exc))
        return tuple(listings), drop_reasons, next_cursor

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
