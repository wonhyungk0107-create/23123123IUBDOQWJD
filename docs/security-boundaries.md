# Security and execution boundaries

## What this system must never do

### Never automate Steam

No Community Market buying or selling. No automated trade-offer acceptance. No
inventory polling. No client or UI control. No session cookies or `steamLoginSecure`
handling. No mobile-authenticator automation. No browser automation. No proxy
rotation to evade rate limits. No CAPTCHA bypass.

Steam Subscriber Agreement §4.C ("Automation"):

> You may not use any form of scripts, bots, macros, or other non-human-controlled
> systems ("Automation") to interact with Content and Services on Steam in any
> manner, including but not limited to…

*Note on this citation:* the widely-quoted phrase "automation software (bots)" does
not appear in the current agreement. The clause above is the current text. Its four
enumerated examples concern account creation, stats, rewards and Overwatch — none
names the Market — so the prohibition reaches Market interaction through the broad
lead-in clause rather than a named bullet. The policy here is unchanged either way;
the citation is stated accurately rather than conveniently.

**Enforcement.** `SteamManualAdapter` returns `POLICY_BLOCKED` for every operation
and has no method that could be overridden into contacting Steam. The trade-up
contract itself is performed by a human following an operator card.

### Never call an undocumented endpoint

If a browser does something the venue's documentation does not describe, it is not an
API we may use. This is the specific practice not inherited from the reference
implementations reviewed in `THIRD_PARTY.md`.

`robots.txt` is honoured. TradeUpSpy disallows `/calculator/custom/*`,
`/calculator/shared/*`, `/calculator/share/*` and `/calculator/weekly/*` — precisely
the parity-check URLs — so the validator contains **no HTTP client at all**. It
compares against manual exports and composes a URL for a person to open.

### Never put an LLM in the money path

Discovery → validation → optimizer → EV → risk policy → execution is deterministic
and reproducible. An LLM may explain a failure, summarise a terms change, cluster
errors or draft operator prose. It never computes a float, a probability, a fee or a
buy decision.

### Never store Steam credentials

Not passwords, session cookies, `steamLoginSecure`, mobile-auth secrets, or browser
extension cookies. There is no code that reads or writes any of them.

## Execution gating

Three independent things must all be true before any automated purchase could occur.
During groundwork none of them is.

| Control | Default | Enforced by |
|---|---|---|
| `live_execution_enabled` | `false` | `Settings`; checked by `RiskPolicy` and every adapter |
| `max_daily_spend` | `0` | `Settings`; the final gate rejects with `DAILY_SPEND_LIMIT_EXCEEDED` |
| Venue `execution_mode` | per venue | `MarketAdapter`; refusals are typed |

`Settings` refuses to construct with a non-zero spend budget while
`live_execution_enabled` is false — a budget without a switch is a misconfiguration,
not a convenience.

The distinction between refusal types is deliberate and load-bearing:

- `UNSUPPORTED` — no documented API exists. **No flag can enable this.** CSFloat is
  here, and its adapter does not even accept a `live_execution_enabled` argument.
- `SUPPORTED_EXECUTION_DISABLED` — the venue documents it; we have chosen not to.
- `OPERATOR_ACTION_REQUIRED` — a human transacts.
- `POLICY_BLOCKED` — forbidden by our own boundaries.

"We chose not to" and "there is no such API" are different facts about a venue, and
collapsing them would make the second look like a configuration away from the first.

## Credentials

Every credential is optional. Absence produces `AUTHENTICATION_REQUIRED` and the scan
continues with whatever sources answered — missing keys never stop the core slice.

Secrets are `SecretStr`. They are never logged, never rendered into an operator card,
and never written to an evidence artifact. `redact()` is applied on every path that
can reach a log or an exception message, because a leaked key in a traceback is a
credential compromise.

`.env` is gitignored. `.env.example` carries names and safe defaults only.

## Kill switches

New buys halt on: an API schema change, an unknown fee schedule, a rule change, a
failed metadata validation, an exceeded revalidation failure rate, repeated
direct-versus-aggregator disagreement, a balance reconciliation failure, a duplicate
asset allocation, a realised price error beyond tolerance, settled P&L past the loss
limit, a terms change, or a suspected credential compromise.

The gate reasons corresponding to the currently implemented subset —
`UNKNOWN_FEE`, `UNVERIFIED_RULESET`, `METADATA_DISCREPANCY`,
`DUPLICATE_ASSET_ALLOCATION`, `LISTING_IDENTITY_MISMATCH` — already reject
candidates. The daily-loss and failure-rate switches require realised data that does
not exist yet; they are named here so their absence is visible rather than assumed.

## Repository hygiene

`.claude/settings.json` denies force-push, `reset --hard`, `checkout --`, `restore`,
`clean -f`, `branch -D`, `rebase` and recursive deletes. The only hook is a
deterministic formatter on edited Python files — no LLM-based hooks, and no full test
suite on every edit.
