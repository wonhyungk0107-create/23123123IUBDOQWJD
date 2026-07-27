# Data contracts — the integration interface

**Status: PROPOSED.** Authored by the Strategy & Profitability side on
`integration/100-data-contracts`. It becomes AGREED when the Data & Engineering
side approves the PR. After that, any change to a type named here goes through a
small dedicated PR reviewed by the other side — never as a rider on a feature
branch.

The work is split two ways with one seam:

- **Data & Engineering** ensures the data is real and the system runs: venue
  adapters, ingestion, revalidation, persistence, scheduling, inventory,
  ledger, deployment.
- **Strategy & Profitability** decides whether a trade-up is mathematically
  valid and worth doing: float mathematics, probabilities, rule resolution,
  bundle optimization, fee-net EV, risk gates.

Verified listings cross the seam in one direction; evaluations, gate decisions
and rejection codes come back. Nothing else crosses.

## The seam

The agreed integration shape:

```python
verified_listings = market_service.get_verified_listings()  # Data & Engineering

evaluation = profitability_engine.evaluate(  # Strategy & Profitability
    listings=verified_listings,
    rules=current_rules,  # resolved TradeupRuleSet
    fees=current_fees,  # FeeSchedule
)
```

Neither facade exists under these names yet. Today the seam lives inside
`pipeline/scan.py` (`ScanPipeline`), which already has the boundary in the
right order: adapters fetch and verify listings (`MarketAdapter.fetch_listings`
/ `verify_listing`, both returning `CapabilityResult`), then the math side
consumes them (enumerate → optimize → value → gate → revalidate → gate again).
Extracting the named facades is future `integration/` work; this document fixes
the *types* that cross the boundary, not the extraction timing.

## Shared modules

These define the contract surface. Neither side edits them outside a dedicated
contracts PR:

| Module | Contract types |
|---|---|
| `src/tradeup/domain/listings.py` | `MarketplaceListing`, `ListingIdentity`, `ListingStatus`, `TradableStatus` |
| `src/tradeup/domain/fees.py` | `FeeQuote`, `FeeRule`, `FeeSchedule`, `FeeOperation`, `UnknownFeeError` |
| `src/tradeup/domain/contracts.py` | `TradeupCandidate`, `CandidateEvaluation`, `CandidateRejection`, `ContractComposition`, `RejectionReason` |
| `src/tradeup/domain/money.py` | `Money`, `Currency`, `BalanceType` |
| `src/tradeup/domain/items.py` | `Rarity`, `QualityType`, `WearCondition` |
| `src/tradeup/domain/valuation.py` | `OutputValuation`, `PriceObservation` |
| `src/tradeup/domain/conversion.py` | `ConversionQuote` |
| `src/tradeup/domain/execution.py` | `CapabilityResult` — the envelope every adapter operation returns |
| `src/tradeup/adapters/base.py` | `ListingQuery`, `ListingVerification`, `MarketAdapter` protocol |
| `tests/factories.py`, `tests/conftest.py` | Builders both suites depend on |

All rules in `.claude/rules/money-and-math.md` apply to every type above; the
constructor invariants below are enforced in code, not by convention.

## Contract 1 — normalized listing: `MarketplaceListing`

One purchasable asset at one venue, priced and float-known. There is no
condition-level ("any Field-Tested") code path anywhere in the system.

| Field | Type | Notes |
|---|---|---|
| `identity` | `ListingIdentity` | `venue` + `listing_id` natural key; ordered for canonical bundle ordering |
| `asset_id` | `str` | The physical asset; one asset can never fill two contract slots |
| `skin_id` | `str` | Registry identity. Never inferred from a display name |
| `market_hash_name` | `str` | For humans and exit-price lookups only |
| `collection_id` | `str` | From the registry |
| `rarity` / `quality_type` | `Rarity` / `QualityType` | From the registry |
| `raw_float` | `Decimal` | Venue-reported wear; guarded by `reject_float` |
| `normalized_float` | `Decimal` in [0, 1] | The scan recomputes it from registry caps and drops disagreement beyond 0.0001 as `METADATA_DISCREPANCY` — a venue's caps are never authoritative |
| `price`, `buyer_fee`, `deposit_fee` | `Money` | Must share one currency and one balance type; `venue_acquisition_cost` is their sum |
| `observed_at`, `verified_at` | tz-aware UTC `datetime` | `verified_at` is set only by direct revalidation against the venue |
| `listing_status` | `ListingStatus` | `UNKNOWN` is never usable; `PRICE_CHANGED` is present-but-not-purchasable |
| `tradable_status`, `trade_lock_until` | `TradableStatus`, `datetime \| None` | Feeds capital-days; lock days round up |
| `raw_payload_hash` | `str`, required | Evidence is not optional |
| `paint_index`, `paint_seed`, `seller_reliability` | optional | `seller_reliability` in [0, 1] when the venue publishes one |

**"Verified" is a promise from Data & Engineering:** identity was
registry-resolved, the float lies inside the stated range, the payload hash was
recorded, timestamps are tz-aware UTC, and `listing_status` reflects the venue
as of `verified_at`. The math side treats `verified_at` staleness as a gate
input (`QUOTE_TOO_OLD`), not as something to repair.

Field mapping from the planning sketch: the sketch's flat `venue`/`listing_id`
are `identity`; everything else in the sketch exists under the same name. The
sketch omitted `market_hash_name`, `normalized_float`, `deposit_fee`,
`listing_status`, `tradable_status`, `raw_payload_hash` and the optionals —
these are load-bearing and stay.

## Contract 2 — fee quote: `FeeQuote` via `FeeSchedule`

Fees are versioned data, never constants. `FeeSchedule.quote` resolves exactly
one `FeeRule` for (venue, operation, basis, moment) or raises
`UnknownFeeError` — on a gap *and* on ambiguity. The math side maps that error
to the `UNKNOWN_FEE` rejection; nothing defaults to zero. A genuine zero fee is
stated through `zero_quote(..., reason=...)` with a source.

`FeeQuote` carries provenance: `venue`, `operation` (`FeeOperation` covers
purchase, deposit, sale, withdrawal, FX, network transfer, payment surcharge),
`basis`, `fee` (same currency, rounded **up**), `rule_id`, `source`,
`quoted_at`, `last_verified`.

Sourcing fee rules (with citations in `docs/source-matrix.md`) is Data &
Engineering work; consuming them pessimistically is Strategy & Profitability
work.

## Contract 3 — trade-up candidate: `TradeupCandidate`

A `ContractComposition` (the collection shape) bound to exact listings
(`TradeupInput`), with computed `TradeupOutcome`s. Invariants enforced at
construction:

- input count equals the composition's total; collection counts match exactly;
- no listing identity and no `asset_id` occupies two slots;
- outcome probabilities are `Fraction`s summing to exactly 1 — not within a
  tolerance;
- `candidate_id` is a content hash (`TU-` + sha256 prefix) over rule version,
  composition, procurement mode and the sorted listing identities, so
  re-scanning the same market state reproduces the same identifier.

A candidate carries **no economics** — evaluation is separate so the same
candidate can be re-evaluated against fresher prices and fees without being
rebuilt.

## Contract 4 — evaluation result: `CandidateEvaluation` + rejections

The economics of one candidate at one moment under one fee schedule. It records
`candidate_id`, `rule_version`, `fee_schedule_id`, `evaluated_at`, and every
field a gate reads, so any decision can be re-derived from the stored record.

Cost definitions (as implemented and commented in `domain/contracts.py`):

- `acquisition_cost` — cash that actually leaves the wallet
  (inputs + buyer fees + deposit fees + FX + surcharge);
- `all_in_cost` — acquisition plus modelled charges: operational cost,
  settlement-rail share, capital carry, partial-fill reserve;
- `ev_net = expected_output_value − all_in_cost`;
- `roi_net = ev_net / acquisition_cost` (capital genuinely committed, not the
  loaded figure).

Decisions come from `execution/policy.py` (`evaluate_discovery`,
`evaluate_final`) as `GateDecision`s. Every failure is a `CandidateRejection`
with machine-readable `RejectionReason` codes; all failing reasons are
collected, never short-circuited, and never collapsed — the rejection census is
the primary output of a shadow run.

## Ownership map

| Area | Owner |
|---|---|
| `domain/mathematics.py`, `domain/rules.py`, `discovery/`, `optimizer/`, `valuation/`, `execution/policy.py`, `tests/property/`, `tests/golden/`, `tests/fixtures/golden_tradeups.json` (hand-computed — never regenerated), `docs/economic-model.md`, `docs/rule-registry.md` | Strategy & Profitability |
| `adapters/`, `metadata/`, `persistence/`, `inventory/`*, `reporting/`, `cli/`, `pipeline/`, `execution/reservations.py`, `migrations/`, `.github/`, `Dockerfile`, `docker-compose.yml`, `pyproject.toml`, `uv.lock`, `tests/contract/`, `tests/live/`, the rest of `tests/fixtures/` (venue payloads) | Data & Engineering |
| Shared-module table above, `tests/unit/`, `tests/integration/` | Shared — dedicated PR, cross-review |

\* `inventory/` does not exist yet; listed because the split assigns it to Data
& Engineering when it appears.

`pipeline/scan.py` is Data & Engineering's orchestration, but it encodes gate
ordering the math side depends on (revalidate *between* the discovery and final
gates, re-evaluating on revalidated data) — changes to that ordering get math
review.

## Workflow (both sides)

Branches: `math/<issue>-<task>`, `infra/<issue>-<task>`,
`integration/<issue>-<task>`. Never commit to `main`; one branch and one
working directory per person and per agent session. A change is mergeable when
`make verify` passes and the other side has reviewed it. Contract changes:
smallest possible PR, this document updated in the same PR.
