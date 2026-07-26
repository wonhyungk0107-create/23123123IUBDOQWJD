# Decision log

Each entry records what a decision de-risks, what it enables, what would make it
unnecessary, and what kind of evidence it produced. Code evidence is the weakest
kind; settled-profit evidence is the only kind that validates the hypothesis.

---

## D-001 — Integer minor units with a balance type, not Decimal dollars

**Uncertainty removed.** Whether Steam Wallet value, venue credit and withdrawable
cash can be silently added together. They cannot: `Money` raises.

**Enables.** A settled-P&L figure that means one specific thing.

**Would make it unnecessary.** Nothing. A system whose objective is cash cannot treat
non-cash as cash.

**Evidence.** Code.

---

## D-002 — `Money` carries arithmetic; `MonetaryObservation` carries provenance

**Uncertainty removed.** Whether every amount needs a timestamp and venue. Requiring
it on the arithmetic type would make every intermediate sum carry meaningless
provenance; omitting it entirely would lose the market origin.

**Enables.** Cheap total arithmetic, with provenance mandatory on anything that came
from a market.

**Evidence.** Code.

---

## D-003 — Exact `Fraction` for probabilities and normalised floats

**Uncertainty removed.** Whether `Σ P(y) = 1` holds. With rationals it holds exactly,
so the invariant is a property of the arithmetic rather than a tolerance we picked.

**Enables.** Probability-weighted EV that rounds once, downward, instead of
accumulating ten favourable roundings.

**Would make it unnecessary.** Nothing; the cost is negligible.

**Evidence.** Code, plus golden cases with hand-computed values.

---

## D-004 — Rules as a versioned, timestamp-resolved registry

**Uncertainty removed.** What happens when Valve changes the mechanics. Answer: a new
ruleset row, and every stored result still says which rules produced it.

**Enables.** Reasoning about historical candidates after a rule change.

**Would make it unnecessary.** Certainty that the mechanics will never change again.

**Evidence.** Code. Both shipped rulesets are `GOLDEN_FIXTURE_ONLY` — they reproduce
worked examples but no authoritative source check has been performed.

---

## D-005 — Pin a commit and commit a projection, not the whole payload

**Uncertainty removed.** Which metadata backed a decision. The manifest records the
upstream commit and the original 5.47 MB payload's SHA-256; the repository carries a
375 KB projection of the fields we consume.

**Enables.** "Diff every update" as a procedure — re-download the commit, re-run the
tool, byte-compare.

**Would make it unnecessary.** An upstream API with stable versioned releases.

**Evidence.** Code. Imports 1,455 skins across 94 collections with 0 errors.

---

## D-006 — Adapter defaults are refusals

**Uncertainty removed.** Whether an unsupported operation can silently fall through
to something else. It cannot: there is no inherited behaviour to fall through to.

**Enables.** The Steam boundary as a structural property rather than a convention.

**Would make it unnecessary.** Nothing.

**Evidence.** Code, plus contract tests asserting each refusal type.

---

## D-007 — Per-collection DP over Pareto frontiers, not OR-Tools

**Uncertainty removed.** Whether exact bundle selection is tractable. Ten of four
hundred listings is 2.6 × 10¹⁹ subsets; the collection constraints are separable, so
the frontier fold is exact and fast.

**Enables.** Sweeping many compositions and float budgets per scan.

**Would make it unnecessary.** A measured benchmark showing the custom solver is
inadequate. Until then, adding a solver dependency is unjustified.

**Evidence.** Code, plus property tests comparing against brute force on randomised
instances.

---

## D-008 — Dominance pruning requires *k* dominators, not one

**Uncertainty removed.** Whether pruning is sound for an exactly-*k* selection. With
one dominator it is not: the dominated listing is still needed whenever its dominator
is also selected. With *k*, a swap is always available.

**Enables.** Aggressive pruning that provably preserves the optimum.

**Evidence.** Code, plus a property test that brute-forces the pruned and unpruned
pools and compares.

---

## D-009 — Solve at wear breakpoints rather than sweeping the float budget

**Uncertainty removed.** Where output value can change. Only at wear boundaries, so
those are the only budgets worth solving — offered just *below* each, since a float
exactly on a boundary lands in the worse band.

**Evidence.** Code.

---

## D-010 — Sequence purchases most-fragile-first

**Uncertainty removed.** How to minimise committed capital at the moment a bundle
fails. Buying the most fragile listing last means learning it is gone only after
paying for everything else.

**Enables.** A failure tree with an exposure figure at every step, printed on the card.

**Evidence.** Code. The survival model is an explicit prior, and it is the number the
operator card flags as least evidenced.

---

## D-011 — Ledger is append-only, enforced at the mapper

**Uncertainty removed.** Whether history can be rewritten when a number becomes
inconvenient. `before_update` and `before_delete` raise; corrections are `REVERSAL`
events.

**Evidence.** Code, plus persistence tests asserting both raise.

---

## D-012 — Demo uses synthetic prices over *real* metadata

**Uncertainty removed.** Whether the demo exercises the real identity, float-cap and
output-pool paths. It does, because the metadata is the pinned production snapshot.
Prices are invented, so the demo proves software behaviour and nothing about markets.

**Enables.** An end-to-end deterministic run with no network and no credentials.

**Would make it unnecessary.** Recorded live market data — which would need
credentials and would stop being deterministic.

**Evidence.** Code. Explicitly **not** market evidence, and every artifact says so.

---

## D-013 — The demo's ledger reports zero rather than simulated events

**Uncertainty removed.** Whether to populate the ledger section with plausible
purchase and sale events so it looks exercised. It is not populated: nothing was
bought, so the honest value is zero events and $0.00.

**Enables.** A status report that cannot be misread as evidence of trading.

**Evidence.** Ledger mechanics are proven by tests instead.

---

## D-014 — "Master" is not mapped to `EXTRAORDINARY`

**Uncertainty removed.** What upstream's `Master` rarity actually is. Verified against
`collections.json` at the pinned revision: it is the agent/character rarity. An
earlier draft of the importer mapped it to `EXTRAORDINARY`, which would have placed
agent characters into knife output pools and produced confidently wrong Covert
economics.

**Enables.** Correct rejection of Covert contracts, since no genuine knife metadata is
available (see D-015).

**Evidence.** Code, plus a regression test asserting `master` is absent from the
mapping.

---

## D-015 — Covert contracts fail closed rather than being approximated

**Uncertainty removed.** Whether five-input Covert contracts can be evaluated from the
pinned source. They cannot: it contains no Extraordinary skins with collection
membership, and `collections.json` — which lists knives — carries no float caps.

**Enables.** Honest coverage: the Covert path is exercised by golden fixtures only,
and real Covert candidates are rejected on an empty output pool.

**Would make it unnecessary.** A source stating knife/glove collection membership
*and* float caps.

**Evidence.** Code. Recorded as a known gap in `docs/rule-registry.md` and `CLAUDE.md`
rather than papered over.

---

## D-016 — "Gone" and "repriced" are separate revalidation outcomes

**Uncertainty removed.** Found during demo wiring: a repriced listing revalidates with
status `PRICE_CHANGED`, which is not `is_purchasable`, so the gate was classifying it
as `LISTING_DISAPPEARED` and skipping the price comparison entirely.

**Enables.** A revalidation census that distinguishes vanishing supply from price
movement — the exact statistic a shadow run exists to measure.

**Evidence.** Code, plus a policy test asserting a price change does *not* report as a
disappearance.

---

## D-017 — Crypto settlement is a priced rail, not a new balance type

**Uncertainty removed.** How to make "cash-withdrawable" mean anything when the
operator's capital enters and exits venues as BTC. Options were a new balance type,
a parallel crypto ledger, or pricing the rail. A balance type would have forced
every existing gate to reason about crypto; pricing the rail keeps the optimizer and
gates in the fiat base currency and charges each contract its share of the round
trip (deposit/withdrawal fees, FX spread, on-chain network fees both ways, and a
volatility haircut) through `settlement_cost` in `all_in_cost`.

**Enables.** BTC/ETH/USDT amounts as exact integer minor units at chain-native
resolution (satoshi/wei/micro-USDT); explicit, provenance-carrying `ConversionQuote`
conversion with pessimistic rounding; a per-contract settlement charge proportional
to the capital a contract actually uses, so a $12 demo contract is not billed a
$500 block's drag.

**Would make it unnecessary.** An operator who funds and withdraws exclusively in
the fiat base currency — in which case the rail stays disabled and the charge is an
explicit zero.

**Evidence.** Code, hand-computed golden round trip in `tests/unit/test_settlement.py`,
and the demo artifact's `crypto_settlement` section (synthetic rate and fees,
labelled as such). The volatility haircut and amortisation horizon are stated
priors; no real venue's crypto fee schedule has been sourced yet, and the source
matrix records exactly that.
