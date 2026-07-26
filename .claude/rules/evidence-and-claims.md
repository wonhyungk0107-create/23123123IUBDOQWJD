# Rules for reports, docs and anything that states a result

## Never claim profit that has not settled

The only number that may be called profit is settled, withdrawable cash attributable
to a contract. Not EV. Not a passing candidate. Not an unsold output. Not Steam
Wallet value.

When reporting status, state all of these explicitly, even when every one is zero:

- settled fee-net profit
- orders placed, fills, trade-ups completed, sales, settlements
- real-market qualified opportunities
- simulated opportunities

**Zero is a result.** A shadow scan that finds nothing has measured the opportunity
frequency, and reporting that honestly is the job. Never pad a section by inventing
plausible events — the demo's ledger reports zero because nothing was bought, and
that is correct.

## Label data by what it actually is

- The offline demo is **synthetic listings and prices over real pinned metadata**.
  Every artifact and every operator card it produces says so.
- A passing synthetic candidate proves software behaviour. It proves nothing about a
  market.
- Priors are labelled as priors. `bundle_completion_probability`, the liquidity
  haircut table and days-to-sale are stated guesses awaiting calibration, and the
  operator card says which number is the least evidenced.

## Source claims

- Do not state a fee, rate limit, endpoint or trade-lock duration without a source.
  If it cannot be verified, write `UNVERIFIED` and say why.
- `docs/source-matrix.md` records what is confirmed and what is not. Keep the
  unresolved-questions section honest; it is more useful than the confirmed section.
- If a figure in this repository turns out to have no source, remove it rather than
  leaving it in place. It will otherwise be treated as measured.

## Rejections are the dataset

Rejection reason codes are the primary output of a shadow run. Never collapse
distinct causes into one, never short-circuit the gate after the first failure, and
never drop a reason to make a summary tidier.
