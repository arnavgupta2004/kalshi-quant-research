# Contract & price semantics

Everything downstream (arbitrage, calibration, market making) rests on these definitions.
Items marked **[verified]** were checked against the live exchange while building Stage 1;
**[assumed]** items were not, and are listed again under "Open assumptions".

## 1. Mechanics

* A **market** is one binary question. It has two complementary **contracts**: YES and NO.
  One YES + one NO always pay exactly **$1** in total, whatever the outcome.
* The API publishes **bids only**, for both sides **[verified]**. Because YES + NO = $1:

      a NO  bid at q   ==  a YES ask at 1 - q     (same size)
      a YES bid at p   ==  a NO  ask at 1 - p

  Asks are therefore *derived*, never stored (`market/order_book.py`).
* Units are exact fixed point: prices are integers in 1/10,000 dollar, quantities in
  1/100 contract (`market/units.py`). Off-grid values raise instead of being rounded.

## 2. Five different things called "the price"

| Concept | Definition | Notes |
|---|---|---|
| **Market price** | last traded price, or the quoted bid/ask | an observation, not a belief |
| **Executable price** | VWAP obtained by walking the book for a given size | depends on size, side, fees, latency (Stage 4) |
| **Implied probability** | the probability that makes a risk-neutral, fee-free agent indifferent at that price | `p` only after subtracting fees and any risk premium |
| **Fair probability** | our model's `P(X=1 \| F_t)` | what the pricing models estimate (Stage 7) |
| **Settlement value** | the amount actually paid, in `[0, 1]` | see §3 - not always 0 or 1 |

The order-book **midpoint is not the probability**: it ignores the size imbalance
(a 1-lot bid against a 10,000-lot ask has the same mid as the reverse), ignores fees, and is
undefined for one-sided books. The code exposes `mid()` for analytics only.

## 3. Settlement is not always {0, 1}  **[verified]**

Among 2,000 recently settled markets: 1,366 `no`, 614 `yes`, and **20 `scalar`** (1%), which paid a
fractional `settlement_value_dollars` (e.g. 0.38). Market rules also void cancelled events at
**$0.50** (e.g. table-tennis: "all markets will resolve to $0.50").

Consequences carried through the model:

* The payoff variable is `X in [0, 1]`, not `{0, 1}`. A YES contract pays `X`; NO pays `1 - X`.
* `YES + NO = $1` still holds exactly, so complementary-contract arbitrage is unaffected.
* Calibration (Stage 8) must decide how to score scalar/void outcomes; the default is to
  **exclude** them from Brier/log-loss and report the excluded fraction.
* `normalize_market` rejects a market whose `result` contradicts its `settlement_value`.

## 4. When are two contracts "the same"?

Similar titles are never sufficient. The normalised `Market` stores what is needed to decide:
event ticker, `Strike` (type / floor / cap / custom), full `Rules` text, `can_close_early`,
expiration times, `price_level_structure` and the event's `settlement_sources`.
The event-level `mutually_exclusive` flag is treated as a *hint to be verified in Stage 3*, not as
proof: e.g. a tennis match winner event is `mutually_exclusive=true`, but an MLB run-spread
event is `false`, and neither flag says whether the outcomes are collectively exhaustive.

## 5. Verified API behaviour (and where it contradicts the docs)

| Finding | Handling |
|---|---|
| REST order-book levels arrive **worst -> best**; the docs say best -> worst | never rely on order; always sort |
| `/markets/orderbooks?tickers=A,B` treats `"A,B"` as **one** ticker and returns `200` + an empty book | send repeated `tickers=A&tickers=B`; reject any returned ticker not in the request |
| `/markets?tickers=A,B` (a different endpoint) **requires** the comma form and rejects repeats | encoded per endpoint, with regression tests |
| The first 1,000+ open markets are multivariate "combo" (`KXMVE...`) parlays | `mve_filter=exclude` by default |
| Public REST reads work **without auth** (docs say auth required) | credentials optional for REST |
| **WebSocket needs auth for every channel** - unauthenticated handshake -> HTTP 401 | WS client fails fast without credentials |
| Docs list a new WS host `external-api-ws.kalshi.com`; it reset the TCP connection here | legacy `api.elections.kalshi.com` is the default; URL is configurable |
| `taker_book_side` `bid` == taker bought YES, `ask` == taker bought NO | confirmed consistent on 2,500 real trades; enforced in normalisation |
| The exchange encodes "absent" as `""` (`result`, `expiration_value`, sizes) | `""` -> `None` at the boundary |
| Data is partitioned live/historical at `GET /historical/cutoff` (currently 2026-07-20) | historical endpoints exposed via `historical=True`; the Stage 2 collector must union both |
| 429 responses carry no `Retry-After`; limits are token-bucket | client-side pacing + exponential backoff with jitter |
| Intermittent `ConnectError('')` on fresh connections | transparent retry (observed and recovered in live runs) |
| **No historical order books exist** (`/historical/markets/{t}/orderbook` -> 404) | book history must be self-recorded (Stage 2 `BookPoller` / `WsBookRecorder`) |
| `/historical/markets` **ignores** `min_close_ts`/`max_close_ts`; it is ordered by `created_time` desc | scan with an early stop on `created_time`; ordering asserted at runtime |
| The live/historical cutoff **moves** (07-20 -> 07-21 within a day) and the live listing overlaps it | trades routed per market from open/close vs cutoff; cutoff recorded per run; dedupe on `trade_id`/ticker |
| ~70,000 settled markets per day; mostly crypto strike ladders; only ~19% have >=100 contracts volume | per-series cap + volume filter in the universe spec |
| Invalid pagination cursors are **silently treated as page 1** (no error) | resume verifies the first page against the recorded one |
| A market's lifetime `volume_fp` equals the sum of its public trade sizes exactly | used as a completeness check (`verify` command); 3,000/3,000 collected markets reconcile |

## 6. Open assumptions

1. **WebSocket `seq` is contiguous per `sid` across all markets on that subscription** (docs). The WS client was
   validated against an in-process mock exchange and the documented message schemas, *not* against the
   live authenticated stream (no credentials were available). If the assumption is wrong the book processor
   reports a flood of `gap` results - loud, not silent.
2. Whether a book can be transiently crossed between two consecutive deltas is unknown; it is counted
   (`crossed_observed`), not treated as fatal.
3. The demo WS host (`demo-api.kalshi.co`) URL is the historical convention and is unverified.
