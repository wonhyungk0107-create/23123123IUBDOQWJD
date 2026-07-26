# Status

**Generated from `artifacts/evidence/verification.json`, which is produced by running
the commands — not transcribed by hand.**

Last verified: 2026-07-26 · Python 3.12.13 · Windows 11 · commit at time of writing.

---

## Headline

**All mandatory groundwork acceptance gates pass.** The shadow-mode vertical slice
runs end to end offline, from pinned metadata import through operator-card generation
and persisted evidence.

**The crypto settlement rail is implemented and exercised.** Capital can be modelled
as funded and withdrawn in BTC/ETH/USDT (chain-native integer minor units), every
cross-currency valuation goes through a provenance-carrying `ConversionQuote`, and
the deposit→trade→withdraw round trip is priced and charged to each contract as
`settlement_cost`. The demo exercises the rail on a clearly-synthetic rate and fee
schedule; **no real venue's crypto fees have been sourced yet** and the rail ships
disabled.

**No economic evidence exists.** Settled fee-net profit is $0.00 across 0 ledger
events. Nothing has been bought, contracted, sold or settled, and no live marketplace
has been contacted with credentials.

---

## Verification commands and exit codes

Every command below was executed; the exit codes are real.

| Gate | Command | Exit |
|---|---|---|
| Format | `uv run ruff format --check .` | 0 |
| Lint | `uv run ruff check .` | 0 |
| Typecheck | `uv run mypy` (strict, 58 files) | 0 |
| Tests | `uv run pytest --cov --cov-report=json` | 0 |
| Coverage floors | `uv run python tools/check_coverage.py` | 0 |
| No placeholders | `uv run python tools/check_no_placeholders.py` | 0 |
| Migrations up | `uv run alembic upgrade head` (2 revisions) | 0 |
| Migrations down | `uv run alembic downgrade base` | 0 |
| Offline demo | `uv run tradeup demo --quiet` | 0 |

`make verify` (lint + typecheck + coverage + demo) exits 0. This pass ran on a
machine without `uv` on PATH; `tools/write_verification.py` executed the same tools
through the project venv's own launchers, and the exact commands are recorded in
`artifacts/evidence/verification.json`.

## Tests

**495 tests pass.** None requires the network. Hypothesis runs in `derandomize` mode.
The opt-in live suite (`pytest -m live`) additionally passed 6/6 on 2026-07-26 with
real credentials — see Live checks.

| Suite | Covers |
|---|---|
| `tests/unit/` | Money, crypto currencies, conversion quotes, settlement rail, items, rules, mathematics, fees, valuation, EV, policy, inventory, ledger, reservations, metadata, discovery, optimizer |
| `tests/property/` | Optimizer vs brute force, pruning soundness, float-budget and unique-asset invariants |
| `tests/contract/` | Adapter success, empty, missing fields, invalid float, unmapped rarity, schema change, 401, 429 + `Retry-After`, 5xx, timeout, disappearance, price change, DMarket signing, policy-blocked venues |
| `tests/golden/` | Seven hand-computed trade-up cases across both rule versions |
| `tests/integration/` | Migrations, idempotent ingestion, unique identity, immutable ledger, candidate replay, full demo, reproducibility |

## Coverage

Overall **86.1%** (floor 80%). Money-critical modules, floor 90%:

| Module | Branch coverage |
|---|---|
| `domain/conversion.py` | 100.0% |
| `valuation/capital.py` | 100.0% |
| `valuation/settlement.py` | 100.0% |
| `domain/money.py` | 99.4% |
| `domain/items.py` | 98.6% |
| `valuation/expected_value.py` | 98.1% |
| `domain/mathematics.py` | 96.6% |
| `optimizer/bundle.py` | 96.0% |
| `valuation/partial_fill.py` | 96.0% |
| `domain/rules.py` | 95.7% |
| `optimizer/pareto.py` | 94.9% |
| `execution/policy.py` | 93.0% |
| `valuation/exit_prices.py` | 92.6% |
| `domain/fees.py` | 91.5% |

Enforced by `tools/check_coverage.py`, which fails the build per module. A single
project-wide percentage would let an untested optimizer hide behind a well-tested
enum.

## Offline demo

`artifacts/evidence/demo-<timestamp>.{md,json,csv,parquet}`

- **1 candidate** cleared every gate; **57 rejected**.
- Rejection census: `QUOTE_TOO_OLD=28`, `BELOW_DISCOVERY_ROI=19`,
  `LISTING_DISAPPEARED=13`, `ASSET_ALREADY_RESERVED=12`,
  `LISTING_PRICE_CHANGED=12`, `BELOW_MIN_ABSOLUTE_PROFIT=2`.
- Data is **synthetic listings and prices over real pinned metadata**. Every artifact
  and the operator card itself say so.
- The **crypto settlement rail** runs on a synthetic BTC/USD rate (100,000) and
  invented fees: round-trip drag $64.60 on a $500 capital basis (12.92%), charged
  proportionally — the approved candidate pays $0.15 on its $11.58 acquisition and
  still clears at 61.31% net ROI. The card labels the rail synthetic and the haircut
  and amortisation horizon as priors.
- Two runs from clean state agree on every field except measured runtime.

## Metadata provenance

| | |
|---|---|
| Source | `ByMykel/CSGO-API` @ `342d49698a77ca5d2da69c4ecd236358c866a364` |
| Upstream SHA-256 | `7aeb9582…c3be32d7` (5,471,848 bytes) |
| Committed projection | `409522b7…8ae088d5` (1,455 entries) |
| Imports as | 1,455 skins / 94 collections, **0 errors** |

## Acceptance gates

| Gate | Status | Evidence |
|---|---|---|
| G1 Clean bootstrap | Pass | `uv sync --extra dev` from clean checkout |
| G2 Database | Pass | Alembic up and down, exit 0 |
| G3 Metadata | Pass | Pinned import, 0 errors, hash recorded |
| G4 Rules | Pass | 7 golden cases + property tests, both rule versions |
| G5 Adapters | Pass | Contract tests; every failure mode returns a typed refusal |
| G6 Optimizer | Pass | Matches brute force under Hypothesis; no duplicate assets |
| G7 Economics | Pass | EV, ROI, lower-bound, capital-days, partial-fill tested |
| G8 Revalidation | Pass | Price change, disappearance, float and identity mismatch, stale quote all reject |
| G9 Ledger | Pass | Append-only enforced; candidate replay reproduces identity |
| G10 Vertical slice | Pass | `tradeup demo` ingest → operator card, exit 0 |
| G11 Quality | Pass | Format, lint, mypy strict, tests, coverage floors, migrations |
| G12 Reproducibility | Pass | Two clean runs agree; runtime normalised (measurement, not result) |
| G13 No concealed incompleteness | Pass | `check_no_placeholders.py` over 58 source files |
| G14 No live risk | Pass | Live execution off, daily spend 0, no order ever placed |
| G15 Evidence | Pass | This file + `artifacts/evidence/verification.json` |

## Economic evidence

| Metric | Value |
|---|---|
| Settled fee-net profit | **$0.00** |
| Ledger events | 0 |
| Orders placed | 0 |
| Fills | 0 |
| Trade-ups completed | 0 |
| Sales / settlements | 0 / 0 |
| Real-market qualified opportunities | **0** |
| Simulated opportunities | 1 (synthetic prices) |

The one simulated opportunity comes from invented prices. It is a demonstration that
the software works, and it is not evidence about the market.

## Live checks

**First authenticated read-only contact: 2026-07-26, CSFloat.** With a real API key
configured, `pytest -m live` passed 6/6 (no skips) and direct probes exercised
`GET /api/v1/listings` and single-listing revalidation. Read-only throughout; no
order was or can be placed (`live_execution_enabled=false` is asserted inside the
live suite itself).

The first live read immediately falsified the hand-authored fixture shape — known
gap #8 doing its job. Live rows carry no `min_float`, `max_float` or `collection`;
identity is `item_name` + `paint_index`, and listings split into `buy_now` and
`auction` types. The adapter was rewritten to resolve identity, collection and
float caps from the pinned registry (fail closed on anything but exactly one
candidate) and to exclude auction rows, whose current price is a bid, not an ask.
Post-fix probe: 5 of 25 rows on a default page parsed as purchasable listings, all
20 drops counted by reason. Details in `docs/source-matrix.md` ("Live response
shape — VERIFIED 2026-07-26").

**DMarket remains unverified live** — keys are configured, but no live DMarket read
has been performed; its payload shapes are still hand-authored approximations.
SkinSnipe has no key and stays a typed refusal.

## Known gaps and unresolved blockers

1. **Covert contracts have no real metadata.** The pinned source contains no
   Extraordinary skins with collection membership, and the endpoint that does list
   knives carries no float caps. Covert candidates correctly fail closed; the
   five-input path is exercised only by golden fixtures.
2. **Both rulesets are `GOLDEN_FIXTURE_ONLY`.** They reproduce worked examples; no
   authoritative source check has been performed. Promotion needs a dated citation.
3. **Fee schedules are synthetic.** The only fee data in the repository is the demo's
   clearly-labelled invented schedule. Real venue fees must be sourced and dated
   before any live evaluation means anything. This now includes the **crypto rail**:
   DMarket documents crypto deposit/withdrawal in help-centre articles, but the
   article bodies returned 403 on direct fetch and every fee figure remains
   UNVERIFIED (see `docs/source-matrix.md`, "Crypto funding and withdrawal"). The
   rail ships disabled and fails closed on unknown fees.
4. **The survival model is a prior.** `bundle_completion_probability` derives from
   quote age, not measured fill rates. It is the least-evidenced number in the system
   and the operator card says so.
5. **Liquidity haircuts and days-to-sale are priors**, pending realised sales.
6. **DMarket's endpoint surface is disputed** between its Swagger and its
   documentation repository. No purchase call is implemented while that stands.
7. **CSFloat has no documented purchase endpoint**, so acquisition there is an
   operator action, not a configuration change.
8. **Adapter payload shapes: CSFloat verified live, DMarket not.** The CSFloat
   fetch/verify shape was corrected against a real authenticated response on
   2026-07-26 (see Live checks). DMarket parsing is still pinned against
   hand-authored approximations and needs its own read-only live run.
9. **No PostgreSQL run has been performed.** The compose profile exists; only SQLite
   has actually been exercised.
10. **Docker has not been built here** — no Docker daemon in this environment. The
    `docker` CI job builds and runs it; that job has not yet executed.

## The single highest-value next task

**Run the read-only shadow scanner against CSFloat with a real API key and record
listing survival at 30s / 5m / 1h.**

Everything above is instrumentation. The one thing that would move this from
"software that works" to "evidence about whether an edge exists" is measuring how
many complete bundles clear 8% net ROI after direct revalidation, and how often those
listings are still buyable minutes later.

That measurement also replaces the least-evidenced number in the model — the bundle
completion prior — with an observation. Until it exists, every ROI figure this system
produces rests on a guess about fill rates.
