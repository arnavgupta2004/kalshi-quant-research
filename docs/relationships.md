# Contract relationships (Stage 3)

The reasoning order is fixed, and each step is tested independently:

```
mathematical constraint  ->  which markets does it really apply to?  ->  how sure are we?  ->  prices
 (market/relationships)       (market/semantics: rules, strikes)       (evidence level)      (margin)
```

A "theoretical arbitrage" is only ever a *price* violating a constraint that has already been shown to
hold. This stage builds and validates everything before the price step; Stage 4 adds fees, depth,
slippage and latency.

## 1. One form for every constraint

A **world** assigns each market YES (1) or NO (0). A **relation** says which worlds are possible.
Every relation is compiled into constraints of a single shape:

> buying `qty_i` contracts of each leg `i` pays **at least `bound`** in every admissible world.

Legs are `(market, YES|NO)` and never negative: Kalshi has no naked shorting, "short YES" *is* "buy NO",
so every arbitrage is a buy-only portfolio. If `cost < bound` the shortfall is riskless profit:

    margin = bound - cost        (per bundle, before fees)

| Relation | Statement | Bundle bought | Bound | Violated when |
|---|---|---|---|---|
| `Complement(m)` | X_yes + X_no = 1 | YES_m + NO_m | $1 | book crossed (never in a valid book, see s.3) |
| `MutuallyExclusive(S)` | sum X <= 1 | NO_i for all i in S | (n-1)$ | sum of YES **bids** > 1 |
| `Exhaustive(S)` | sum X >= 1 | YES_i for all i | $1 | sum of YES **asks** < 1 |
| `Partition(S)` | sum X = 1 | both of the above | | either |
| `Implies(A,B)` | X_A <= X_B | YES_B + NO_A | $1 | bid(A) > ask(B) |
| `Chain(m0..mk)` | m0 ⊆ m1 ⊆ ... | `Implies` for **every** pair | | any pair |
| `Union(parts, W)` | X_W = sum X_p | cover: NO_W + YES_p; sum: YES_W + NO_p | $1 ; n$ | parts' bids > W's ask, or W priced above its parts |

### Why these are trustworthy (`tests/test_relationships.py`)

For every relation and size, four independent checks, none of which reuses the constraint builder:

1. **soundness** - every admissible world satisfies every constraint;
2. **characterisation** - every *inadmissible* world violates at least one constraint;
3. **tightness** - every bound is attained (no slack that would hide an arbitrage);
4. **LP oracle** - with random bid/ask quotes, "some constraint violated" is *exactly* "no probability
   distribution over admissible worlds fits inside the quotes" (checked with scipy's LP in both
   directions: hypothesis tests in the suite, plus a one-off stress of 37,500 random quote sets across
   every relation type with **0 mismatches**, 22,758 violated / 14,742 coherent; and a
   coherent-prices-never-flag property test).

Two things the oracle taught us. First, the oracle itself must be trusted: hypothesis found an
exactly-coherent instance that HiGHS called infeasible when the LP was scaled by the 10,000 tick size
(tolerance 1e-6); it now solves in probability units and `test_oracle_is_numerically_robust` guards it.
Second, a design point worth remembering: **a `Chain` needs all O(k^2) pairs**. With spreads,
`bid0 <= ask1` and `bid1 <= ask2` do not imply `bid0 <= ask2`, so an adjacent-pairs implementation
silently misses arbitrage (regression test `test_chain_needs_non_adjacent_pairs`).

## 2. Deciding which relations apply - never from titles

`market/semantics.py` infers relations from settlement definitions and labels each with an
**evidence level** and the assumptions it rests on:

| Level | Meaning | Example |
|---|---|---|
| `PROVEN` | follows from the definitions alone | "BTC above 68,700" implies "BTC above 68,600" |
| `LATTICE` | proven *if the settlement variable is quantised* | temperature buckets [80,81],[82,83] partition the outcomes only if temperature is an integer |
| `DECLARED` | asserted by the exchange (`event.mutually_exclusive`) | a tennis match's two outcomes |
| `EMPIRICAL` | supported by >= 30 settled events of the series, upper 95% violation bound <= 10% | 3-way soccer is exhaustive because "Tie" exists |
| `UNVERIFIED` | no support | exact-score markets that omit some scores |

**Same variable <=> same rules template.** `strike_type` alone is wrong: an MLB spread event has six
`greater` markets - "Dodgers win by more than *k*" and "Giants win by more than *k*" - with identical
strike types and values but different variables. Markets are comparable only if `rules_primary`
is identical once the comparison clause is masked, they close at the same instant, and `custom_strike`
matches. The strike fields must also agree with the comparison parsed from the text (exactly, or on the
shared grid, which caps the level at `LATTICE`); disagreement is a reported conflict and the market is
excluded. Completeness: exhaustiveness/partition are only asserted when *every* market of the event is
held (`event.market_tickers` fully covered).

### Semantic assumptions (each is an explicit, testable statement)

| | Assumption | Corroborated by |
|---|---|---|
| A1 | Every market resolves normally; void/scalar settlement can break relations | quantified in s.4 |
| A2 | `between a and b` includes both endpoints | 546 lattice partitions, 0 violations |
| A3 | Half-integer strikes (x.5) describe an **integer-valued** variable (Kalshi uses them on counts to avoid ties) | count-prop chains, 0 violations |
| A4 | Otherwise the variable is quantised at the strike grid (10^-d for the most decimals used) | 10,893 cross-event unions, 0 violations |
| A5 | Identical rules template + close time + `custom_strike` => same variable at the same instant | zero cross-family false links in tests |
| A6 | `mutually_exclusive=True` means at most one market resolves YES | 9,541 flagged events, 0 with two YES |

### Real Kalshi structures implemented

* **Threshold ladders** (`KXBTCD` "above X", total games, gas prices, player props "N+") -> `Chain`.
* **Bucket events** (`KXHIGHNY`, `KXBTC` price ranges) -> `MutuallyExclusive`, and `Partition` on the grid.
* **Ladder <-> range across events**: `KXBTCD` "above 68,699.99" == the union of every `KXBTC` bucket from
  68,700 up (incl. the open top tail) -> `Union`. Both series publish the same index at the same instant, so
  this is a genuine cross-event identity.
* **Multi-outcome events** (3-way soccer, match winners) -> exchange-declared `MutuallyExclusive`.
* **Complement**, for every market.

## 3. A structural fact for Stage 4: same-market YES/NO arbitrage cannot exist

Kalshi publishes bids only, and asks are *derived*: `ask_YES = 1 - bid_NO`. Hence
`ask_YES + ask_NO = 2 - bid_YES - bid_NO >= 1` in any uncrossed book. Buying both sides of one market
can never lock in a profit; `Complement` is an **invariant that detects corrupted/crossed books** (none
in 2,129 recorded snapshots). The exploitable "complementary" opportunities are therefore *cross-market*:
a two-outcome event (`Partition`), implications, unions.

## 4. Verification against real settled outcomes

`python -m scripts.stage3_relationship_study study` replays every inferred relation against the realised
settlement values (fractional values included) of **14,383 settled events / 142,709 markets from 713
series** (`data/collectors/events.py` -> `var/relations.duckdb`, fingerprint `1306fa87faf2a24c`).
Empirical evidence is leave-one-out: an event is never judged by its own outcome.

| relation | level | instances | violated | rate |
|---|---|---:|---:|---:|
| chain | PROVEN | 5,246 | **0** | 0% |
| chain | LATTICE (count props, index encodings) | 2,273 | **0** | 0% |
| mutually_exclusive | PROVEN | 596 | **0** | 0% |
| partition | LATTICE (buckets on the grid) | 546 | **0** | 0% |
| union (ladder = sum of buckets) | LATTICE | 10,893 | **0** | 0% |
| mutually_exclusive | DECLARED | 8,994 | **0** | 0% |
| partition / exhaustive | EMPIRICAL | 8,051 | 9 | 0.11% (6 of 7,837 clean; 3 of 214 with a void) |
| partition / exhaustive | UNVERIFIED | 943 | 43 | 4.6% |

Reading the table:

* **Every structural claim survived reality** (0 of ~19,550 violated), including the lattice and
  inclusive-endpoint assumptions A2-A4 across two independent event families.
* **The evidence levels are calibrated**: violation rate falls monotonically UNVERIFIED (4.6%) ->
  EMPIRICAL (0.11%) -> DECLARED/structural (0%).
* **The exchange flag is reliable for exclusivity** (0 events with two YES) but says nothing about
  exhaustiveness: 49 flagged events (0.5%) had *no* YES. Series such as `KXKLEAGUESPREAD` resolved with no
  YES in 12 of 12 events - exclusive, not exhaustive - and stay UNVERIFIED.
* **Resolution risk is concentrated in player props**: `scalar` (void) settlement in 83% of
  `KXNFLFIRSTTD`, 75% `KXNFLTD`, 43% `KXVENFUTVEGAME` events versus ~2.3% of flagged events overall
  (224 of 9,541). Avoid props for anything that assumes a clean partition.
* **Structure coverage differs sharply by category**: >= 92% of Weather, Economics and Financials
  events have a relation at `LATTICE` or better, versus 23% of Sports (thousands of two/three-outcome
  events that rest on the exchange flag) and 0% of Mentions.
* **Conflicts: 7** of 142,709 markets (`KXDATACENTCON`: strike `65.0` vs text "$65B"), all refused rather
  than guessed. The first run reported 7,510 - four undocumented conventions (K/M/B suffixes, bare "N+"
  thresholds, `>= 26900` encoded as `> 26899.99`, half-integer counts); each is now handled, tested
  with the real strings, and any relation that depends on grid-only agreement is capped at `LATTICE`.

## 5. First look at prices (recorded books, **not an arbitrage claim**)

`scripts.stage3_relationship_study scan` prices the constraints of open events against
`var/books_stage3.duckdb`: 10 exclusivity relations, 6 priceable per cycle, 60 cycles, **0 violations**,
best margin exactly 0. This is top-of-book, fee-free, and non-atomic REST data (~360 ms request-to-receive
window) - it demonstrates the machinery, nothing more.

## 6. Limitations and failure cases

* Unions are only found when the ladder's edges align exactly with bucket edges; partial unions
  (`X_W >= sum X_p`) are not inferred.
* Relations *between* families that need domain knowledge are not inferred (e.g. "Dodgers win by >0" and
  "Giants win by >0" are exclusive, but nothing in the strikes says so).
* The 12/60 most recent settled events per series bias the sample toward recent event formats; the
  `EMPIRICAL` threshold (30 events, <= 10% bound) is a policy, not a fitted value.
* Rules-text parsing is heuristic: it fails safe (cross-checked against strikes) but its recall is
  incomplete - many markets are simply excluded (`Analysis.excluded`).
* Only the real-line/grid semantics of *numeric* thresholds are proven; everything else leans on the
  exchange flag or history.

## 7. Reproduce

```bash
.venv/bin/python -m data.collectors.run events --config configs/events.yaml        # 7.9k events, ~3 min
.venv/bin/python -m data.collectors.run events --config configs/events_deep.yaml   # 60 events for flagged series
.venv/bin/python -m scripts.stage3_relationship_study study                         # -> results/stage3/
.venv/bin/python -m data.collectors.run books --config configs/books.yaml --db var/books_stage3.duckdb --duration 300
.venv/bin/python -m scripts.stage3_relationship_study scan --books-db var/books_stage3.duckdb
```

> **Addendum (Stage 4).** Replaying real order books exposed two more semantic gaps, both fixed: an `is exactly N`
> comparator (approval-rating exact-value markets) and a tie-break when a closed point interval and an open
> half-line share a lower bound. Partition instances rose 513 -> 546 (33 more events now *proven* instead of
> merely exchange-declared) with 0 violations. See `docs/arbitrage.md` s.7.
