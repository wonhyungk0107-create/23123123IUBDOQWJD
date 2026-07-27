"""Input-side liquidity depth: real depth, not just the cheapest listing.

A candidate whose economics rest on one cheap listing is a race, not a
strategy: the cheapest listing is exactly the one most likely to be sniped,
and a bundle priced off it silently assumes nine more fills at prices nobody
verified. This gate requires the market to demonstrate depth: at least
``required_depth`` qualifying listings aggregated across every configured
venue, whose cheapest-``required_depth`` mean acquisition cost sits at or
below the reference price the item's economics already use.

Failing closed is the point. Fewer qualifying listings than required is not
"use what exists" — it is the same condition as an unknown fee: an
economically material unknown that must stop the candidate, coded
``INSUFFICIENT_INPUT_DEPTH``. Cross-currency listings are excluded rather
than converted (conversion belongs to ``ConversionQuote``, not a screening
gate), and the mean rounds **up** — a cost never rounds in the candidate's
favour.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from tradeup.domain.contracts import RejectionReason
from tradeup.domain.items import QualityType
from tradeup.domain.listings import MarketplaceListing
from tradeup.domain.money import BalanceType, Currency, Money

__all__ = [
    "DEFAULT_REQUIRED_DEPTH",
    "DepthEvidence",
    "assess_input_depth",
    "depth_rejection_reasons",
    "qualifying_listings",
]

#: How many purchasable listings an input must show before it counts as deep.
#: Matches the contract size: a ten-input bundle backed by fewer than ten
#: purchasable units of its constrained input is not executable depth.
DEFAULT_REQUIRED_DEPTH = 10


def qualifying_listings(
    listings: Sequence[MarketplaceListing],
    *,
    skin_id: str,
    quality: QualityType,
    currency: Currency,
    balance_type: BalanceType = BalanceType.CASH_WITHDRAWABLE,
    float_low: Decimal | None = None,
    float_high: Decimal | None = None,
) -> tuple[MarketplaceListing, ...]:
    """Listings that could actually fill this input slot.

    Registry identity (``skin_id``), quality, usability (purchasable and able
    to reach Steam), an optional raw-float band, and a single currency and
    balance type. A listing in another currency is excluded, never converted:
    a screening gate has no business holding an FX quote.
    """
    selected = []
    for listing in listings:
        if listing.skin_id != skin_id:
            continue
        if listing.quality_type is not quality:
            continue
        if not listing.is_usable:
            continue
        if listing.currency is not currency or listing.balance_type is not balance_type:
            continue
        if float_low is not None and listing.raw_float < float_low:
            continue
        if float_high is not None and listing.raw_float > float_high:
            continue
        selected.append(listing)
    return tuple(selected)


@dataclass(frozen=True, slots=True)
class DepthEvidence:
    """Why an input's depth gate passed or failed, card-ready."""

    skin_id: str
    required_depth: int
    qualifying_count: int
    #: Mean acquisition cost of the cheapest ``required_depth`` listings,
    #: rounded up. ``None`` when the count itself already failed the gate —
    #: a mean over too few listings would dress thin supply up as a price.
    cheapest_mean: Money | None
    target_unit_price: Money
    #: Distinct venues represented among the cheapest listings counted.
    venues: tuple[str, ...]
    passed: bool
    detail: str


def assess_input_depth(
    qualifying: Sequence[MarketplaceListing],
    *,
    skin_id: str,
    target_unit_price: Money,
    required_depth: int = DEFAULT_REQUIRED_DEPTH,
) -> DepthEvidence:
    """Judge one input's market depth against its reference price.

    ``qualifying`` must already have passed :func:`qualifying_listings` for
    this slot. The gate passes only when at least ``required_depth`` listings
    qualify *and* the mean acquisition cost of the cheapest ``required_depth``
    of them is at or below ``target_unit_price``.
    """
    if required_depth < 1:
        raise ValueError(f"required_depth must be at least 1, got {required_depth}")
    count = len(qualifying)
    if count < required_depth:
        return DepthEvidence(
            skin_id=skin_id,
            required_depth=required_depth,
            qualifying_count=count,
            cheapest_mean=None,
            target_unit_price=target_unit_price,
            venues=tuple(sorted({item.identity.venue for item in qualifying})),
            passed=False,
            detail=(
                f"only {count} qualifying listing(s) across all venues; "
                f"{required_depth} required — failing closed, not falling back"
            ),
        )
    cheapest = sorted(
        qualifying,
        key=lambda item: (item.venue_acquisition_cost.minor_units, str(item.identity)),
    )[:required_depth]
    total = sum(item.venue_acquisition_cost.minor_units for item in cheapest)
    # Cost mean rounds up: the candidate never benefits from the remainder.
    mean = Money(
        -(-total // required_depth),
        target_unit_price.currency,
        target_unit_price.balance_type,
    )
    venues = tuple(sorted({item.identity.venue for item in cheapest}))
    if mean <= target_unit_price:
        return DepthEvidence(
            skin_id=skin_id,
            required_depth=required_depth,
            qualifying_count=count,
            cheapest_mean=mean,
            target_unit_price=target_unit_price,
            venues=venues,
            passed=True,
            detail=(
                f"cheapest-{required_depth} mean {mean} at or below reference "
                f"{target_unit_price} across {len(venues)} venue(s)"
            ),
        )
    return DepthEvidence(
        skin_id=skin_id,
        required_depth=required_depth,
        qualifying_count=count,
        cheapest_mean=mean,
        target_unit_price=target_unit_price,
        venues=venues,
        passed=False,
        detail=(
            f"cheapest-{required_depth} mean {mean} exceeds reference "
            f"{target_unit_price}; depth exists but not at the price the "
            "economics assumed"
        ),
    )


def depth_rejection_reasons(evidences: Sequence[DepthEvidence]) -> tuple[RejectionReason, ...]:
    """The gate's contribution to a candidate's rejection-reason set."""
    if any(not evidence.passed for evidence in evidences):
        return (RejectionReason.INSUFFICIENT_INPUT_DEPTH,)
    return ()
