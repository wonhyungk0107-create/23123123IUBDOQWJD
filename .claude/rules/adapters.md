# Rules for `src/tradeup/adapters/`

## The execution boundary

- **Never write code that interacts with Steam.** No Community Market calls, no
  trade-offer acceptance, no inventory polling, no session cookies, no mobile
  authenticator, no browser automation, no proxy rotation. The trade-up itself is
  performed by a human following an operator card.
- Never call an endpoint that is not in the venue's published documentation. If a
  browser does something the docs do not describe, that is not an API we may use.
- `execution_mode` states the *venue's* capability, and it must be accurate:
  - `UNSUPPORTED` — no documented purchase endpoint exists (CSFloat is here).
  - `AUTOMATED` — one is documented. Still gated by `live_execution_enabled`.
  - `OPERATOR_APPROVAL_REQUIRED` — a human transacts.
  Do not promote a venue to `AUTOMATED` without a citation in
  `docs/source-matrix.md`.

## Every operation returns a typed result

- Return `CapabilityResult`. Never raise past the adapter boundary, and never return
  an empty list to mean "failed".
- `MarketAdapter`'s defaults are refusals. A subclass supports an operation only by
  overriding it. Do not add a base implementation that does real work.
- Distinguish refusal reasons: `AUTHENTICATION_REQUIRED` (no key) is not
  `UNSUPPORTED` (no such API) is not `POLICY_BLOCKED` (we forbid it).

## Parsing

- Parse strictly. A missing price, float, collection or rarity is a
  `TransportSchemaError`, not a default. A permissive parser invents bargains.
- Read JSON with `parse_float=Decimal`.
- Validate that the float lies inside the stated range. An impossible float is a data
  fault.
- Record `raw_payload_hash` on every listing. Evidence is not optional.
- Secrets go through `redact()` on every path that can reach a log or an exception.

## Tests

- Contract tests live in `tests/contract/` and use `FixtureTransport`. No mandatory
  test may touch the network.
- Cover: success, empty, each missing field, unknown item, invalid float, 401, 429
  with `Retry-After`, 5xx, timeout, schema change, disappearance, price change.
