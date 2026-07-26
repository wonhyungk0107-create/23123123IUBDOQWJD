"""Opt-in, read-only live smoke checks.

Run with ``make live-smoke`` (``pytest -m live``). Every test here skips with a clear
reason when its credential is absent, so a run without keys is a clean skip rather
than a failure or — worse — a pass that proves nothing.

Hard rules for anything added to this file:

* **Read-only.** No purchase, no target creation, no trade acceptance, no state change
  of any kind at any venue.
* **Never manufacture a successful result.** If a source cannot be reached, the test
  fails or skips with the real error. It does not fall back to a fixture and report
  success.
* **Never assert on market content.** Prices and inventory change; asserting a
  specific listing exists produces a flaky test that teaches nothing. Assert on the
  *contract*: that the response parses, that failures are typed, that no code path
  changed venue state.

These are excluded from the default suite by the ``live`` marker, so the mandatory
gates never depend on the public internet.
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime

import pytest

from tradeup.adapters.base import ListingQuery
from tradeup.adapters.csfloat import CSFloatAdapter
from tradeup.adapters.http import HttpxTransport, RestClient, RetryPolicy
from tradeup.adapters.manual import SteamManualAdapter
from tradeup.config import Settings
from tradeup.domain.execution import CapabilityStatus, ExecutionMode
from tradeup.domain.items import Rarity
from tradeup.domain.listings import ListingIdentity

pytestmark = pytest.mark.live

RARITY_MAP = {
    "consumer grade": Rarity.CONSUMER,
    "industrial grade": Rarity.INDUSTRIAL,
    "mil-spec grade": Rarity.MIL_SPEC,
    "restricted": Rarity.RESTRICTED,
    "classified": Rarity.CLASSIFIED,
    "covert": Rarity.COVERT,
}


def _settings() -> Settings:
    return Settings()


def _requires(name: str, value: object) -> None:
    if not value:
        pytest.skip(f"{name} is not configured; skipping live check (this is not a failure)")


async def _csfloat_call(settings: Settings, coro_name: str, *args: object) -> object:
    import httpx

    async with httpx.AsyncClient() as client:
        rest = RestClient(
            transport=HttpxTransport(client, clock=lambda: datetime.now(UTC)),
            user_agent=settings.http_user_agent,
            timeout_seconds=float(settings.http_timeout_seconds),
            retry=RetryPolicy(max_attempts=settings.http_max_retries),
            secrets={
                "csfloat_api_key": (
                    settings.csfloat_api_key.get_secret_value() if settings.csfloat_api_key else ""
                )
            },
        )
        adapter = CSFloatAdapter(
            rest,
            api_key=(
                settings.csfloat_api_key.get_secret_value() if settings.csfloat_api_key else None
            ),
            rarity_by_name=RARITY_MAP,
        )
        method = getattr(adapter, coro_name)
        return await method(*args, moment=datetime.now(UTC))


class TestCSFloatReadOnly:
    def test_fetch_listings_parses_or_fails_with_a_typed_reason(self) -> None:
        settings = _settings()
        _requires("TRADEUP_CSFLOAT_API_KEY", settings.csfloat_api_key)

        result = asyncio.run(_csfloat_call(settings, "fetch_listings", ListingQuery(limit=5)))
        assert result.status in {  # type: ignore[attr-defined]
            CapabilityStatus.SUPPORTED_READ_ONLY,
            CapabilityStatus.RATE_LIMITED,
            CapabilityStatus.TEMPORARILY_UNAVAILABLE,
            CapabilityStatus.AUTHENTICATION_REQUIRED,
        }
        if result.ok:  # type: ignore[attr-defined]
            for listing in result.unwrap():  # type: ignore[attr-defined]
                # Provenance and range validity, not market content.
                assert len(listing.raw_payload_hash) == 64
                assert 0 <= listing.normalized_float <= 1
                assert listing.price.minor_units >= 0

    def test_revalidating_an_absent_listing_never_reports_it_active(self) -> None:
        settings = _settings()
        _requires("TRADEUP_CSFLOAT_API_KEY", settings.csfloat_api_key)

        result = asyncio.run(
            _csfloat_call(
                settings,
                "verify_listing",
                ListingIdentity("csfloat", "000000000000000000000000"),
            )
        )
        if result.ok:  # type: ignore[attr-defined]
            assert not result.unwrap().is_active  # type: ignore[attr-defined]


class TestExecutionBoundaryHoldsLive:
    """The boundary must hold with real credentials present, not only without them."""

    def test_csfloat_still_has_no_purchase_path(self) -> None:
        settings = _settings()
        adapter = CSFloatAdapter(
            RestClient(transport=None, user_agent="x"),  # type: ignore[arg-type]
            api_key=(
                settings.csfloat_api_key.get_secret_value() if settings.csfloat_api_key else None
            ),
        )
        assert adapter.execution_mode is ExecutionMode.UNSUPPORTED

    def test_steam_is_policy_blocked_regardless_of_configuration(self) -> None:
        result = asyncio.run(
            SteamManualAdapter().fetch_listings(ListingQuery(), moment=datetime.now(UTC))
        )
        assert result.status is CapabilityStatus.POLICY_BLOCKED

    def test_live_execution_is_off(self) -> None:
        settings = _settings()
        assert not settings.live_execution_enabled, (
            "live execution is enabled; live-smoke must never run against a "
            "configuration that could place an order"
        )
        assert settings.max_daily_spend.is_zero


def test_environment_reports_which_credentials_are_present() -> None:
    """Always runs. Documents what a live run could and could not exercise."""
    settings = _settings()
    available = settings.available_credentials()
    print("\nlive credential availability (values are never printed):")
    for name, present in sorted(available.items()):
        print(f"  {name:<26} {'present' if present else 'absent'}")
    if not any(available.values()):
        print("  -> no live source is reachable; every live check above skipped")
    # Presence is information, not a requirement.
    assert set(available) >= {"csfloat_api_key", "dmarket_public_key", "skinsnipe_api_key"}
    assert os.environ.get("TRADEUP_LIVE_EXECUTION_ENABLED", "false").lower() != "true"
