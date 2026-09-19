# Same-event arbitrage (Stage 4)

```
observed book -> constraint -> displayed margin -> liquidity -> fees -> slippage / size -> net edge
 (order_book)   (relationships)   Constraint.check    min size     exact     walk the book    classified
                                                                    fees      optimise size    Opportunity
```

This stage turns a *proven* pricing constraint (Stage 3) into a **classified, sized, fee-adjusted trade**,
and measures how many apparent opportunities survive each filter. It deliberately stops before latency
and lifecycle statistics (Stage 6): here the question is only what the *static* structure of the market
offers.

## 1. Fees (`market/fees.py`) - exact, per series, three rounding models

Money is integer micro-dollars (µ$): `price_ticks x qty_centi` is already µ$, so cost, payoff and fees
add with **no rounding error**, and the identity `net = payoff - vwap_cost - fees = gross - slippage - fees`
holds exactly (tested).

| | |
|---|---|
| Model | `fee = 0.07 x multiplier x C x P x (1 - P)`, `ceil` to 6 decimals **per fill** (`trade_fee`, Kalshi docs) |
| Verified | the docs' own worked example: 1 contract at $0.055 -> `ceil_6dp($0.00363825) = $0.003639` (`tests/test_fees.py`) |
| Per series | `fee_type` / `fee_multiplier` come from `GET /series/{t}` (recorded in `series_meta`); maker fee (0.0175) exists only for `quadratic_with_maker_fees` and is irrelevant to a taker |
| Rounding models | `EXACT_6DP` (default) - `CENT_PER_FILL` (conservative bound) - `CENT_PER_ORDER`. Whole-cent rounding is a *balance* artefact (the excess accumulates per order and is rebated), so cent rounding is a sensitivity, not the truth |
| Unknown fee | falls back to the standard taker fee and is flagged `known=False` on every opportunity |

Consequence: the fee peaks at $1.75 per 100 contracts at 50c and vanishes at the extremes, so **a
multi-leg bundle pays it several times** - a 6-leg bundle costs ~5.4c of fees per $1 of payoff.

## 2. Execution model (`arbitrage/execution.py`)

Assumptions, stated rather than hidden:

* **atomic, simultaneous** execution of all legs against the book *as observed* - no leg risk, no
  latency (both Stage 6);
* **taker only**, whole contracts, every leg lifts the derived asks best-first;
* legs consume their books independently; fees per fill; no fee at settlement;
* the capital is locked until settlement: `roi` and `annualized_roi` are reported, but the
  cost of capital is not subtracted. (Kalshi's `MECNET` collateral netting on exclusive events can reduce
  the capital tied up; it does not change the payoff arithmetic and is not modelled.)

**Sizing.** Net profit is piecewise linear between price-level boundaries, but the quadratic fee is not
monotone in price, so profit is **not concave** and a greedy "stop at the first losing step" is wrong.
`optimize` evaluates every level boundary (rounded down/up to whole lots), takes the profit-maximising
size (`best`), and separately bisects the break-even size (`breakeven`, beyond which *total* profit is
negative; the marginal contract turns negative much earlier). Verified against pricing every whole-contract
size on 6,000 random books: under the default fee model the search is **exactly optimal** (worst gap 0 µ$);
under whole-cent rounding the fee is a sawtooth and the optimum can move by less than one cent per fill.

## 3. Classification and the funnel (`arbitrage/opportunity.py`, `detector.py`)

| Class | Meaning |
|---|---|
| `theoretical` | displayed prices violate a proven constraint, but less than the minimum size is available there |
| `unprofitable_after_costs` | tradable, but fees and slippage consume the edge at every size |
| `partially_executable` | net-profitable, but the profit-maximising size is below the target size |
| `executable` | the profit-maximising size reaches the target (default 100 contracts) |
| `expired_before_execution` | edge gone before the order could arrive - **Stage 6** (latency) |

`Funnel` counts constraints surviving each stage (the spec's central ablation): *priced -> displayed ->
liquid -> after fees -> after slippage -> executable*. Details that matter:

* The **same trade appears inside several relations** (the exclusivity constraint sits in the
  exchange-declared relation, the proven one and the partition). It is counted **once**, credited to the
  strongest evidence; a first version counted it two or three times.
* Only relations at or above `min_level` are priced; the weaker claims can be included as an ablation
  (`any_evidence`).
* Nested ladders use a **lossless O(k) pre-filter**: a 188-rung ladder has 17,578 pairs, but a pair can
  only be violated if `ask_NO(i) + ask_YES(j) < $1`. Equivalence with the full scan is property-tested.

## 4. The guarantee is tested end to end

For every detected opportunity, in **every admissible world**, the realised P&L computed from the actual
fills and fees is `>= net_micro`, and equals it in at least one world (`test_realised_pnl_is_at_least_the_net_edge_in_every_possible_world`
plus a hypothesis version over random books, all relations and all three rounding models). Books priced
from a coherent probability model never yield an opportunity (no false positives).

**Same-market YES/NO arbitrage cannot exist** on a valid book (Stage 3): `ask_YES + ask_NO = 2 - bid_YES -
bid_NO >= 1`. `crossed_books` is an integrity alarm, and none of the 64,852 recorded snapshots (2,129 + 62,723) was
crossed; the tightest book ever seen had YES bid + NO bid = 99c. The exploitable "complementary" case is a two-outcome event (a `Partition`).

## 5. A worked example (`python -m scripts.stage4_arbitrage_demo worked`)

Real structure (the NYC temperature event: six buckets, a proven partition), hand-built books whose YES
asks sum to **0.94**:

| contracts | payoff | vwap cost | slippage | fees | **net** |
|---:|---:|---:|---:|---:|---:|
| 1 | $1.00 | $0.94 | $0 | $0.054 | $0.006 |
| 90 | $90.00 | $84.60 | $0 | $4.840 | **$0.560** |
| 100 | $100.00 | $94.10 | $0.10 | $5.383 | $0.517 |
| 200 | $200.00 | $189.90 | $1.90 | $10.853 | -$0.753 |

A **6.0c** displayed edge leaves **0.63%** net ($0.56 on $89.44): fees take 90% of it, the thinnest leg
(90 contracts) caps the size, and beyond it slippage turns the marginal contract negative. In all six
possible worlds the realised P&L is exactly $0.5603.

## 6. Real books: 535 cycles, 44 minutes, 100 structured events (`results/stage4/replay.json`)

Books recorded by the REST poller (`configs/books_structural.yaml`, chunks fetched concurrently): 589
tickers, 887 relations at `DECLARED` or better, fee schedules for 20 series. Every cycle replayed under five
cost models:

| stage | baseline fees | fee-free | cent rounding | fees x2 | any evidence level |
|---|---:|---:|---:|---:|---:|
| constraint-cycles priced | 238,398 | 238,398 | 238,398 | 238,398 | 272,242 |
| **displayed violation** | **327** | 327 | 327 | 327 | 343 |
| liquid (>= 1 contract) | 275 | 275 | 275 | 275 | 282 |
| **after fees** | **0** | 275 | 0 | 0 | 0 |
| after slippage | 0 | 275 | 0 | 0 | 0 |
| executable (>= 100 contracts) | 0 | 54 | 0 | 0 | 0 |
| distinct episodes | 3 | 3 | 3 | 3 | 18 |

What this says:

* **0.14% of constraint-cycles look violated, and none survives fees.** Displayed margins are small: 229 are
  <= 1c and 98 are 1-2c; none exceeds 2c. Fees on the same bundles are ~3.1c.
* **Fees, not liquidity, decide the outcome.** Fee-free, 275 of 275 liquid violations would pass and 54 would be
  executable at 100 contracts - the best worth $1.25 on ~$860 (0.15%).
* **The violations are one artefact.** 325 of 327 come from a single event, `KXTRUMPAPPROVE` (approval-rating
  buckets: a complete, exchange-declared, and - since this stage - *proven* partition). Four dead outcomes
  are bid at Kalshi's **1c minimum tick** with thousands of contracts each; together with the live buckets
  the YES bids sum to $1.02. It is a genuine mutual-exclusivity violation, but a tick-size floor
  overround on illiquid tails, and fees exceed it (buying NO on the live 74c outcome alone costs 1.35c).
* **Closest approach** by kind (best margin, ticks; negative = no violation): mutually exclusive +200,
  complement -100 (books are tightest at YES bid + NO bid = 99c), exhaustive -700, union -1500.
* Including weaker evidence adds 16 more displayed cases (two tennis matches whose exhaustiveness is only
  empirical); none survives fees either.

**This is not evidence that arbitrage never exists** - it is 44 minutes of one market regime, taker-only,
with no latency. It is a measurement of what the *static* structure offers, and a baseline for Stage 6.

## 7. Bugs the real data found in earlier stages (all fixed, with regression tests)

* `KXTRUMPAPPROVE`'s "is exactly 39.4%" markets had no comparator the parser knew, so the event fell back to
  the exchange flag; an `eq` comparator (with a lookbehind so "is" stays in the template and "at exactly
  1:00 PM" is not mistaken for a threshold) now proves it. Partition instances 513 -> 546, 0 violations.
* Two markets sharing a lower bound (`[40,40]` and `(40,inf)`) were tiled in ticker order, which sometimes
  looked like a gap: analysis now provably does not depend on the order markets arrive in.
* Opening an older-schema database read-only crashed (`series_meta` missing); reads now tolerate it, and the
  content fingerprint no longer depends on schema version or process bookkeeping.
* The book poller now fetches chunks concurrently (a sequential 20-chunk cycle skewed one event's markets by
  ~10 s) and unwraps `TaskGroup` errors so a transient API failure cannot kill a days-long recorder.
* An LP oracle instability (Stage 3) and a mislabelled break-even size were found by property tests.

## 8. Limitations

* **Not atomic, not latency-adjusted**: REST snapshots have a ~360 ms request-to-receive window and chunks
  are only near-simultaneous; sub-second effects need the WebSocket recorder (still unverified live).
* **Taker, whole contracts, target 100 contracts**: other sizes/roles are parameters, not results.
* **Fee schedule from the API + docs**, verified against one worked example, not against an actual fill.
* **One regime**: the recording is a Saturday morning; frequency conclusions belong to Stage 6 over days.
* No position limits, no cost of capital, no `MECNET` netting.

## 9. Reproduce

```bash
.venv/bin/python -m data.collectors.run books --config configs/books_structural.yaml --duration 2700
.venv/bin/python -m scripts.stage4_arbitrage_demo worked
.venv/bin/python -m scripts.stage4_arbitrage_demo replay --books-db var/books_structural.duckdb
```
