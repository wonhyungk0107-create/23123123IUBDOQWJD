# Architecture

## The shape of the problem

A trade-up contract consumes N exact assets and produces one random output. Whether
that is profitable depends on facts that a condition-level calculator cannot see:
which specific listings exist right now, at what exact floats, at what all-in cost
after every venue fee, whether they can all still be bought by the time you reach the
tenth one, and what the output will actually fetch net of selling fees weeks later.

The system is therefore organised around **exact assets** and **settled cash**, and
every layer is built to fail closed rather than to produce a plausible number.

## Data flow

```
                    pinned metadata snapshot (ByMykel, commit-pinned, hashed)
                                    │
   marketplace adapters ────────────┼──────────► raw payload hashes (evidence)
   (CSFloat, DMarket, fixture)      │
            │                       ▼
            └──────────────► normalisation
                             registry is authoritative for float caps;
                             venue disagreement ⇒ drop the listing
                                    │
                                    ▼
                       composition enumeration        float breakpoints
                       (collection shapes, bounded    (wear boundaries, where
                        by actual supply)              output value can change)
                                    │                        │
                                    └────────┬───────────────┘
                                             ▼
                                  exact-asset optimizer
                          per-collection DP over cost/float Pareto
                          frontiers, folded on the shared float budget
                                             │
                                             ▼
                        outcome projection + conservative valuation
                        (probabilities, output floats, exit prices)
                                             │
                                             ▼
                                    expected value
                        costs up, revenue down, exact rational weighting
                                             │
                                             ▼
                                  discovery gate (looser ROI)
                                             │
                                             ▼
                                  reservation (all-or-nothing)
                                             │
                                             ▼
                     direct revalidation — requery every listing at its venue
                                             │
                                             ▼
                                   final gate (strict ROI)
                                             │
                          ┌──────────────────┴──────────────────┐
                          ▼                                     ▼
                  operator action card                  rejection + reason codes
                          │                                     │
                          └──────────────┬──────────────────────┘
                                         ▼
                            persistence + immutable ledger
```

## Why it is layered this way

**`domain/` performs no I/O.** No network, no clock, no configuration. Everything
money-critical is a pure function of its inputs, which is what makes an evaluation
reproducible from persisted data months later — and what makes the maths testable
without a fixture server.

**Adapters return typed refusals rather than raising.** `CapabilityResult` carries
either data or a specific reason. This is the structural guarantee behind the Steam
boundary: `UNSUPPORTED` has no code path leading to an alternative mechanism, so
there is nowhere for a fallback to hide.

**The optimizer never enumerates listing combinations.** Ten of four hundred is
2.6 × 10¹⁹ subsets. The collection constraints are separable — they interact only
through one shared float budget — so each collection contributes a Pareto frontier of
(total float, minimum cost) and the frontiers fold together over that single
dimension. Exact, not heuristic, and milliseconds rather than never.

**Value is a step function, so the float budget is swept at its steps.** Output value
barely moves as the average float creeps up, then falls off a cliff at a wear
boundary. Solving at each breakpoint (and just below it) covers every distinct answer
without a continuous search.

**Revalidation sits between two gates.** The discovery gate uses a looser ROI bar
precisely so there is room for drift between the scan and the requery. The final gate
runs on revalidated data. Evaluating once and checking twice would let a price that
moved mid-scan pass a bar it no longer deserves.

**Reservations lock the listing, not the contract.** One listing satisfies many
candidate contracts. Claims are all-or-nothing per bundle, backed by a partial unique
index on active reservations, because a half-reserved bundle cannot complete *and*
blocks the candidate that could have.

## Injected dependencies

| Dependency | Injected as | Why |
|---|---|---|
| Time | `Clock` protocol | Quote staleness, trade locks and capital-days are economic quantities derived from "now". The demo must reproduce exactly. |
| HTTP | `Transport` protocol | `FixtureTransport` means no mandatory test touches the network. |
| Sleep / jitter | `RestClient.sleeper`, `random_fn` | Retry behaviour must be assertable. |
| Fees | `FeeSchedule` | Fees are versioned data, not constants. |
| Rules | `TradeupRuleSet` | Valve changes the mechanics; the code must not need editing when they do. |

## Persistence

SQLite by default and PostgreSQL through the same repository layer. Money is stored
as integer minor units; exact decimals (floats, ROI, probabilities) are stored as
text, because SQLite has no exact decimal type and a `REAL` column would reintroduce
binary error at the point it is least affordable.

The ledger is append-only, enforced by mapper-level `before_update` and
`before_delete` guards. Corrections are `REVERSAL` events. The value of a ledger is
that it cannot be quietly rewritten when a number becomes inconvenient.

## What is deliberately absent

- No message broker, no Kubernetes, no microservices. A single process contends with
  nothing; a broker would be ceremony around a problem that does not exist yet.
- No dashboard. The vertical slice has to work before anything renders it.
- No OR-Tools. The custom solver is exact and fast on realistic inputs; adding a
  dependency needs a measured benchmark, not a preference.
- No LLM anywhere in the money path.
