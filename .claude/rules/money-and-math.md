# Rules for `src/tradeup/domain/` and `src/tradeup/valuation/`

These modules decide whether to spend money. The rules below are not style
preferences; each one exists because breaking it produces a plausible wrong number
rather than an error.

## Money

- Money is `Money`: integer minor units + currency + balance type. There is no other
  representation. No `float`, no bare `int`, no `Decimal` dollars.
- Never add, compare or subtract across currencies or balance types. `Money` raises;
  do not work around it. Steam Wallet credit is not withdrawable cash.
- Costs round **up** (`scaled_up`), revenue rounds **down** (`scaled_down`). When the
  direction is ambiguous, pick the pessimistic one.
- A zero fee must be a stated fact with a source (`FeeSchedule.zero_quote`), never
  the result of a lookup that found nothing. Missing fee → `UnknownFeeError` → the
  candidate is rejected with `UNKNOWN_FEE`.

## Floats and probabilities

- Item floats are `Decimal`. `reject_float()` guards the entry points; keep it.
- Probabilities and normalised floats are `Fraction` internally. This is why
  `Σ P(y) == 1` is exactly true rather than true within a tolerance. Do not convert
  to `Decimal` before summing.
- Probability-weighted money accumulates as an exact rational and rounds **once**,
  downward. Rounding per term lets small favourable roundings accumulate into a
  contract that clears the gate on arithmetic noise.
- Never use `==` on binary floats anywhere near a threshold. There should be no
  binary floats to compare.

## Rules and metadata

- Anything Valve can change is looked up from the resolved `TradeupRuleSet`: input
  count, quality mixing, float method, probability method. Never hard-code "ten
  inputs" or "Souvenirs are prohibited".
- Every rule-sensitive result records the `rule_version` that produced it.
- Item identity, float caps and output pools come from the metadata registry. Never
  parse a display name to work out what an item is.
- Unknown rarity, missing float cap, unknown collection → skip with an ERROR issue.
  Fail closed; never map to the "closest" tier.

## Tests

- New maths gets a golden case in `tests/fixtures/golden_tradeups.json` with
  hand-computed expected values, not values copied from the implementation.
- Modules listed in `tools/check_coverage.py` must stay at or above 90% branch
  coverage. `make coverage` enforces it.
