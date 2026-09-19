# Backtest engine (Stage 5)

```
local DB ──► StoreFeed ──► [ BookUpdate | BookConfirm | TradeTick | MarketClose | Settlement ]
                                       │  time-ordered, information-time stamped
                                       ▼
   Strategy ◄── Context (state as of `now`) ◄── Backtest engine ──► Portfolio ──► metrics
        │  Order(buy side @ limit, IOC/GTC/POST_ONLY)   │  latency ─► taker walk / maker queue models
        └───────────────────────────────────────────────┘
```

A backtest replays **only local data** in chronological order. The whole design is about one question:
*could a live system have known this at this instant?*

## 1. Causality, enforced structurally

| Guard | How |
|---|---|
| **Information time, not event time** | every event is stamped with when a live system could know it: book = *receive* time; trade = exchange time + `trade_delay` (250 ms); settlement = settlement time + delay; a market close is announced *when it happens*, never earlier |
| **Whitelisted `MarketInfo`** | strategies see static description + the *scheduled* end (`expected_expiration_time`) only. Hidden: `close_time`, `expiration_time`, `status`, `result`, `settlement_*`, `volume`, `open_interest`. A new `Market` field stays invisible until deliberately exposed |
| **Outcome revealed only by event** | `ctx.outcome(t)` is `None` until the `Settlement` event is processed |
| **Latency simulated** | an order takes effect `latency_submit` after the decision and fills against the book *as it is at arrival*; the strategy hears of fills `latency_ack` later; a cancel is not instant, so a fill can beat it |
| **No leverage** | cash is reserved for every resting buy order; orders that would overdraw are rejected |
| **Deterministic** | no randomness; identical inputs give identical outputs |
| **Simultaneous events are atomic** | everything stamped with one instant (e.g. the books of one poll response) is applied *before* the strategy sees any of it (see s.6) |
| **Fixed same-instant order** | `SETTLEMENT < CLOSE < BOOK < TRADE < ORDER_ARRIVAL < CANCEL < FILL_NOTICE < WAKE` |

### Why `close_time` is hidden (a real leak, measured)

In the 2,988 settled markets of the history dataset, `close_time` differs from the scheduled expiry in
**97%**, and 2,414 of the 2,988 closed *early* (doubles tennis matches ~216 min early). It is the moment the
event ended. A strategy given "time to close" would know when each game finished.

### The look-ahead theorem (tested)

For any strategy and any feed, truncating the feed after time *T* leaves every decision and fill at or
before *T* unchanged (`test_decisions_up_to_time_T_depend_only_on_events_up_to_T`: 120 random feeds, each with a maker model
drawn from optimistic / pessimistic / queue). A **meta-test** proves the check has teeth: a strategy that peeks at the total feed length is flagged
by the same comparison. Mutation checks (deliberately breaking the engine or a fill model, then confirming a test fails) were used to
find weak tests; among the mutants caught: pessimistic fills at the price level, the sign of the cancellation
term, no self-impact, and batching disabled. Surviving mutants each became a new test.

## 2. Fill models (`backtest/fills.py`)

**Taker** orders walk the derived asks of the last snapshot, with `max_staleness`, `extra_slippage_ticks`
and `depth_fraction` as explicit haircuts; your own fills consume liquidity until the next snapshot.
Zero-haircut fills equal Stage 4's `walk_asks` exactly (property-tested).

**Maker (resting) orders are underdetermined: historical data contains no queue position.** So passive
results are reported as a *range* over nested models:

| model | fills when |
|---|---|
| `pessimistic` | the market trades *through* our price (so by price-time priority we were exhausted) or a snapshot shows it crossing us |
| `queue(a, c)` | `a` x (displayed size at our price) contracts are ahead; prints at our price consume the queue first; inferred cancellations (a snapshot showing less size than prints explain) move us forward by share `c`; through-prints and crossings fill us |
| `optimistic` | `queue(a=0)`: first in line |

Cumulative fills satisfy `pessimistic <= queue(1,0) <= queue(a,c) <= optimistic` at every instant
(hypothesis over random tapes; a mutation that let the pessimistic model take at-price prints, and one that
reversed the cancellation sign, both survived a first version of the tests and are now caught).

## 3. Accounting (`backtest/portfolio.py`)

Exact integer µ$, exchange-faithful: orders are buys of a side; a YES and NO on one market net to $1 of cash;
settlement pays `X` per YES and `1-X` per NO (fractional for void/scalar markets). Marks are **liquidation
values** (best bid), never mids. The conservation identity `cash == initial + sum((payoff-price)*qty) - fees`
is property-tested against an independent recomputation, and the real-data run's P&L was recomputed from raw
fills and settlements outside the engine (identical to the µ$).

**Stage 4 <-> Stage 5:** with zero latency and full fills, the backtest's realised P&L equals the detector's
predicted net edge *exactly*, in every admissible world (`test_backtest_pnl_equals_the_detectors_predicted_net_edge_exactly`).
With latency, the same trade turns into leg risk: only one leg fills and the "arbitrage" is a directional bet
(`test_latency_turns_a_riskless_arbitrage_into_leg_risk`).

## 4. The dataset: books + trades + settlements for the same markets

Book recordings hold only *open* markets and the trade history holds only *settled* ones, so neither can be
backtested alone. `python -m data.collectors.run refresh` completes a recorded-books DB with current market
state and the trade tape. `configs/books_shortlived.yaml` selects markets **scheduled to expire within 3 h at
recording time** (ex-ante), so settlements arrive soon.

`var/books_shortlived.duckdb`: 177 markets (sports totals/spreads/moneylines), 57,823 keyframes, 116,006
trades, 107 settled. Backtest universe: **53** settled markets with >= 200 snapshots and >= 30 in-window
trades (a post-hoc filter on realised coverage - disclosed, not point-in-time).

**Data quality found on the way** (why the feed has `BookConfirm`): recorders store a book only on change, so
the age of the last stored snapshot says nothing about freshness. Liveness comes from `poll_log`, and its
gaps are real: 715 cycles, median gap 3.0 s, but **a 1,300 s (21.7 min) outage**, a 193 s and three
30-80 s gaps. `BookConfirm` events (one per poll cycle) refresh a book's age without changing its levels, so a
strict `max_staleness` rejects a book inside an outage but not one that is merely unchanged.

## 5. Results on real data (`results/stage5/backtest_demo.json`)

### Audit
Cutting the real feed at five random times inside the recording, decisions before each cut (3,452 to 7,731
orders, 872 to 1,959 fills) are **identical** to the untruncated run. The audit reports a cut with no decisions
as *vacuous* rather than passing it (the first version drew cuts from the whole feed and exercised nothing).

### Passive quoter across the fill models - a test instrument, not a strategy
A naive "join the best bid on both sides" quoter, 10 contracts, 53 markets, 64 minutes:

| maker model | 200 ms latency | 1,000 ms | fills (200 ms) | 30 s markout |
|---|---:|---:|---:|---:|
| pessimistic | -$74.48 | -$110.19 | 1,488 | -184 ticks |
| queue(a=1, c=0) | -$48.61 | -$73.31 | 1,814 | -131 |
| queue(a=1, c=.5) | -$44.69 | -$66.27 | 1,965 | -111 |
| queue(a=.5, c=.5) | -$39.99 | -$72.43 | 2,486 | -80 |
| optimistic | -$0.27 | -$29.74 | 3,689 | -13 |

* The model choice moves P&L by **$74** - more than any plausible edge - which is the point: a passive result
  is a range, and **even the most generous model does not make this strategy profitable** (-$0.27 at best).
* Latency hurts uniformly (every model is worse at 1 s).
* Adverse selection is visible at short horizons: 30 s after a fill the price has moved *against* us
  (markout -13 to -184 ticks; worst in the pessimistic model, where we fill only when the market trades
  through us). At 300 s the markout is *positive* for every model but the pessimistic one (+41 to +78 ticks),
  i.e. the price partly reverts, yet net P&L is still negative. I did not investigate why; markout
  is measured against mid, whereas P&L is realised at settlement.

### Arbitrage taker through the engine (Stage 4 detector, 200 ms latency)

| variant | opportunities | fills | net | worst single settlement |
|---|---:|---:|---:|---:|
| baseline fees | 10 | 27 | **+$0.02** | -$4.47 |
| fee-free (engine exercise) | 37 | 105 | +$9.69 | **-$199.45** |
| fee-free, unverified relations included | 126 | 300 | +$159.30 | -$167.81 |

Fees erase the edge again (consistent with Stage 4). The fee-free rows are an *engine exercise*, and they show
what Stage 4 assumed away: with 200 ms latency a "riskless" bundle often fills only some legs, so a
+$9.69 total hides a **-$199** single-market outcome. The last row includes relations with no evidence
(non-exhaustive events); its profit is windfall from outcomes outside the assumed structure, not arbitrage.

## 6. A bug only real data could expose (fixed, with regression tests)

The first arbitrage run reported **+$983.50 at baseline fees**, contradicting Stage 4. The engine's accounting
was right (an independent recomputation matched to the µ$); the *inputs the strategy saw* were wrong. At
10:14:54 a goal repriced one game (YES bids): PER 0.23 -> 0.66, TIE 0.70 -> 0.21, GAR 0.05 -> 0.01. All three new books
arrived in **one poll response** (identical timestamp) and were mutually consistent, but the engine applied
them one at a time and called the strategy after each. After PER's update, before TIE's, the strategy saw
PER 0.66 next to TIE 0.70 - an impossible market with a phantom exclusivity "arbitrage". Its orders arrived
after all three had updated, so only some legs filled (e.g. PER and GAR filled, TIE expired), leaving
directional bets that happened to win.

Fix: feed events sharing one timestamp are simultaneous and are applied *together* before any strategy
callback; `ArbitrageTaker` reacts once per instant. Regression tests reproduce the scenario and a mutant with
batching disabled fails them. Events at *different* instants (even 1 ns apart) remain separate - a real
sequence of messages does show intermediate states, and the tests document that boundary.

## 7. Limitations

* **Snapshots, not deltas.** Books arrive every ~3 s (REST); the engine assumes the book at an order's
  arrival equals the last snapshot, which is **optimistic for takers** (`max_staleness` and slippage are the
  levers). WebSocket delta replay - the right feed for sub-second work - is unverified live (no API key).
* **Queue position is assumed, not observed** (s.2); passive conclusions hold only across the model range.
* **No market impact on other participants**: our fills do not change what others do.
* **One 64-minute regime**, sports-heavy, on a Saturday; universe filtered post hoc on coverage.
* **History dataset (Stage 2) is survivorship-biased** (filtered on lifetime volume); the book datasets are
  ex-ante. Every result states which it used.
* `trade_delay` (250 ms) and latencies are parameters, not measurements of any real venue.
* Reference strategies are test instruments; nothing here is a trading recommendation.

## 8. Reproduce

```bash
.venv/bin/python -m data.collectors.run books   --config configs/books_shortlived.yaml --duration 2400
.venv/bin/python -m data.collectors.run refresh --config configs/refresh.yaml     # after markets settle
.venv/bin/python -m scripts.stage5_backtest_demo --db var/books_shortlived.duckdb
```
