# CS:GO / CS2 Trade-Up Contract Mechanics — Reference

> **Provenance and standing (added on import, 2026-07-26).** Operator-supplied
> community reference, imported verbatim below the line. It is undated and not
> Valve-authoritative, so it does **not** promote the rule registry's validation
> status — the versioned registry in `src/tradeup/domain/rules.py` remains the
> only authority the money path may consult (`docs/rule-registry.md`).
>
> **Verified 2026-07-26:** its four core rules and its worked example agree with
> the `2026-05-souvenir-covert` ruleset exactly; the example is pinned as the
> golden case `external_reference_3_7_worked_example`.
>
> Two reading notes: (1) §3's "average the input floats" means the average of
> **adjusted/normalised** floats — the reference's own example values (0.2857…
> = 2/7) are already scaled into each skin's float cap, matching the registry's
> `z_i = (f_i − a_i)/(b_i − a_i)`; averaging raw floats would be wrong for any
> capped skin. (2) The reference omits souvenir eligibility, the five-input
> Covert contract, and the Extraordinary tier, all of which the registry models.

---

> **Purpose:** This file is a standing reference for Claude Fable 5. Whenever a trade-up contract calculation, simulation, or explanation is needed, use the rules below as ground truth. Do not re-derive the mechanics from general knowledge — follow this spec exactly.

## 1. What a Trade-Up Contract Is

A trade-up contract consumes **10 input skins of the same rarity** and produces **1 output skin of the next rarity tier up**. Rarity progression:

Consumer Grade → Industrial Grade → Mil-Spec (blue) → Restricted (purple) → Classified (pink) → Covert (red)

StatTrak inputs always produce a StatTrak output; non-StatTrak inputs always produce a non-StatTrak output.

## 2. Which Output Collection Is Selected

Each of the 10 inputs belongs to a specific **collection** (e.g., Broken Fang, Recoil Case). The output skin is drawn from the pool of next-rarity skins across **all collections represented in the 10 inputs**, weighted by how many inputs came from each collection.

- Probability of a given collection producing the output = (number of inputs from that collection) / 10
- That probability is then split evenly across all eligible output skins within that collection at the target rarity.

**Example:** 3 inputs from Collection A, 7 inputs from Collection B, with Collection A having 5 eligible outputs and Collection B having 5 eligible outputs:
- Collection A total chance = 3/10 = 30% → 30% ÷ 5 skins = 6% each
- Collection B total chance = 7/10 = 70% → 70% ÷ 5 skins = 14% each

## 3. Float (Wear) Calculation

**Step 1 — Average the input floats:**

```
Average Input Float = (sum of all 10 input float values) / 10
```

**Step 2 — Map the average into the output skin's float range:**

```
Output Float = (Output Max Float − Output Min Float) × Average Input Float + Output Min Float
```

- `Output Min Float` / `Output Max Float` are the specific float range bounds defined for that exact output skin (not the wear-tier bounds — the skin's own min/max, which can be narrower than 0.00–1.00).
- If a skin's range is the full 0.00–1.00, the formula collapses to `Output Float = Average Input Float`.

**Note on "adjusted float":** if an input skin has a float cap (some skins/collections cap max float below 1.00, e.g., 0.28), use the *adjusted* float value already scaled to that cap when computing the average, not the raw in-game float.

## 4. Wear Condition Bounds

Used to translate a float value into a condition name:

| Condition | Float Range |
|---|---|
| Factory New (FN) | 0.00 – 0.07 |
| Minimal Wear (MW) | 0.07 – 0.15 |
| Field-Tested (FT) | 0.15 – 0.38 |
| Well-Worn (WW) | 0.38 – 0.45 |
| Battle-Scarred (BS) | 0.45 – 1.00 |

## 5. Worked Example

**Inputs:**
- 3× StatTrak P250 | Contaminant | Mil-Spec | Broken Fang | adjusted float 0.285714285714285754 (FT)
- 7× StatTrak UMP-45 | Roadblock | Mil-Spec | Recoil Case | adjusted float 0.089999999999999997 (MW)

**Average input float:**
```
(3 × 0.285714285714285754) + (7 × 0.089999999999999997) = 1.487142857142857
1.487142857142857 / 10 = 0.1487142857142857
```

**Collection weighting:**
- Broken Fang: 3/10 = 30% total → split across 5 eligible Restricted outputs = 6% each
- Recoil Case: 7/10 = 70% total → split across 5 eligible Restricted outputs = 14% each

**Output float:** Since each output skin's range is 0.00–1.00, `Output Float = Average Input Float = 0.14871428571428574` → falls in Minimal Wear (0.07–0.15).

**Resulting outcome table:**

| Output Skin | Collection | Chance |
|---|---|---|
| AWP \| Exoskeleton (Restricted) | Broken Fang | 6% |
| Dual Berettas \| Dezastre (Restricted) | Broken Fang | 6% |
| SSG 08 \| Parallax (Restricted) | Broken Fang | 6% |
| Nova \| Clear Polymer (Restricted) | Broken Fang | 6% |
| UMP-45 \| Gold Bismuth (Restricted) | Broken Fang | 6% |
| Dual Berettas \| Flora Carnivora (Restricted) | Recoil Case | 14% |
| R8 Revolver \| Crazy 8 (Restricted) | Recoil Case | 14% |
| SG 553 \| Dragon Tech (Restricted) | Recoil Case | 14% |
| P90 \| Vent Rush (Restricted) | Recoil Case | 14% |
| M249 \| Downtown (Restricted) | Recoil Case | 14% |

All output skins share the same float (0.14871428571428574, Minimal Wear) because the average input float is identical for every possible outcome — only the *probability* of landing on each skin changes based on collection weighting.

## 6. Quick-Reference Formulas

```
Average Input Float = Σ(input floats) / 10
Output Float = (OutputMax − OutputMin) × AverageInputFloat + OutputMin
Collection Chance = (inputs from collection) / 10
Per-Skin Chance = Collection Chance / (number of eligible output skins in that collection)
```
