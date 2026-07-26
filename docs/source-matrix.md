# Source Matrix — Contracts, Capabilities and Evidence

**Date:** 2026-07-25
**Status:** research record for shadow mode. No source in this document has been
used to place a live order.

## How to read this document

Every claim below is tagged with how it was obtained:

- **VERIFIED** — read directly from the operator's own primary source (its
  documentation, its `robots.txt`, its licence file, or the GitHub API) during
  this research pass, on 2026-07-25.
- **SECONDARY** — obtained from a page the operator publishes but which is not
  the normative reference (blog post, help-centre article, marketing page), or
  from search-engine summaries of a page that could not be rendered directly.
- **UNVERIFIED** — could not be confirmed. The reason is stated inline. An
  unverified item is a blocker for anything that touches money.

Several vendor pages in this space are client-rendered single-page applications
or sit behind bot protection. Where a fetch returned 403, 404, 522, or an empty
render, that is recorded rather than worked around. Nothing in this document was
obtained by scraping, by calling an undocumented endpoint, or by inference from a
third-party reimplementation.

**Fee percentages, rate limits and lock durations quoted here are research notes,
not configuration.** The fee schedule is versioned data and must be re-confirmed
against the venue immediately before any purchase, per `CLAUDE.md`.

---

## 1. ByMykel/CSGO-API

**Role.** Metadata seed: item identity, collections, rarities, float minimum and
maximum, quality variants. It is the input to the rules registry, not a price or
execution source.

**Repository.** <https://github.com/ByMykel/CSGO-API>

**Pinned commit.** `342d49698a77ca5d2da69c4ecd236358c866a364` — **VERIFIED** to
exist via the GitHub commits API. Author `ByMykel`, committed
`2026-07-21T19:55:54Z`, message `[bot::update-group] manifest 7673916425787288234`.
The commit message shows the repository is refreshed by an automated bot against
a Valve manifest, which is precisely why we pin and diff rather than track `main`.

**Official/documented public API.** No. The project describes itself as "An
unofficial JSON API for Counter-Strike 2" (**VERIFIED** from the README). It is
static JSON served from GitHub raw hosting under
`https://raw.githubusercontent.com/ByMykel/CSGO-API/main/public/api/{language}`
(**VERIFIED**). There is no service contract, no versioned API, and no SLA.

**Authentication.** None. Public static files.

**Purchase/execution operation.** None. Not a marketplace.

**Rate limits.** Not published by the project. The README states no rate limits
(**VERIFIED** — absence confirmed by reading it). Because delivery is via
`raw.githubusercontent.com`, GitHub's own limits and terms govern fetching, not
ByMykel's. The applicable GitHub raw-content limit was **not** confirmed in this
pass — **UNVERIFIED**. Practical mitigation: we consume a pinned commit, so
steady-state fetch volume is one request per pin change, which makes the limit
close to irrelevant for us.

**Terms-of-service constraints.** The repository is MIT licensed (see
`THIRD_PARTY.md`). The README carries **no** usage restriction, **no** attribution
demand beyond MIT, and **no** Valve trademark notice (**VERIFIED** — absence
confirmed by reading the README). Note the important limit: the JSON is derived
from Valve's game files, and an MIT grant from ByMykel cannot convey rights in
Valve's underlying intellectual property. We treat the data as factual reference
material about game mechanics.

**Unresolved questions.**

- What is the actual GitHub raw-content rate limit and does our refresh cadence
  approach it?
- Does any float min/max in the payload disagree with a Valve-authoritative
  source? The rules registry must fail closed on material-source disagreement,
  and we currently have only one material source.

---

## 2. CSFloat

**Role.** Exact-float listings and float verification. Primary read source for
listing-level truth.

**Documentation.** <https://docs.csfloat.com/> (**VERIFIED**, renders). The
documentation source is open at <https://github.com/csfloat/docs>, licensed
Apache-2.0 (**VERIFIED** via the GitHub API `license` field), described as "API
Docs for csfloat.com". The endpoint list below was cross-checked against the raw
documentation source `source/index.html.md` in that repository, which agreed with
the rendered site.

**Official/documented public API.** Yes, but small. Exactly three endpoints are
documented (**VERIFIED**, both from the rendered docs and the docs source):

| Method | Path | Auth |
|---|---|---|
| GET | `/api/v1/listings` | not required |
| GET | `/api/v1/listings/<ID>` | not required |
| POST | `/api/v1/listings` | required |

`POST /api/v1/listings` is a **seller** operation — it lists an item you already
own for sale (fields include `asset_id`, `type` of `buy_now` or `auction`, and
`price`). It is not a buy operation.

**Authentication.** API key in an `Authorization` header, sent as the bare key:
`Authorization: <API-KEY>` (**VERIFIED**). Keys are issued from the developer tab
of the user profile (**VERIFIED**).

### CRITICAL QUESTION — does a documented purchase/buy endpoint exist?

**No. The CSFloat API as documented is read-only for buyers.** There is no
documented endpoint for purchase, checkout, cart, payment, bid, or offer
acceptance. This was checked twice: against the rendered documentation at
`docs.csfloat.com` and against the raw Slate source `source/index.html.md` in
`csfloat/docs`. Both enumerate the same three endpoints and nothing more
(**VERIFIED**).

Consequence for this system: **CSFloat is a discovery and verification source
only.** Any CSFloat input in a bundle must go on an operator card with a direct
listing link. Community projects are known to call unlisted CSFloat routes; we do
not, and a CSFloat adapter must never implement `execute` as an HTTP call.

**Rate limits.** **Not documented.** The official documentation states no rate
limit (**VERIFIED** — absence confirmed against the docs source). Third-party
client libraries and community write-ups assert figures such as "200 requests per
hour per API key" and various per-5-minute buckets; these are **UNVERIFIED**
and mutually inconsistent, and must not be encoded as constants. The adapter
should treat rate limiting as discovered at runtime — honour `429` and any
`Retry-After`, back off exponentially, and record observed ceilings as
measurements rather than configuration.

**Terms-of-service constraints.** **UNVERIFIED.** `csfloat.com/support/tos` and
`csfloat.com/support/terms` both render as an empty client-side application shell
to a non-browser client; no terms text could be retrieved. We therefore do not
know CSFloat's stated position on automated access, scraping, or acceptable API
call volume. This is a gap that must be closed by a human reading the terms in a
browser before any sustained polling, and certainly before execution.

**Unresolved questions.**

- What do CSFloat's terms of service actually say about automated access and
  polling frequency?
- What is the real rate limit, and is it per key, per IP, or per endpoint?
- Are the buyer fees we model correct and current? No fee schedule was found in
  the API documentation at all, so fees must come from the checkout flow or from
  support documentation and be re-verified before each purchase.
- Does `GET /api/v1/listings/<ID>` reliably distinguish "sold" from "delisted"?
  The revalidation gate depends on this.

---

## 3. DMarket

**Role.** Listings, plus **targets** (standing buy orders with float and price
bounds). This is the only venue in the matrix with a documented purchase API, so
it is the intended primary automated execution venue — currently disabled.

**Documentation.** The root <https://docs.dmarket.com/> returns **HTTP 404**
(**VERIFIED** — it 404s). The live reference is the Swagger UI at
<https://docs.dmarket.com/v1/swagger.html> (**VERIFIED**, renders). A separate,
older GitHub repository <https://github.com/dmarket/dmarket-doc> also exists and
documents a **different** endpoint surface (see the discrepancy note below).
DMarket additionally publishes help-centre articles under `support.dmarket.com`;
those returned **HTTP 403** to a non-browser client and could not be read
directly (**UNVERIFIED** as a source in this pass).

### CRITICAL QUESTION — what is the request-signing scheme?

**VERIFIED** from the Swagger reference. Three headers on every authenticated
request:

| Header | Content |
|---|---|
| `X-Api-Key` | the public key, "must be a hex string in lowercase" |
| `X-Sign-Date` | Unix timestamp, e.g. `1605619994`; must not be more than **2 minutes** old |
| `X-Request-Sign` | `dmar ed25519` followed by the hex-encoded 64-byte signature |

The signed string is built as:

```
(HTTP Method) + (Route path + HTTP query params) + (body string) + (timestamp)
```

with the documented example:

```
POST/get-item?Amount=%220.25%22&Limit=%22100%22&Offset=%22150%22&Order=%22desc%22&1605619994
```

The signature algorithm is **Ed25519** (NaCl), signed with the account's secret
key. The key pair is generated in the API section of DMarket account settings
(**SECONDARY**, from DMarket's own help centre and blog).

Two implementation hazards follow directly from the documented example and must
be pinned by contract tests before any signed write:

1. Query parameters appear in the signed string **already percent-encoded**, and
   the example shows quote characters encoded as `%22`. Signing a differently
   normalised or differently ordered query string will fail. Do not rely on an
   HTTP client's own serialiser matching this.
2. The two-minute `X-Sign-Date` window means clock skew on the host is a
   correctness bug, not a nuisance. NTP discipline is a precondition for
   execution.

### CRITICAL QUESTION — is there a documented target-creation and purchase API?

**Yes on both counts** (**VERIFIED** from the Swagger reference):

| Purpose | Method | Path |
|---|---|---|
| Create targets (standing buy orders) | POST | `/marketplace-api/v1/user-targets/create` |
| List the user's targets | GET | `/marketplace-api/v2/user/targets` |
| Find existing buy orders for a title | GET | `/marketplace-api/v1/targets-by-title/{game_id}/{title}` |
| Buy offers | PATCH | `/exchange/v1/offers-buy` |
| Aggregated prices | POST | `/marketplace-api/v1/aggregated-prices` |
| Account balance | GET | `/account/v1/balance` |
| User profile | GET | `/account/v1/user` |

DMarket's own product page for the Trading API states it can "Purchase items" and
"Create and cancel targets", and that targets support "phase, float values, and
paint seed" parameters (**SECONDARY** — DMarket blog, not the normative
reference). That float-filter capability is what makes Mode B (target
accumulation) plausible at all.

**Endpoint-surface discrepancy — must be resolved before implementation.** The
`dmarket/dmarket-doc` GitHub repository documents a materially different set of
paths, including `POST /trading/v1/buy/offers` for purchasing (with a response
carrying `TotalSucceed` / `TotalFailed`), `GET /account/v1/user/balance` for
balance, `GET /offers/v1/search`, `GET /trading/v1/inventory`,
`POST /trading/v1/offers/cancel`, and `POST /trading/v1/offers/update-price`
(**VERIFIED** that the repository says this). This does **not** match the Swagger
reference (`/exchange/v1/offers-buy`, `/account/v1/balance`). One of the two is
stale. Which one is live is **UNVERIFIED** — we did not call the API, having no
key and no mandate to. Treat the Swagger UI as the more likely current surface
and the GitHub repo as historical, but **confirm empirically against a live
authenticated read endpoint before writing any execution code**, and pin the
result in a contract test.

**Rate limits.** DMarket publishes limits in its FAQ (**SECONDARY** — read from
`dmarket.com/faq`, which rendered, but this is a FAQ rather than the API
reference):

| Bucket | Unauthorised (per IP) | Authorised (per account) |
|---|---|---|
| Sign-in | 20 RPM | 20 RPM |
| Fee | 2 RPS | 110 RPS |
| Last sales | — | 6 RPS |
| Market items | 2 RPS | 10 RPS |
| Other methods | 6 RPS | 20 RPS |

Responses are documented to carry remaining-quota headers including
`X-RateLimit-Remaining-Second` and `RateLimit-Reset`. The "110 RPS" figure for the
authorised fee bucket is an order of magnitude out of line with its neighbours and
looks like a documentation error or a typo for 10 RPS; treat it as **UNVERIFIED**
and do not design a burst pattern that depends on it. In all cases the adapter
should drive its own pacing from the returned headers rather than from these
numbers.

### CRITICAL QUESTION — documented trade-lock / trade-protection holding periods

The picture is **7 days**, sourced from DMarket's blog rather than from a formal
policy page, so this is **SECONDARY** throughout.

- Steam trade protection allows reversal of "all trades made in the previous
  seven days"; during that window there is "No consuming items, like opening CS2
  cases or using sprays", "No modifying items, like applying or removing
  stickers", and "No transferring items, like trading". After a reversal the
  account receives "a 30-day cooldown for CS2 skins trading".
- On DMarket specifically: "Any item you receive in Steam is now locked for 7
  days, during which it can be rolled back (returned to the original owner)".
  Such items "cannot be withdrawn to Steam until the protection period ends".
- You "cannot give away an item under Trade Protection in an exchange unless you
  are the one who deposited it on DMarket".
- Items received in an exchange "cannot be sold, listed, or re-traded" for a
  period that "matches the longest Trade Protection time from the items you gave
  away in the exchange".

Publication date of the DMarket update article: **Sep 6, 2025** (**SECONDARY**).
The corresponding help-centre article, "Trading with Steam trade protection
update", could not be read (403) — **UNVERIFIED**.

**Direct contradiction with our own working assumption.** `CLAUDE.md` currently
cites a "DMarket ~10-day third-party invisibility" window in the inventory state
machine. **No source found in this pass supports 10 days.** Every figure located
points to 7. The 10-day number is **UNVERIFIED** and should be treated as
suspect. Because capital-days feeds directly into the ranking score
(`EV_lower_confidence_bound / ExpectedCapitalDays`), a wrong lock duration
systematically mis-ranks every candidate. The honest resolution is not to pick
7 or 10 from documentation at all, but to measure actual observed transition
times in shadow mode and let the model learn them — which is what the state
machine was designed to do.

**Terms-of-service constraints.** DMarket's Terms of Use contain **no explicit
prohibition** on bots, automated access, scraping, or API use (**SECONDARY** —
the terms page rendered and was searched; absence is reported by that read, not
proven). The nearest applicable clause is a Fair Use Policy at section 3.10
covering use that "exceeds the average usage, cause significant network
congestion, disruption or is fraudulent, or suspicious, or a non-ordinary use".
DMarket actively markets the Trading API for automated trading, so automated
access is plainly sanctioned in principle; the practical constraint is volume and
conduct, governed by fair use plus the published rate limits.

### Crypto funding and withdrawal

Researched 2026-07-26 for the crypto settlement rail (`valuation/settlement.py`).

- DMarket publishes help-centre articles titled "Crypto payments - funds deposit
  and withdrawal" (`support.dmarket.com/hc/en-us/articles/43447619366929`) and
  "Bitcoin - how to make a deposit?" (`.../articles/25195565108241`) — the URLs
  and titles were confirmed via search results, but a direct fetch of the article
  body returned **HTTP 403** (bot protection), so their contents are **SECONDARY**
  at best. Nothing below may be treated as a configured fee.
- Per search-result summaries of those articles (**SECONDARY**): deposits are
  accepted in BTC, ETH, USDT (ERC-20), Bitcoin Cash, Litecoin and Solana; deposit
  amounts are entered in USD and the balance is USD-denominated; DMarket itself
  adds no crypto deposit fee but the payment processor's own fee applies
  (reported "around 2%"); withdrawal is stated to carry no DMarket cashout fee,
  only the provider's. **Every one of these fee statements is UNVERIFIED** — none
  may enter a `FeeSchedule` until read from the operator's own account screen or a
  directly rendered policy page, with a date.
- Consequence for configuration: the crypto rail ships **disabled**, and the only
  crypto fee schedule in the repository is the demo's clearly-labelled synthetic
  one. Enabling the rail against DMarket requires sourcing, dating and entering
  the real deposit, withdrawal, spread and network fees first — the fail-closed
  `UnknownFeeError` path enforces this.

**Unresolved questions.**

- Which endpoint surface is live: Swagger (`/exchange/v1/offers-buy`) or the
  GitHub doc repo (`/trading/v1/buy/offers`)?
- Exact byte-level construction of the signed string for a request with a JSON
  body and multiple query parameters — including parameter ordering and encoding.
- What float precision do targets actually match on? `CLAUDE.md` flags
  `floatPartValue`; if target matching is coarser than our contract's `z_max`
  budget, Mode B silently buys items that break the bundle. This is the single
  most important unknown for the autonomy path.
- Is the fee charged on a target fill the same as on a direct buy, and is it
  quoted before commitment?
- Are DMarket balances withdrawable cash or venue-reusable, per balance type, and
  what are the withdrawal fees? Help-centre articles on crypto deposit/withdrawal
  exist but returned 403 on direct fetch; see "Crypto funding and withdrawal"
  above. The fee figures remain UNVERIFIED.
- Is 110 RPS on the fee bucket real or a typo?
- Confirm the 7-day figure against a normative policy page rather than a blog.

---

## 4. SkinSnipe

**Role.** Cross-market price reference. Validation only — per `CLAUDE.md`, any
price that matters is re-queried at the venue that will actually transact.

**Documentation.** <https://www.skinsnipe.com/api> exists but is
client-rendered; a non-browser fetch returned only the page shell with no
endpoint content (**VERIFIED** that the page did not render its content to us).
Everything below is therefore **SECONDARY**, from indexed excerpts of that page.

**Official/documented public API.** Yes, but **gated**. Coverage is described as
price information across "more than 20 digital markets" for Counter-Strike 2,
Rust, Dota 2 and Team Fortress 2. Documented capabilities include lowest price per
market, item quantity in stock for supported markets, price history, a 30-day
average Steam price with approximate 30-day volume, an instability/safety flag on
displayed prices, and notification management. Price-history depth is
plan-dependent — one tier is described as 30 days, another as 90.

Critically: **the endpoint URLs and even the base URL are not public.** The
documentation states they are shown only to users who have logged in and paid for
a plan ("for security reasons, the base URL will be shown only to users who have
paid for one of the available plans"). We therefore cannot record a single
verified endpoint path here, and must not guess one.

**Authentication.** **UNVERIFIED.** The project reserves a `SKINSNIPE_API_KEY`
credential, which implies key-based auth, but the header name, parameter name, and
key format are all unknown because the documentation is paywalled.

**Purchase/execution operation.** None found. SkinSnipe is a price aggregator, not
a marketplace, so a buy endpoint would be surprising. But since the endpoint list
is gated, "no purchase endpoint exists" is an inference, not a verified fact —
recorded as **UNVERIFIED**. It does not matter operationally: we would not execute
through an aggregator regardless, because the aggregator is not the counterparty.

**Rate limits.** Unknown — behind the paywall. **UNVERIFIED.**

**Terms-of-service constraints.** Not retrieved. **UNVERIFIED.**

**Note on independence.** A Chrome Web Store extension is published under the name
"TradeUpSpy & SkinSnipe Inventory Tool" (**SECONDARY**), which suggests the two
services are affiliated or operated together. If so, SkinSnipe and TradeUpSpy are
**not independent** cross-checks of one another, and a parity agreement between
them is weaker evidence than it appears. This should be confirmed before either is
given weight in a disagreement-quarantine decision.

**Unresolved questions.**

- What are the endpoint paths, base URL, auth header, and rate limits? All
  require a paid subscription to learn.
- Which 20+ markets are covered, and is coverage per-market complete or
  best-effort? A missing market silently biases the "lowest price" figure.
- What does the instability/safety flag actually mean, and can it be used as a
  liquidity-haircut input?
- Are SkinSnipe and TradeUpSpy the same operator? If yes, downgrade parity
  independence.
- What are the licence terms on redistributing or storing their price data?

---

## 5. TradeUpSpy

**Role.** Parity validation — a golden-case verifier for our float and probability
maths, explicitly **not** a dependency.

**Site.** <https://www.tradeupspy.com/tradeups>

### Does ANY public API exist?

**No public API was found.** No developer documentation, no API subdomain, no
published endpoint reference, and no terms-page mention of API access were
located. Note the epistemics: this is **absence of evidence**, established by
search plus direct fetching, not an explicit statement from the operator that no
API exists. Recorded as **UNVERIFIED-NEGATIVE** — high confidence, but not a
quotation. A third-party GitHub project named `tradeupapi` exists and is
unaffiliated; it is not an official interface and is irrelevant to us.

This confirms the existing position in `CLAUDE.md`: TradeUpSpy is a manual,
human-in-the-loop parity check.

**robots.txt.** **VERIFIED**, retrieved in full from
<https://www.tradeupspy.com/robots.txt>:

```
User-Agent: *
Disallow: /legacy/*
Disallow: /logged
Disallow: /login-succesful
Disallow: /ref/*
Disallow: /calculator/weekly/*
Disallow: /calculator/custom/*
Disallow: /calculator/shared/*
Disallow: /calculator/share/*
Disallow: /profile/settings
Disallow: /profile/premium/method/steam
Disallow: /profile/premium/order-completed
Allow: /

Sitemap: https://www.tradeupspy.com/sitemap.xml
```

**This is the operationally important finding in this section.** The four
`Disallow` rules on `/calculator/custom/*`, `/calculator/shared/*`,
`/calculator/share/*` and `/calculator/weekly/*` cover exactly the URLs a parity
check would want to hit — a custom or shared calculator state encoding a specific
candidate contract. The operator has explicitly asked automated clients to stay
off those paths.

Therefore: **the TradeUpSpy validator adapter must never fetch a
`/calculator/...` URL programmatically.** It may compose the URL and hand it to a
human, who opens it in a browser and reads the result back into the system as an
operator-entered parity verdict. `robots.txt` governs automated crawlers; it does
not govern a person clicking a link. That distinction is the entire design of this
adapter and must not erode.

**Terms of service.** **UNVERIFIED.** `www.tradeupspy.com/terms` renders as a
client-side application shell and returned no terms text to a non-browser client.
We do not know their stated position on scraping, data reuse, or automated access
beyond what `robots.txt` signals. A human must read the terms in a browser before
the parity workflow is used at any volume.

**Authentication / purchase.** Not applicable. There is no API to authenticate to
and TradeUpSpy is not a marketplace. It does sell a premium subscription; if the
parity workflow relies on premium features, that is a licence the operator holds
personally, and their subscriber terms then apply to how its output may be used.

**Unresolved questions.**

- What do the terms of service actually say about reuse of calculator output?
- Is TradeUpSpy operated by the same party as SkinSnipe (see above)? If so, parity
  independence is weaker than assumed.
- Which rule version and float formula does TradeUpSpy implement? A parity
  disagreement is only diagnostic if we can attribute it — our quarantine
  decomposition (metadata / rule-version / float-formula / probability /
  price-timestamp / fee) needs to know what they compute, and they do not publish
  it.
- Does the premium subscription's terms permit an operator to transcribe results
  into a private system?

---

## 6. CS.MONEY

**Role.** Extra inventory. Manual link only.

**Official/documented public API.** **None found.** No developer portal, no
published API reference, and no partner-API documentation was located. CS.MONEY
publishes a support site and an FAQ, neither of which documents a public
programmatic interface. Recorded as **UNVERIFIED-NEGATIVE** — we found nothing,
but we cannot quote a denial. Any endpoint under `cs.money` that a third-party
project calls is, by definition, undocumented, and `CLAUDE.md` forbids
undocumented endpoints.

**Authentication.** Not applicable — no documented API.

**Purchase/execution operation.** None documented. **OPERATOR_ACTION_REQUIRED.**

**Rate limits.** Not applicable / unknown.

**Terms-of-service constraints.** **UNVERIFIED.** `cs.money/terms-of-use/`
returned **HTTP 403** to a non-browser client — the site is behind bot protection.
That 403 is itself weak evidence of the operator's posture toward automated
clients, and should be read as a reason for caution rather than as permission.

**Reversal window.** `CLAUDE.md` cites a "CS.MONEY ~8-day reversal window". **No
source was found for this figure in this pass** — **UNVERIFIED**. Same treatment
as the DMarket 10-day figure: do not trust it, measure it.

**Adapter status.** Manual link only. The adapter may render a URL for an operator
to open. It must not fetch, must not authenticate, and must return `UNSUPPORTED`
from `execute`.

**Unresolved questions.**

- Does a partner or affiliate API exist under NDA or on application? Worth one
  email; worth nothing until answered in writing.
- What is the real reversal/hold window, and does it differ by acquisition path?
- What are the terms on automated access? Currently unreadable without a browser.

---

## 7. SkinSwap

**Role.** Extra inventory. Manual link only.

**Official/documented public API.** **None found.** `skinswap.com/api` returns
**HTTP 404** (**VERIFIED**). No developer documentation was located by search.
Recorded as **UNVERIFIED-NEGATIVE**.

Secondary descriptions characterise SkinSwap as a Steam-trade-bot exchange: users
log in via Steam, select skins, and receive a bot-initiated trade offer
(**SECONDARY**). If accurate, that model is structurally awkward for us — the
counterparty flow terminates in a Steam trade offer that a human must accept,
which is exactly the boundary we refuse to automate. Secondary sources also cite
an operator entity and a trade fee "around 10%"; both are **UNVERIFIED** and must
not be used as a fee constant.

**Authentication.** Not applicable — no documented API.

**Purchase/execution operation.** None documented. **OPERATOR_ACTION_REQUIRED.**

**Rate limits / terms.** **UNVERIFIED.** Not retrieved.

**Adapter status.** Manual link only; `execute` returns `UNSUPPORTED`.

**Unresolved questions.**

- Does any documented partner API exist?
- What is the actual fee schedule, and is it quoted before commitment?
- Does the Steam-bot flow imply a trade-protection hold on received items? If so,
  the capital-days cost may make this venue uneconomic regardless of headline
  price.

---

## 8. Steam / Steam Community Market

**Role.** Receipt of purchased inputs, the manual trade-up contract itself, and
optionally an exit venue. **Human-only.**

**Official/documented public API.** The Steam Web API exists and is documented,
but it is not a Community Market trading interface. It is irrelevant to execution
here.

### The prohibition, quoted

The current Steam Subscriber Agreement, **section 4.C, headed "Automation"**,
states (**VERIFIED**, retrieved from
<https://store.steampowered.com/subscriber_agreement/>):

> "You may not use any form of scripts, bots, macros, or other non-human-controlled
> systems ("Automation") to interact with Content and Services on Steam in any
> manner, including but not limited to:
>
> - Automating the Steam account creation process,
> - Faking gameplay statistics (e.g., inflated wins or losses, XP, playtime),
> - Earning rewards or progress without genuine user input,
> - Participating in adjudication systems (like peer reviews or "overwatch")
>   through automated means, including influencing outcomes or reporting users by
>   scripted action rather than informed judgment."

**Link:** <https://store.steampowered.com/subscriber_agreement/>

**Two honest caveats, both material.**

1. **The widely-quoted older wording is gone.** Many third-party projects and
   articles cite the phrase "Cheats, automation software (bots), mods, hacks, or
   any other unauthorized third-party software, to modify or automate any
   Subscription Marketplace process". The literal string "automation software"
   **does not appear** in the current agreement (**VERIFIED** by a targeted search
   of the document). If our documentation or code comments quote that older
   phrasing, they are quoting a superseded version and should be corrected. The
   phrase "Subscription Marketplace" does still appear elsewhere in the agreement,
   including a section on trading between subscribers.
2. **The enumerated examples do not name the Market.** Section 4.C's bullet list
   is about account creation, gameplay statistics, rewards, and Overwatch — none
   of them is marketplace trading. The prohibition's force for our purposes comes
   from the **lead-in clause**, which is deliberately broad: automation may not be
   used "to interact with Content and Services on Steam **in any manner**,
   including but not limited to" the listed examples. A scripted Community Market
   order or an automated trade-offer acceptance is automation interacting with
   Steam services, and is caught by that lead-in. We should be candid that this
   rests on the general clause rather than on a bullet naming the Market.

Neither caveat changes the policy. The conservative reading is the correct one and
the project's hard boundary stands: **never automate Steam.** The cost of being
wrong is account termination and loss of the entire skin inventory, which is a
catastrophic, uninsurable, non-recoverable loss. There is no expected-value
calculation under which that is worth attempting.

**Steam Web API Terms of Use** (**VERIFIED**, <https://steamcommunity.com/dev/apiterms>):

- "You are limited to one hundred thousand (100,000) calls to the Steam Web API
  per day."
- "You may not use the Steam Web API or Steam Data in any way that violates the
  Steam Subscriber Agreement" — so the SSA automation clause reaches through the
  Web API terms as well.
- The key must be kept confidential and not shared with any third party.
- The developer must not "intercept or store the end user's Steam password on log
  in" — consistent with our absolute rule against storing Steam credentials,
  session cookies, or `steamLoginSecure`.
- Also prohibited: degrading the operation or performance of Steam, unsolicited
  marketing, and presenting data so that it appears endorsed by or affiliated with
  Valve.

**Purchase/execution operation.** Technically present in the Steam client and
website; **contractually and by our own policy, forbidden to automate.**

**Adapter status.** `POLICY_BLOCKED`. There is no Steam adapter that performs
network calls to Steam on the money path. Steam appears in the system only as
operator instructions on a card and as manually recorded outcomes. Any future code
that imports a Steam session library should be treated as a security incident, not
a feature.

**A further economic note.** Even if automation were permitted, Steam Wallet funds
have no cash value and cannot be withdrawn. Under the objective function —
cash-withdrawable settled profit — a Steam sale scores as `NON_WITHDRAWABLE` /
`STEAM_WALLET` and is only ever chosen when Wallet balance is independently
wanted. The compliance boundary and the economic boundary happen to agree.

**Unresolved questions.**

- Has any Valve statement clarified whether third-party marketplace API trading
  (which ultimately settles as a Steam trade offer accepted by a human) is
  affected by section 4.C? Our reading is that a human accepting a trade offer is
  not automation, but this has not been confirmed from a Valve source.
- What is the current trade-protection behaviour on the Steam side, from a Valve
  source rather than from DMarket's blog?

---

## 9. "Skins Money" — unidentified

**Role in the brief.** Named as a source in the project brief with no URL and no
further description.

**Identification attempt — FAILED.** This service could not be identified. What
was tried and what came back:

- Direct fetch of `https://skins.money` — returned **HTTP 522** (Cloudflare
  origin unreachable). A 522 means a Cloudflare edge exists for the hostname but
  the backing origin did not respond. This is **not** evidence that a functioning
  service exists there, and is certainly not evidence of a documented API.
- Search for "Skins.Money", "skinsmoney.com", "skins-money" as a CS2 marketplace
  or cash-out site: no matching service in current market round-ups or review
  sites.

**Plausible candidates, none confirmed.** The name may be a garbled reference to
one of: **CS.MONEY** (`cs.money`) — already covered separately in this matrix and
the closest lexical match; **SkinsMonkey** (`skinsmonkey.com`); **Skins.com**; or
**SkinCashier**. Each is a real service, but there is no evidence for which, if
any, was meant. Guessing here would be worse than useless — a wrong guess would
attach a real venue's fee schedule and terms to a source we were never asked to
integrate.

**Recommendation.** **Keep the adapter disabled.** Concretely:

- No adapter should be registered for this name.
- If one exists as a stub, it must return `UNSUPPORTED` from every method
  including `fetch_listings`, not merely from `execute`.
- Do not resolve the ambiguity by picking the nearest-sounding real site.
- The only correct next step is to ask whoever wrote the brief which service they
  meant, and to obtain a URL. Until that answer arrives, this entry stays at
  `UNSUPPORTED` and contributes nothing to discovery.

**Status:** UNIDENTIFIED / UNVERIFIED. Adapter disabled.

---

## Capability summary

| Source | Role | Documented API | Auth | Execution capability | Our adapter status |
|---|---|---|---|---|---|
| ByMykel/CSGO-API | Item metadata, collections, float ranges | Unofficial static JSON, no service contract | None (public files) | SUPPORTED_READ_ONLY | Active, pinned at `342d496…`; diff every update |
| CSFloat | Exact-float listings, float verification | Yes — 3 endpoints only | API key in `Authorization` header | SUPPORTED_READ_ONLY | Active read/verify; `execute` hard-wired to `UNSUPPORTED` |
| DMarket | Listings + targets | Yes — Swagger; surface contested | `X-Api-Key` + `X-Sign-Date` + `X-Request-Sign` (Ed25519) | SUPPORTED_EXECUTION_DISABLED | Built but gated; `LIVE_EXECUTION_ENABLED=false` |
| SkinSnipe | Cross-market price reference | Yes, but paywalled — endpoints not public | Presumed API key; unconfirmed | SUPPORTED_READ_ONLY | Validation only; never a purchase basis |
| TradeUpSpy | Parity validation | None found | N/A | UNSUPPORTED | Human-in-the-loop only; never fetch `/calculator/*` |
| CS.MONEY | Extra inventory | None found | N/A | OPERATOR_ACTION_REQUIRED | Manual link only; no network calls |
| SkinSwap | Extra inventory | None found (`/api` → 404) | N/A | OPERATOR_ACTION_REQUIRED | Manual link only; no network calls |
| Steam / SCM | Receipt, manual contract, optional exit | Web API exists but not for Market trading | N/A (forbidden) | POLICY_BLOCKED | No Steam network calls on the money path, ever |
| "Skins Money" | Unidentified | Unknown — service not identified | Unknown | UNSUPPORTED | Disabled; do not register an adapter |

---

## Unresolved questions

Items that must be confirmed **before any live execution**. Grouped by severity.

### Blocking — execution must not be enabled until these are closed

1. **DMarket endpoint surface.** Determine empirically whether
   `PATCH /exchange/v1/offers-buy` or `POST /trading/v1/buy/offers` is live, and
   likewise for balance. Two official DMarket sources disagree. Pin the answer in
   a contract test that fails loudly on drift.
2. **DMarket signature construction.** Establish the exact byte-level signed
   string for a POST with a JSON body and multiple query parameters, including
   parameter ordering and percent-encoding. Verify against a harmless
   authenticated read before any write. Add NTP-skew monitoring — the
   `X-Sign-Date` window is two minutes.
3. **DMarket target float precision.** Confirm what `floatPartValue` actually
   matches on, and whether a created target can bind an item outside our `z_max`
   budget. If target matching is coarser than our float constraint, Mode B is
   unsafe and must stay disabled regardless of everything else.
4. **Fee schedules, everywhere.** No venue in this matrix publishes a machine-
   readable fee schedule in its API documentation. Every buyer fee, seller fee,
   deposit fee, withdrawal fee and FX spread currently in the model is
   **UNVERIFIED** until confirmed against the venue and recorded with
   `source` and `last_verified`. Third-party numbers seen during this research
   (e.g. "CSFloat ~2%", "CSFloat 2.8% + $0.30", "DMarket 2.5%", "SkinSwap ~10%")
   come from other people's code and review sites and **must not** be adopted.
5. **Balance-type semantics on DMarket.** Which balances are cash-withdrawable
   versus venue-reusable, and at what withdrawal cost? The objective function is
   defined in cash-withdrawable settled profit; without this the objective is not
   computable.
6. **CSFloat terms of service.** Currently unreadable without a browser. A human
   must read and record them before sustained polling.

### High — corrects a live modelling error

7. **Lock durations.** The "DMarket ~10-day" and "CS.MONEY ~8-day" figures in
   `CLAUDE.md` have **no located source**. Every figure found points to 7 days for
   Steam trade protection. Either source them or replace them with measured
   transition times from shadow mode. Capital-days drives the ranking score, so a
   wrong constant mis-ranks every candidate in the system.
8. **Correct the superseded Steam quotation.** Any place in this codebase or its
   docs quoting "automation software (bots)" or "Subscription Marketplace process"
   as current SSA language is quoting a withdrawn version. Cite section 4.C
   "Automation" instead.
9. **CSFloat rate limit.** Unpublished. Adapter must discover it at runtime from
   `429` responses and back off, and must not encode a community-sourced constant.
10. **Delisted vs. sold.** Confirm that CSFloat and DMarket let us distinguish a
    listing that sold from one that was withdrawn. The revalidation gate and the
    partial-fill model both depend on it, and conflating the two biases the
    measured fill rate.

### Medium — affects confidence in validation, not correctness of execution

11. **SkinSnipe / TradeUpSpy independence.** A shared browser extension suggests
    common operation. If they are the same operator, parity agreement between them
    is not independent corroboration and the quarantine logic over-weights it.
12. **SkinSnipe contract.** Endpoints, auth header, rate limits and data-reuse
    terms are all behind a paid plan. Decide whether to buy a plan or drop the
    source; a source we cannot characterise cannot be trusted as a validator.
13. **TradeUpSpy terms of service.** Unreadable without a browser. Needed before
    the parity workflow is used at any volume, even manually.
14. **TradeUpSpy rule version.** Without knowing which trade-up rule version and
    float formula they implement, a parity disagreement cannot be attributed to a
    cause, which defeats the purpose of the quarantine decomposition.

### Low — housekeeping, but do not let them rot

15. **"Skins Money" identity.** Ask the brief's author for a URL. Keep disabled
    until answered.
16. **CS.MONEY and SkinSwap partner APIs.** Worth one enquiry each. Worth nothing
    until answered in writing; a support agent's verbal "sure, go ahead" is not a
    documented API.
17. **GitHub raw-content rate limit** for ByMykel fetches.
18. **Second material source for float ranges.** The rules registry is required to
    fail closed on material-source disagreement, but currently has only one
    material source, so that check cannot fire.

---

*Compiled 2026-07-25. Every "not documented" above means we looked and did not
find it — not that we know it does not exist. Where a page could not be rendered
or returned an error, that is recorded inline rather than papered over. No
undocumented endpoint was called in the course of this research.*
