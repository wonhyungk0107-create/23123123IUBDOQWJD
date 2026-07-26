"""Skinport price-source contract tests.

Fixture payloads follow the documented response shape of GET /v1/sales/history
(docs.skinport.com/sales/history). All mandatory tests run offline.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import Any

from tradeup.adapters.http import FixtureTransport, HttpResponse, RestClient, RetryPolicy
from tradeup.adapters.skinport import SKINPORT_BASE_URL, SkinportPriceSource, WantedName
from tradeup.domain.execution import CapabilityStatus
from tradeup.domain.items import QualityType, WearCondition
from tradeup.domain.valuation import ValuationSource

NOW = datetime(2026, 7, 25, 12, 0, 0, tzinfo=UTC)
HISTORY_URL = f"{SKINPORT_BASE_URL}/sales/history"


def response(payload: Any, *, status: int = 200) -> HttpResponse:
    return HttpResponse(
        status_code=status,
        url=HISTORY_URL,
        headers={},
        content=json.dumps(payload).encode("utf-8"),
        received_at=NOW,
        elapsed_seconds=0.01,
    )


def sales_row(
    name: str,
    *,
    median_7d: float | None = 21.5,
    volume_7d: int = 12,
    median_30d: float | None = 20.0,
    volume_30d: int = 40,
) -> dict[str, Any]:
    return {
        "market_hash_name": name,
        "currency": "USD",
        "item_page": "https://skinport.com/item/x",
        "market_page": "https://skinport.com/market?item=x",
        "last_24_hours": {"min": None, "max": None, "avg": None, "median": None, "volume": 0},
        "last_7_days": {
            "min": 20.0,
            "max": 25.0,
            "avg": 22.0,
            "median": median_7d,
            "volume": volume_7d,
        },
        "last_30_days": {
            "min": 18.0,
            "max": 26.0,
            "avg": 21.0,
            "median": median_30d,
            "volume": volume_30d,
        },
        "last_90_days": {"min": 15.0, "max": 30.0, "avg": 21.0, "median": 20.5, "volume": 120},
    }


def source(
    responses: dict[str, HttpResponse] | None = None,
    transport: FixtureTransport | None = None,
) -> tuple[SkinportPriceSource, FixtureTransport]:
    fixture = transport or FixtureTransport(responses or {})
    client = RestClient(
        transport=fixture,
        user_agent="test-agent",
        retry=RetryPolicy(max_attempts=2, base_delay_seconds=0.0, jitter_ratio=0.0),
        sleeper=lambda _: asyncio.sleep(0),
    )
    return SkinportPriceSource(client), fixture


def wanted(*names: str) -> dict[str, WantedName]:
    return {
        name: (f"skin-{index}", QualityType.NORMAL, WearCondition.FIELD_TESTED)
        for index, name in enumerate(names)
    }


def fetch(src: SkinportPriceSource, wanted_names: dict[str, WantedName]) -> Any:
    return asyncio.run(src.fetch_sales_observations(wanted_names, moment=NOW))


class TestSuccess:
    def test_parses_completed_sale_observations_per_window(self) -> None:
        src, _ = source(
            {FixtureTransport.key("GET", HISTORY_URL): response([sales_row("AK | X (FT)")])}
        )
        result = fetch(src, wanted("AK | X (FT)"))
        assert result.ok
        observations = result.unwrap()
        # One observation per window with sales: 7d and 30d.
        assert len(observations) == 2
        seven_day = observations[0]
        assert seven_day.venue == "skinport"
        assert seven_day.source is ValuationSource.COMPLETED_SALE
        assert seven_day.skin_id == "skin-0"
        assert seven_day.gross.minor_units == 2150  # 21.5 USD, exact
        assert seven_day.evidence_count == 12
        assert observations[1].gross.minor_units == 2000
        assert "1/1 wanted names priced" in result.detail

    def test_unrequested_rows_are_ignored(self) -> None:
        src, _ = source(
            {
                FixtureTransport.key("GET", HISTORY_URL): response(
                    [sales_row("AK | X (FT)"), sales_row("Item | Unasked (FN)")]
                )
            }
        )
        result = fetch(src, wanted("AK | X (FT)"))
        assert result.ok
        assert {o.skin_id for o in result.unwrap()} == {"skin-0"}

    def test_windows_without_sales_are_skipped_and_counted(self) -> None:
        row = sales_row("AK | X (FT)", median_7d=None, volume_7d=0, median_30d=None, volume_30d=0)
        src, _ = source({FixtureTransport.key("GET", HISTORY_URL): response([row])})
        result = fetch(src, wanted("AK | X (FT)"))
        assert result.ok
        assert result.unwrap() == ()
        assert "0/1 wanted names priced" in result.detail
        assert "1 rows without usable sales" in result.detail

    def test_batches_by_fifty_names_per_request(self) -> None:
        names = [f"Item | N{i} (FT)" for i in range(60)]
        src, fixture = source(
            {FixtureTransport.key("GET", HISTORY_URL): response([sales_row(names[0])])}
        )
        result = fetch(src, wanted(*names))
        assert result.ok
        assert len(fixture.calls) == 2

    def test_names_beyond_the_request_budget_are_reported(self) -> None:
        names = [f"Item | N{i:03d} (FT)" for i in range(7 * 50 + 25)]
        src, fixture = source(
            {FixtureTransport.key("GET", HISTORY_URL): response([sales_row(names[0])])}
        )
        result = fetch(src, wanted(*names))
        assert result.ok
        assert len(fixture.calls) == 7
        assert "25 names beyond the 7-request budget" in result.detail


class TestItemQuotes:
    def test_parses_the_whole_catalogue_in_one_request(self) -> None:
        rows = [
            {
                "market_hash_name": "AK | X (FT)",
                "currency": "USD",
                "min_price": 12.34,
                "median_price": 15.0,
                "quantity": 7,
            },
            {
                "market_hash_name": "AK | Y (FN)",
                "currency": "USD",
                "min_price": None,
                "median_price": None,
                "quantity": 0,
            },
        ]
        src, fixture = source(
            {FixtureTransport.key("GET", f"{SKINPORT_BASE_URL}/items"): response(rows)}
        )
        result = asyncio.run(src.fetch_item_quotes(moment=NOW))
        assert result.ok
        quotes = result.unwrap()
        assert len(fixture.calls) == 1
        assert quotes["AK | X (FT)"].min_price is not None
        assert quotes["AK | X (FT)"].min_price.minor_units == 1234  # Decimal, exact
        assert quotes["AK | X (FT)"].quantity == 7
        assert quotes["AK | Y (FN)"].min_price is None
        assert "2 items quoted" in result.detail

    def test_a_row_without_a_quantity_refuses(self) -> None:
        row = {"market_hash_name": "AK | X (FT)", "currency": "USD", "min_price": 1.0}
        src, _ = source(
            {FixtureTransport.key("GET", f"{SKINPORT_BASE_URL}/items"): response([row])}
        )
        result = asyncio.run(src.fetch_item_quotes(moment=NOW))
        assert not result.ok
        assert "quantity" in result.detail


class TestFailsClosed:
    def test_wrong_currency_refuses(self) -> None:
        row = sales_row("AK | X (FT)")
        row["currency"] = "EUR"
        src, _ = source({FixtureTransport.key("GET", HISTORY_URL): response([row])})
        result = fetch(src, wanted("AK | X (FT)"))
        assert not result.ok
        assert "USD was requested" in result.detail

    def test_non_list_response_refuses(self) -> None:
        src, _ = source({FixtureTransport.key("GET", HISTORY_URL): response({"weird": True})})
        result = fetch(src, wanted("AK | X (FT)"))
        assert not result.ok
        assert result.status is CapabilityStatus.TEMPORARILY_UNAVAILABLE

    def test_row_without_a_name_refuses(self) -> None:
        row = sales_row("AK | X (FT)")
        del row["market_hash_name"]
        src, _ = source({FixtureTransport.key("GET", HISTORY_URL): response([row])})
        result = fetch(src, wanted("AK | X (FT)"))
        assert not result.ok

    def test_429_is_rate_limited(self) -> None:
        src, _ = source({FixtureTransport.key("GET", HISTORY_URL): response([], status=429)})
        result = fetch(src, wanted("AK | X (FT)"))
        assert result.status is CapabilityStatus.RATE_LIMITED

    def test_items_wrong_currency_refuses(self) -> None:
        row = {"market_hash_name": "AK | X (FT)", "currency": "EUR", "quantity": 3}
        src, _ = source(
            {FixtureTransport.key("GET", f"{SKINPORT_BASE_URL}/items"): response([row])}
        )
        result = asyncio.run(src.fetch_item_quotes(moment=NOW))
        assert not result.ok
        assert "USD was requested" in result.detail

    def test_brotli_header_is_requested(self) -> None:
        """The documented API mandates Accept-Encoding: br."""

        captured: list[dict[str, str]] = []

        class _CapturingTransport:
            async def send(
                self,
                method: str,
                url: str,
                *,
                params: dict[str, str] | None = None,
                headers: dict[str, str] | None = None,
                timeout_seconds: float = 15.0,
            ) -> HttpResponse:
                captured.append(dict(headers or {}))
                return response([sales_row("AK | X (FT)")])

        client = RestClient(transport=_CapturingTransport(), user_agent="test-agent")
        src = SkinportPriceSource(client)
        result = asyncio.run(src.fetch_sales_observations(wanted("AK | X (FT)"), moment=NOW))
        assert result.ok
        assert captured[0].get("accept-encoding") == "br"
