# CS2 Trade-Up Procurement Agent

Exact-asset, multi-market procurement optimizer for CS2 trade-up contracts. Measures
**executable, fee-net, lock-adjusted, settled** profit.

Path-specific rules live in `.claude/rules/`. Read the one covering the code you are
touching — they carry the detail this file deliberately omits.

## Economic objective

Settled, attributable, fee-net, **cash-withdrawable** profit. Nothing else counts.

Distinguish these states and never conflate them: displayed opportunity → positive
candidate → directly revalidated candidate → simulated fill → purchased → contracted
→ sold → **settled**. Only the last is profit.

Cash, Steam Wallet value, reusable venue credit, promotional credit, pending
proceeds and inventory marks are not interchangeable. `Money` enforces this: it
refuses arithmetic across balance types.

## Safety and execution boundaries

**Never automate Steam.** No Community Market orders, no trade-offer acceptance, no
inventory polling, no session cookies, no mobile-authenticator handling, no browser
automation, no proxy rotation, no CAPTCHA bypass. Steam Subscriber Agreement §4.C
("Automation") prohibits it. The trade-up is performed by a human following an
operator card.

**Never call an undocumented endpoint.** If a browser does it and the docs do not
describe it, we do not use it.

**No LLM in the money path.** Discovery → validation → optimizer → EV → risk policy →
execution is deterministic. An LLM may explain a failure or draft prose; it never
computes a float, probability, fee or buy decision.

**Live execution is off.** `live_execution_enabled=false`, `max_daily_spend=0`. The
settings model refuses a spend budget without the execution switch.

**Never store** Steam passwords, session cookies, `steamLoginSecure`, mobile-auth
secrets or extension cookies.

## Architecture

```
marketplace adapters → normalisation (registry is authoritative for float caps)
→ composition enumerator → exact-asset optimizer → outcome + fee-net valuation
→ discovery gate → reservation → direct revalidation → final gate
→ operator card → immutable ledger
```

| Package | Responsibility |
|---|---|
| `domain/` | Immutable models + pure maths. No I/O, no config, no clock. |
| `metadata/` | Pinned snapshot import, registry, validation. |
| `adapters/` | One protocol per venue; every operation returns `CapabilityResult`. |
| `discovery/` | Collection compositions and float breakpoints. |
| `optimizer/` | Cheapest bundle via per-collection DP over cost/float Pareto frontiers. |
| `valuation/` | Fees, exit prices, capital-days, partial-fill risk, EV. |
| `execution/` | Risk gates and listing reservations. Never spends. |
| `pipeline/` | The scan, end to end. |
| `reporting/` | Operator cards; console, Markdown, JSON, CSV, Parquet. |
| `persistence/` | SQLAlchemy + Alembic. Append-only ledger. |

Time is injected (`clock.py`). Nothing outside it calls `datetime.now`.

## Canonical commands

```bash
make bootstrap    # uv sync --extra dev
make verify       # lint + typecheck + coverage floors + demo   <- the gate
make demo         # deterministic offline run, no network, no orders
make test         # pytest
make coverage     # pytest --cov + tools/check_coverage.py
make migrate      # alembic upgrade head
make live-smoke   # opt-in, read-only; skips without credentials
```

Without GNU make, run the `uv run …` command under each target in the `Makefile`.

CLI: `tradeup doctor` (is anything wired up?), `metadata import|validate`,
`rules validate`, `listings ingest|verify`, `candidates scan|explain|export`,
`ledger reconcile`, `demo`, `capabilities`.

## Money and Decimal rules

1. Money is integer minor units + currency + balance type. No float, ever.
2. Cross-currency or cross-balance-type arithmetic raises. Do not work around it.
3. Costs round up; revenue rounds down. Ambiguity resolves pessimistically.
4. Item floats are `Decimal`; probabilities and normalised floats are `Fraction`.
   `Σ P(y) == 1` exactly, not within a tolerance.
5. Probability-weighted money accumulates exactly and rounds **once**, downward.
6. An unknown material fee raises `UnknownFeeError` and rejects the candidate. A zero
   fee must be a sourced statement, never a failed lookup.
7. Exact decimals persist as text. There is no `REAL` column on the money path.
8. Cross-currency value moves only through `ConversionQuote` (rate + venue + source
   + timestamp; exact rationals; costs convert rounding up, proceeds rounding down).
   Crypto currencies use chain-native minor units — satoshi, wei, micro-USDT. The
   crypto settlement rail (`valuation/settlement.py`) prices the capital round trip
   and charges contracts `settlement_cost`; it is off by default, its haircut and
   amortisation horizon are stated priors, and no real venue's crypto fees have
   been sourced yet (see `docs/source-matrix.md`).

## Trade-up rules are versioned data

`N` is 10, or 5 for eligible Covert contracts. Souvenir handling changes by version.
Never hard-code these — resolve a `TradeupRuleSet` by timestamp and record its
`rule_version` on every result. Resolution fails closed on zero or overlapping
matches.

Item identity, float caps and output pools come from the pinned metadata registry.
Never infer what an item is from its display name.

**Known gap:** the pinned ByMykel revision contains no Extraordinary (knife/glove)
skins with collection membership, so Covert→Extraordinary contracts have no output
pool and correctly fail closed. Fixing this needs a source that states knife
collection membership *and* float caps.

## Test requirements

- No mandatory test touches the network. Use `FixtureTransport` / `FixtureMarketAdapter`.
- Hypothesis runs in `derandomize` mode — same examples every run.
- Golden cases in `tests/fixtures/golden_tradeups.json` carry hand-computed values.
  Never regenerate them from the implementation.
- Modules in `tools/check_coverage.py` must hold ≥90% branch coverage; the whole
  project ≥80%. `make coverage` enforces both.
- `tools/check_no_placeholders.py` fails on TODO/FIXME, stub bodies and
  commented-out code in `src/`.

## Source-adapter policy

| Venue | Execution | Note |
|---|---|---|
| ByMykel CSGO-API | n/a | Metadata. Pinned commit + payload hash; diff every update. |
| CSFloat | `UNSUPPORTED` | Read/verify only. **No documented purchase endpoint exists.** |
| DMarket | `AUTOMATED`, gated | Ed25519 signing. Purchase unimplemented while two official sources disagree on the endpoint surface. |
| SkinSnipe | `UNSUPPORTED` | Price reference only. Never execution truth. |
| Skinport | n/a | Keyless documented API. Completed-sale price evidence + sourced exit fees; 8 req/5 min, Brotli required. Never execution truth. |
| TradeUpSpy | manual | No API; `robots.txt` disallows the calculator paths. Parity runs off manual exports. Its discovery strategy is reimplemented licitly as `candidates prospects` (registry + Skinport `/v1/items`); never scrape their site. |
| CS.MONEY, SkinSwap | operator | Manual link only. |
| Steam | `POLICY_BLOCKED` | Human only. |
| "Skins Money" | `POLICY_BLOCKED` | Service never identified. Stays disabled. |

See `docs/source-matrix.md` for what is verified and what is not.

## Definition of completion

A change is done when `make verify` passes: ruff format check, ruff lint, mypy
strict, the full test suite, both coverage floors, and the offline demo — plus the
demo producing identical results across two clean runs.

## Claiming results

**Never claim profitability without settled evidence.** Report settled fee-net
profit, orders, fills, trade-ups, sales and settlements explicitly, even when every
one is zero. Zero is a real result: it constrains opportunity frequency.

A passing candidate in the demo is a software demonstration over synthetic prices. It
says nothing about the market. Label synthetic data as synthetic everywhere it
appears, and state which numbers are uncalibrated priors rather than measurements.

Rejection reason codes are the dataset. Collect every failing reason rather than
short-circuiting, and never drop one to tidy a summary.
