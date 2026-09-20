# Stage 9 — A baseline market maker for binary contracts

Everything here is **paper**: the strategy places *simulated* orders against recorded order books and
trade tapes. No order is ever sent to Kalshi, and no credential is read.

The aim is a rigorous, reproducible measurement of what a plain, well-behaved market maker earns on
Kalshi's binary contracts, and *why*. It is not an attempt to show a profit. Where the answer is
"nothing", this document says so.

## 1. What was built

| module | what it holds |
|---|---|
| `market_making/quoting.py` | the pricing mathematics: a bounded-support version of Avellaneda–Stoikov for a 0/1 claim |
| `market_making/risk.py` | binary-contract inventory risk (§13 of the spec): worst/best/expected settlement value, event exposure, limits, kill-switch |
| `market_making/baseline.py` | `BinaryMarketMaker`, the non-adaptive strategy that runs on the Stage 5 engine |
| `research/market_making_analysis.py` | the evaluation (§18): P&L decomposition, risk, execution, breakdowns, ablations, sweeps |
| `research/market_making_hypotheses.py` | the pre-registered hypotheses, scored mechanically |
| `scripts/stage9_baseline_mm.py` | the driver: frozen-code guard, development / confirmatory roles |

Tests: `tests/test_mm_quoting.py` (15), `test_mm_risk.py` (15), `test_mm_baseline.py` (20),
`test_mm_analysis.py` (24); the full suite is 687 tests. The risk and quoting properties are asserted
against brute force and independent recomputations.

**Mutation testing.** 17 deliberate defects in the evaluation and hypothesis code and 35 in the
quoting, risk and strategy code were injected one at a time; each must make some test fail. Thirteen
survived at first. Eleven exposed a real gap (e.g. NO-side edge and markout were untested because
the test mid was exactly 50¢; killing the strategy did not check that quotes in *quiet* markets are
pulled; sibling-market positions were not checked against the event limit), so the tests were
strengthened. The other two are equivalent in effect and still survive: `headroom` halves a size until
it fits, so a different shrink factor still returns a size that fits (its docstring says "largest";
it is *a size that fits, within 2× of the largest*), and the `_cancelling` guard in `_manage`
duplicates the guard inside `_cancel`.

## 2. Why Avellaneda–Stoikov cannot be used as derived

Avellaneda & Stoikov (2008) price a dealer in a stock with mid `dS = σ dW`, CARA utility, a horizon `T`
and fill intensity `λ(δ) = A e^{−kδ}`. They get a reservation price and a spread

```
r = s − q γ σ² (T − t)            spread = γ σ² (T − t) + (2/γ) ln(1 + γ/k)
```

Each ingredient breaks for a contract that settles at 0 or $1:

1. **Unbounded price.** `s` is arithmetic Brownian, so `r` can leave [0, 1]. A Kalshi price is strictly
   inside (0, $1).
2. **Constant volatility.** A probability is a bounded martingale; its variance must vanish at 0 and 1
   (locally `σ√(p(1−p))`). A constant `σ` is wrong exactly where risk is smallest.
3. **A deterministic risk clock.** AS's inventory risk shrinks with `(T − t)`. For a martingale that
   ends at `X ∈ {0,1}` the total remaining variance is `E[(X − p_t)² | F_t] = p_t (1 − p_t)`,
   *whatever the time left*. It moves when information arrives (`p_t` drifting to an edge), not with
   the clock. Time to settlement still matters, but through how fast information (and adverse
   selection) arrives, not through a `(T − t)` factor. §10 measures this.
4. **The terminal condition.** AS liquidates at the market price at `T`. A binary position *settles*: a
   jump to 0 or $1. The worst case is the whole cost.
5. **YES/NO symmetry.** Kalshi has only bids: the "ask" on YES is a bid on NO. The right model is
   invariant under `p → 1−p, q → −q`.

## 3. The bounded formulation

Hold `q` YES contracts (`q < 0` = long NO) to settlement, with belief `p` and CARA utility. Terminal
wealth is `qX`, so the exact certainty equivalent is

```
V(q) = −(1/γ) ln( 1 − p + p e^{−γq} )
bid*(q) = V(q+1) − V(q)        ask*(q) = V(q) − V(q−1) = bid*(q−1)
```

`bid*` is the most the dealer will pay for one more YES; `ask*` the least he will take to sell one.
These are tested to satisfy:

* `0 < bid* < ask* < 1` for every `q` — bounded **by construction**, no clipping;
* decreasing in `q` (the inventory skew);
* the duality `ask*(q; p) = 1 − bid*(−q; 1−p)`;
* `γ → 0` gives `p`; for small `γ`, `bid*(q) ≈ p − γ p(1−p)(q + ½)` — AS's form with the remaining
  variance `p(1−p)` in place of `σ²(T−t)`;
* the structural spread `ask* − bid* ≈ γ p (1−p)`: widest at 50%, vanishing at the edges.

The AS **liquidity term** is unchanged because it comes from the fill-intensity model, not from the
price process: `δ = (1/γ) ln(1 + γ/k)`. The quotes are `bid*(q) − δ` and `ask*(q) + δ`, rounded to the
tick *away from* the reservation price and clipped so they never cross the visible market.

`γ` is risk aversion **per contract**; useful values are 0.02–0.1 for positions of tens of contracts.
`k` is per dollar of distance; `δ ≈ 1/k` (2¢ at `k = 50`).

## 4. The baseline strategy

* **Fair value** = the book mid. Stage 8 found the market price well calibrated and no Stage 7 model
  beat it, so this baseline does not try to out-forecast the market.
* **Quotes** a YES bid and a NO bid (the YES ask is a NO bid at `1 − a`), post-only, `size` = 10
  contracts each, `γ = 0.05`, `k = 50`, tick 1¢.
* **No quote** when the book is stale (> 30 s), one-sided, wider than 15¢, the mid is outside
  [3¢, 97¢], the market is closed, or < 15 min to the scheduled end.
* **Size** is halved until the worst *settlement* loss after a fill stays inside the limits (§5); it
  is *a* size that fits, within 2× of the largest that does.
* **Order management under latency**: a submitted order is not visible until it reaches the
  exchange, and a cancel takes effect only after its own latency, so the strategy acts on a
  (market, side) at most once per `busy_s`, re-checks after a wake-up, and never stacks duplicates
  (tested).

It is **deliberately non-adaptive**: no order-book imbalance, order flow, recent-fill or
adverse-selection signal. Those are Stage 10's components, and this baseline is what they will be
ablated against.

## 5. Inventory risk (spec §13)

For a market, the whole future of a position is two numbers,

```
pnl_if_YES = yes·$1 − cost      pnl_if_NO = no·$1 − cost
worst = min, best = max, expected = p·pnl_if_YES + (1−p)·pnl_if_NO
```

The worst case is exact and model-free; it is the quantity limits are written in (not the
mark-to-market, which says nothing about settlement).

* **Event exposure.** Markets of one event are not independent: in a mutually exclusive and
  exhaustive event at most one YES wins, so long-YES across every outcome cannot lose on all. The
  event worst case is the minimum of the settlement P&L over the *admissible worlds* that the Stage 3
  relations allow (verified against brute-force enumeration); with no known relation it falls back to
  the conservative sum of per-market worsts. Portfolio exposure is the sum of event worst cases.
* **Limits** (`RiskLimits`): per-market position (50), per-event worst-case loss ($150), portfolio
  worst-case loss ($1,000), drawdown from the equity peak ($300), stop-quoting horizon before the
  scheduled end (15 min), book age (30 s), price band [3¢, 97¢].
* **Skew** is the soft pressure toward the limits; the limits are hard constraints. Inventory
  skew lives in the pricing model, not here.
* **Kill-switch.** A drawdown ≥ the limit, or worst-case portfolio loss > 1.05× its limit, cancels
  every resting order and stops quoting for good (audited once).

Partition evidence: an event's markets are treated as one partition only at Stage 3 evidence
`DECLARED` or better (the conservative choice; §7 reports the `EMPIRICAL` alternative).

## 6. How the evaluation works (spec §15, §18)

Per run, on the Stage 5 engine (information-time events, latency, taker walk, maker queue models with
the pessimistic and optimistic bounds):

* **P&L** — gross, net, realised, mark-to-market, fees, and the split *gross = spread captured +
  inventory contribution*. *Spread captured* is `(mid at the fill − fill price)` on the side bought;
  the rest is what the inventory earned afterwards and at settlement.
* **Risk** — volatility per minute, annualised Sharpe/Sortino (**not meaningful over a 1–6 h
  window**; shown for completeness), maximum drawdown, worst settled market and event, inventory
  distribution, event concentration of settled P&L, kill-switch state, peak worst-case exposure.
* **Execution** — order and quantity fill rates, cancellation rate, quote lifetime, spread captured,
  inventory turnover, and **adverse selection** as the *markout*: mid `h` seconds after a fill minus
  the fill price, on the side bought (negative = the market moved against the quote).
* **Breakdowns** — category, liquidity (trailing-hour volume), time to resolution, probability range.
* **Settled P&L per fill** = `qty × (payoff − price)`. For markets that have settled this is *exact*
  (a YES/NO pair nets to $1 either way), so it sums to the engine's settled P&L (tested), and any
  grouping of fills attributes P&L without approximation. It does not depend on the stale-book mid.
* **Inventory risk vs time to resolution** — per fill, the standard deviation of holding the inventory
  to settlement, `|q| √(p(1−p))`, and the 30 s markout, by time-to-resolution bucket.
* **Sensitivity** — five maker fill models × two latencies (200 ms, 1 s); a 3×3 grid over `γ` and
  `k`; the `EMPIRICAL` partition level.
* **Ablation** — A naive join of the best bid; B fixed width, no skew, no limits; C fixed width, no
  skew, limits + kill-switch; D skew, no limits; E skew + limits, no kill-switch; F the baseline. The
  no-skew variants use `γ ≈ 0` and a fixed half-spread equal to the baseline's at `p = ½`, so only the
  skew differs.

All intervals are 95% percentile intervals of an **event-cluster bootstrap** (siblings of one event
share an outcome). Databases are separate $10,000 accounts; their P&L adds, and rates are
re-estimated over all fills with events (prefixed by database) as the clusters.

## 7. Data, roles and caveats

| role | databases | notes |
|---|---|---|
| development | `books_shortlived`, `books_structural` | ~1.1 h and ~0.7 h of polled books. `books_shortlived` is 176 live-sports markets with 116k trades and 107 settled; `books_structural` has **no trade tape**, so passive fills there come only from book changes and are rare |
| confirmatory | `books_research_short`, `books_research_wide` | ~6 h each; never run with a market maker. They *were* used for the Stage 6 arbitrage scans and the Stage 8 calibration (so their calibration holdout is spent), but no market-making claim has touched them |

**Caveats that shape every result.**

1. *The recorded books are polled about every 3 s and arrive ~0.4 s late; the trade tape is timely.* A
   market maker on a live feed would re-centre its quotes within ~100 ms. Here the strategy quotes
   around a stale mid while fills are triggered by fresh trades, so **the backtest over-states adverse
   selection relative to a live-feed maker**. It is a bound on what this information set allows, not an
   estimate of a live system.
2. Live-sports markets dominate the data: prices jump on events, so the mid a maker quotes around is
   often about to move. That is where the adverse selection lives.
3. Fees use the declared schedule throughout. Maker fees were $0 on every development series and
   $12.5 (3% of the loss) on the confirmatory ones, so fee sensitivity is uninformative here: fees
   are not what decides these results.
4. `pricing.dataset.load_tapes` orders same-timestamp prints arbitrarily (a defect found in Stage 8 and
   left unpatched because Stage 7/8 results are frozen; a follow-up task exists). The market maker
   reads the engine's trade feed, not that loader, for its fills; the loader only supplies the
   trailing-hour volume used for the *liquidity breakdown*, which is insensitive to tie order.
5. A 1–6 hour window is a handful of independent events. The tables show the number of events behind
   every row; rows resting on fewer than 20 are flagged, never headlined.

## 8. Pre-registered hypotheses

Written **after** the development run and **before** the confirmatory data were run with a market
maker. The strategy, the evaluation, the engine files it runs on and the driver are frozen by the
fingerprint below; `--expect-fingerprint` makes the confirmatory run refuse if any of it changed.

Frozen code fingerprint: **`e036156024e9174a`**.

Sufficiency rule: a claim resting on fewer than 20 independent events is *inconclusive*, never
consistent or not.

| id | hypothesis | criterion |
|---|---|---|
| **M1** | The baseline is adversely selected | 30 s markout of its fills (cents, bought side): 95% interval entirely below 0 |
| **M2** | It has no edge, whatever the fill model | hold-to-settlement P&L per fill < 0 **and** net P&L < 0 in every one of the 10 fill-model × latency cells |
| **M3a** | Skew reduces inventory risk | D (skew) has lower mean \|inventory\| **and** a no-worse worst-event loss than B (no skew) |
| **M3b** | …but does not buy profit *(from development, not derived)* | net(D) ≤ net(B) |
| **M4** | The kill-switch caps the drawdown | max drawdown with it (F) ≤ without it (E) |
| **M5** | Wider or more risk-averse quoting is not the fix | no (γ, k) cell has a settled P&L per fill whose 95% interval lies above 0 |
| **M6** | Adverse selection is worse near resolution *(weak: few near-settlement events)* | fill-weighted 30 s markout more negative within 1.5 h of resolution than beyond |
| **M7** | The fill-model bounds behave as bounds | at each latency, fills(optimistic) ≥ fills(pessimistic) |

M1, M2, M5 come from theory (fills against a stale mid are informed) and the development picture; M3b
and M6 are read directly off the development numbers and are labelled as such. The development run
satisfies all eight by construction — that is *not* evidence. Only the confirmatory run tests them.

## 9. Development results (`results/stage9/dev`)

`books_shortlived` + `books_structural`; 1,402 fills over 46 events, 1,383 of them in the live-sports
recording (`books_structural` has no trade tape and produced 19).

* Baseline net **−$317** (gross −$317, fees $0): spread captured −$49, inventory contribution −$268.
  The kill-switch tripped in `books_shortlived` at a $300 drawdown.
* 30 s markout **−2.07¢ [−2.86, −1.40]**; hold-to-settlement P&L **−$0.30 per fill [−0.42, −0.21]**.
* Every one of the 10 fill-model × latency cells and every one of the 9 (γ, k) cells lost money;
  the widest quotes (k = 25) lost least in total only because they were filled least.
* The ablation ordering, and the fact that skew cut inventory ~8× while *increasing* the loss, is what
  M3 and M6 were written from.

## 10. Confirmatory results (`results/stage9/confirm`)

`books_research_short` + `books_research_wide`: ~6 h each, 2,410 fills, 22,415 contracts, 81 events.
The frozen code (fingerprint `e036156024e9174a`) was run once, with `--expect-fingerprint`.

| | |
|---|---|
| Net P&L (two $10k accounts) | **−$425.5** (gross −$413.1, fees $12.5) |
| Spread captured / inventory contribution | −$21.1 / −$392.0 |
| 30 s markout of fills | **−1.42¢ [−1.85, −1.04]** (81 events) |
| Hold-to-settlement P&L per fill | **−$0.177 [−0.229, −0.130]** (47 events, 1,901 settled fills) |
| Max drawdown / worst settled event | $300 (kill-switch tripped in `_short`) / −$10.7 |
| Mean \|inventory\| after a fill | 6.0 contracts (p90 10, max 39) |
| Order fill rate | 8.5% (`_short`), 6.1% (`_wide`); 90–92% of orders are cancelled |

**Pre-registered hypotheses: all eight consistent** — but read them for what they say:

| id | result |
|---|---|
| M1 adverse selection | **holds.** −1.42¢ [−1.85, −1.04] |
| M2 no edge under any fill model | **holds.** −$0.177/fill; net < 0 in 10/10 cells (−$425 to −$495). The fill model moves fills 2,000 → 4,100 but not the sign |
| M3a skew reduces inventory risk | **holds, strongly.** mean \|inventory\| 5.8 vs 51.8 contracts; worst event −$21 vs −$141 |
| M3b …but not profit | holds (−$744 skew vs −$623 no skew, both unlimited): the skewed maker trades ~1.6× as much, and each extra contract is adverse |
| M4 kill-switch caps drawdown | holds: $300 vs $600. It caps a loss; it creates no edge (net −$425 with it, −$744 without) |
| M5 width/risk aversion is not the fix | holds: 0/9 cells above zero; every cell −$345 to −$552 |
| M6 worse near resolution | **satisfied by the letter, null in substance.** −1.44¢ (<1.5 h) vs −1.41¢ (≥1.5 h) is a 0.03¢ gap; the shortest bucket (<0.5 h) has the *least* negative markout (−1.09¢). Not evidence for the claim |
| M7 fill-model bounds | holds: 1,950 ≤ 4,129 (200 ms), 1,990 ≤ 3,944 (1 s) |

### What the data say about *why*

* **The loss is adverse selection, not fees or a missing spread.** Fees are 3% of the loss. Gross
  P&L splits into spread captured (−$21, i.e. no better than zero against a mid that is at most
  ~3 s stale) and an inventory contribution of −$392: the market moves through a resting quote and
  keeps going. Fills are informative about the next price.
* **It is concentrated in Sports** (1,805 of 2,410 fills, 60 events): markout −1.58¢ [−2.09, −1.13];
  settled −$0.18/fill [−0.24, −0.13]; −$317 of the −$336 settled P&L. Crypto, Entertainment, Politics
  and Economics have too few events (≤ 7 each) for an interval that excludes zero, and their
  markouts are mixed. That is *absence of evidence*, not evidence that those markets are safe.
* **Mid-range probabilities are worst** (20–80%: settled −$0.25/fill; the tails 0–5% and 80–100%
  are indistinguishable from zero), where a jump in a live game moves the price furthest.
* **Liquidity**: 97% of fills are in "busy" books (> 100 contracts/hour) with markout −1.47¢
  [−1.91, −1.06]; 71 fills in thin books are inconclusive.
* **Naive join fills the most and loses the most in total** (−$1,101 on 127k contracts) but least
  per contract (−0.9¢, vs −1.9¢ for the baseline): quoting further from the touch did not select
  better fills, it just filled less. The baseline's smaller loss is volume, not quality.

### Inventory risk near settlement

The bounded-martingale claim of §2.3 is that the risk of a position depends on the *state*
(`p(1−p)`), not on a `(T − t)` clock. The remaining standard deviation of the inventory a maker is
left holding, `|q|√(p(1−p))`, is flat across time to resolution:

| time to resolution | <0.5 h | 0.5–1.5 h | 1.5–4 h | 4–12 h | ≥12 h |
|---|---|---|---|---|---|
| mean remaining std of held inventory | $2.17 | $2.39 | $2.52 | $2.32 | $2.46 |
| 30 s markout | −1.09¢ | −1.57¢ | −1.73¢ | +0.05¢ | −0.87¢ |
| events | 6 | 21 | 51 | 8 | 17 |

The strategy stops *adding* inventory 15 minutes before the scheduled end but does not liquidate
what it already holds, so inventory held at that point rides the settlement jump; that outcome is
in the settled P&L above, and the worst-case limits are what bound it. Development had 2.4–2.5 in
the two well-populated buckets and 4.5 in `<0.5 h`, but from 16 fills in 2 events; the confirmatory
data (255 fills, 6 events in that bucket) show no such rise. Neither is a basis for a claim about
the final minutes of a market.

## 11. Limitations and what this does not show

1. **Stale mid.** The strategy quotes around a polled book (~3 s stale, ~0.4 s late) while fills are
   triggered by timely trades. A live-feed maker would re-centre in ~100 ms. So these results are what
   a maker with *this* information set earns; they over-state adverse selection for a live system and
   **do not** show that market making on Kalshi is unprofitable. They show that quoting around a
   stale mid is.
2. **Small sample of independent events**: 81 confirmatory events, 60 of them Sports, from two ~6 h
   windows. No claim is made about other periods, seasons or market types.
3. **Fill realism.** Passive fills come from a queue model over recorded trades. The five models bracket
   the assumption (fills vary ~2×) and no P&L conclusion depends on it, but a live queue position
   is not observable here.
4. **Partition evidence.** The `EMPIRICAL`-partition sensitivity is identical to `DECLARED`: the event
   loss limit never bound for the skewed baseline (mean inventory 6 contracts). The partition logic
   matters for the no-skew variants (B, C), where it does bind.
5. **Sharpe ratios** are over 1–6 hours and are meaningless; they are printed only because the
   spec asks for them.
6. **Parameters were not tuned**: (γ, k) = (0.05, 50) was set from theory before any run; the grid is
   a sensitivity check, not a search. No configuration profits, so no selection effect arises.

## 12. Reproducing

```bash
python -m scripts.stage9_baseline_mm --role development --out results/stage9/dev
python -m scripts.stage9_baseline_mm --role confirmatory --out results/stage9/confirm \
    --expect-fingerprint e036156024e9174a
python -m research.market_making_report results/stage9/confirm/market_making.json
python -m research.market_making_hypotheses results/stage9/confirm/market_making.json
```

## 13. What Stage 10 inherits

The baseline loses to informed flow at every setting. Stage 10 adds the components that address it —
order-book imbalance, microprice, trade-flow imbalance, an adverse-selection estimate and
volatility-conditioned width — and ablates each against this baseline on the same frozen
development / confirmatory split. The bar is the numbers above: a markout of −1.42¢ and −$0.177 per
fill. Note Stage 8 found the microprice significantly *worse* than the mid as a forecast, so the
prior for that component is not favourable.
