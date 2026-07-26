"""Skinport price reference -- completed-sale evidence for output valuation.

Documented, keyless API (docs.skinport.com): ``GET /v1/sales/history`` returns
aggregated completed-sale statistics per ``market_hash_name`` over rolling windows.
Constraints stated in the documentation and honoured here:

* No authentication. This source costs nothing and holds no credentials.
* **8 requests per 5 minutes**, responses cached for 5 minutes. The fetcher batches
  names and refuses to exceed a request budget rather than tripping the limit.
* ``Accept-Encoding: br`` is mandatory (Brotli; the ``brotli`` package decodes it).

Role boundary: this is *price evidence*, never execution truth. Observations are
tagged ``COMPLETED_SALE`` at venue ``skinport``, and the exit valuation applies
Skinport's sourced fees when it elects to exit there. Skinport states names, not
item identities, so the caller supplies the name -> (skin_id, quality, wear) map,
composed from the registry -- names are composed and looked up, never parsed.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal

from tradeup.adapters.http import (
    RestClient,
    TransportAuthError,
    TransportError,
    TransportRateLimited,
    TransportSchemaError,
)
from tradeup.domain.execution import CapabilityResult, CapabilityStatus
from tradeup.domain.items import QualityType, WearCondition
from tradeup.domain.money import BalanceType, Currency, Money
from tradeup.domain.valuation import PriceObservation, ValuationSource

__all__ = ["SKINPORT_BASE_URL", "SkinportPriceSource", "WantedName"]

SKINPORT_BASE_URL = "https://api.skinport.com/v1"
VENUE = "skinport"

#: Documented request budget: 8 requests per 5 minutes. One is held back so a
#: scan never consumes the entire window and starves an immediate retry.
_MAX_REQUESTS = 7

#: Names per request. The parameter is comma-delimited; this keeps URLs short.
_NAMES_PER_REQUEST = 50

#: Sales windows to read, freshest first. Each contributes one observation so the
#: low-quantile exit read has more than a single point to stand on.
_WINDOWS = ("last_7_days", "last_30_days")

#: (skin_id, quality, wear) — the identity a name was composed from.
WantedName = tuple[str, QualityType, WearCondition]


class SkinportPriceSource:
    """Bulk completed-sale observations from the documented sales-history endpoint."""

    venue = VENUE

    def __init__(
        self,
        client: RestClient,
        *,
        base_url: str = SKINPORT_BASE_URL,
        currency: Currency = Currency.USD,
    ) -> None:
        self._client = client
        self._base_url = base_url.rstrip("/")
        self._currency = currency

    async def fetch_sales_observations(
        self,
        wanted: Mapping[str, WantedName],
        *,
        moment: datetime,
    ) -> CapabilityResult[tuple[PriceObservation, ...]]:
        """Observations for every wanted name the request budget can cover.

        Names beyond the budget are *reported*, not silently skipped: a scan that
        quietly failed to price half its outputs would present artificially thin
        evidence as if it were the market's fault.
        """
        names = sorted(wanted)
        batches = [
            names[start : start + _NAMES_PER_REQUEST]
            for start in range(0, len(names), _NAMES_PER_REQUEST)
        ]
        unqueried = [name for batch in batches[_MAX_REQUESTS:] for name in batch]
        batches = batches[:_MAX_REQUESTS]

        observations: list[PriceObservation] = []
        answered: set[str] = set()
        skipped_rows = 0
        try:
            for batch in batches:
                response = await self._client.get(
                    f"{self._base_url}/sales/history",
                    params={
                        "app_id": "730",
                        "currency": self._currency.value,
                        "market_hash_name": ",".join(batch),
                    },
                    headers={"accept-encoding": "br"},
                )
                rows = response.json()
                if not isinstance(rows, list):
                    raise TransportSchemaError("skinport sales-history response is not a list")
                for row in rows:
                    parsed = self._parse_row(row, wanted, moment)
                    if parsed is None:
                        skipped_rows += 1
                        continue
                    name, row_observations = parsed
                    answered.add(name)
                    observations.extend(row_observations)
        except TransportError as exc:
            return self._failure(moment, exc)

        details: list[str] = [f"{len(answered)}/{len(wanted)} wanted names priced"]
        if unqueried:
            details.append(
                f"{len(unqueried)} names beyond the {_MAX_REQUESTS}-request budget were not queried"
            )
        if skipped_rows:
            details.append(f"{skipped_rows} rows without usable sales were skipped")
        return CapabilityResult.succeeded(
            self.venue,
            "fetch_sales_observations",
            moment,
            tuple(observations),
            detail="; ".join(details),
        )

    # -- parsing --------------------------------------------------------------

    def _parse_row(
        self,
        row: object,
        wanted: Mapping[str, WantedName],
        moment: datetime,
    ) -> tuple[str, list[PriceObservation]] | None:
        """One name's windows -> observations. ``None`` when nothing is usable."""
        if not isinstance(row, Mapping):
            raise TransportSchemaError("skinport sales-history row is not an object")
        name = row.get("market_hash_name")
        if not isinstance(name, str) or not name:
            raise TransportSchemaError("skinport sales-history row has no market_hash_name")
        identity = wanted.get(name)
        if identity is None:
            # The endpoint may answer names we did not ask about; ignoring an
            # unrequested row is not a data fault.
            return None
        currency = row.get("currency")
        if currency != self._currency.value:
            raise TransportSchemaError(
                f"skinport returned {currency!r} prices; {self._currency.value} was requested"
            )
        skin_id, quality, wear = identity

        observations: list[PriceObservation] = []
        for window in _WINDOWS:
            aggregate = row.get(window)
            if not isinstance(aggregate, Mapping):
                continue
            median = aggregate.get("median")
            volume = aggregate.get("volume")
            if median is None or not isinstance(volume, int) or volume < 1:
                continue
            if not isinstance(median, Decimal | int):
                raise TransportSchemaError(
                    f"skinport {window} median for {name!r} is not numeric: {median!r}"
                )
            observations.append(
                PriceObservation(
                    skin_id=skin_id,
                    quality=quality,
                    wear=wear,
                    venue=self.venue,
                    gross=Money.from_major(
                        Decimal(median), self._currency, BalanceType.CASH_WITHDRAWABLE
                    ),
                    source=ValuationSource.COMPLETED_SALE,
                    observed_at=moment,
                    evidence_count=volume,
                )
            )
        if not observations:
            return None
        return name, observations

    def _failure(
        self, moment: datetime, exc: TransportError
    ) -> CapabilityResult[tuple[PriceObservation, ...]]:
        status = CapabilityStatus.TEMPORARILY_UNAVAILABLE
        retry_after: int | None = None
        if isinstance(exc, TransportRateLimited):
            status = CapabilityStatus.RATE_LIMITED
            retry_after = exc.retry_after_seconds
        elif isinstance(exc, TransportAuthError):
            status = CapabilityStatus.AUTHENTICATION_REQUIRED
        return CapabilityResult(
            status=status,
            venue=self.venue,
            operation="fetch_sales_observations",
            observed_at=moment,
            detail=str(exc) or type(exc).__name__,
            retry_after_seconds=retry_after,
        )
