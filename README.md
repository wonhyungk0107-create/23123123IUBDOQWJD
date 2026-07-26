# CS2 Trade-Up Procurement Agent

An exact-asset, multi-market procurement optimizer for CS2 trade-up contracts,
measuring **executable, fee-net, lock-adjusted, settled** profit.

> **Status: shadow mode.** This system has placed no orders, completed no
> trade-ups, and settled no profit. It is instrumentation built to determine
> whether an executable edge exists — not evidence that one does.

## What it does

Scans sanctioned third-party marketplace APIs for exact listings, enumerates valid
trade-up collection compositions, solves for the cheapest bundle satisfying a
composition's float budget, prices the result net of every modelled fee, and emits
an operator action card only when the candidate clears configurable profitability
and risk gates. The Steam-side trade-up is performed manually by a human operator.

Capital can be funded and settled through a crypto rail (BTC/ETH/USDT at
chain-native minor units): when `TRADEUP_CRYPTO_SETTLEMENT_ENABLED=true`, the
deposit → trade → withdraw round trip is priced explicitly (venue fees, FX spread,
network fees, volatility haircut) and each contract is charged its share, so EV
means profit that actually reaches the operator's wallet. See
[`docs/economic-model.md`](docs/economic-model.md).

## Hard boundaries

- **No Steam automation.** No Steam Community Market orders, no automated trade
  confirmation, no session cookies, no mobile-authenticator handling, no browser
  automation, no proxy rotation. The Steam Subscriber Agreement prohibits it.
- **No LLM in the money path.** Discovery through execution is deterministic.
- **Live execution disabled by default.** `LIVE_EXECUTION_ENABLED=false` and
  `MAX_DAILY_SPEND=0`.

See [`docs/security-boundaries.md`](docs/security-boundaries.md).

## Quick start

```bash
make bootstrap     # create the venv and install (uv)
make migrate       # apply migrations to a fresh SQLite database
make demo          # deterministic offline end-to-end run
make verify        # format, lint, typecheck, tests, coverage
```

On Windows without GNU make, every target has a direct equivalent:

```powershell
uv sync --extra dev
uv run tradeup demo
uv run pytest
```

## Documentation

| Document | Contents |
|---|---|
| [`CLAUDE.md`](CLAUDE.md) | Durable instructions for engineering sessions |
| [`docs/architecture.md`](docs/architecture.md) | Module boundaries and data flow |
| [`docs/economic-model.md`](docs/economic-model.md) | EV, fees, gates, capital model |
| [`docs/source-matrix.md`](docs/source-matrix.md) | Per-venue capabilities and evidence |
| [`docs/rule-registry.md`](docs/rule-registry.md) | Trade-up rule versions and provenance |
| [`docs/tradeup-mechanics-reference.md`](docs/tradeup-mechanics-reference.md) | Operator-supplied mechanics reference (verified against the registry) |
| [`docs/security-boundaries.md`](docs/security-boundaries.md) | What this system must never do |
| [`docs/operator-runbook.md`](docs/operator-runbook.md) | Manual operator procedure |
| [`docs/decision-log.md`](docs/decision-log.md) | Design decisions and what they de-risk |
| [`docs/status.md`](docs/status.md) | Acceptance-gate results and evidence |
| [`THIRD_PARTY.md`](THIRD_PARTY.md) | Reviewed open-source components and licences |

## Licence

Proprietary. Third-party provenance is tracked in `THIRD_PARTY.md`.
