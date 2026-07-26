# Operator runbook

You are the execution layer. The agent finds candidates and does the arithmetic; you
decide whether to spend money, perform every Steam action by hand, and record what
actually happened. Nothing in this system can buy anything.

## Before a session

```bash
tradeup doctor
```

Confirms the execution boundary, the metadata revision and its hash, the active rule
version and whether it is approvable, database reachability, which credentials are
present (never their values), and the effective gates. Exits non-zero if anything is
wrong.

**Stop if:** `live_execution_enabled` is true and you did not deliberately enable it;
the active ruleset is `UNVERIFIED`; metadata reports errors.

## Reading a card

Every approved candidate produces one. The fields that decide whether to act:

| Field | What to do with it |
|---|---|
| **Expires** | Past it, the quotes are stale. Rescan; do not buy. |
| **Net ROI** | Already net of every modelled fee. Not a gross figure. |
| **Lower-confidence-bound EV** | Must be positive. If it is barely positive, the case rests on thin evidence. |
| **Worst case** | The loss if the least valuable output lands. Assume you will see it. |
| **Bundle completion probability** | **The least trustworthy number on the card.** A prior from quote age, not a measured fill rate. |
| **Expected capital-days** | How long the money is gone. |
| **Outcome distribution** | Check the *evidence* column, not just the price. `CROSS_MARKET_REFERENCE` with n=1 is a guess. |
| **Assumptions** | Read them. They state what the number rests on. |

**Decline whenever** the reasoning is unclear, the evidence is thin, the exposure
exceeds your comfort, or anything in front of you contradicts the card. Declining is
a valid, recorded outcome — and during groundwork it is the expected one.

## Buying, if you choose to

Follow the **purchase sequence exactly**. It is ordered most-fragile-first so that if
the bundle is going to fail, it fails while you have spent the least. Each step shows
the running exposure and what you lose if the next listing is gone.

After each purchase, verify the asset ID matches the card before continuing. If any
listing is gone or repriced, **stop**. Do not substitute a similar listing — the
contract was solved for those exact floats, and a substitution silently changes the
output distribution. Record `LISTING_GONE` or `PRICE_CHANGED` and liquidate what you
hold.

## Steam actions — human only

Every step here is performed by you, in the client. No part may be automated.

1. Accept each incoming trade offer and confirm on the mobile authenticator.
2. Verify in Steam that all inputs arrived and each float matches the card.
3. Open the trade-up contract screen and add exactly the listed inputs.
4. Re-check the input count and that nothing unintended was added.
5. Execute the contract.
6. Record the actual output skin and its **exact float**.
7. List the output at the recommended exit venue.
8. Record the sale, and record the settled amount once proceeds clear.

Step 6 is the one that matters most for calibration. The realised output and float
are what let predicted-versus-actual comparison say *why* a prediction was wrong.

## Recording outcomes

Record one of: `APPROVED`, `REJECTED`, `PURCHASED`, `LISTING_GONE`, `PRICE_CHANGED`,
`RECEIVED_IN_STEAM`, `TRADEUP_COMPLETED`, `OUTPUT_RECORDED`, `LISTED_FOR_SALE`,
`SOLD`, `SETTLED`.

**Record failures with the same care as successes.** A vanished listing is the single
most valuable data point this project can collect: it is direct evidence about
whether the displayed edge is executable at all. Under-recording failures produces a
dataset that says the strategy works.

A contract is not profitable until `SETTLED`. An unsold output is inventory.

## When to stop

Halt and investigate on: a metadata validation failure, a rule change, an unknown fee,
repeated disagreement between an aggregator and a direct requery, a balance that does
not reconcile, an output that was not in the predicted distribution (that means the
rules or metadata are wrong, which is far worse than an unlucky draw), or settled
losses past your limit.

## Current state

Nothing has been bought. No trade-up has been performed. No profit has settled.
`tradeup ledger reconcile` reports `$0.00` and zero events, and that is accurate.

## Scheduled confirm-and-calibrate batches

A Windows scheduled task named **"CS2 Tradeup Confirm Batch"** runs
`tools/run_confirm_batch.ps1` every 8 hours (from 08:00, while the user is
logged on). Each run executes `tradeup candidates confirm-batch --top 3` —
read-only, within both measured API budgets — and appends its output to
`artifacts/logs/confirm-batch-<date>.log` (gitignored).

- Inspect: `schtasks /Query /TN "CS2 Tradeup Confirm Batch" /V /FO LIST`
- Run now: `schtasks /Run /TN "CS2 Tradeup Confirm Batch"`
- Remove: `schtasks /Delete /TN "CS2 Tradeup Confirm Batch" /F`
- Review the accumulated fit at any time: `tradeup candidates calibration`

The calibration history lives in the operator database (`tradeup.db`,
`prospect_confirmations`, append-only). The verification battery
(`tools/write_verification.py`) runs against a throwaway database precisely so
it can never destroy this history; do not run raw `alembic downgrade` against
the operator database either.
