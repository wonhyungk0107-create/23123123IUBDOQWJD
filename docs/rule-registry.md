# Trade-up rule registry

## Why rules are data

CS2 trade-up mechanics change. Souvenir eligibility changed. Covert contracts
changed. Code that hard-codes "ten inputs, Souvenirs prohibited" produces confidently
wrong economics the day Valve ships a patch, and the failure is silent because the
arithmetic still balances.

So every rule-sensitive value is looked up from a `TradeupRuleSet` resolved by
timestamp, and every computed result records the `rule_version` that produced it.

Resolution is strict. Zero matching rulesets means we have no sourced statement of
the mechanics and must not trade. More than one means the registry is inconsistent
and must not be trusted to pick a winner. Both raise.

## Mathematics

For input *i* with published float caps `[a_i, b_i]` and actual float `f_i`:

```
z_i = (f_i − a_i) / (b_i − a_i)
z̄   = (1/N) Σ z_i
```

For output *y* with caps `[a_y, b_y]`:

```
f_y = a_y + z̄ · (b_y − a_y)
```

For output *y* in collection *c*, where *n_c* inputs came from *c* and *c* has *k_c*
eligible outputs at the next rarity:

```
P(y) = n_c / (N · k_c)
```

**Invariants, enforced not assumed:**

- every `z_i ∈ [0, 1]` — a float outside its skin's published range is a metadata or
  adapter fault and raises rather than clamping
- every `f_y` inside that output's legal range
- `Σ P(y) = 1` **exactly** — probabilities are `Fraction`, so this is a property of
  the arithmetic rather than a tolerance check
- an empty output pool invalidates the candidate; the probability mass has nowhere to
  go, so it is never renormalised away

Wear boundaries are 0.07 / 0.15 / 0.38 / 0.45. A float sitting exactly on a boundary
belongs to the **higher-wear** band: 0.07 is Minimal Wear, not Factory New. This is
why the optimizer targets budgets just *below* a breakpoint.

## Shipped rule versions

### `legacy-10-input` — 2013-08-14 → 2026-05-01

- Ten inputs at Consumer through Classified.
- **Covert is deliberately absent**: under these rules there is no
  Covert → Extraordinary contract, and asking for one raises.
- Normal and StatTrak only, never mixed. Souvenir prohibited entirely.

### `2026-05-souvenir-covert` — 2026-05-01 → open

- Ten inputs, or **five** for Covert.
- Souvenir inputs permitted and mixable with Normal; Souvenir attributes are
  stripped and the output is a plain Normal item one rarity higher.
- StatTrak still never mixes with anything.

| Input qualities | Output |
|---|---|
| Normal | Normal |
| StatTrak | StatTrak |
| Souvenir | Normal (stripped) |
| Normal + Souvenir | Normal (stripped) |
| Normal + StatTrak | **not permitted** |

## Validation status

Both shipped rulesets are **`GOLDEN_FIXTURE_ONLY`** with `validated_at = None`.

They reproduce the worked examples in `tests/fixtures/golden_tradeups.json`, and no
authoritative source check has been performed in this environment. That is a
deliberate, machine-readable admission rather than a footnote: `RuleValidationStatus`
has an `approvable` property, and an `UNVERIFIED` ruleset is rejected by the risk
gate with `UNVERIFIED_RULESET`.

Promoting either to `VERIFIED_AGAINST_SOURCE` requires a dated citation added to
`source_references` and a matching `validated_at`.

## Metadata provenance

Item identity, float caps, collection membership and output pools come from a pinned
snapshot, never from parsing display names.

| | |
|---|---|
| Source | `ByMykel/CSGO-API`, `public/api/en/skins.json` |
| Pinned commit | `342d49698a77ca5d2da69c4ecd236358c866a364` |
| Upstream SHA-256 | `7aeb9582c5f3308be78c78d2fd3681e3c469c67c0aeeeb7a9e54adb5c3be32d7` |
| Upstream size | 5,471,848 bytes, 2,126 entries |
| Committed projection | `data/metadata/bymykel-342d496.json`, 374,826 bytes, 1,455 entries |
| Imports as | 1,455 skins across 94 collections, 0 errors |

The repository commits a *projection* — only the fields we consume — plus a manifest
recording the upstream commit and the original payload hash. Anyone can re-download
that commit, re-run `tools/project_bymykel.py`, and byte-compare. That is what makes
"pin the commit, diff every update" a procedure rather than an intention.

Float caps are parsed with `parse_float=Decimal`. Letting the JSON decoder turn
`0.07` into a binary float would poison every normalisation downstream, invisibly.

### Rarity mapping is explicit

An upstream rarity label not in the mapping is an ERROR and the entry is skipped —
never mapped to the closest tier. Deliberately absent:

- **`Contraband`** — no trade-up semantics.
- **`Master`** — verified against `collections.json` at the pinned revision to be the
  *agent/character* rarity (e.g. "Lt. Commander Ricksaw | NSWC SEAL"). An earlier
  draft of this importer mapped it to `EXTRAORDINARY`, which would have placed agents
  into knife output pools.
- **`Exceedingly Rare`** — never observed in the pinned payload; adding it would be a
  guess about a label we have not seen.

### Known coverage gap: Covert contracts

The pinned snapshot contains **zero** Extraordinary skins with collection membership.
Knives and gloves are not published with a collection in `skins.json`, and
`collections.json` — which does list them — carries no float caps.

Consequence: every Covert → Extraordinary contract has an empty output pool and is
rejected. That is the correct fail-closed behaviour, and it means the five-input
Covert path is exercised only by golden fixtures, never against real metadata.

Closing this requires a source that states knife/glove collection membership **and**
float caps, recorded here with its provenance.
