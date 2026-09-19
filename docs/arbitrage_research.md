# Arbitrage research (Stage 6)

Stage 4 asked what the *static* structure of the market offers. Stage 5 built a replay engine that cannot
look ahead. This stage asks the four questions of the spec's Experiments A-D, in the spec's format
(hypothesis, data, methodology, assumptions, metrics, results, statistical uncertainty, failure cases,
limitations), plus the category study:

| | Question |
|---|---|
| **A** | How often do structural probability constraints appear to be violated? |
| **B** | What fraction of detected opportunities stay executable after fees and available liquidity? |
| **C** | How quickly does apparent arbitrage edge disappear? |
| **D** | How does latency affect executable arbitrage? |

The code is `research/` (scanner, statistics, analysis, latency, report), driven by
`scripts/stage6_arbitrage_research.py`. Every number below is pasted from a generated report
(`results/stage6/*/report.md`), never retyped.

## 1. Method

### 1.1 Opportunity episodes (`research/scanner.py`)

A **sighting** is a constraint whose displayed prices violate a proven-or-declared relation in the book we
had at some instant. An **episode** is one uninterrupted run of the same sighting; the scanner records what
the spec's lifecycle table asks for: detection time, markets, relation, legs, gross edge, top-of-book and
total liquidity, fees, net edge under each cost model, best size, when it was last seen alive and first
seen gone, why it ended, whether it settled profitably.

* **Incremental but exact.** Only relations touching a book that changed are re-priced. A property test
  proves the open episodes equal a from-scratch `detect` over *every* relation after every batch (with a
  non-vacuity check that the random feeds contain violations).
* **Simultaneous events are one observation** (the Stage 5 lesson). Events with an identical timestamp are
  applied before anything is evaluated.
* **Detection is when *we* first saw it.** A lifetime is measured from there and is *interval-censored*:
  seen alive at `a`, seen gone at `b`, lived somewhere in `[a, b)`. Nothing is imputed.
* **Recording gaps** (> 30 s with no book-side event) censor open episodes at the last time they were
  confirmed and are excluded from exposure.
* **Mutation-tested:** 15 deliberate breakages of the scanner and 17 of the analysis code were each caught
  by a test; survivors found on the way became tests or were deleted as dead code.

### 1.2 Sightings vs. confirmed (the central methodological point)

Books arrive in poll cycles, in chunks of ~50 tickers a fraction of a second apart. A cross-market
"violation" can therefore be **two moments stitched together**: it appears when the first chunk lands and
vanishes when the second does. On the first development dataset **83 of 86 episodes were exactly that**
(one live tennis match; each lived ~0.1 s, the gap between its two markets' chunks; only 2 persisted).
**This did not replicate:** in the confirmatory data only 8% of non-persisted sightings lived under a second
(hypothesis A3 failed, s.4). The artefact is real but event-specific; the sightings/confirmed split is kept
because it is cheap and safe, not because artefacts dominate.

Such a sighting may be a real stale-quote race or a snapshot artefact; polling data cannot say which. So
every table has two populations:

| Population | Definition | Reading |
|---|---|---|
| **sightings** | every episode | upper bound: everything a faster feed might have raced (or an artefact) |
| **confirmed** | still there at the end of a *later* poll cycle (all legs re-fetched) | a standing violation; cannot be a composite |

The truth about "how often is there an opportunity" lies between them, and the gap is itself a result.
Conditioning on "confirmed" makes S(t) = 1 for t below one poll cycle *by construction*; the tables say so
and do not present it as evidence of fast survival.

### 1.3 Uncertainty (`research/stats.py`)

* **Clusters are events.** The episodes of one event share one market state; a naive bootstrap over
  episodes would report a tight interval around an effective sample size near 1. Every interval resamples
  whole events (percentile, 2,000 draws, fixed seed). Each table reports the number of events and an
  *effective number of independent events* (1/HHI of the episode counts).
* **Two interval types for proportions:** Wilson (treats episodes as independent - optimistic) next to the
  event-cluster bootstrap. The wider one is the honest one.
* **Zero counts get bounds, not `[0, 0]`.** A bootstrap over all-zero rows is false precision. With none
  seen in *n* events, the share of events that ever show one is at most `1 - 0.05^(1/n)`; the naive
  per-hour bound is `-ln(0.05)/hours`.
* **Lifetimes are bounds.** `P(life > t)` is reported as `[lower, upper]` (seen alive past *t* / not seen
  dead by *t*), plus Kaplan-Meier under both readings of each interval. An exponential fit is shown only as
  a labelled model. Coverage of every interval type is checked in simulation.
* **Low-n rows are flagged** (< 5 independent events) and not interpreted.

### 1.4 Latency (`research/latency_analysis.py`)

The spec asks for 1-250 ms. Snapshots are seconds apart, so those delays **cannot be resolved**; the
analysis says so quantitatively instead of inventing a number.

* **As-of probes.** For each opportunity and each delay D, the order that would have arrived D after
  detection is evaluated against two books: the last one observed by then (*optimistic*, what the Stage 5
  engine assumes) and the first one observed after it (*pessimistic*: every change landed just before the
  order). Each metric is reported as a bracket per opportunity. The bracket **is** the data's resolving
  power: wide below the polling interval, closing above it.
* **No-flicker assumption.** The bracket treats an edge seen alive at both ends of a poll interval as alive
  throughout it. An edge that vanished and returned *between* two polls is invisible to any polling data,
  and the brackets do not cover it.
* **Engine sweep.** The Stage 4 detector driven through the Stage 5 engine at each latency on the dataset
  with settlements. It adds what probes cannot: **leg risk** (legs of one bundle filling unevenly) and
  realised P&L. It sits on the optimistic side of the bracket.
* **Cost models.** `baseline` = standard taker fees; `fees_half` = a hypothetical fee tier; `fee_free` = a
  counterfactual that isolates market structure from the fee schedule. Sizes are capped at 100 contracts
  (`DetectorParams.max_size`, which provably changes no classification) so a $30 edge on a 1,000-contract
  book is reported at a size a trader would send.

### 1.5 Development vs. confirmatory data

The analysis code was developed against two recordings (`books_structural`, 44 min; `books_shortlived`,
64 min). To avoid reporting numbers the method was tuned on, **two fresh recordings** (`books_research_wide`, `books_research_short`; planned for 3 hours each, see s.4 for what
was actually observed) were taken and analysed **once**, with the code frozen: the
confirmatory run refuses to start unless its code fingerprint matches the one recorded here. Development
results are labelled as such; the confirmatory results are the ones to believe.

## 2. Pre-specified hypotheses (frozen before the confirmatory data were analysed)

Frozen code fingerprint: **`663d74308d808b79`** (`research/`, `arbitrage/`, `backtest/`, `market/` and the
driver script; the confirmatory run refuses to start on any other). At the time of writing the confirmatory
recordings were still in progress and only their poll counts and gaps had been inspected - no episode.

Each hypothesis states the development value it rests on and what "consistent" means on the confirmatory
data. They are reported against these criteria whatever happens.

| # | Hypothesis | Development evidence | Consistent if (confirmatory, pooled, evidence >= DECLARED) |
|---|---|---|---|
| **A1** | Standing (confirmed) violations exist and are rare | 31.1 [12.9, 56.4] per 1,000 relation-hours; 0.02% of relation-cycles | confirmed rate's lower 95% bound > 0 **and** rate < 200 per 1,000 rh |
| **A2** | Most sightings do not persist a full poll cycle (they are composites or races) | 22 of 73 persisted (30%) | persisted share of sightings < 50% |
| **A3** | The composite fingerprint holds: short-lived sightings die within about one inter-chunk delay | 71% of non-persisted lived < 1 s (median 0.10 s) | > 50% of non-persisted sightings have an upper lifetime bound < 1 s |
| **A4** | Violations sit in sports; other categories show none | all 71 sightings in Sports (Crypto 187 rh, Entertainment 84 rh: 0) | every non-sports category with >= 5 events has 0 confirmed episodes, or is reported as a counter-example |
| **A5** | The count is concentrated in a few events | top event 52% of sightings; 3.4 effective events | descriptive: top event's share and effective independent events are reported; no criterion |
| **A6** | Proven relations are rarely the violated ones | 3 of 73 sightings PROVEN | PROVEN share of confirmed < 25% |
| **B1** | Standard fees leave no confirmed opportunity executable at 100 contracts | 0 / 22 (sightings: 9 / 73 = 12%) | confirmed baseline executable share < 5% |
| **B2** | Fees, not liquidity, decide it: removing fees multiplies the executable share | fee-free 3 / 22 vs baseline 0 / 22 | fee-free executable share (confirmed) > 2x the baseline share |
| **B3** | A *proven* relation never pays less than promised; a declared/unverified one rarely does | 0 shortfalls in 39 settled tradable episodes (fee-free view; realised = promised to the cent in each) | zero PROVEN shortfalls (a theorem); DECLARED+UNVERIFIED shortfall share < 10% |
| **C1** | A standing edge lives seconds, not minutes | KM median 6.0-9.0 s | confirmed median lifetime (both readings) inside 3-30 s |
| **C2** | Standing edges end mostly because someone trades them away | 15 of 22 consumed (68%) | consumed share of ended confirmed episodes > 50% |
| **C3** | Bigger displayed size dies faster (someone wants it) | Spearman(liquidity, lifetime) = -0.52 [-0.59, 0.01], sightings | point estimate < 0 |
| **D1** | Sub-cadence latencies are unidentified; the bracket closes above the cadence | width 21 pp at 1 ms, 1-3 pp at 0.5-2 s | sightings bracket width >= 10 pp at 1 ms and <= 5 pp at 1 s |
| **D2** | Half of the displayed edge is gone within a couple of seconds | sightings 49-51% alive at 1 s, 37-42% at 3 s | sightings alive at 3 s in 25-60% (either bound) |
| **D3** | Leg risk is negligible up to ~250 ms and large by a few seconds (engine, DECLARED fee-free) | partial fills 0% to 250 ms, 35% at 3 s, 56% at 10 s | partial share < 5% at 250 ms and > 20% at 3 s |
| **D4** | With standard fees no latency makes the fully hedged bundles worth more than a few dollars | fully filled scored bundles: $0.01 in total (baseline) | scored full-bundle P&L (baseline, every latency) < $5 |

A5 is descriptive only. If a hypothesis fails it is reported as a failure, with the numbers.

*Note added after the confirmatory run:* `research/hypotheses.py` (which scores this table) and the
report renderer were added afterwards. Excluding them, the code hashes to `663d74308d808b79` - identical to
what ran. A label in the renderer ("recording-minutes" counted the sleep gap described below) was also
corrected; no number changed.

## 3. Development results

These are the numbers the hypotheses were formed from (`results/stage6/dev/report.md`; two recordings,
109 minutes). They are **not** evidence of anything beyond those recordings - the method was tuned on them.

<!-- BEGIN dev -->
role **development** - git `f9b052dc88` - code `663d74308d808b79` - seed 20260919 - 2000 bootstrap draws - evidence >= DECLARED

| dataset | span (min, incl. gaps) | markets | relations | settled | episodes (all levels) | fingerprint |
|---|---|---|---|---|---|---|
| books_structural | 44.4 | 589 | 887 | 2 | 86 | 3e9339d7224601ea |
| books_shortlived | 64.2 | 177 | 324 | 107 | 103 | dc782d40efb60a15 |

Observed 81 minutes of recording (span 109 minus 27 in gaps), 1,250 poll cycles, 161 events, 970 relations = **675 relation-hours** (evidence >= the stated level). Recording gaps > 30 s: 4.

| population | episodes | events with / observed | episodes per 1,000 relation-hours | share of relation-cycles violated | top event's share | effective independent events | if none: 95% upper bound |
|---|---|---|---|---|---|---|---|
| sightings | 71 | 18 / 161 | 105.2 [29.3, 244.8] | 0.02 [0.01, 0.04] % | 52% | 3.4 | - |
| confirmed | 21 | 12 / 161 | 31.1 [12.9, 56.4] | 0.02 [0.01, 0.04] % | 19% | 9.4 | - |

Chunk-skew fingerprint: 22 of 73 sightings persisted to a later poll cycle (30%); the 51 that did not lived at most p50 = 0.10 s, p75 = 2.91 s (71% under one second).

Confirmed episodes by relation kind: chain: 2 (1 events), exhaustive: 0 (0 events), mutually_exclusive: 20 (12 events), partition: 0 (0 events)

**sightings** (73 displayed)

| cost model | displayed | liquid | after fees | after slippage | executable (100 contracts) | executable share |
|---|---|---|---|---|---|---|
| baseline | 73 | 71 | 12 | 12 | 9 | 12% |
| fees_half | 73 | 71 | 30 | 30 | 19 | 26% |
| fee_free | 73 | 71 | 71 | 71 | 31 | 42% |

**confirmed** (22 displayed)

| cost model | displayed | liquid | after fees | after slippage | executable (100 contracts) | executable share |
|---|---|---|---|---|---|---|
| baseline | 22 | 22 | 3 | 3 | 0 | 0% |
| fees_half | 22 | 22 | 9 | 9 | 1 | 5% |
| fee_free | 22 | 22 | 22 | 22 | 3 | 14% |

Edge and liquidity (p10 / p50 / p90):

| population | cost model | n | gross edge (cents per contract) | median liquidity at best / capacity (contracts) | tradable | net profit per opportunity ($, at <=100 contracts) | total net $ |
|---|---|---|---|---|---|---|---|
| sightings | baseline | 73 | 1.0 / 1.0 / 3.0 | 22 / 90505 | 12 | 0.00 / 0.63 / 2.78 | 13.18 |
| sightings | fee_free | 73 | 1.0 / 1.0 / 3.0 | 22 / 90505 | 71 | 0.03 / 0.63 / 3.00 | 74.47 |
| confirmed | baseline | 22 | 1.0 / 1.0 / 1.0 | 5 / 4492 | 3 | 0.00 / 0.00 / 0.00 | 0.01 |
| confirmed | fee_free | 22 | 1.0 / 1.0 / 1.0 | 5 / 4492 | 22 | 0.02 / 0.09 / 0.94 | 5.60 |

| dataset | cost model | evidence | settled & tradable | events | realised < promised | share (Wilson) | realised $ | promised $ | worst $ |
|---|---|---|---|---|---|---|---|---|---|
| books_shortlived | baseline | DECLARED | 3 | 2 | 0 | 0% (0%-56%) | 0.01 | 0.01 | 0.00 |
| books_shortlived | baseline | UNVERIFIED | 1 | 1 | 0 | 0% (0%-79%) | 0.00 | 0.00 | 0.00 |
| books_shortlived | fee_free | PROVEN | 2 | 1 | 0 | 0% (0%-66%) | 0.42 | 0.42 | 0.12 |
| books_shortlived | fee_free | DECLARED | 18 | 7 | 0 | 0% (0%-18%) | 5.57 | 5.57 | 0.01 |
| books_shortlived | fee_free | UNVERIFIED | 19 | 9 | 0 | 0% (0%-17%) | 5.81 | 5.81 | 0.01 |

**sightings** (n = 73, censored 0); Kaplan-Meier median lifetime 0.0 to 2.8 s (pessimistic to optimistic reading of each interval)

| t (s) | P(life > t): lower bound | upper bound |
|---|---|---|
| 0.1 | 0.36 [0.15, 0.80] | 0.64 [0.43, 1.00] |
| 0.5 | 0.32 [0.13, 0.73] | 0.52 [0.24, 1.00] |
| 1 | 0.32 [0.13, 0.73] | 0.51 [0.23, 1.00] |
| 3 | 0.32 [0.13, 0.73] | 0.42 [0.20, 0.90] |
| 5 | 0.18 [0.06, 0.47] | 0.32 [0.13, 0.73] |
| 10 | 0.12 [0.03, 0.37] | 0.14 [0.04, 0.40] |
| 30 | 0.07 [0.01, 0.24] | 0.07 [0.01, 0.24] |
| 60 | 0.01 [0.00, 0.08] | 0.01 [0.00, 0.08] |

Ended 73 (censored 0: {}); causes {'quote_change': 49, 'consumed': 24}; consumed by a trade: 33 [11, 81] % (Wilson 23%-44%).
Spearman vs lifetime lower bound - edge: -0.33 [-0.42, 0.30]; liquidity: -0.52 [-0.59, 0.01]; depth: -0.62 [-0.70, -0.06].

Exponential fit (model, not identified below the polling interval): mean life 26.6 s, rate 0.0376 [0.0077, 0.5385] per s.

**confirmed** (n = 22, censored 0); Kaplan-Meier median lifetime 6.0 to 9.0 s (pessimistic to optimistic reading of each interval)

| t (s) | P(life > t): lower bound | upper bound |
|---|---|---|
| 0.1 | 1.00 [1.00, 1.00] | 1.00 [1.00, 1.00] |
| 0.5 | 1.00 [1.00, 1.00] | 1.00 [1.00, 1.00] |
| 1 | 1.00 [1.00, 1.00] | 1.00 [1.00, 1.00] |
| 3 | 1.00 [1.00, 1.00] | 1.00 [1.00, 1.00] |
| 5 | 0.59 [0.38, 0.82] | 1.00 [1.00, 1.00] |
| 10 | 0.41 [0.19, 0.65] | 0.45 [0.23, 0.71] |
| 30 | 0.23 [0.05, 0.45] | 0.23 [0.05, 0.45] |
| 60 | 0.05 [0.00, 0.17] | 0.05 [0.00, 0.17] |

Ended 22 (censored 0: {}); causes {'quote_change': 7, 'consumed': 15}; consumed by a trade: 68 [37, 91] % (Wilson 47%-84%).
Spearman vs lifetime lower bound - edge: 0.17 [no interval]; liquidity: -0.17 [-0.61, 0.28]; depth: 0.03 [-0.63, 0.45].

Exponential fit (model, not identified below the polling interval): mean life 87.1 s, rate 0.0115 [0.0035, 0.1034] per s.


**sightings: does the displayed edge survive D?** (optimistic to pessimistic reading)

| D (s) | n | edge still displayed | unresolved by the data | edge size retained |
|---|---|---|---|---|
| 0.001 | 72 | 79 to 100 % | 21% | 66 to 100 % |
| 0.01 | 72 | 78 to 90 % | 12% | 65 to 85 % |
| 0.05 | 72 | 67 to 83 % | 17% | 52 to 74 % |
| 0.25 | 72 | 50 to 53 % | 3% | 33 to 36 % |
| 1 | 73 | 49 to 51 % | 1% | 32 to 34 % |
| 3 | 73 | 37 to 42 % | 5% | 24 to 29 % |
| 10 | 73 | 16 to 18 % | 1% | 13 to 14 % |
| 30 | 73 | 14 to 16 % | 3% | 11 to 15 % |
| 60 | 73 | 5 to 5 % | 0% | 4 to 4 % |

sightings: executable at first sight -> still fillable whole D later:

| cost model | D (s) | executable at first sight | observable | still fillable (range) |
|---|---|---|---|---|
| baseline | 0.1 | 9 | 8 | 1-1 |
| baseline | 1.0 | 9 | 9 | 0-0 |
| baseline | 3.0 | 9 | 9 | 0-0 |
| baseline | 10.0 | 9 | 9 | 0-0 |
| fees_half | 0.1 | 19 | 18 | 5-5 |
| fees_half | 1.0 | 19 | 19 | 1-2 |
| fees_half | 3.0 | 19 | 19 | 0-1 |
| fees_half | 10.0 | 19 | 19 | 0-0 |
| fee_free | 0.1 | 31 | 30 | 8-10 |
| fee_free | 1.0 | 31 | 31 | 4-5 |
| fee_free | 3.0 | 31 | 31 | 2-4 |
| fee_free | 10.0 | 31 | 31 | 1-1 |

**confirmed: does the displayed edge survive D?** (optimistic to pessimistic reading)

| D (s) | n | edge still displayed | unresolved by the data | edge size retained |
|---|---|---|---|---|
| 0.001 | 22 | 100 to 100 % | 0% | 100 to 100 % |
| 0.01 | 22 | 100 to 100 % | 0% | 100 to 100 % |
| 0.05 | 22 | 100 to 100 % | 0% | 100 to 100 % |
| 0.25 | 22 | 100 to 100 % | 0% | 96 to 100 % |
| 1 | 22 | 100 to 100 % | 0% | 96 to 100 % |
| 3 | 22 | 100 to 100 % | 0% | 96 to 100 % |
| 10 | 22 | 50 to 50 % | 0% | 50 to 50 % |
| 30 | 22 | 23 to 23 % | 0% | 25 to 25 % |
| 60 | 22 | 14 to 14 % | 0% | 17 to 17 % |

confirmed: executable at first sight -> still fillable whole D later:

| cost model | D (s) | executable at first sight | observable | still fillable (range) |
|---|---|---|---|---|
| baseline | 0.1 | 0 | 0 | 0-0 |
| baseline | 1.0 | 0 | 0 | 0-0 |
| baseline | 3.0 | 0 | 0 | 0-0 |
| baseline | 10.0 | 0 | 0 | 0-0 |
| fees_half | 0.1 | 1 | 1 | 1-1 |
| fees_half | 1.0 | 1 | 1 | 0-1 |
| fees_half | 3.0 | 1 | 1 | 0-0 |
| fees_half | 10.0 | 1 | 1 | 0-0 |
| fee_free | 0.1 | 3 | 3 | 3-3 |
| fee_free | 1.0 | 3 | 3 | 2-3 |
| fee_free | 3.0 | 3 | 3 | 2-2 |
| fee_free | 10.0 | 3 | 3 | 1-1 |


**books_shortlived - declared_baseline_fees**

| latency (ms) | opportunities | bundles | fully filled | partial (leg risk) | unfilled | net $ (whole run) | scored bundles | full bundles $ | partial bundles $ | worst bundle $ |
|---|---|---|---|---|---|---|---|---|---|---|
| 0 | 10 | 10 | 10 | 0 | 0 | 0.98 | 6 | 0.01 | 0.00 | 0.00 |
| 5 | 10 | 10 | 10 | 0 | 0 | 0.98 | 6 | 0.01 | 0.00 | 0.00 |
| 25 | 10 | 10 | 10 | 0 | 0 | 0.98 | 6 | 0.01 | 0.00 | 0.00 |
| 100 | 10 | 10 | 10 | 0 | 0 | 0.98 | 6 | 0.01 | 0.00 | 0.00 |
| 250 | 10 | 10 | 10 | 0 | 0 | 0.98 | 6 | 0.01 | 0.00 | 0.00 |
| 1000 | 10 | 10 | 9 | 1 | 0 | 2.33 | 6 | 0.01 | 1.35 | 0.00 |
| 3000 | 10 | 10 | 6 | 4 | 0 | 1.85 | 6 | -0.02 | 1.43 | -0.03 |
| 10000 | 10 | 10 | 1 | 9 | 0 | -1.28 | 6 | 0.00 | -1.46 | -0.53 |

**books_shortlived - declared_fee_free**

| latency (ms) | opportunities | bundles | fully filled | partial (leg risk) | unfilled | net $ (whole run) | scored bundles | full bundles $ | partial bundles $ | worst bundle $ |
|---|---|---|---|---|---|---|---|---|---|---|
| 0 | 114 | 114 | 114 | 0 | 0 | 127.62 | 29 | 4.97 | 0.00 | 0.01 |
| 5 | 114 | 114 | 114 | 0 | 0 | 127.62 | 29 | 4.97 | 0.00 | 0.01 |
| 25 | 114 | 114 | 114 | 0 | 0 | 127.62 | 29 | 4.97 | 0.00 | 0.01 |
| 100 | 114 | 114 | 114 | 0 | 0 | 127.62 | 29 | 4.97 | 0.00 | 0.01 |
| 250 | 114 | 114 | 114 | 0 | 0 | 127.62 | 29 | 4.97 | 0.00 | 0.01 |
| 1000 | 114 | 114 | 113 | 1 | 0 | 128.39 | 29 | 4.77 | 0.97 | 0.01 |
| 3000 | 115 | 115 | 74 | 40 | 1 | 193.95 | 28 | 2.16 | 81.89 | -6.49 |
| 10000 | 115 | 115 | 43 | 64 | 8 | 175.61 | 27 | 0.27 | 57.81 | -8.46 |

**books_shortlived - any_evidence_fee_free**

| latency (ms) | opportunities | bundles | fully filled | partial (leg risk) | unfilled | net $ (whole run) | scored bundles | full bundles $ | partial bundles $ | worst bundle $ |
|---|---|---|---|---|---|---|---|---|---|---|
| 0 | 220 | 218 | 218 | 0 | 0 | 34.99 | 40 | 7.78 | 0.00 | 0.01 |
| 5 | 220 | 218 | 218 | 0 | 0 | 34.99 | 40 | 7.78 | 0.00 | 0.01 |
| 25 | 220 | 218 | 217 | 1 | 0 | 42.64 | 40 | 7.78 | 0.00 | 0.01 |
| 100 | 220 | 218 | 217 | 1 | 0 | 42.64 | 40 | 7.78 | 0.00 | 0.01 |
| 250 | 220 | 218 | 216 | 2 | 0 | 41.76 | 40 | 7.78 | 0.00 | 0.01 |
| 1000 | 220 | 218 | 215 | 3 | 0 | 42.53 | 40 | 7.58 | 0.97 | 0.01 |
| 3000 | 222 | 220 | 133 | 86 | 1 | 40.15 | 39 | 2.59 | 31.38 | -56.44 |
| 10000 | 223 | 221 | 65 | 146 | 10 | 61.02 | 38 | 0.27 | -10.73 | -54.17 |


| category | relation-hours | events | sightings | sightings per 1,000 rh | confirmed | confirmed per 1,000 rh | executable (no fees) | executable (fees) | < 5 events |
|---|---|---|---|---|---|---|---|---|---|
| Climate and Weather | 12 | 2 | 0 | 0 (<= 252.8 naive) | 0 | 0 (<= 252.8 naive) | 0 | 0 | yes |
| Commodities | 23 | 1 | 0 | 0 (<= 130.5 naive) | 0 | 0 (<= 130.5 naive) | 0 | 0 | yes |
| Crypto | 187 | 6 | 0 | 0 (<= 16.0 naive) | 0 | 0 (<= 16.0 naive) | 0 | 0 |  |
| Economics | 16 | 1 | 0 | 0 (<= 183.8 naive) | 0 | 0 (<= 183.8 naive) | 0 | 0 | yes |
| Entertainment | 84 | 9 | 0 | 0 (<= 35.5 naive) | 0 | 0 (<= 35.5 naive) | 0 | 0 |  |
| Politics | 14 | 2 | 0 | 0 (<= 212.9 naive) | 0 | 0 (<= 212.9 naive) | 1 | 0 | yes |
| Science and Technology | 15 | 2 | 0 | 0 (<= 202.3 naive) | 0 | 0 (<= 202.3 naive) | 0 | 0 | yes |
| Sports | 323 | 138 | 71 | 219.9 [64.4, 482.8] | 21 | 65.0 [27.3, 108.5] | 2 | 0 |  |

| relations trusted at >= | sightings | per 1,000 rh | confirmed | per 1,000 rh | executable with fees (sightings / confirmed) | executable no fees (sightings / confirmed) |
|---|---|---|---|---|---|---|
| PROVEN | 3 | 3.5 [0.0, 12.1] | 3 | 3.5 [0.0, 12.1] | 0 / 0 | 1 / 1 |
| DECLARED | 73 | 105.2 [29.3, 244.8] | 22 | 31.1 [12.9, 56.4] | 9 / 0 | 31 / 3 |
| UNVERIFIED | 189 | 219.4 [75.3, 469.1] | 52 | 60.0 [25.8, 101.4] | 19 / 0 | 66 / 6 |


<!-- END dev -->

## 4. Confirmatory results

**What was actually recorded.** Both recordings were planned for 3 hours. The machine slept for about
4.4 hours mid-run and the recorders' clocks paused, so each holds **~105 minutes of real data** (about 211
minutes in total, after two 265-minute gaps that the scanner treats as recording gaps: open episodes censored,
no exposure counted). The recordings were stopped there because extending them needed over an hour more
awake time and no episode had been inspected - a duration choice that cannot depend on the results. That is
less than half the confirmation the design intended; every interval below reflects it.

`results/stage6/confirm/report.md` has every table; the hypothesis scoring is
`results/stage6/confirm/hypotheses.md`.

### 4.1 Verdicts against the pre-specified criteria

<!-- BEGIN hyp -->
11 of 15 scored hypotheses consistent.

| # | verdict | observed | criterion |
|---|---|---|---|
| A1 | consistent | 12.9 [5.9, 22.5] | lower bound > 0 and rate < 200 per 1,000 rh |
| A2 | consistent | 48% persisted | persisted share of sightings < 50% |
| A3 | NOT consistent | 8% of 26 under 1 s | > 50% of non-persisted sightings have upper bound < 1 s |
| A4 | NOT consistent | counter-examples: Climate and Weather (5 confirmed, 5 events) | no non-sports category with >= 5 events has a confirmed episode |
| A6 | consistent | 0 of 24 confirmed are PROVEN | PROVEN share of confirmed < 25% |
| B1 | consistent | 1 / 24 = 4.2% | confirmed baseline executable share < 5% |
| B2 | consistent | fee-free 29.2% vs baseline 4.2% | fee-free executable share > 2x baseline |
| B3 | consistent | 0 shortfalls in 133 settled tradable episodes (0 PROVEN) | 0 PROVEN shortfalls; overall shortfall share < 10% |
| C1 | consistent | 9.6 to 12.0 s | both readings of the median inside 3-30 s |
| C2 | NOT consistent | 33% consumed | consumed share of ended confirmed episodes > 50% |
| C3 | consistent | rho = -0.32 | Spearman(liquidity, lifetime) < 0 |
| D1 | NOT consistent | width 4 pp at 1 ms, 0 pp at 1 s | width >= 10 pp at 1 ms and <= 5 pp at 1 s |
| D2 | consistent | 58% to 74% alive at 3 s | either bound in 25-60% (lenient, as pre-specified) |
| D3 | consistent | 250 ms: 0/105, 3000 ms: 28/106 | partial share < 5% at 250 ms and > 20% at 3 s (DECLARED, fee-free) |
| D4 | consistent | max over latencies of scored full-bundle P&L = $2.51 | scored full-bundle P&L with standard fees < $5 at every latency |


<!-- END hyp -->

### 4.2 Tables

<!-- BEGIN confirm -->
role **confirmatory** - git `f9b052dc88` - code `663d74308d808b79` - seed 20260919 - 2000 bootstrap draws - evidence >= DECLARED

| dataset | span (min, incl. gaps) | markets | relations | settled | episodes (all levels) | fingerprint |
|---|---|---|---|---|---|---|
| books_research_wide | 373.4 | 595 | 892 | 43 | 33 | 84611a500eeff4b9 |
| books_research_short | 367.8 | 399 | 535 | 382 | 172 | a21d5d461e0d0897 |

Observed 211 minutes of recording (span 741 minus 530 in gaps), 3,356 poll cycles, 154 events, 1195 relations = **1862 relation-hours** (evidence >= the stated level). Recording gaps > 30 s: 2.

| population | episodes | events with / observed | episodes per 1,000 relation-hours | share of relation-cycles violated | top event's share | effective independent events | if none: 95% upper bound |
|---|---|---|---|---|---|---|---|
| sightings | 50 | 23 / 154 | 26.8 [14.5, 43.9] | 0.01 [0.00, 0.03] % | 12% | 15.8 | - |
| confirmed | 24 | 13 / 154 | 12.9 [5.9, 22.5] | 0.01 [0.00, 0.03] % | 17% | 10.3 | - |

Chunk-skew fingerprint: 24 of 50 sightings persisted to a later poll cycle (48%); the 26 that did not lived at most p50 = 3.00 s, p75 = 3.18 s (8% under one second).

Confirmed episodes by relation kind: exhaustive: 0 (0 events), mutually_exclusive: 19 (11 events), partition: 5 (2 events)

**sightings** (50 displayed)

| cost model | displayed | liquid | after fees | after slippage | executable (100 contracts) | executable share |
|---|---|---|---|---|---|---|
| baseline | 50 | 45 | 4 | 4 | 2 | 4% |
| fees_half | 50 | 45 | 12 | 12 | 6 | 12% |
| fee_free | 50 | 45 | 45 | 45 | 12 | 24% |

**confirmed** (24 displayed)

| cost model | displayed | liquid | after fees | after slippage | executable (100 contracts) | executable share |
|---|---|---|---|---|---|---|
| baseline | 24 | 22 | 3 | 3 | 1 | 4% |
| fees_half | 24 | 22 | 7 | 7 | 3 | 12% |
| fee_free | 24 | 22 | 22 | 22 | 7 | 29% |

Edge and liquidity (p10 / p50 / p90):

| population | cost model | n | gross edge (cents per contract) | median liquidity at best / capacity (contracts) | tradable | net profit per opportunity ($, at <=100 contracts) | total net $ |
|---|---|---|---|---|---|---|---|
| sightings | baseline | 50 | 1.0 / 1.0 / 2.0 | 14 / 15582 | 2 | 0.14 / 0.14 / 0.15 | 0.29 |
| sightings | fee_free | 50 | 1.0 / 1.0 / 2.0 | 14 / 15582 | 44 | 0.02 / 0.42 / 1.00 | 30.50 |
| confirmed | baseline | 24 | 1.0 / 1.0 / 1.7 | 11 / 7042 | 1 | 0.14 / 0.14 / 0.14 | 0.14 |
| confirmed | fee_free | 24 | 1.0 / 1.0 / 1.7 | 11 / 7042 | 21 | 0.01 / 0.42 / 1.00 | 18.83 |

| dataset | cost model | evidence | settled & tradable | events | realised < promised | share (Wilson) | realised $ | promised $ | worst $ |
|---|---|---|---|---|---|---|---|---|---|
| books_research_wide | fee_free | DECLARED | 4 | 1 | 0 | 0% (0%-49%) | 0.79 | 0.79 | 0.10 |
| books_research_wide | fee_free | UNVERIFIED | 14 | 2 | 0 | 0% (0%-22%) | 8.89 | 8.89 | 0.04 |
| books_research_short | baseline | DECLARED | 2 | 2 | 0 | 0% (0%-66%) | 0.29 | 0.29 | 0.14 |
| books_research_short | baseline | UNVERIFIED | 5 | 4 | 0 | 0% (0%-43%) | 2.09 | 2.09 | 0.00 |
| books_research_short | fee_free | DECLARED | 33 | 17 | 0 | 0% (0%-10%) | 19.43 | 19.43 | 0.01 |
| books_research_short | fee_free | UNVERIFIED | 82 | 22 | 0 | 0% (0%-4%) | 52.55 | 52.55 | 0.01 |

**sightings** (n = 50, censored 0); Kaplan-Meier median lifetime 1.9 to 5.0 s (pessimistic to optimistic reading of each interval)

| t (s) | P(life > t): lower bound | upper bound |
|---|---|---|
| 0.1 | 0.90 [0.78, 0.98] | 1.00 [1.00, 1.00] |
| 0.5 | 0.88 [0.74, 0.96] | 0.96 [0.87, 1.00] |
| 1 | 0.66 [0.49, 0.80] | 0.96 [0.87, 1.00] |
| 3 | 0.48 [0.35, 0.58] | 0.74 [0.59, 0.87] |
| 5 | 0.40 [0.26, 0.51] | 0.52 [0.37, 0.63] |
| 10 | 0.18 [0.06, 0.31] | 0.26 [0.12, 0.38] |
| 30 | 0.10 [0.00, 0.21] | 0.10 [0.00, 0.21] |
| 60 | 0.08 [0.00, 0.19] | 0.08 [0.00, 0.19] |

Ended 50 (censored 0: {}); causes {'quote_change': 27, 'consumed': 23}; consumed by a trade: 46 [28, 68] % (Wilson 33%-60%).
Spearman vs lifetime lower bound - edge: -0.00 [-0.25, 0.21]; liquidity: -0.32 [-0.69, 0.17]; depth: -0.14 [-0.45, 0.22].

Exponential fit (model, not identified below the polling interval): mean life 15.7 s, rate 0.0637 [0.0325, 0.2019] per s.

**confirmed** (n = 24, censored 0); Kaplan-Meier median lifetime 9.6 to 12.0 s (pessimistic to optimistic reading of each interval)

| t (s) | P(life > t): lower bound | upper bound |
|---|---|---|
| 0.1 | 1.00 [1.00, 1.00] | 1.00 [1.00, 1.00] |
| 0.5 | 1.00 [1.00, 1.00] | 1.00 [1.00, 1.00] |
| 1 | 1.00 [1.00, 1.00] | 1.00 [1.00, 1.00] |
| 3 | 1.00 [1.00, 1.00] | 1.00 [1.00, 1.00] |
| 5 | 0.83 [0.65, 0.96] | 1.00 [1.00, 1.00] |
| 10 | 0.38 [0.14, 0.62] | 0.54 [0.27, 0.77] |
| 30 | 0.21 [0.00, 0.41] | 0.21 [0.00, 0.41] |
| 60 | 0.17 [0.00, 0.38] | 0.17 [0.00, 0.38] |

Ended 24 (censored 0: {}); causes {'quote_change': 16, 'consumed': 8}; consumed by a trade: 33 [14, 59] % (Wilson 18%-53%).
Spearman vs lifetime lower bound - edge: -0.10 [-0.62, 0.47]; liquidity: -0.62 [-0.91, 0.01]; depth: -0.01 [-0.43, 0.51].

Exponential fit (model, not identified below the polling interval): mean life 30.6 s, rate 0.0327 [0.0169, 0.1051] per s.


**sightings: does the displayed edge survive D?** (optimistic to pessimistic reading)

| D (s) | n | edge still displayed | unresolved by the data | edge size retained |
|---|---|---|---|---|
| 0.001 | 50 | 96 to 100 % | 4% | 97 to 100 % |
| 0.01 | 50 | 96 to 100 % | 4% | 97 to 100 % |
| 0.05 | 50 | 96 to 100 % | 4% | 97 to 100 % |
| 0.25 | 50 | 96 to 100 % | 4% | 97 to 100 % |
| 1 | 50 | 96 to 96 % | 0% | 97 to 97 % |
| 3 | 50 | 58 to 74 % | 16% | 62 to 78 % |
| 10 | 50 | 24 to 26 % | 2% | 28 to 32 % |
| 30 | 50 | 10 to 12 % | 2% | 12 to 13 % |
| 60 | 50 | 10 to 10 % | 0% | 12 to 12 % |

sightings: executable at first sight -> still fillable whole D later:

| cost model | D (s) | executable at first sight | observable | still fillable (range) |
|---|---|---|---|---|
| baseline | 0.1 | 1 | 1 | 1-1 |
| baseline | 1.0 | 1 | 1 | 1-1 |
| baseline | 3.0 | 1 | 1 | 0-0 |
| baseline | 10.0 | 1 | 1 | 0-0 |
| fees_half | 0.1 | 4 | 4 | 4-4 |
| fees_half | 1.0 | 4 | 4 | 4-4 |
| fees_half | 3.0 | 4 | 4 | 1-2 |
| fees_half | 10.0 | 4 | 4 | 0-0 |
| fee_free | 0.1 | 10 | 10 | 10-10 |
| fee_free | 1.0 | 10 | 10 | 10-10 |
| fee_free | 3.0 | 10 | 10 | 5-7 |
| fee_free | 10.0 | 10 | 10 | 1-1 |

**confirmed: does the displayed edge survive D?** (optimistic to pessimistic reading)

| D (s) | n | edge still displayed | unresolved by the data | edge size retained |
|---|---|---|---|---|
| 0.001 | 24 | 100 to 100 % | 0% | 100 to 100 % |
| 0.01 | 24 | 100 to 100 % | 0% | 100 to 100 % |
| 0.05 | 24 | 100 to 100 % | 0% | 100 to 100 % |
| 0.25 | 24 | 100 to 100 % | 0% | 100 to 100 % |
| 1 | 24 | 100 to 100 % | 0% | 100 to 100 % |
| 3 | 24 | 100 to 100 % | 0% | 103 to 103 % |
| 10 | 24 | 50 to 54 % | 4% | 59 to 66 % |
| 30 | 24 | 21 to 25 % | 4% | 24 to 28 % |
| 60 | 24 | 17 to 17 % | 0% | 21 to 21 % |

confirmed: executable at first sight -> still fillable whole D later:

| cost model | D (s) | executable at first sight | observable | still fillable (range) |
|---|---|---|---|---|
| baseline | 0.1 | 0 | 0 | 0-0 |
| baseline | 1.0 | 0 | 0 | 0-0 |
| baseline | 3.0 | 0 | 0 | 0-0 |
| baseline | 10.0 | 0 | 0 | 0-0 |
| fees_half | 0.1 | 1 | 1 | 1-1 |
| fees_half | 1.0 | 1 | 1 | 1-1 |
| fees_half | 3.0 | 1 | 1 | 1-1 |
| fees_half | 10.0 | 1 | 1 | 0-0 |
| fee_free | 0.1 | 5 | 5 | 5-5 |
| fee_free | 1.0 | 5 | 5 | 5-5 |
| fee_free | 3.0 | 5 | 5 | 5-5 |
| fee_free | 10.0 | 5 | 5 | 1-1 |


**books_research_wide - declared_baseline_fees**

| latency (ms) | opportunities | bundles | fully filled | partial (leg risk) | unfilled | net $ (whole run) | scored bundles | full bundles $ | partial bundles $ | worst bundle $ |
|---|---|---|---|---|---|---|---|---|---|---|
| 0 | 1 | 1 | 1 | 0 | 0 | 0.01 | 1 | 0.01 | 0.00 | 0.01 |
| 5 | 1 | 1 | 1 | 0 | 0 | 0.01 | 1 | 0.01 | 0.00 | 0.01 |
| 25 | 1 | 1 | 1 | 0 | 0 | 0.01 | 1 | 0.01 | 0.00 | 0.01 |
| 100 | 1 | 1 | 1 | 0 | 0 | 0.01 | 1 | 0.01 | 0.00 | 0.01 |
| 250 | 1 | 1 | 1 | 0 | 0 | 0.01 | 1 | 0.01 | 0.00 | 0.01 |
| 1000 | 1 | 1 | 1 | 0 | 0 | 0.01 | 1 | 0.01 | 0.00 | 0.01 |
| 3000 | 1 | 1 | 1 | 0 | 0 | 0.01 | 1 | 0.01 | 0.00 | 0.01 |
| 10000 | 1 | 1 | 0 | 1 | 0 | -2.74 | 1 | 0.00 | -2.74 | -2.74 |

**books_research_wide - declared_fee_free**

| latency (ms) | opportunities | bundles | fully filled | partial (leg risk) | unfilled | net $ (whole run) | scored bundles | full bundles $ | partial bundles $ | worst bundle $ |
|---|---|---|---|---|---|---|---|---|---|---|
| 0 | 36 | 36 | 36 | 0 | 0 | 13.54 | 7 | 7.89 | 0.00 | 0.06 |
| 5 | 36 | 36 | 36 | 0 | 0 | 13.54 | 7 | 7.89 | 0.00 | 0.06 |
| 25 | 36 | 36 | 36 | 0 | 0 | 13.54 | 7 | 7.89 | 0.00 | 0.06 |
| 100 | 36 | 36 | 36 | 0 | 0 | 13.54 | 7 | 7.89 | 0.00 | 0.06 |
| 250 | 36 | 36 | 36 | 0 | 0 | 13.54 | 7 | 7.89 | 0.00 | 0.06 |
| 1000 | 36 | 36 | 36 | 0 | 0 | 13.54 | 7 | 7.89 | 0.00 | 0.06 |
| 3000 | 36 | 36 | 36 | 0 | 0 | 13.54 | 7 | 7.89 | 0.00 | 0.06 |
| 10000 | 44 | 44 | 19 | 24 | 1 | -98.23 | 6 | 0.00 | -98.48 | -53.42 |

**books_research_wide - any_evidence_fee_free**

| latency (ms) | opportunities | bundles | fully filled | partial (leg risk) | unfilled | net $ (whole run) | scored bundles | full bundles $ | partial bundles $ | worst bundle $ |
|---|---|---|---|---|---|---|---|---|---|---|
| 0 | 64 | 64 | 64 | 0 | 0 | 35.59 | 35 | 29.95 | 0.00 | 0.04 |
| 5 | 64 | 64 | 64 | 0 | 0 | 35.59 | 35 | 29.95 | 0.00 | 0.04 |
| 25 | 64 | 64 | 64 | 0 | 0 | 35.59 | 35 | 29.95 | 0.00 | 0.04 |
| 100 | 64 | 64 | 64 | 0 | 0 | 35.59 | 35 | 29.95 | 0.00 | 0.04 |
| 250 | 64 | 64 | 64 | 0 | 0 | 35.59 | 35 | 29.95 | 0.00 | 0.04 |
| 1000 | 64 | 64 | 64 | 0 | 0 | 35.59 | 35 | 29.95 | 0.00 | 0.04 |
| 3000 | 64 | 64 | 64 | 0 | 0 | 35.59 | 35 | 29.95 | 0.00 | 0.04 |
| 10000 | 72 | 72 | 28 | 43 | 1 | -167.32 | 34 | 9.00 | -176.57 | -53.42 |

**books_research_short - declared_baseline_fees**

| latency (ms) | opportunities | bundles | fully filled | partial (leg risk) | unfilled | net $ (whole run) | scored bundles | full bundles $ | partial bundles $ | worst bundle $ |
|---|---|---|---|---|---|---|---|---|---|---|
| 0 | 4 | 4 | 4 | 0 | 0 | 2.49 | 4 | 2.49 | 0.00 | 0.14 |
| 5 | 4 | 4 | 4 | 0 | 0 | 2.49 | 4 | 2.49 | 0.00 | 0.14 |
| 25 | 4 | 4 | 4 | 0 | 0 | 2.49 | 4 | 2.49 | 0.00 | 0.14 |
| 100 | 4 | 4 | 4 | 0 | 0 | 2.49 | 4 | 2.49 | 0.00 | 0.14 |
| 250 | 4 | 4 | 4 | 0 | 0 | 2.49 | 4 | 2.49 | 0.00 | 0.14 |
| 1000 | 4 | 4 | 4 | 0 | 0 | 2.49 | 4 | 2.49 | 0.00 | 0.14 |
| 3000 | 4 | 4 | 2 | 1 | 1 | 8.75 | 3 | 2.20 | 6.54 | 0.14 |
| 10000 | 4 | 4 | 0 | 3 | 1 | -41.51 | 3 | 0.00 | -41.51 | -54.27 |

**books_research_short - declared_fee_free**

| latency (ms) | opportunities | bundles | fully filled | partial (leg risk) | unfilled | net $ (whole run) | scored bundles | full bundles $ | partial bundles $ | worst bundle $ |
|---|---|---|---|---|---|---|---|---|---|---|
| 0 | 69 | 69 | 69 | 0 | 0 | 42.29 | 68 | 42.83 | 0.00 | 0.01 |
| 5 | 69 | 69 | 69 | 0 | 0 | 42.29 | 68 | 42.83 | 0.00 | 0.01 |
| 25 | 69 | 69 | 69 | 0 | 0 | 42.29 | 68 | 42.83 | 0.00 | 0.01 |
| 100 | 69 | 69 | 69 | 0 | 0 | 42.29 | 68 | 42.83 | 0.00 | 0.01 |
| 250 | 69 | 69 | 69 | 0 | 0 | 42.29 | 68 | 42.83 | 0.00 | 0.01 |
| 1000 | 70 | 70 | 66 | 4 | 0 | 118.55 | 69 | 41.66 | 77.43 | -0.45 |
| 3000 | 70 | 70 | 39 | 28 | 3 | -8.72 | 66 | 26.22 | -35.30 | -53.00 |
| 10000 | 75 | 75 | 6 | 64 | 5 | -348.36 | 69 | 2.29 | -351.01 | -73.00 |

**books_research_short - any_evidence_fee_free**

| latency (ms) | opportunities | bundles | fully filled | partial (leg risk) | unfilled | net $ (whole run) | scored bundles | full bundles $ | partial bundles $ | worst bundle $ |
|---|---|---|---|---|---|---|---|---|---|---|
| 0 | 246 | 245 | 245 | 0 | 0 | 146.60 | 241 | 147.72 | 0.00 | 0.01 |
| 5 | 246 | 245 | 245 | 0 | 0 | 146.60 | 241 | 147.72 | 0.00 | 0.01 |
| 25 | 246 | 245 | 245 | 0 | 0 | 146.60 | 241 | 147.72 | 0.00 | 0.01 |
| 100 | 246 | 245 | 245 | 0 | 0 | 146.60 | 241 | 147.72 | 0.00 | 0.01 |
| 250 | 246 | 245 | 245 | 0 | 0 | 146.60 | 241 | 147.72 | 0.00 | 0.01 |
| 1000 | 247 | 246 | 232 | 14 | 0 | 36.59 | 242 | 133.27 | -95.56 | -34.00 |
| 3000 | 247 | 246 | 142 | 100 | 4 | -114.52 | 238 | 81.81 | -195.28 | -62.79 |
| 10000 | 253 | 252 | 46 | 194 | 12 | -769.83 | 236 | 26.93 | -796.89 | -73.00 |


| category | relation-hours | events | sightings | sightings per 1,000 rh | confirmed | confirmed per 1,000 rh | executable (no fees) | executable (fees) | < 5 events |
|---|---|---|---|---|---|---|---|---|---|
| Climate and Weather | 72 | 5 | 8 | 110.8 [0.0, 276.9] | 5 | 69.2 [0.0, 180.0] | 0 | 0 |  |
| Commodities | 56 | 1 | 0 | 0 (<= 53.5 naive) | 0 | 0 (<= 53.5 naive) | 0 | 0 | yes |
| Crypto | 593 | 6 | 0 | 0 (<= 5.0 naive) | 0 | 0 (<= 5.0 naive) | 0 | 0 |  |
| Economics | 40 | 1 | 0 | 0 (<= 75.4 naive) | 0 | 0 (<= 75.4 naive) | 0 | 0 | yes |
| Entertainment | 190 | 8 | 0 | 0 (<= 15.8 naive) | 0 | 0 (<= 15.8 naive) | 0 | 0 |  |
| Politics | 81 | 3 | 0 | 0 (<= 36.9 naive) | 0 | 0 (<= 36.9 naive) | 0 | 0 | yes |
| Science and Technology | 36 | 2 | 0 | 0 (<= 82.7 naive) | 0 | 0 (<= 82.7 naive) | 0 | 0 | yes |
| Sports | 794 | 128 | 42 | 52.9 [29.6, 82.4] | 19 | 23.9 [10.9, 40.9] | 7 | 1 |  |

| relations trusted at >= | sightings | per 1,000 rh | confirmed | per 1,000 rh | executable with fees (sightings / confirmed) | executable no fees (sightings / confirmed) |
|---|---|---|---|---|---|---|
| PROVEN | 0 | 0.0 [0.0, 0.0] | 0 | 0.0 [0.0, 0.0] | 0 / 0 | 0 / 0 |
| DECLARED | 50 | 26.8 [14.5, 43.9] | 24 | 12.9 [5.9, 22.5] | 2 / 1 | 12 / 7 |
| UNVERIFIED | 205 | 91.0 [50.0, 142.9] | 75 | 33.3 [17.2, 53.4] | 3 / 1 | 41 / 21 |


<!-- END confirm -->

## 5. What the four experiments say

Read these with the size of the sample in mind: **~211 minutes of recording, 24 confirmed episodes in 13
events, one Saturday.** They describe this window; they do not establish how the market behaves in general.

**A - frequency.** Standing violations of declared or proven relations existed, but rarely: **12.9 [5.9, 22.5]
per 1,000 relation-hours** (24 episodes; 0.01% of relation-cycles), 26.8 [14.5, 43.9] counting every sighting.
The rate is *not stable across windows*: the development data gave 31.1 and 105.2, 2.4x and 3.9x higher,
because one live event supplied half the sightings there (52% vs 12% here; 3.4 vs 15.8 effective independent
events). Almost all of it is sports; Crypto (593 relation-hours), Entertainment (190) and the small
categories showed none, with the honest bound "no more than ~5 per 1,000 relation-hours" for Crypto (a naive,
optimistic Poisson bound). The exception (A4) is Climate & Weather: two events whose six-bucket partitions sold
for 1-3c under $1 in total, with 0.2-10 contracts at the best price - real, standing (the persistent ones lived 15-180 s), and far too
small to trade.

**B - executability.** This is where the edge dies. Of 24 confirmed episodes, **1 (4%) is executable at 100
contracts with standard fees** (total net profit **$0.14**); without fees 7 (29%). Fees are the binding
constraint, then size: even fee-free, the median opportunity is worth $0.42 at up to 100 contracts. Whenever
a relation *did* settle, the bundle paid at least what it promised - **0 shortfalls in 133 settled tradable
episodes, including the declared and unverified relations** - so the relations themselves were sound here; it
is the price that leaves nothing after fees. (82 of those episodes sat on relations at the
"unverified" level, which nothing had verified: the assumption held every time - evidence, not proof.)

**C - decay.** A standing edge lasts seconds: Kaplan-Meier median **9.6-12.0 s** (both readings), and at least 83%
are still there 5 s after detection (upper bound 100%) but only 38-54% at 10 s. Sightings are shorter (1.9-5.0 s). What ended them
is less clear than in the development data: only **33% of ended confirmed episodes were taken by a trade**
(hypothesis C2 failed; the development figure was 68%) - most simply moved or were pulled. The trade-tape
attribution is only as sharp as the polling interval, so treat that split as indicative.

**D - latency.** Sightings alive at 3 s: 58-74%; at 10 s: 24-26%. Through the engine, leg risk was absent up
to 250 ms (0 of 105 bundles partially filled), 26% at 3 s (28 of 106) and much larger at 10 s (24 of 44 in the
wide recording). But with standard fees there is almost nothing to lose or win: the scored, fully hedged
bundles earned **at most $2.51 in total at any latency**. **Latencies of 1-250 ms cannot be resolved from snapshots
seconds apart** - the brackets say so - and they rest on the no-flicker assumption of s.1.4. What the data do
show is that the displayed edge that survives its first re-observation mostly survives a couple of seconds.

**Category study.** Reported per category with event-cluster intervals; only Sports has enough events and
episodes to say anything (52.9 [29.6, 82.4] sightings and 23.9 [10.9, 40.9] confirmed per 1,000 relation-hours),
and categories with fewer than 5 events are flagged and not interpreted. No category difference is claimed.

## 6. What worked, what failed, and why

**Worked.**
* Treating the *event*, not the episode, as the unit of independence. It turned an apparently large sample
  (71 sightings) into 3.4 effective events in the development data - and explains why the rate moved 2-4x
  between windows.
* Interval-censored lifetimes and optimistic/pessimistic latency brackets: they refused to produce a
  precise-looking 5 ms number the data cannot support.
* Freezing hypotheses and code before the confirmatory data. Without it the four failures below would have
  been easy to explain away; with it they are just results.
* Scoring realised payoffs against promises: settlement is a free empirical test of every "declared" claim.

**Failed (pre-specified, not met).**
* **A3** - the chunk-skew artefact that dominated one development event (83 of 86 episodes) is absent
  elsewhere: only 8% of short-lived sightings lived under a second. It is a hazard to guard against
  (`persisted`), not a general description of sightings.
* **A4** - not only sports: Climate & Weather showed sub-tradable but standing violations.
* **C2** - trades are not the main reason edges disappear here (33%, not >50%).
* **D1** - the sub-cadence bracket was 4 pp wide, not >= 10 pp: in this window nearly every sighting that
  was still there at the next observation stayed. That narrows the bracket; it does not remove the no-flicker
  assumption or the fact that 1-250 ms is physically unobserved.

**Why the headline is negative.** A cross-market arbitrage here needs 2-6 legs; each pays the quadratic
fee (up to ~1.75c per contract at 50c), the displayed edge of a confirmed episode is 1-1.7c (p10-p90; median 1c),
and the median size at the best price is 11 contracts. The structure is right (relations held at settlement); the price and the fee schedule leave
no edge for a taker.

## 7. Limitations and failure cases

* **Small, single-regime sample.** ~211 minutes on one Saturday, 24 confirmed episodes in 13 events, half of
  the intended confirmatory recording. Rates moved 2-4x between windows.
* **Polling, not a stream.** REST snapshots every ~3-5 s, non-atomic across chunks. The WebSocket recorder is
  unverified (no API key). Nothing below ~1 s is measured.
* **No-flicker assumption** in every latency bracket; an edge that vanishes and returns between polls is invisible.
* **Taker only, standard fee schedule.** Maker fills are Stage 9-10; fee tiers are a counterfactual (`fees_half`).
* **Evidence levels.** At the weakest threshold there are 205 sightings, 75 confirmed; the relations that settled
  held, which is not a proof of exhaustiveness for other events.
* **Selection.** Events are the top by 24-hour volume plus structured series; Crypto and Entertainment
  contribute exposure but the sample is dominated by sports hours (794 of 1,862 relation-hours).
* **Development numbers are tuned-on.** Only section 4 is confirmatory, and only where a hypothesis
  was pre-specified. Everything else in this document is descriptive.
* **Capital and netting.** No cost of capital is charged; collateral netting on exclusive events is not modelled.

## 8. Reproduce

```bash
# record (public endpoints only, <= 4 requests/s), then add settlements and the trade tape
python -m data.collectors.run books   --config configs/books_research_wide.yaml --duration 10800
python -m data.collectors.run refresh --config configs/refresh_research_wide.yaml

# development analysis (what the hypotheses were formed from)
python -m scripts.stage6_arbitrage_research --role development \
    --db var/books_structural.duckdb --db var/books_shortlived.duckdb --out results/stage6/dev --engine-sweep

# confirmatory analysis - refuses to run if the analysis code changed
python -m scripts.stage6_arbitrage_research --role confirmatory \
    --db var/books_research_wide.duckdb --db var/books_research_short.duckdb --out results/stage6/confirm \
    --engine-sweep --expect-fingerprint 663d74308d808b79

python -m research.report     results/stage6/confirm/arbitrage_research.json > results/stage6/confirm/report.md
python -m research.hypotheses results/stage6/confirm/arbitrage_research.json > results/stage6/confirm/hypotheses.md
```
