# Economic model

## The objective function

Settled, attributable, fee-net, cash-withdrawable profit. Not EV, not a passing
candidate, not an unsold output, not Steam Wallet value.

## Cost

```
acquisition_cost = Σ (price + buyer_fee + deposit_fee) + fx_cost + payment_surcharge
all_in_cost      = acquisition_cost + operational_cost
                                    + capital_carry_cost
                                    + partial_fill_reserve
```

`acquisition_cost` is cash that leaves the wallet. `all_in_cost` adds charges that
are real but paid to no one: the opportunity cost of committed capital, operations,
and the expected cost of failing to complete the bundle.

## Value

```
EV_net  = Σ P(y) · V_net(y) − all_in_cost
ROI_net = EV_net / acquisition_cost
```

ROI is taken against **acquisition cost**, not the loaded figure. Dividing by
`all_in_cost` would flatter a slow contract by inflating its own denominator with its
own carry charge.

`Σ P(y) · V_net(y)` accumulates as an exact rational over minor units and rounds
once, downward. Rounding each term separately lets ten small favourable roundings
push a marginal contract over the line on arithmetic noise.

## Output valuation

The highest active ask is not a price; it is an aspiration. Valuing outputs from asks
is the easiest way to manufacture a profitable-looking contract that loses money on
exit. So valuation walks an evidence hierarchy and reads the *low* end of the best
rung available:

| Rank | Source | Haircut* | Days to sale* |
|---|---|---|---|
| 1 | Executable cash bid | 0% | 0 |
| 2 | Exact-float completed sale | 3% | 5 |
| 3 | Completed sale | 5% | 7 |
| 4 | Depth-adjusted ask | 12% | 14 |
| 5 | Cross-market reference | 18% | 21 |
| 6 | Conservative fallback | 25% | 30 |
| — | `UNVALUABLE` | contributes zero | — |

\* **Priors, not measurements.** Replace with observed values from a shadow run.

Then:

```
V_net(y) = gross − seller_fee − withdrawal_fee − fx − liquidity_haircut − carry
```

Three refusals matter more than the arithmetic:

- **A lower quantile, never the mean.** The mean is flattered by lucky sales.
- **Only realisable balance types.** A Steam Wallet price is excluded outright rather
  than converted at par.
- **Unknown material fee ⇒ no valuation.** It raises, and the candidate is rejected
  with `UNKNOWN_FEE`. It never becomes an implicit zero.

## Capital

Money in a contract is unavailable for the next one. 12% over thirty days is worse
than 8% over five, and a system ranking on ROI alone will systematically prefer the
slower trade.

```
capital_days = input_trade_lock + transfer + contract_execution + time_to_sale + settlement
carry_cost   = principal · annual_rate · capital_days / 365     (rounded up)
```

Stages are sequential, not overlapping. Approved candidates rank by
`profit_per_capital_day`, not ROI.

## Partial-fill risk

A contract is a basket order with no atomic cross-venue execution. Any input can
vanish between the scan and the click, leaving orphan inventory bought at retail and
liquidated at wholesale.

```
P(complete)  = Π survival(listing_i)
E[loss]      = Σ  P(fail at step k) · (cost_committed_before_k − salvage)
```

Purchases are sequenced **most-fragile-first**. That is not a preference: buying the
listing most likely to vanish last means discovering it is gone only after paying for
everything else.

Survival decays with quote age toward a floor and scales by any venue-published
seller reliability. This is the least-evidenced number in the whole model, and the
operator card says so explicitly.

## Gates

Discovery uses the looser bar so there is room for drift before revalidation; the
final gate runs on revalidated data.

| Gate | Default |
|---|---|
| `DISCOVERY_MIN_NET_ROI` | 0.12 |
| `FINAL_MIN_NET_ROI` | 0.08 |
| `MIN_ABSOLUTE_EXPECTED_PROFIT` | $3.00 |
| `MAX_CONTRACT_COST` | $500 |
| `MAX_WORST_CASE_LOSS` | $400 |
| `MAX_PARTIAL_FILL_EXPOSURE` | $150 |
| `MAX_QUOTE_AGE_SECONDS` | 300 |
| `MAX_EXPECTED_CAPITAL_DAYS` | 21 |
| `MAX_UNVALUABLE_PROBABILITY_MASS` | 0.10 |
| `MAX_DAILY_SPEND` | **0** |
| `LIVE_EXECUTION_ENABLED` | **false** |

Settings refuse a discovery bar below the final bar — such a configuration surfaces
only candidates that can never be approved.

The final gate checks, and reports **every** failure rather than the first: discovery
threshold still met, every listing directly requeried, all present, prices unchanged,
floats unchanged, asset identity unchanged, quotes fresh, ruleset approvable, output
evidence sufficient, unvaluable mass within limit, final ROI, positive
lower-confidence-bound EV, worst case, contract cost, partial-fill exposure, capital
days, no duplicate asset, every listing reserved, balance sufficient, daily limits,
and a sanctioned execution method.

Rejection codes are the dataset. Counting them by cause across a shadow run is how we
learn whether opportunities die on economics, on availability, or on our own model
gaps — a distinction free-text reasons would destroy.

## What would falsify the hypothesis

- Complete bundles above 8% rarely exist after direct revalidation.
- They exist but cannot be filled before listings vanish.
- Fees and capital delays consume the edge.
- Output sales land materially below the modelled exit.
- Profit concentrates in a few lucky outcomes rather than the distribution.
- Orphan losses exceed contract profits.
- Opportunity frequency cannot justify the capital and operator attention.

Each is measurable from data this system already records. None has been measured yet.
