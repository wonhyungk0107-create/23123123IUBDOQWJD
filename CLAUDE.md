# CS2 Trade-Up Procurement Agent

Build an **exact-asset, multi-market procurement optimizer** measuring executable, fee-net, lock-adjusted, *settled* profit. Not another trade-up calculator, not a TradeUpSpy scraper.

Edge hypothesis: sellers price skins as condition-level commodities and ignore marginal value inside a float-constrained bundle. Inefficiency persists because exact matching is combinatorial, inventory is fragmented, and completing all 5/10 inputs before listings vanish is hard.

## Hard boundaries

**Never automate Steam.** The Steam Subscriber Agreement prohibits scripts/bots interacting with Steam services. Forbidden: SCM buy/sell, trade-offer acceptance, inventory polling, client/UI control, cookie-based browser automation, proxy rotation, automated mobile-auth confirmations.

Compliant flow: scan sanctioned third-party APIs → execute via documented marketplace APIs → emit exact Steam-side instructions → operator accepts transfers and performs the contract manually → record result → exit via sanctioned venue.

**No LLM in the hot path.** Hot path is deterministic: API response → validation → optimizer → EV → risk policy → sanctioned execution. An LLM may explain failures, summarize terms changes, cluster errors, draft operator instructions. It never computes floats, probabilities, fees, or buy decisions.

**Never store:** Steam password, session cookies, `steamLoginSecure`, mobile-auth secrets, extension cookies.
**Credentials:** `CSFLOAT_API_KEY`, `DMARKET_PUBLIC_KEY`, `DMARKET_SECRET_KEY`, `SKINSNIPE_API_KEY`, `TELEGRAM_BOT_TOKEN`, `DATABASE_URL`, `ENCRYPTION_KEY`.

## Money rules

1. **Gate on net ROI, not EV.** `EV_net = Σ P(y)·V_net(y) − C_all_in`; `ROI_net = EV_net / C_all_in`. Gate is `ROI_net ≥ 8%` *after* buyer fees, deposit/processing fees, FX, withdrawal fees, seller fees, slippage, liquidity haircut, partial-fill risk, capital carry, venue taxes. Never accept a displayed TradeUpSpy return as final.
2. **Balance types are not fungible.** Tag every price `CASH_WITHDRAWABLE | VENUE_REUSABLE_BALANCE | STEAM_WALLET | NON_WITHDRAWABLE_CREDIT`. Steam Wallet funds have no cash value. Objective = cash-withdrawable settled profit.
3. **Exact assets only.** No condition-average prices. Every input needs listing ID, asset ID, exact float, collection, rarity, Normal/StatTrak/Souvenir, price, trade-lock state, availability, and any pattern/sticker premium.
4. Money in **integer minor units**; Decimal/fixed-point for all threshold comparisons. No binary float comparisons at decision boundaries.
5. TradeUpSpy is a **golden-case verifier, not a dependency** (no documented public API). Disagreements are quarantined and decomposed: metadata / rule-version / float-formula / probability / price-timestamp / fee mismatch.

## Trade-up mathematics

Normalized input float, for input `i` with skin min `a_i`, max `b_i`, actual float `f_i`:

```
z_i = (f_i − a_i) / (b_i − a_i)
z̄   = (1/N) Σ z_i
f_y = a_y + z̄ · (b_y − a_y)
```

Outcome probability, `n_c` inputs from collection `c`, `k_c` eligible next-rarity outputs:

```
P(y) = n_c / (N · k_c)       invariant: Σ P(y) = 1  (else candidate invalid)
```

Costs and value:

```
C_i       = Price + BuyerFee + DepositFee + FX + PaymentTax + ExpectedFailureCost
C_all_in  = Σ C_i + PartialFillReserve + Ops + CapitalCarry
V(y,v)    = SalePrice − SellerFee − WithdrawalFee − FX − LiquidityHaircut − CarryCost
V_net(y)  = max_v V(y,v)     # executable prices only, not highest ask
```

Also compute `P(profit)`, `WorstCase = min_y V_net(y) − C_all_in`, and expected capital-days.

**Rank by capital velocity, not ROI:** `Score = EV_lower_confidence_bound / ExpectedCapitalDays`. 8% over 5 days beats 12% over 30.

Exit-price hierarchy: exact-float completed sales → exact-condition completed sales → live cash bids → depth-adjusted asks → conservative reference → no valuation. Use a lower quantile, never the mean.

## Versioned rules registry

`N` is 10, or 5 for eligible Covert contracts. Souvenir inputs (post May 2026) may mix with normal inputs; Souvenir attributes are stripped and the output is a normal item one rarity higher.

Never hard-code: "Souvenirs cannot be used", "every contract uses ten inputs", "raw average float determines output", "all Covert inputs share an output universe".

Each rule row: `rule_version, effective_from, effective_until, input_count, input_quality_constraints, output_quality, probability_method, float_method, eligible_output_mapping, source_reference, validated_at`.

Registry owns identity, paint index, collection, rarity, float min/max, wear states, quality eligibility, next-rarity outcomes, explicit knife/glove mappings (never inferred from display names). **Fail closed** on: new collection, changed collection mapping, changed rule source, probabilities ≠ 1, missing float cap, material source disagreement.

## Sources

| Source | Role | Execution |
|---|---|---|
| ByMykel CSGO-API | metadata, collections, float ranges | free; pin commit, diff every update |
| CSFloat | exact-float listings, verification | read/verify only; no undocumented purchase calls |
| DMarket | listings + **targets** | primary documented auto-buy venue |
| SkinSnipe | cross-market + historical prices | validation only; always requery direct venue |
| TradeUpSpy | parity check | manual/licensed; no public API |
| CS.MONEY, SkinSwap | extra inventory | manual-link adapter only |
| Steam | receipt + manual trade-up | human-only |
| "Skins Money" | unidentified | keep adapter disabled |

Fee schedules are versioned data, never constants (`venue, operation, tier, balance_type, effective_from/until, fixed_fee, percentage_fee, source, last_verified`). Confirm applicable fee immediately before purchase.

## Architecture

```
marketplace APIs → adapters + raw-event archive → normalization/identity
→ versioned rules graph → composition enumerator → exact-asset optimizer
→ pricing/fee/lock model → direct revalidation → risk gate
→ [DMarket target | operator approval card] → inventory queue
→ manual Steam receipt + trade-up → exit optimizer → settled P&L ledger
```

**Adapter interface** — every venue implements `fetch_listings`, `verify_listing`, `fetch_fee_quote`, `fetch_account_state`, `execute`. `execute` returns `AUTOMATED | OPERATOR_APPROVAL_REQUIRED | UNSUPPORTED` so an unsupported source can never silently fall back to browser automation.

**Enumerator** — never enumerate raw listing combinations. Enumerate *collection compositions* (10A, 9A+1B, …, 5-item Coverts), derive output probabilities and normalized-float breakpoints, then ask the optimizer for the cheapest bundle satisfying that composition and `z_max`.

**Optimizer** — minimize `Σ c_j x_j` s.t. `Σ x_j = N`, `Σ z_j x_j ≤ N·z_max`, `Σ_{j∈c} x_j = n_c`, `x_j ∈ {0,1}`. Prune dominated listings (≤ cost and ≤ normalized float), keep a cost/float Pareto frontier, use DP or branch-and-bound first; CP-SAT only if it becomes a bottleneck. Recompute the chosen bundle in exact decimals before approval.

**Reservations** — one listing can appear in many candidate contracts; lock the underlying listing, not the displayed contract: `UNCLAIMED → SOFT_RESERVED → PURCHASE_PENDING → PURCHASED | RELEASED | STALE`.

**Inventory states** — `DISCOVERED → PURCHASE_REQUESTED → PURCHASED → TRADE_PROTECTED → PLATFORM_INVENTORY → WITHDRAWABLE → TRANSFER_PENDING → RECEIVED_IN_STEAM → TRADEUP_ELIGIBLE → CONSUMED → OUTPUT_CREATED → OUTPUT_LISTED → SOLD_UNSETTLED → SETTLED`. Use actual transition times (DMarket ~10-day third-party invisibility; CS.MONEY ~8-day reversal window), never same-day recycling.

**Ledger** — immutable events (deposit, balance credit, purchase, each fee type, FX, withdrawal, transfer, consumption, output creation/listing, sale, refund, reversal, writeoff). A contract is not profitable until proceeds settle and are attributed to it. Store predicted vs. actual output, sale value, fees, proceeds, ROI, capital-days, prediction error.

**Listing store fields** — `venue, listing_id, asset_id, skin_id, market_hash_name, collection_id, rarity, quality_type, raw_float, normalized_float, paint_index, paint_seed, price_minor, currency, balance_type, buyer_fee_minor, deposit_fee_minor, trade_lock_until, tradable_status, seller_reliability, observed_at, verified_at, listing_status, raw_payload_hash`.

## Partial-fill risk

A contract is a basket order with no atomic cross-venue execution. Subtract `P(bundle fails) · (CostPurchased − Salvage)` from EV *before* buying the first item.

- **Mode A — complete-bundle:** every input verified and buyable now. Default for operator-approved contracts.
- **Mode B — target accumulation:** DMarket targets under strict float/price bounds, into a reusable pool. Best path to autonomy. Verify actual float precision of targets (`floatPartValue`) before trusting narrow-decimal contracts.
- **Mode C — inventory-assisted:** hold only inputs with multiple profitable downstream uses; track `current_contract_value, best_alternative_contract_value, standalone_liquidation_value, days_held, opportunity_cost`; liquidate when best downstream EV goes negative.

## Gates

`MIN_DISCOVERY_NET_ROI = 0.12` (buffer absorbs staleness; calibrate from observed slippage) · `MIN_FINAL_NET_ROI = 0.08` · plus `MIN_ABSOLUTE_EV_USD` (~$3–5), `MAX_CONTRACT_COST_USD`, `MAX_WORST_CASE_LOSS_USD`, `MAX_PARTIAL_FILL_EXPOSURE_USD`, `MAX_QUOTE_AGE_SECONDS`, `MAX_CAPITAL_DAYS`, `MAX_DAILY_SPEND_USD`, `MAX_DAILY_SETTLED_LOSS_USD`.

**Execution gate, in order:** still above discovery threshold → requery every listing ID directly → all active → floats still match → recompute all-in cost → fee schedule current → refresh output prices → `ROI_net ≥ 8%` → lower-confidence EV positive → partial-fill exposure within limit → balance sufficient → daily/per-contract limits pass → listings reserved → execution method sanctioned. Venues without a documented purchase API get an operator card, never an invented endpoint.

**Operator card:** contract ID, expiry, all-in cost, expected net output, EV, ROI, capital-days, P(profit), worst case, full output distribution, every input listing + float + link, purchase sequence, trade-lock status, manual Steam steps, recommended exit. Operator marks `PURCHASED | LISTING_GONE | PRICE_CHANGED | REJECTED | RECEIVED_IN_STEAM | TRADEUP_COMPLETED | OUTPUT_RECORDED | LISTED_FOR_SALE | SOLD | SETTLED`.

**Portfolio:** track capital by collection/venue, capital in trade protection, capital in incomplete bundles, correlated output-market exposure, drawdown, turnover. Size by the min of venue liquidity, validated edge capacity, risk capital, operator capacity. Fixed small allocations first; fractional Kelly only after calibration. Never full Kelly on an uncalibrated model.

**Kill switches (halt new buys):** API schema change, unknown fee schedule, rule change, failed metadata validation, revalidation failure rate exceeded, repeated direct-vs-aggregator disagreement, balance reconciliation failure, duplicate asset allocation, realized price error beyond tolerance, settled P&L past loss limit, terms change, possible credential compromise.

## Stack

Shadow: Python 3.12, asyncio, httpx, Pydantic, SQLAlchemy + Alembic, Polars, SQLite + Parquet, APScheduler, Telegram bot, pytest/mypy/ruff. Live: same monolith + PostgreSQL, Docker Compose, encrypted secrets, nightly backups. No RabbitMQ/Redis until multiple workers contend over reservations.

```
src/
  domain/     money floats items listings tradeups outcomes rules
  metadata/   bymykel registry validation rule_versions
  adapters/   base csfloat dmarket skinsnipe tradeupspy_validator csmoney_manual skinswap_manual
  discovery/  composition_graph boundaries pareto enumerator
  optimizer/  bundle branch_and_bound inventory_assisted partial_fill
  valuation/  fees exit_prices liquidity capital_days expected_value
  execution/  policy reservations dmarket_targets operator_approval
  inventory/  state_machine bundles reconciliation
  ledger/     events pnl tax_lots
  monitoring/ alerts telegram health kill_switches
tests/  golden_tradeups/ contract_tests/ test_float_math test_probabilities
        test_fee_engine test_bundle_optimizer test_duplicate_reservations test_partial_fill
```

## Build sequence

0. **Money truth** — fix all thresholds above and the objective (cash-withdrawable settled profit).
1. **Rules and math** — metadata importer, collection graph, normalization, probability engine, Souvenir/Covert rules, golden tests. Ship when every probability sums to 1, every checked output float matches, no unknown collection mappings, money is integer minor units.
2. **Shadow scanner** — ByMykel + CSFloat + DMarket + SkinSnipe + manual TradeUpSpy checks. Buy nothing. Log every candidate, exact listing IDs, initial vs. requeried ROI, listing survival at 30s/5m/1h, whether a complete bundle stayed obtainable, parity result, output price drift, capital-days. This phase decides whether the inefficiency is executable or merely displayed.
3. **Paper procurement** — simulate real order sequences; disappearing listing = failed fill; compute orphan salvage loss and simulated settled P&L. Most public-calculator opportunities probably die here.
4. **Human-approved live pilot** — small complete bundles; operator approves, receives, contracts, records, sells, records settled proceeds. First contracts validate operations, not profitability.
5. **DMarket target automation** — only documented targets with valid identity, float filter, max price, spend caps, expiry, immediate reconciliation.
6. **Inventory-assisted procurement** — bounded pool of repeatedly useful inputs with decay tracking.
7. **Exit optimization** — compare immediate bid vs. patient ask across DMarket/CSFloat/CS.MONEY; Steam only if Wallet value is independently wanted. Optimize `P(sale by T)·NetAsk + (1−P)·NetFallback − CarryCost`.

## Proof of edge

Do not claim an edge from attractive screenshots. Required: golden-case float/probability agreement across all rule versions; price-disagreement, staleness and fee-accuracy statistics; complete-bundle fill rate, partial-fill frequency, orphan liquidation loss, venue failure/reversal rates; predicted vs. actual output value, days-to-sale and fees; settled fee-net P&L, realized ROI, capital-days, max drawdown, bootstrap CI on mean net ROI, broken out by collection/venue/contract type. Scale only while the lower confidence bound stays positive.

**Kill the strategy if:** complete >8% bundles rarely exist after revalidation; they can't be filled before listings vanish; fees and capital delays eat the edge; output sales land materially below assumptions; profits concentrate in a few lucky outcomes; orphan losses exceed contract profits; opportunity frequency can't justify the capital and complexity.

## First deliverable

Not a buyer — a ranked, timestamped table:

| Contract | Inputs | Cost | Net EV | Net ROI | P(profit) | Worst case | Capital-days | Verified | TUS parity |
|---|---|---|---|---|---|---|---|---|---|
| TU-001 | 10 | $42.10 | $5.38 | 12.78% | 66% | −$18.40 | 12 | Yes | Yes |
| TU-002 | 10 | $18.70 | $1.89 | 10.11% | 40% | −$9.20 | 9 | Yes | No |
| TU-003 | 5 | $410.00 | $29.00 | 7.07% | 22% | −$290.00 | 18 | Yes | Yes |

Only TU-001 qualifies. TU-002 is quarantined on parity disagreement; TU-003 fails the 8% gate despite positive expected dollars.

## Prior art

`twaldin/trade-up-bot` (closest comparison: CSFloat/DMarket/Skinport, exact combinations, normalized floats, staleness checks, shared-listing claims, 89 collections) — study its failure modes; don't inherit its fee assumptions or its habit of keeping negative-EV contracts for high hit probability. `HingedGuide/tradeup_optimizer` (MIT) — reuse structure and CSFloat adapter patterns, replace the raw-average math. Soniclev repo — reuse async adapters, retries, raw archival, Telegram, Docker; discard Steam automation, proxy rotation, undocumented endpoints, and MVP RabbitMQ/Redis.

The scanner is not the moat. The defensible asset is the dataset linking listing appearance → bundle availability → purchase feasibility → actual output → actual sale → settled fee-net return.
