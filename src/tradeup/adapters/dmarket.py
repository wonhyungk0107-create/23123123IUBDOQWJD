"""DMarket adapter -- read-only market data, with the execution boundary in place.

Signing scheme, verified against DMarket's documentation (see docs/source-matrix.md):

* ``X-Api-Key``      -- the public key, lowercase hex
* ``X-Sign-Date``    -- unix seconds; the server rejects anything older than ~2 minutes
* ``X-Request-Sign`` -- ``"dmar ed25519 " + hex(Ed25519_sign(method + path_and_query +
  body + timestamp))``

Two hazards are handled explicitly rather than discovered later:

* **The signed path must be already percent-encoded.** The documented example signs
  ``%22``, not ``"``. Building the string from a decoded path produces a signature
  that verifies locally and fails remotely.
* **Clock skew is a correctness bug, not a nuisance.** The timestamp comes from the
  injected clock, so a skewed host fails loudly in tests rather than intermittently
  in production.

Execution: DMarket does document purchase and target-creation endpoints, so this is
the one venue whose ``execution_mode`` is ``AUTOMATED``. It is nonetheless gated by
``live_execution_enabled`` and returns ``SUPPORTED_EXECUTION_DISABLED`` during
groundwork -- deliberately distinguishable from CSFloat's ``UNSUPPORTED``, because
"we chose not to" and "no such API" are different facts.

**Unresolved before any live execution:** two official DMarket sources disagree on
the endpoint surface (Swagger says ``PATCH /exchange/v1/offers-buy`` and
``GET /account/v1/balance``; the dmarket-doc repository says
``POST /trading/v1/buy/offers`` and ``GET /account/v1/user/balance``). No purchase
call is implemented while that disagreement stands.

**Measured 2026-07-26:** the signing scheme is verified live (signed
``/trade-aggregator/v1/last-sales`` and ``/account/v1/user`` answer 200), but the
``/exchange/v1`` market reads this adapter parses are **retired at the venue** —
both answer 410 Gone naming ``/marketplace-api/v2/offers`` as the replacement,
whose schema differs and is not yet pinned (docs/source-matrix.md). Until that
migration, ``fetch_listings``/``verify_listing`` fail typed and loud with the
venue's 410 detail; they do not fall back to anything.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import quote

from nacl.signing import SigningKey

from tradeup.adapters.base import AccountState, ListingQuery, ListingVerification, MarketAdapter
from tradeup.adapters.http import (
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

__all__ = ["DMARKET_BASE_URL", "DMarketAdapter", "Ed25519Signer", "build_signature_payload"]

DMARKET_BASE_URL = "https://api.dmarket.com"
VENUE = "dmarket"

#: An Ed25519 seed is 32 bytes; DMarket's dashboard exports the secret as either
#: the bare seed (64 hex chars) or libsodium's expanded form seed||public
#: (128 hex chars). Anything else is a corrupted paste, not a key.
_SEED_BYTES = 32
_EXPANDED_BYTES = 64

_TRANSPORT_STATUS: Mapping[type[TransportError], CapabilityStatus] = {
    TransportAuthError: CapabilityStatus.AUTHENTICATION_REQUIRED,
    TransportRateLimited: CapabilityStatus.RATE_LIMITED,
    TransportTimeout: CapabilityStatus.TEMPORARILY_UNAVAILABLE,
    TransportSchemaError: CapabilityStatus.TEMPORARILY_UNAVAILABLE,
}


class Ed25519Signer:
    """Ed25519 request signer over libsodium (PyNaCl), for DMarket's scheme.

    ``sign`` returns the 64-byte detached signature; ``signed_headers`` hex-encodes
    it. Deterministic by construction (RFC 8032), so the same request signs to the
    same bytes — which is what makes signatures testable against published vectors.
    """

    def __init__(self, seed: bytes) -> None:
        if len(seed) != _SEED_BYTES:
            raise ValueError(f"Ed25519 seed must be {_SEED_BYTES} bytes, got {len(seed)}")
        self._key = SigningKey(seed)

    @classmethod
    def from_hex(cls, secret_key_hex: str) -> Ed25519Signer:
        """Build from DMarket's hex secret, failing closed on anything malformed.

        The 128-hex expanded form embeds the public key in its second half; when
        that half does not match the key the seed derives, the paste is corrupt and
        signing with it would produce a signature the venue rejects with an
        indistinguishable 401 — so it raises here instead.
        """
        try:
            raw = bytes.fromhex(secret_key_hex.strip())
        except ValueError as exc:
            raise ValueError("DMarket secret key is not valid hex") from exc
        if len(raw) == _SEED_BYTES:
            return cls(raw)
        if len(raw) == _EXPANDED_BYTES:
            signer = cls(raw[:_SEED_BYTES])
            derived_public = bytes(signer._key.verify_key)
            if raw[_SEED_BYTES:] != derived_public:
                raise ValueError(
                    "DMarket secret key is corrupt: its embedded public half does "
                    "not match the key its seed derives"
                )
            return signer
        raise ValueError(
            f"DMarket secret key must be {_SEED_BYTES} or {_EXPANDED_BYTES} bytes of "
            f"hex, got {len(raw)}"
        )

    @property
    def public_key_hex(self) -> str:
        """Lowercase hex of the derived public key — DMarket's ``X-Api-Key`` value."""
        return bytes(self._key.verify_key).hex()

    def sign(self, payload: bytes) -> bytes:
        return self._key.sign(payload).signature


def build_signature_payload(method: str, path_and_query: str, body: str, timestamp: int) -> str:
    """Exact string DMarket signs: ``method + path+query + body + timestamp``.

    ``path_and_query`` must already be percent-encoded. Kept as a free function so
    the encoding rule can be pinned by a test without constructing an adapter.
    """
    return f"{method.upper()}{path_and_query}{body}{timestamp}"


def encode_query(params: Mapping[str, str]) -> str:
    """Percent-encode a query string in a stable order.

    Sorted so the same logical request always produces the same signature, which is
    what makes signing reproducible in tests.
    """
    if not params:
        return ""
    parts = [f"{quote(k, safe='')}={quote(v, safe='')}" for k, v in sorted(params.items())]
    return "?" + "&".join(parts)


def _decimal(value: Any, field: str) -> Decimal:
    if value is None:
        raise TransportSchemaError(f"dmarket payload is missing {field}")
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise TransportSchemaError(f"dmarket {field} is not numeric: {value!r}") from exc


class DMarketAdapter(MarketAdapter):
    """Read-only DMarket client with a hard execution boundary."""

    venue = VENUE
    execution_mode = ExecutionMode.AUTOMATED
    capability_note = (
        "Read-only during groundwork. Purchase and target endpoints are documented "
        "but not implemented while two official sources disagree on the endpoint "
        "surface; no order or target is ever created."
    )

    def __init__(
        self,
        client: RestClient,
        *,
        public_key: str | None = None,
        signer: object | None = None,
        base_url: str = DMARKET_BASE_URL,
        live_execution_enabled: bool = False,
        rarity_by_name: Mapping[str, Rarity] | None = None,
    ) -> None:
        super().__init__(live_execution_enabled=live_execution_enabled)
        self._client = client
        self._public_key = public_key
        self._signer = signer
        self._base_url = base_url.rstrip("/")
        self._rarity_by_name = rarity_by_name or {}

    @property
    def has_credentials(self) -> bool:
        return bool(self._public_key) and self._signer is not None

    def signed_headers(
        self, method: str, path_and_query: str, body: str, timestamp: int
    ) -> dict[str, str]:
        """Build the three DMarket auth headers.

        Raises when no signer is configured rather than sending an unsigned request:
        an unsigned request would return a 401 that looks like a credential problem
        when it is actually a configuration bug.
        """
        if self._public_key is None or self._signer is None:
            raise TransportAuthError("DMarket public key and signer are required")
        payload = build_signature_payload(method, path_and_query, body, timestamp)
        sign = self._signer.sign(payload.encode("utf-8"))  # type: ignore[attr-defined]
        signature = sign.hex() if isinstance(sign, bytes) else str(sign)
        return {
            "X-Api-Key": self._public_key,
            "X-Sign-Date": str(timestamp),
            "X-Request-Sign": f"dmar ed25519 {signature}",
        }

    def _failure[T](
        self, operation: str, moment: datetime, exc: TransportError
    ) -> CapabilityResult[T]:
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

    # -- parsing -------------------------------------------------------------

    def _parse_listing(
        self, payload: Mapping[str, Any], observed_at: datetime, payload_hash: str
    ) -> MarketplaceListing:
        listing_id = payload.get("itemId") or payload.get("assetId")
        if not isinstance(listing_id, str) or not listing_id:
            raise TransportSchemaError("dmarket offer has no itemId")

        title = payload.get("title")
        if not isinstance(title, str) or not title:
            raise TransportSchemaError(f"dmarket offer {listing_id} has no title")

        extra = payload.get("extra")
        if not isinstance(extra, Mapping):
            raise TransportSchemaError(f"dmarket offer {listing_id} has no extra object")

        raw_float = _decimal(extra.get("floatValue"), f"offer {listing_id} floatValue")
        collection_id = extra.get("collection")
        if isinstance(collection_id, list) and collection_id:
            collection_id = collection_id[0]
        if not isinstance(collection_id, str) or not collection_id:
            raise TransportSchemaError(
                f"dmarket offer {listing_id} states no collection; refusing to guess"
            )

        rarity_name = extra.get("rarity")
        rarity = self._rarity_by_name.get(str(rarity_name).strip().lower()) if rarity_name else None
        if rarity is None:
            raise TransportSchemaError(
                f"dmarket offer {listing_id} has unmapped rarity {rarity_name!r}"
            )

        price_block = payload.get("price")
        if not isinstance(price_block, Mapping) or "USD" not in price_block:
            raise TransportSchemaError(f"dmarket offer {listing_id} has no USD price")
        try:
            price_minor = int(str(price_block["USD"]))
        except ValueError as exc:
            raise TransportSchemaError(
                f"dmarket offer {listing_id} price is not an integer minor-unit value"
            ) from exc

        quality = QualityType.NORMAL
        category = str(extra.get("category", "")).lower()
        if "stattrak" in category or extra.get("stattrak") is True:
            quality = QualityType.STATTRAK
        elif "souvenir" in category or extra.get("souvenir") is True:
            quality = QualityType.SOUVENIR

        # DMarket does not publish per-listing normalised float; it is derived from
        # the registry's float caps by the ingestion layer. Until then the raw value
        # stands in only when the caps are absent, which the ingester rejects.
        min_float = _decimal(extra.get("floatMin", "0"), f"offer {listing_id} floatMin")
        max_float = _decimal(extra.get("floatMax", "1"), f"offer {listing_id} floatMax")
        if max_float <= min_float:
            raise TransportSchemaError(f"dmarket offer {listing_id} has a degenerate float range")
        normalized = (raw_float - min_float) / (max_float - min_float)
        if not (Decimal(0) <= normalized <= Decimal(1)):
            raise TransportSchemaError(
                f"dmarket offer {listing_id} float {raw_float} is outside its stated range"
            )

        lock_status = TradableStatus.TRADABLE
        lock_until = None
        if extra.get("tradeLock") or extra.get("tradeLockDuration"):
            lock_status = TradableStatus.TRADE_LOCKED

        return MarketplaceListing(
            identity=ListingIdentity(VENUE, listing_id),
            asset_id=str(payload.get("assetId") or listing_id),
            skin_id=f"{extra.get('paintIndex', 'unknown')}@{collection_id}",
            market_hash_name=title,
            collection_id=collection_id,
            rarity=rarity,
            quality_type=quality,
            raw_float=raw_float,
            normalized_float=normalized,
            price=Money(price_minor, Currency.USD, BalanceType.CASH_WITHDRAWABLE),
            buyer_fee=Money.zero(Currency.USD, BalanceType.CASH_WITHDRAWABLE),
            deposit_fee=Money.zero(Currency.USD, BalanceType.CASH_WITHDRAWABLE),
            observed_at=observed_at,
            listing_status=ListingStatus.ACTIVE,
            tradable_status=lock_status,
            raw_payload_hash=payload_hash,
            trade_lock_until=lock_until,
        )

    # -- operations ----------------------------------------------------------

    async def fetch_listings(
        self, query: ListingQuery, *, moment: datetime
    ) -> CapabilityResult[Sequence[MarketplaceListing]]:
        if not self.has_credentials:
            return self._refuse(
                "fetch_listings",
                moment,
                CapabilityStatus.AUTHENTICATION_REQUIRED,
                "DMARKET_PUBLIC_KEY and DMARKET_SECRET_KEY are not configured",
            )
        # Both official sources still document /exchange/v1/market/items, but it
        # answered 410 Gone on 2026-07-26 (docs/source-matrix.md). Exact-name
        # queries — the only shape the pipeline needs — go through the equally
        # documented /exchange/v1/offers-by-title, which answers.
        if query.market_hash_name:
            path = "/exchange/v1/offers-by-title"
            params = {"Title": query.market_hash_name, "Limit": str(query.limit)}
        else:
            path = "/exchange/v1/market/items"
            params = {"gameId": "a8db", "limit": str(query.limit), "currency": "USD"}
        query_string = encode_query(params)
        timestamp = int(moment.timestamp())
        try:
            headers = self.signed_headers("GET", path + query_string, "", timestamp)
            # The query is embedded in the URL so the bytes sent are exactly the
            # bytes signed; letting the HTTP layer re-encode params can diverge
            # from encode_query and invalidate the signature remotely.
            response = await self._client.get(
                f"{self._base_url}{path}{query_string}", headers=headers
            )
            document = response.json()
            rows = document.get("objects") if isinstance(document, Mapping) else None
            if not isinstance(rows, list):
                raise TransportSchemaError("dmarket market response has no 'objects' list")
            listings = tuple(
                self._parse_listing(row, moment, response.payload_sha256) for row in rows
            )
        except TransportError as exc:
            return self._failure("fetch_listings", moment, exc)
        return CapabilityResult.succeeded(self.venue, "fetch_listings", moment, listings)

    async def verify_listing(
        self, identity: ListingIdentity, *, moment: datetime
    ) -> CapabilityResult[ListingVerification]:
        if not self.has_credentials:
            return self._refuse(
                "verify_listing",
                moment,
                CapabilityStatus.AUTHENTICATION_REQUIRED,
                "DMarket credentials are not configured",
            )
        params = {"gameId": "a8db", "itemId": identity.listing_id, "currency": "USD"}
        path = "/exchange/v1/market/items"
        query_string = encode_query(params)
        timestamp = int(moment.timestamp())
        try:
            headers = self.signed_headers("GET", path + query_string, "", timestamp)
            response = await self._client.get(
                f"{self._base_url}{path}{query_string}", headers=headers
            )
            document = response.json()
            rows = document.get("objects") if isinstance(document, Mapping) else []
            if not rows:
                return CapabilityResult.succeeded(
                    self.venue,
                    "verify_listing",
                    moment,
                    ListingVerification(
                        identity=identity,
                        status=ListingStatus.WITHDRAWN,
                        verified_at=moment,
                        detail="offer no longer present in market results",
                    ),
                )
            listing = self._parse_listing(rows[0], moment, response.payload_sha256)
        except TransportError as exc:
            return self._failure("verify_listing", moment, exc)
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
                detail="revalidated via market items query",
            ),
        )

    async def fetch_account_state(self, *, moment: datetime) -> CapabilityResult[AccountState]:
        return self._refuse(
            "fetch_account_state",
            moment,
            CapabilityStatus.TEMPORARILY_UNAVAILABLE,
            "balance endpoint path is disputed between DMarket's Swagger "
            "(/account/v1/balance) and dmarket-doc (/account/v1/user/balance); "
            "resolve empirically before relying on a balance figure",
        )


def sha256_body(body: str) -> str:
    """Hash a request body for evidence archival."""
    return hashlib.sha256(body.encode("utf-8")).hexdigest()
