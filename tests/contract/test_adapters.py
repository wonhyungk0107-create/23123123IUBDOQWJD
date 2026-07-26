"""Adapter contract tests.

The CSFloat fixture shape was verified against a live authenticated read on
2026-07-26 (see docs/source-matrix.md): rows carry ``type``/``state`` and an item
with ``item_name``/``paint_index`` but no collection or float caps, which the
registry supplies. The DMarket payloads remain hand-authored approximations of the
documented shape — a live smoke run is still required before trusting them.

The behaviour under test is uniformly "fail closed". Every malformed, partial or
surprising payload must produce a typed refusal, never a listing with a plausible
default in place of a field we did not actually receive.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from tradeup.adapters.base import ListingQuery
from tradeup.adapters.csfloat import CSFLOAT_BASE_URL, CSFloatAdapter
from tradeup.adapters.dmarket import DMarketAdapter, build_signature_payload, encode_query
from tradeup.adapters.http import (
    FixtureTransport,
    HttpResponse,
    RestClient,
    RetryPolicy,
    TransportTimeout,
    redact,
)
from tradeup.adapters.manual import (
    CSMoneyManualAdapter,
    SkinSwapManualAdapter,
    SteamManualAdapter,
    skins_money_adapter,
)
from tradeup.domain.execution import CapabilityStatus, ExecutionMode
from tradeup.domain.items import Collection, FloatRange, QualityType, Rarity, Skin
from tradeup.domain.listings import ListingIdentity
from tradeup.metadata.registry import MetadataRegistry

NOW = datetime(2026, 7, 25, 12, 0, 0, tzinfo=UTC)
LISTINGS_URL = f"{CSFLOAT_BASE_URL}/listings"

RARITY_MAP: Mapping[str, Rarity] = {
    "mil-spec grade": Rarity.MIL_SPEC,
    "restricted": Rarity.RESTRICTED,
}


def make_registry() -> MetadataRegistry:
    """A one-skin registry matching the fixture listing's identity facts."""
    skin = Skin(
        skin_id="skin-redline@collection-set-huntsman",
        name="AK-47 | Redline",
        market_hash_base="AK-47 | Redline",
        collection_id="collection-set-huntsman",
        rarity=Rarity.MIL_SPEC,
        float_range=FloatRange(Decimal("0.1"), Decimal("0.7")),
        paint_index=282,
        available_qualities=frozenset({QualityType.NORMAL, QualityType.STATTRAK}),
    )
    return MetadataRegistry(
        source="test",
        revision="test-rev",
        payload_sha256="0" * 64,
        imported_at=NOW,
        skins=[skin],
        collections=[
            Collection(
                collection_id="collection-set-huntsman",
                name="The Huntsman Collection",
                skin_ids=frozenset({skin.skin_id}),
            )
        ],
    )


def response(
    payload: Any,
    *,
    status: int = 200,
    url: str = LISTINGS_URL,
    headers: dict[str, str] | None = None,
) -> HttpResponse:
    body = json.dumps(payload).encode("utf-8") if not isinstance(payload, bytes) else payload
    return HttpResponse(
        status_code=status,
        url=url,
        headers=headers or {},
        content=body,
        received_at=NOW,
        elapsed_seconds=0.01,
    )


def good_listing(listing_id: str = "L-1") -> dict[str, Any]:
    """A payload shaped like the live listing object (authenticated read, 2026-07-26).

    Live rows carry no ``min_float``, ``max_float`` or ``collection``; identity is
    ``item_name`` + ``paint_index``, and the registry supplies the rest.
    """
    return {
        "id": listing_id,
        "price": 1234,
        "type": "buy_now",
        "state": "listed",
        "item": {
            "asset_id": f"asset-{listing_id}",
            "market_hash_name": "AK-47 | Redline (Field-Tested)",
            "item_name": "AK-47 | Redline",
            "float_value": 0.2345,
            "paint_index": 282,
            "def_index": 7,
            "rarity": 4,
            "rarity_name": "Mil-Spec Grade",
            "is_stattrak": False,
            "is_souvenir": False,
            "type": "skin",
            "paint_seed": 501,
        },
    }


def csfloat_adapter(
    responses: dict[str, HttpResponse],
    *,
    api_key: str | None = "test-key",
    sleeper_calls: list[float] | None = None,
    registry: MetadataRegistry | None = None,
) -> CSFloatAdapter:
    async def _sleep(seconds: float) -> None:
        if sleeper_calls is not None:
            sleeper_calls.append(seconds)

    client = RestClient(
        transport=FixtureTransport(responses),
        user_agent="test-agent",
        retry=RetryPolicy(max_attempts=2, base_delay_seconds=0.0, jitter_ratio=0.0),
        sleeper=_sleep,
        random_fn=lambda: 0.5,
        secrets={"api_key": api_key} if api_key else {},
    )
    return CSFloatAdapter(
        client,
        api_key=api_key,
        rarity_by_name=RARITY_MAP,
        registry=registry if registry is not None else make_registry(),
    )


def fetch(adapter: CSFloatAdapter) -> Any:
    return asyncio.run(adapter.fetch_listings(ListingQuery(limit=10), moment=NOW))


class TestCSFloatSuccess:
    def test_parses_a_recorded_success_payload(self) -> None:
        adapter = csfloat_adapter(
            {FixtureTransport.key("GET", LISTINGS_URL): response({"data": [good_listing()]})}
        )
        result = fetch(adapter)
        assert result.ok
        listing = result.unwrap()[0]
        assert listing.identity == ListingIdentity("csfloat", "L-1")
        assert listing.raw_float == Decimal("0.2345")
        # (0.2345 - 0.1) / (0.7 - 0.1) = 0.1345 / 0.6
        assert listing.normalized_float == Decimal("0.1345") / Decimal("0.6")
        assert listing.price.minor_units == 1234
        assert listing.rarity is Rarity.MIL_SPEC

    def test_payload_hash_is_recorded_as_evidence(self) -> None:
        adapter = csfloat_adapter(
            {FixtureTransport.key("GET", LISTINGS_URL): response({"data": [good_listing()]})}
        )
        listing = fetch(adapter).unwrap()[0]
        assert len(listing.raw_payload_hash) == 64

    def test_empty_payload_is_success_with_no_listings(self) -> None:
        """No listings is a legitimate answer, not a failure."""
        adapter = csfloat_adapter(
            {FixtureTransport.key("GET", LISTINGS_URL): response({"data": []})}
        )
        result = fetch(adapter)
        assert result.ok
        assert result.unwrap() == ()

    def test_json_numbers_are_parsed_as_decimal_not_float(self) -> None:
        adapter = csfloat_adapter(
            {FixtureTransport.key("GET", LISTINGS_URL): response({"data": [good_listing()]})}
        )
        listing = fetch(adapter).unwrap()[0]
        assert isinstance(listing.raw_float, Decimal)

    def test_a_partially_parseable_page_keeps_good_rows_and_counts_drops(self) -> None:
        """Observed live 2026-07-26: pages mix buy-now and auction rows. A row the
        strict parse excludes is counted in the detail — never defaulted — and the
        good rows still reach the scanner."""
        auction = good_listing("L-auction")
        auction["type"] = "auction"
        adapter = csfloat_adapter(
            {
                FixtureTransport.key("GET", LISTINGS_URL): response(
                    {"data": [good_listing(), auction]}
                )
            }
        )
        result = fetch(adapter)
        assert result.ok
        listings = result.unwrap()
        assert [entry.identity.listing_id for entry in listings] == ["L-1"]
        assert "1/2 rows dropped" in result.detail
        assert "bid, not an ask" in result.detail

    def test_identity_resolves_through_the_registry(self) -> None:
        """Collection and float caps come from the registry, not the payload."""
        adapter = csfloat_adapter(
            {FixtureTransport.key("GET", LISTINGS_URL): response({"data": [good_listing()]})}
        )
        listing = fetch(adapter).unwrap()[0]
        assert listing.skin_id == "skin-redline@collection-set-huntsman"
        assert listing.collection_id == "collection-set-huntsman"

    def test_a_stattrak_prefixed_name_resolves_to_the_base_skin(self) -> None:
        """CSFloat bakes the quality prefix into the name; the registry does not."""
        payload = good_listing()
        payload["item"]["item_name"] = "StatTrak™ AK-47 | Redline"
        payload["item"]["market_hash_name"] = "StatTrak™ AK-47 | Redline (Field-Tested)"
        payload["item"]["is_stattrak"] = True
        adapter = csfloat_adapter(
            {FixtureTransport.key("GET", LISTINGS_URL): response({"data": [payload]})}
        )
        listing = fetch(adapter).unwrap()[0]
        assert listing.skin_id == "skin-redline@collection-set-huntsman"
        assert listing.quality_type is QualityType.STATTRAK


class _SequencedTransport:
    """Returns queued responses in order, recording each request's params.

    FixtureTransport keys by URL alone, which cannot distinguish cursor pages.
    """

    def __init__(self, responses: list[HttpResponse]) -> None:
        self._responses = list(responses)
        self.params: list[dict[str, str]] = []

    async def send(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        timeout_seconds: float = 15.0,
    ) -> HttpResponse:
        self.params.append(dict(params or {}))
        return self._responses.pop(0)


def sequenced_adapter(responses: list[HttpResponse]) -> tuple[CSFloatAdapter, _SequencedTransport]:
    transport = _SequencedTransport(responses)
    client = RestClient(
        transport=transport,
        user_agent="test-agent",
        retry=RetryPolicy(max_attempts=1),
    )
    adapter = CSFloatAdapter(
        client, api_key="test-key", rarity_by_name=RARITY_MAP, registry=make_registry()
    )
    return adapter, transport


class TestCSFloatQueryAndPagination:
    def test_documented_filters_are_sent(self) -> None:
        from tradeup.domain.money import Currency, Money

        adapter, transport = sequenced_adapter([response({"data": [good_listing()]})])
        asyncio.run(
            adapter.fetch_listings(
                ListingQuery(
                    rarity=Rarity.MIL_SPEC,
                    quality=QualityType.NORMAL,
                    max_price=Money(50_000, Currency.USD),
                    min_price=Money(500, Currency.USD),
                    limit=10,
                ),
                moment=NOW,
            )
        )
        sent = transport.params[0]
        assert sent["type"] == "buy_now"
        assert sent["sort_by"] == "lowest_price"
        assert sent["rarity"] == "3"
        assert sent["category"] == "1"
        assert sent["max_price"] == "50000"
        assert sent["min_price"] == "500"
        assert sent["limit"] == "10"

    def test_cursor_pagination_follows_documented_pages(self) -> None:
        adapter, transport = sequenced_adapter(
            [
                response({"data": [good_listing("L-1")], "cursor": "page-2"}),
                response({"data": [good_listing("L-2")], "cursor": ""}),
            ]
        )
        result = asyncio.run(adapter.fetch_listings(ListingQuery(limit=100), moment=NOW))
        assert result.ok
        assert [entry.identity.listing_id for entry in result.unwrap()] == ["L-1", "L-2"]
        assert "cursor" not in transport.params[0]
        assert transport.params[1]["cursor"] == "page-2"
        assert "2 page(s)" in result.detail

    def test_pagination_stops_at_the_requested_limit(self) -> None:
        adapter, transport = sequenced_adapter(
            [response({"data": [good_listing("L-1")], "cursor": "page-2"})]
        )
        result = asyncio.run(adapter.fetch_listings(ListingQuery(limit=1), moment=NOW))
        assert result.ok
        assert len(result.unwrap()) == 1
        assert len(transport.params) == 1  # the second page was never requested


class TestCSFloatFailsClosed:
    @pytest.mark.parametrize(
        "missing",
        ["asset_id", "market_hash_name", "item_name", "float_value", "rarity_name"],
    )
    def test_missing_required_item_field_refuses(self, missing: str) -> None:
        payload = good_listing()
        del payload["item"][missing]
        adapter = csfloat_adapter(
            {FixtureTransport.key("GET", LISTINGS_URL): response({"data": [payload]})}
        )
        result = fetch(adapter)
        assert not result.ok
        assert result.status is CapabilityStatus.TEMPORARILY_UNAVAILABLE

    def test_missing_price_refuses(self) -> None:
        payload = good_listing()
        del payload["price"]
        adapter = csfloat_adapter(
            {FixtureTransport.key("GET", LISTINGS_URL): response({"data": [payload]})}
        )
        assert not fetch(adapter).ok

    def test_float_outside_the_registry_range_refuses(self) -> None:
        """An impossible float is a data fault, not a bargain."""
        payload = good_listing()
        payload["item"]["float_value"] = 0.99
        adapter = csfloat_adapter(
            {FixtureTransport.key("GET", LISTINGS_URL): response({"data": [payload]})}
        )
        result = fetch(adapter)
        assert not result.ok
        assert "outside the registry's stated range" in result.detail

    def test_an_unknown_item_refuses_rather_than_guessing(self) -> None:
        payload = good_listing()
        payload["item"]["item_name"] = "Weapon | Not In The Registry"
        adapter = csfloat_adapter(
            {FixtureTransport.key("GET", LISTINGS_URL): response({"data": [payload]})}
        )
        result = fetch(adapter)
        assert not result.ok
        assert "refusing to guess" in result.detail

    def test_a_paint_index_mismatch_refuses(self) -> None:
        """Name and paint index must agree; disagreement means wrong identity."""
        payload = good_listing()
        payload["item"]["paint_index"] = 999
        adapter = csfloat_adapter(
            {FixtureTransport.key("GET", LISTINGS_URL): response({"data": [payload]})}
        )
        result = fetch(adapter)
        assert not result.ok
        assert "refusing to guess" in result.detail

    def test_an_auction_only_page_refuses(self) -> None:
        """A page with nothing purchasable at its stated price is a refusal, not
        an empty success: the market state is unknown, not quiet."""
        auction = good_listing()
        auction["type"] = "auction"
        adapter = csfloat_adapter(
            {FixtureTransport.key("GET", LISTINGS_URL): response({"data": [auction]})}
        )
        result = fetch(adapter)
        assert not result.ok
        assert "none parsed" in result.detail

    def test_a_missing_registry_refuses(self) -> None:
        """Without the registry there is no identity authority; nothing may parse."""
        client_responses = {
            FixtureTransport.key("GET", LISTINGS_URL): response({"data": [good_listing()]})
        }
        client = RestClient(
            transport=FixtureTransport(client_responses),
            user_agent="test-agent",
            retry=RetryPolicy(max_attempts=1),
        )
        adapter = CSFloatAdapter(client, api_key="k", rarity_by_name=RARITY_MAP, registry=None)
        result = fetch(adapter)
        assert not result.ok
        assert "no metadata registry" in result.detail

    def test_unmapped_rarity_refuses_rather_than_guessing(self) -> None:
        payload = good_listing()
        payload["item"]["rarity_name"] = "Something New"
        adapter = csfloat_adapter(
            {FixtureTransport.key("GET", LISTINGS_URL): response({"data": [payload]})}
        )
        result = fetch(adapter)
        assert not result.ok
        assert "unmapped rarity" in result.detail

    def test_schema_change_refuses(self) -> None:
        """The response shape changed entirely; we must not improvise."""
        adapter = csfloat_adapter(
            {FixtureTransport.key("GET", LISTINGS_URL): response({"results": {"weird": True}})}
        )
        assert not fetch(adapter).ok

    def test_non_json_body_refuses(self) -> None:
        adapter = csfloat_adapter(
            {FixtureTransport.key("GET", LISTINGS_URL): response(b"<html>maintenance</html>")}
        )
        assert not fetch(adapter).ok


class TestCSFloatTransportFailures:
    def test_missing_credentials_is_authentication_required(self) -> None:
        adapter = csfloat_adapter({}, api_key=None)
        result = fetch(adapter)
        assert result.status is CapabilityStatus.AUTHENTICATION_REQUIRED
        assert "not configured" in result.detail

    def test_401_is_authentication_required(self) -> None:
        adapter = csfloat_adapter(
            {FixtureTransport.key("GET", LISTINGS_URL): response({}, status=401)}
        )
        assert fetch(adapter).status is CapabilityStatus.AUTHENTICATION_REQUIRED

    def test_429_is_rate_limited_and_carries_retry_after(self) -> None:
        calls: list[float] = []
        adapter = csfloat_adapter(
            {
                FixtureTransport.key("GET", LISTINGS_URL): response(
                    {}, status=429, headers={"retry-after": "7"}
                )
            },
            sleeper_calls=calls,
        )
        result = fetch(adapter)
        assert result.status is CapabilityStatus.RATE_LIMITED
        assert result.retry_after_seconds == 7
        # The venue's own guidance is obeyed rather than our backoff curve.
        assert calls == [7.0]

    def test_500_retries_then_reports_unavailable(self) -> None:
        calls: list[float] = []
        adapter = csfloat_adapter(
            {FixtureTransport.key("GET", LISTINGS_URL): response({}, status=500)},
            sleeper_calls=calls,
        )
        result = fetch(adapter)
        assert result.status is CapabilityStatus.TEMPORARILY_UNAVAILABLE
        assert len(calls) == 1  # max_attempts=2 means one sleep between two tries

    def test_timeout_is_temporarily_unavailable(self) -> None:
        class TimingOutTransport:
            async def send(self, *args: object, **kwargs: object) -> HttpResponse:
                raise TransportTimeout("timed out")

        client = RestClient(
            transport=TimingOutTransport(),  # type: ignore[arg-type]
            user_agent="test",
            retry=RetryPolicy(max_attempts=1),
        )
        adapter = CSFloatAdapter(client, api_key="k", rarity_by_name=RARITY_MAP)
        assert fetch(adapter).status is CapabilityStatus.TEMPORARILY_UNAVAILABLE


class TestCSFloatRevalidation:
    def test_a_present_listing_verifies_active(self) -> None:
        url = f"{CSFLOAT_BASE_URL}/listings/L-1"
        adapter = csfloat_adapter(
            {FixtureTransport.key("GET", url): response(good_listing(), url=url)}
        )
        result = asyncio.run(adapter.verify_listing(ListingIdentity("csfloat", "L-1"), moment=NOW))
        assert result.ok
        assert result.unwrap().is_active

    def test_an_absent_listing_verifies_unknown_not_active(self) -> None:
        """We could not confirm it. UNKNOWN is never treated as purchasable."""
        adapter = csfloat_adapter({})
        result = asyncio.run(adapter.verify_listing(ListingIdentity("csfloat", "gone"), moment=NOW))
        assert result.ok
        assert not result.unwrap().is_active


class TestCSFloatExecutionBoundary:
    def test_csfloat_has_no_purchase_path_at_all(self) -> None:
        """UNSUPPORTED, not merely disabled: no documented buy endpoint exists."""
        adapter = csfloat_adapter({})
        assert adapter.execution_mode is ExecutionMode.UNSUPPORTED

    def test_purchase_intent_is_refused_as_unsupported(self) -> None:
        from datetime import timedelta

        from tradeup.domain.execution import PurchaseIntent
        from tradeup.domain.money import Currency, Money

        adapter = csfloat_adapter({})
        intent = PurchaseIntent(
            intent_id="i-1",
            candidate_id="TU-1",
            identity=ListingIdentity("csfloat", "L-1"),
            max_price=Money(1000, Currency.USD),
            execution_mode=ExecutionMode.UNSUPPORTED,
            created_at=NOW,
            expires_at=NOW + timedelta(minutes=5),
            sequence_position=0,
        )
        result = asyncio.run(adapter.create_purchase_intent(intent, moment=NOW))
        assert result.status is CapabilityStatus.UNSUPPORTED
        assert "no documented purchase API" in result.detail


class TestDMarketSigning:
    def test_signature_payload_is_method_path_body_timestamp(self) -> None:
        assert build_signature_payload("get", "/x?y=1", "", 1700) == "GET/x?y=11700"

    def test_query_is_percent_encoded_and_sorted(self) -> None:
        """The signed path must be pre-encoded; signing a decoded path fails remotely."""
        encoded = encode_query({"title": 'AK "Redline"', "gameId": "a8db"})
        assert encoded.startswith("?gameId=a8db&title=")
        assert "%22" in encoded
        assert '"' not in encoded

    def test_missing_signer_refuses_rather_than_sending_unsigned(self) -> None:
        client = RestClient(transport=FixtureTransport({}), user_agent="t")
        adapter = DMarketAdapter(client, public_key=None, signer=None)
        result = asyncio.run(adapter.fetch_listings(ListingQuery(), moment=NOW))
        assert result.status is CapabilityStatus.AUTHENTICATION_REQUIRED

    def test_execution_mode_is_automated_but_gated_off(self) -> None:
        client = RestClient(transport=FixtureTransport({}), user_agent="t")
        adapter = DMarketAdapter(client, live_execution_enabled=False)
        assert adapter.execution_mode is ExecutionMode.AUTOMATED
        result = asyncio.run(
            adapter.create_purchase_intent(None, moment=NOW)  # type: ignore[arg-type]
        )
        assert result.status is CapabilityStatus.SUPPORTED_EXECUTION_DISABLED

    def test_balance_endpoint_refuses_while_sources_disagree(self) -> None:
        client = RestClient(transport=FixtureTransport({}), user_agent="t")
        adapter = DMarketAdapter(client, public_key="k", signer=object())
        result = asyncio.run(adapter.fetch_account_state(moment=NOW))
        assert not result.ok
        assert "disputed" in result.detail


class TestManualAndBlockedVenues:
    def test_manual_venues_require_an_operator(self) -> None:
        for adapter in (CSMoneyManualAdapter(), SkinSwapManualAdapter()):
            result = asyncio.run(adapter.fetch_listings(ListingQuery(), moment=NOW))
            assert result.status is CapabilityStatus.OPERATOR_ACTION_REQUIRED
            assert adapter.browse_url.startswith("https://")

    def test_steam_is_policy_blocked_on_every_operation(self) -> None:
        adapter = SteamManualAdapter()
        for call in (
            adapter.fetch_listings(ListingQuery(), moment=NOW),
            adapter.verify_listing(ListingIdentity("steam", "x"), moment=NOW),
            adapter.fetch_account_state(moment=NOW),
        ):
            assert asyncio.run(call).status is CapabilityStatus.POLICY_BLOCKED

    def test_steam_refusal_cites_the_subscriber_agreement(self) -> None:
        result = asyncio.run(SteamManualAdapter().fetch_listings(ListingQuery(), moment=NOW))
        assert "4.C" in result.detail

    def test_unidentified_service_stays_disabled(self) -> None:
        adapter = skins_money_adapter()
        result = asyncio.run(adapter.fetch_listings(ListingQuery(), moment=NOW))
        assert result.status is CapabilityStatus.POLICY_BLOCKED
        assert "not identified" in result.detail


class TestRedaction:
    def test_secrets_are_replaced_in_messages(self) -> None:
        message = "request failed with key sk-supersecret"
        assert redact(message, {"api_key": "sk-supersecret"}) == (
            "request failed with key <redacted:api_key>"
        )

    def test_redaction_without_secrets_is_a_no_op(self) -> None:
        assert redact("plain message", None) == "plain message"


class TestRetryPolicy:
    def test_backoff_grows_exponentially(self) -> None:
        policy = RetryPolicy(base_delay_seconds=1.0, jitter_ratio=0.0, max_delay_seconds=100)
        assert policy.delay_for(1, 0.5) == 1.0
        assert policy.delay_for(2, 0.5) == 2.0
        assert policy.delay_for(3, 0.5) == 4.0

    def test_backoff_is_capped(self) -> None:
        policy = RetryPolicy(base_delay_seconds=1.0, jitter_ratio=0.0, max_delay_seconds=3)
        assert policy.delay_for(10, 0.5) == 3.0

    def test_jitter_is_deterministic_given_the_random_value(self) -> None:
        policy = RetryPolicy(base_delay_seconds=1.0, jitter_ratio=0.5)
        assert policy.delay_for(1, 0.0) == 0.5
        assert policy.delay_for(1, 1.0) == 1.5

    def test_delay_never_goes_negative(self) -> None:
        policy = RetryPolicy(base_delay_seconds=1.0, jitter_ratio=1.0)
        assert policy.delay_for(1, 0.0) >= 0.0

    def test_invalid_policies_rejected(self) -> None:
        with pytest.raises(ValueError):
            RetryPolicy(max_attempts=0)
        with pytest.raises(ValueError):
            RetryPolicy(jitter_ratio=2.0)


def test_fixture_transport_refuses_unregistered_requests() -> None:
    """A test must not pass by exercising a call it never actually made."""
    transport = FixtureTransport({})
    with pytest.raises(Exception, match="no fixture registered"):
        asyncio.run(transport.send("GET", "https://example.invalid/x"))
