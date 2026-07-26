# Third-Party Provenance and Licence Audit

**Date:** 2026-07-25
**Project licence:** Proprietary.

---

## Statement of provenance

> **NO code has been copied from any of the repositories reviewed in this
> document into this project.**
>
> **All domain logic — float normalisation, outcome-probability derivation,
> composition enumeration, the bundle optimiser, the fee and capital model, and
> the risk gates — was implemented from first principles from the mathematics and
> rules documented in `CLAUDE.md` and `docs/rule-registry.md`.**
>
> **The repositories below were reviewed as references only: to understand the
> problem domain, to learn from other people's failure modes, and to avoid
> repeating their mistakes. Reading a repository is not reuse. No file, function,
> class, type definition, test fixture, configuration value, constant, or schema
> from any of them appears in this codebase.**

This distinction matters legally as well as ethically. Two of the four
repositories below carry **no licence at all**, which under default copyright law
means all rights are reserved and copying would be infringement. The provenance
statement above is therefore not a courtesy — it is the reason this project is
distributable.

**Scope of this audit.** Licence facts were verified on 2026-07-25 against the
GitHub REST API `license` field and, where a licence file exists, against the raw
licence text. Descriptive claims about what each project does come from its
README. Where something could not be verified, it is marked **UNVERIFIED** with
the reason.

**A note on "ideas".** Copyright protects expression, not ideas, algorithms, or
facts. Learning that a rival tool integrates three marketplaces, or that it
refreshes on a twenty-minute cycle, is not a licensed act. But the boundary is
only safe if it is respected strictly: no transcription, no "port to Python", no
copying of constants or structure. Where a design idea below was worth adopting,
it was re-derived and re-specified against our own requirements, and in most cases
substantially changed in the process.

---

## 1. Soniclev/steam_csmoney

**URL:** <https://github.com/Soniclev/steam_csmoney>
**Description (from GitHub):** "A system for monitoring items prices between
CS.MONEY and the Steam market"
**Language:** Python · **Stars:** 80 · **Created:** 2022-09-14 · **Last push:**
2024-05-22 · Not archived.

### Licence

**NONE. No licence of any kind.** (**VERIFIED**)

- The GitHub API returns `"license": null` for the repository.
- The repository root contains no `LICENSE`, `LICENCE`, `COPYING`, or `NOTICE`
  file. Root contents are: `.dockerignore`, `.gitignore`,
  `.pre-commit-config.yaml`, `Dockerfile`, `README.md`, `Taskfile.yml`, `bot.py`,
  `csmoney_parser.py`, `docker-compose.yml`, `logging.yaml`, `poetry.lock`,
  `prod.env`, `pyproject.toml`, `steam_parser.py`, `worker.py`, plus directories
  `.github`, `common`, `images`, `price_monitoring`, `proxy_http`, `tests`,
  `utils`.

### Proprietary use permitted?

**NO.** Absent an express licence, the author retains all rights by default under
copyright law. Public visibility on GitHub grants only the rights in GitHub's
Terms of Service — essentially to view and to fork **within GitHub** — and
confers no right to copy the code into another project, proprietary or otherwise.
Copying anything from this repository would be infringement.

**Code copied:** **None.**

### Ideas worth reusing

These are architectural patterns, independently re-implemented against our own
requirements:

- **Async parser/worker separation.** Independent collectors feeding workers via a
  queue, so a slow or failing source cannot stall the others. We adopted the shape
  and rejected the transport — see below.
- **Retry and failure resilience as a first-class concern** rather than an
  afterthought bolted onto the HTTP client.
- **Raw-response archival before parsing.** Keeping the payload lets you re-derive
  history after a schema change instead of losing it. In our system this becomes
  the raw-event archive and `raw_payload_hash`, and it is the foundation of the
  dataset that is the actual defensible asset here.
- **Telegram as the operator channel with a user whitelist.** Right choice for a
  single-operator system: no web UI to secure, no session management, push
  notification for free.
- **Docker / Docker Compose deployment** for reproducible environments.
- **Discipline signals worth matching:** near-100% unit-test coverage,
  `pylint`/`mypy`/`black` in CI, `pre-commit` hooks. Our equivalent is
  `ruff`/`mypy`/`pytest` under `make verify`.

### What must NOT be inherited

- **Steam automation.** The repository parses the Steam market programmatically.
  This is the exact boundary this project refuses to cross. See
  `docs/source-matrix.md` §8 for the Steam Subscriber Agreement §4.C "Automation"
  language. The risk is not a fine, it is irrecoverable loss of the account and
  the entire skin inventory.
- **Proxy rotation.** The project supports HTTP, SOCKS4 and SOCKS5 proxies with
  separate proxy pools for Steam and CS.MONEY. Rotating proxies exists for exactly
  one purpose: to evade a rate limit or a block that the operator applied
  deliberately. Adopting it would mean the system's economics depend on evading
  controls — which is both a compliance failure and a fragile foundation, since
  the edge would vanish the moment detection improved. Explicitly forbidden in
  `CLAUDE.md`.
- **Undocumented CS.MONEY endpoints.** CS.MONEY publishes no public API
  (`docs/source-matrix.md` §6); anything this project calls is therefore reverse-
  engineered. Undocumented endpoints carry no stability contract, no terms
  coverage, and no recourse when they change silently — which for a system moving
  real money is a correctness risk before it is a compliance one.
- **RabbitMQ and Redis at MVP.** Justified for their fan-out topology; unjustified
  for a single-worker monolith. Two extra network services, two extra failure
  modes, and a durability story that has to be reasoned about — in exchange for
  nothing until multiple workers actually contend over listing reservations. We
  use SQLite plus in-process asyncio and will revisit only when reservation
  contention is real.
- **Its objective.** This is a *price-difference monitor*: it reports spreads. It
  does not model fees, trade locks, capital-days, partial-fill risk, or settlement.
  A displayed spread is not an executable profit. Inheriting its framing would
  reproduce precisely the error this project exists to avoid.

---

## 2. twaldin/trade-up-bot

**URL:** <https://github.com/twaldin/trade-up-bot>
**Description (from GitHub):** "Finds profitable trade-up contracts using real
marketplace listings in CS2"
**Language:** TypeScript · **Stars:** 4 · **Created:** 2026-03-16 · **Last push:**
2026-07-24 (actively developed).

### Licence

**NONE. No licence of any kind.** (**VERIFIED**)

- The GitHub API returns `"license": null`.
- No `LICENSE`, `LICENCE`, `COPYING`, or `NOTICE` file at the repository root.
  Root contents are: `.env.example`, `.gitignore`, `AGENTS.md`, `CLAUDE.md`,
  `README.md`, `components.json`, `index.html`, `package-lock.json`,
  `package.json`, `tsconfig.json`, `user_acquisition.md`, `vite.config.ts`, plus
  directories `.github`, `discord-bot`, `public`, `scripts`, `server`, `shared`,
  `src`, `tests`.

### Proprietary use permitted?

**NO.** All rights reserved by default. Same analysis as above.

Two additional reasons for care with this one specifically. It is the **closest
direct comparison** to this project, which makes accidental convergence easy to
allege and harder to disprove — so the separation must be visibly strict. And the
presence of `user_acquisition.md` indicates a commercial product, meaning the
author has an active interest in their code not being reused.

**Code copied:** **None.** This project is Python; that one is TypeScript. No
file, type, constant, or test fixture was transcribed, ported, or translated.

### Ideas worth reusing (as validation of our own design, mostly)

- **Normalised float maths, which it gets right.** Its README states:
  `outputFloat = outputMin + avg(normalizedInputFloats) * (outputMax - outputMin)`.
  This is the correct formula and it independently corroborates our `z_i` / `z̄`
  derivation in `CLAUDE.md`. Corroboration of a formula that is publicly
  documented game mechanics is not derivation from their code.
- **Targeting condition boundaries.** Concentrating search near wear-tier
  breakpoints, where a small float change produces a large price change, is where
  the value is. Our composition enumerator derives normalised-float breakpoints
  per composition for the same reason.
- **Listing-staleness verification as an explicit pipeline stage,** with removal
  cascading to every affected candidate. Confirms our design: revalidate every
  listing ID directly at the execution gate, and treat a vanished listing as a
  first-class event.
- **Claimed/reserved listings excluded from discovery.** One listing can appear in
  many candidate contracts. Our answer is the reservation state machine
  (`UNCLAIMED → SOFT_RESERVED → PURCHASE_PENDING → …`) locking the underlying
  listing rather than the displayed contract.
- **Time-bounded parallel discovery workers** so a scan cycle has a bounded
  deadline.
- **Collection coverage of 89 collections** including knife and glove pools —
  useful as a sanity check that our registry's collection count is in the right
  region. Our mappings are built from ByMykel metadata with explicit knife/glove
  mappings, never inferred from display names.

### What must NOT be inherited

- **Its fee constants.** The README cites buyer fees of CSFloat 2.8% + $0.30,
  DMarket 2.5%, Skinport 0%, and seller fees from 2% (CSFloat/DMarket) to 12%
  (Skinport). **These are UNVERIFIED third-party assertions and must not be
  adopted as configuration.** No venue in our matrix publishes a machine-readable
  fee schedule in its API docs (`docs/source-matrix.md`, unresolved question 4);
  every fee here is versioned data carrying `source` and `last_verified`, and must
  be re-confirmed against the venue immediately before purchase. Copying a rival's
  fee table is how you build a model that is confidently wrong.
- **Keeping negative-EV contracts.** The README states: "Trade-ups with >25%
  chance to profit are kept even with negative EV." This is the single most
  important failure mode to avoid, and it is worth being precise about why. A high
  probability of a small win paired with a small probability of a large loss has a
  seductive hit rate and a negative expectation. Run it repeatedly and it converges
  to ruin — faster than it feels like it should, because the losses land on the
  same capital that funds the next attempt. Our gate is `ROI_net ≥ 8%` on
  **expectation, net of every modelled cost**, with `P(profit)` recorded as
  diagnostic context and never as a substitute. `WorstCase` is computed and
  bounded separately.
- **Displayed profit as the objective.** As with the previous project, gross
  spread is not settled profit. Our objective is cash-withdrawable settled profit
  after fees, FX, withdrawal, liquidity haircut, partial-fill risk and capital
  carry.
- **Any assumption that a discovered bundle is an obtainable bundle.** A contract
  is a basket order with no atomic cross-venue execution. Partial-fill risk must be
  subtracted *before* the first purchase.

---

## 3. HingedGuide/tradeup_optimizer

**URL:** <https://github.com/HingedGuide/tradeup_optimizer>
**Language:** Python · **Stars:** 2 · **Created:** 2025-12-04 · **Last push:**
2026-07-17.

### Licence

**MIT License.** (**VERIFIED**)

- GitHub API: `spdx_id: "MIT"`, `name: "MIT License"`, `key: "mit"`.
- Raw `LICENSE` file confirms: **"Copyright (c) 2025 Ties Kuijpers"**.

### Proprietary use permitted?

**YES.** MIT is permissive and expressly allows use, copying, modification,
merging, publication, distribution, sublicensing and sale, including within
closed-source proprietary software. The **only** obligation is that "The above
copyright notice and this permission notice shall be included in all copies or
substantial portions of the Software." The software is provided "AS IS" with no
warranty.

**Practical consequence:** this is the one repository here from which we *could*
lawfully have taken code, provided we reproduced the copyright and permission
notice. **We did not.** Should any future contributor wish to, the attribution
obligation must be honoured in this file and the fact recorded in the table below
— and it would need to survive review of the concerns listed under "must not be
inherited", which is the harder bar.

**Code copied:** **None.**

### Ideas worth reusing

- **Module layout.** A clean separation of `cli.py` (entry point), `core/`
  (trade-up mathematics and expected value), `data_sources/` (API clients), and
  `models.py` (Pydantic data structures). Our `src/` tree follows the same
  instinct — domain, metadata, adapters, discovery, optimizer, valuation,
  execution, inventory, ledger, monitoring — with finer granularity because our
  problem has more moving parts.
- **Pydantic for typed boundary models.** Same choice, independently correct: parse
  untrusted API payloads into validated types at the edge rather than passing
  dictionaries around.
- **ByMykel as the metadata source and CSFloat as the price source** — the same
  pairing this project uses, arrived at for the same reasons (ByMykel is the only
  usable free metadata dump; CSFloat is the only float-exact source with real
  documentation).
- **Bulk listing retrieval from CSFloat** rather than per-item lookups, to stay
  within an unknown and unpublished rate limit.
- **The "inverse" framing.** Working backwards from a desired output to the maximum
  admissible average input float is a genuinely useful reframing, and it is the
  same insight our composition enumerator uses when it derives normalised-float
  breakpoints per composition before asking the optimiser for the cheapest bundle
  satisfying `z_max`. We reached it independently from the constraint structure,
  but it is worth acknowledging that this project expresses it clearly.

### What must NOT be inherited

- **Its float mathematics.** The README states the formula as
  `OutcomeFloat = (AvgInput * (Max - Min)) + Min` where `AvgInput` is the average
  of **raw** input floats. **This is wrong.** The correct formula normalises each
  input against *its own* skin's float range before averaging:

  ```
  z_i = (f_i − a_i) / (b_i − a_i)          # per-input normalisation
  z̄   = (1/N) Σ z_i
  f_y = a_y + z̄ · (b_y − a_y)
  ```

  The two agree only in the degenerate case where every input skin shares
  identical float bounds. In every realistic mixed-collection contract they
  diverge, and the divergence is largest for exactly the skins with unusual caps —
  which are disproportionately the ones worth trading up. A raw-average model will
  predict the wrong output wear tier and therefore the wrong output price, and it
  will do so most often on the candidates it rates most attractive. This is a
  silent, systematically biased error, which is the worst kind. `CLAUDE.md`
  explicitly forbids hard-coding "raw average float determines output". Our
  implementation normalises per input, and golden-case tests assert output floats
  against known contracts across every rule version.
- **Its fee assumptions.** The README motivates the tool with "CSFloat (~2% fee)"
  versus "Steam takes a ~15% fee". Directionally plausible, **UNVERIFIED**, and not
  usable as configuration. Same treatment as every other third-party fee number:
  it goes in the versioned fee schedule with a source and a verification date, or
  it does not enter the model.
- **Steam Community Market as a live pricing fallback.** Reasonable for a
  calculator; wrong for us on two counts. Steam Wallet proceeds are not
  cash-withdrawable, so a Steam price is not a valid `V_net` for our objective
  unless Wallet balance is independently wanted. And automated SCM interaction is
  outside our boundary regardless.
- **Calculator framing generally.** This is a tool that answers "is this trade-up
  profitable on paper?" That is a strictly easier question than "can I actually
  buy all ten of these right now, receive them, contract them, sell the output, and
  end up with more withdrawable cash than I started with?" Structure and adapter
  patterns transfer; the objective function does not.

---

## 4. ByMykel/CSGO-API — the data payload

**URL:** <https://github.com/ByMykel/CSGO-API>
**Pinned in this project at commit:** `342d49698a77ca5d2da69c4ecd236358c866a364`
(**VERIFIED** to exist; author `ByMykel`, dated `2026-07-21T19:55:54Z`, message
`[bot::update-group] manifest 7673916425787288234`).

### What licence covers the JSON payload?

**MIT License**, covering the repository as a whole — which includes the generated
JSON in `public/api/`, since the licence is repository-scoped and no separate data
licence, data-usage policy, or terms file exists. (**VERIFIED**)

Raw `LICENSE` text confirms: **"MIT License / Copyright (c) 2023 ByMykel"**,
followed by the standard MIT grant and warranty disclaimer.

The README (**VERIFIED** by reading it) contains **no** separate data licence,
**no** rate limits, **no** usage restrictions, and **no** Valve trademark or
attribution notice. The absence of a distinct data licence is why the
repository-level MIT grant is the operative one.

### Proprietary use permitted?

**YES, with two qualifications that matter.**

1. **MIT attribution.** If we redistribute the JSON or a substantial portion of
   it, the copyright notice and permission notice must travel with it. This file
   records the notice. Note that internal use within a closed system is not
   redistribution; the obligation bites if the data is shipped onward.

2. **ByMykel cannot license Valve's intellectual property, and MIT does not
   pretend to.** The JSON is generated from Counter-Strike 2 game files — the
   commit message (`[bot::update-group] manifest …`) shows it is refreshed
   automatically against a Valve manifest. An MIT grant from ByMykel conveys
   ByMykel's rights in the compilation and the extraction tooling. It cannot
   convey rights in Valve's underlying assets, names, or trademarks, and the
   project makes no such claim.

   In practice this is a low-risk position for our use: we consume **factual
   attributes about game mechanics** — item identity, collection membership,
   rarity tier, float minimum and maximum. Facts are not themselves copyrightable,
   and a compilation of them attracts only thin protection. We do not redistribute
   Valve artwork, item images, icons, or marketing copy, and we do not present the
   data as endorsed by or affiliated with Valve. If the project's surface ever
   expands to shipping images or a public data mirror, this analysis must be
   revisited before that ships.

**Code copied:** **None.** The repository's own extraction scripts were not used.
We consume the published JSON payload as data, at a pinned commit.

### Ideas worth reusing

- **Pinning to an explicit commit and diffing on every update.** The repository is
  bot-updated against Valve manifests, so `main` can move without warning. Pinning
  converts a silent upstream change into an explicit, reviewable event — which is
  what makes the "fail closed on changed collection mapping" rule enforceable at
  all.
- **Language-partitioned static JSON served from a CDN-backed raw host.** Cheap,
  cacheable, no auth, no rate-limit negotiation, no service dependency.

### What must NOT be inherited

- **Treating it as authoritative without validation.** It is explicitly "An
  unofficial JSON API". Our rules registry must fail closed on a new collection, a
  changed collection mapping, a missing float cap, or probabilities that do not sum
  to 1 — and it must do so on ingest, not at execution time.
- **Assuming stability.** Bot-driven updates against upstream manifests mean the
  schema and the values can both move. Diff every update; never auto-adopt.
- **Single-source confidence.** This is currently our **only** material source for
  float ranges, which means the registry's "fail closed on material-source
  disagreement" rule cannot actually fire — there is nothing to disagree with. A
  second independent source is an outstanding requirement
  (`docs/source-matrix.md`, unresolved question 18).
- **Any expectation of an SLA.** Delivery is via GitHub raw hosting, so GitHub's
  availability and rate limits apply, not ByMykel's. Pinning keeps our steady-state
  request volume near zero, which makes this largely moot in practice.

---

## Summary

| Project | Licence | Proprietary use OK? | Code copied? | Ideas reused |
|---|---|---|---|---|
| [Soniclev/steam_csmoney](https://github.com/Soniclev/steam_csmoney) | **None** — no LICENSE file; GitHub API `license: null`. All rights reserved. | **No** | **No** | Async parser/worker split; retry resilience; raw-response archival; Telegram operator channel with whitelist; Docker Compose; strict lint/type/test discipline |
| [twaldin/trade-up-bot](https://github.com/twaldin/trade-up-bot) | **None** — no LICENSE file; GitHub API `license: null`. All rights reserved. | **No** | **No** | Corroboration of normalised-float formula; condition-boundary targeting; staleness verification as a pipeline stage; excluding claimed listings; time-bounded parallel discovery; 89-collection coverage as a sanity check |
| [HingedGuide/tradeup_optimizer](https://github.com/HingedGuide/tradeup_optimizer) | **MIT** — "Copyright (c) 2025 Ties Kuijpers" | **Yes**, with attribution if redistributed | **No** | Module layout (cli/core/data_sources/models); Pydantic boundary models; ByMykel + CSFloat pairing; bulk CSFloat retrieval; the inverse "max admissible float" framing |
| [ByMykel/CSGO-API](https://github.com/ByMykel/CSGO-API) (data) | **MIT** — "Copyright (c) 2023 ByMykel"; repository-scoped, covers the JSON; no separate data licence exists | **Yes**, with attribution; MIT cannot convey Valve's underlying IP | **No** — data consumed at a pinned commit; extraction scripts not used | Commit pinning with diff-on-update; language-partitioned static JSON delivery |

### Attribution notices

Retained here in satisfaction of the MIT notice requirement for the two
MIT-licensed works reviewed, and reproduced in full should any portion ever be
redistributed:

```
MIT License
Copyright (c) 2023 ByMykel
```

```
MIT License
Copyright (c) 2025 Ties Kuijpers
```

Full MIT text: <https://opensource.org/licenses/MIT>

### Review triggers

This audit is a point-in-time snapshot and must be re-run when:

- A dependency or reference project changes its licence. Both unlicensed
  repositories above could add one at any time — `twaldin/trade-up-bot` in
  particular is actively developed (last push 2026-07-24, one day before this
  audit).
- The ByMykel pin advances to a new commit.
- Any new third-party repository is reviewed, or any new runtime dependency is
  added.
- The project's distribution surface changes — in particular, if it ever ships
  item images, a public data mirror, or any redistribution of the ByMykel payload,
  the Valve-IP analysis in §4 must be revisited **before** that ships.

---

*Compiled 2026-07-25. Licence facts verified against the GitHub REST API and raw
licence files on that date. Descriptive claims about each project come from its
own README. Fee percentages and other numeric assertions quoted from these
projects are recorded as **UNVERIFIED third-party claims** and are not used as
configuration anywhere in this system.*
