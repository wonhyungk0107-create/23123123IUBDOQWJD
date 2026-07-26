"""Exact marketplace listings.

The unit of procurement is a *specific asset at a specific venue*, never "an
AK-47 | Redline in Field-Tested". Condition-average pricing is what makes public
trade-up calculators look profitable and makes real procurement fail, so there is
no code path here that accepts a condition-level price.

A listing carries its own provenance: when it was observed, when it was last
revalidated against the venue, and a hash of the raw payload it was parsed from.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from tradeup.domain._guards import reject_float
from tradeup.domain.items import QualityType, Rarity
from tradeup.domain.money import BalanceType, Currency, Money

__all__ = [
    "ListingIdentity",
    "ListingStatus",
    "MarketplaceListing",
    "TradableStatus",
]


class ListingStatus(enum.StrEnum):
    """What we last observed about a listing's availability."""

    ACTIVE = "ACTIVE"
    SOLD = "SOLD"
    WITHDRAWN = "WITHDRAWN"
    PRICE_CHANGED = "PRICE_CHANGED"
    UNKNOWN = "UNKNOWN"
    """Revalidation could not determine status. Treated as unusable, never as ACTIVE."""

    @property
    def is_purchasable(self) -> bool:
        """Present *and* still at the price we quoted."""
        return self is ListingStatus.ACTIVE

    @property
    def is_present(self) -> bool:
        """The asset still exists at the venue, whatever it now costs.

        Distinct from :attr:`is_purchasable` because "gone" and "repriced" are
        different market events with different implications. Collapsing them would
        make the revalidation census -- the main thing a shadow run measures --
        report vanishing supply where there was only a price move.
        """
        return self in {ListingStatus.ACTIVE, ListingStatus.PRICE_CHANGED}


class TradableStatus(enum.StrEnum):
    """Whether the asset can move to Steam, and when."""

    TRADABLE = "TRADABLE"
    TRADE_LOCKED = "TRADE_LOCKED"
    UNTRADABLE = "UNTRADABLE"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True, order=True)
class ListingIdentity:
    """Natural key for a listing. Unique per venue.

    Ordered so bundles have a deterministic canonical ordering, which is what makes
    a candidate's identity reproducible across scans.
    """

    venue: str
    listing_id: str

    def __post_init__(self) -> None:
        if not self.venue or not self.listing_id:
            raise ValueError("listing identity requires both venue and listing_id")

    def __str__(self) -> str:
        return f"{self.venue}:{self.listing_id}"


@dataclass(frozen=True, slots=True)
class MarketplaceListing:
    """One purchasable asset, priced and float-known.

    ``price`` is the sticker price. ``buyer_fee`` and ``deposit_fee`` are the
    venue-side costs of acquiring *this* listing; the sum is what actually leaves
    the wallet. All three must share a currency and balance type -- mixing them is
    a category error that the money layer refuses.
    """

    identity: ListingIdentity
    asset_id: str
    skin_id: str
    market_hash_name: str
    collection_id: str
    rarity: Rarity
    quality_type: QualityType
    raw_float: Decimal
    normalized_float: Decimal
    price: Money
    buyer_fee: Money
    deposit_fee: Money
    observed_at: datetime
    listing_status: ListingStatus
    tradable_status: TradableStatus
    raw_payload_hash: str
    paint_index: int | None = None
    paint_seed: int | None = None
    trade_lock_until: datetime | None = None
    verified_at: datetime | None = None
    #: Venue-reported seller reliability in [0, 1], when the venue publishes one.
    seller_reliability: Decimal | None = None

    def __post_init__(self) -> None:
        reject_float(self.raw_float, f"{self.identity} raw_float")
        reject_float(self.normalized_float, f"{self.identity} normalized_float")
        if not (Decimal(0) <= self.normalized_float <= Decimal(1)):
            raise ValueError(
                f"normalized_float {self.normalized_float} outside [0, 1] for {self.identity}"
            )
        if self.observed_at.tzinfo is None:
            raise ValueError("observed_at must be timezone-aware UTC")
        if self.verified_at is not None and self.verified_at.tzinfo is None:
            raise ValueError("verified_at must be timezone-aware UTC")
        if self.trade_lock_until is not None and self.trade_lock_until.tzinfo is None:
            raise ValueError("trade_lock_until must be timezone-aware UTC")
        currencies = {self.price.currency, self.buyer_fee.currency, self.deposit_fee.currency}
        if len(currencies) != 1:
            raise ValueError(f"listing {self.identity} mixes currencies {currencies}")
        balances = {
            self.price.balance_type,
            self.buyer_fee.balance_type,
            self.deposit_fee.balance_type,
        }
        if len(balances) != 1:
            raise ValueError(f"listing {self.identity} mixes balance types {balances}")
        if self.seller_reliability is not None and not (
            Decimal(0) <= self.seller_reliability <= Decimal(1)
        ):
            raise ValueError(f"seller_reliability {self.seller_reliability} outside [0, 1]")
        if not self.raw_payload_hash:
            raise ValueError("raw_payload_hash is required; evidence is not optional")

    # -- money ---------------------------------------------------------------

    @property
    def currency(self) -> Currency:
        return self.price.currency

    @property
    def balance_type(self) -> BalanceType:
        return self.price.balance_type

    @property
    def venue_acquisition_cost(self) -> Money:
        """Price plus venue-side buyer and deposit fees.

        This is *not* the all-in cost: FX, payment surcharges, partial-fill reserve
        and capital carry are contract-level and applied by the valuation layer.
        """
        return self.price + self.buyer_fee + self.deposit_fee

    # -- freshness -----------------------------------------------------------

    @property
    def effective_observation_time(self) -> datetime:
        """Most recent moment this listing was confirmed against the venue."""
        if self.verified_at is None:
            return self.observed_at
        return max(self.observed_at, self.verified_at)

    def quote_age_seconds(self, now: datetime) -> Decimal:
        delta = (now - self.effective_observation_time).total_seconds()
        return Decimal(str(max(delta, 0.0))).quantize(Decimal("0.001"))

    def is_stale(self, now: datetime, max_age_seconds: int) -> bool:
        return self.quote_age_seconds(now) > Decimal(max_age_seconds)

    # -- transfer readiness --------------------------------------------------

    def is_trade_locked(self, now: datetime) -> bool:
        if self.tradable_status is TradableStatus.TRADE_LOCKED:
            return self.trade_lock_until is None or self.trade_lock_until > now
        return False

    def trade_lock_days_remaining(self, now: datetime) -> int:
        """Whole days until this asset can move. Rounded up; never negative.

        Capital-days modelling uses this, so rounding down would systematically
        understate how long money is tied up.
        """
        if self.trade_lock_until is None or self.trade_lock_until <= now:
            return 0
        seconds = (self.trade_lock_until - now).total_seconds()
        return int(-(-seconds // 86400))

    @property
    def is_usable(self) -> bool:
        """Purchasable and capable of reaching Steam at all."""
        return (
            self.listing_status.is_purchasable
            and self.tradable_status is not TradableStatus.UNTRADABLE
        )

    def __str__(self) -> str:
        return f"{self.identity} {self.market_hash_name} float={self.raw_float} price={self.price}"
