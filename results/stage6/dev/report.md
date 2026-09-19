# Stage 6 results

role **development** - git `f9b052dc88` - code `663d74308d808b79` - seed 20260919 - 2000 bootstrap draws - evidence >= DECLARED

| dataset | minutes | markets | relations | settled | episodes (all levels) | fingerprint |
|---|---|---|---|---|---|---|
| books_structural | 44.4 | 589 | 887 | 2 | 86 | 3e9339d7224601ea |
| books_shortlived | 64.2 | 177 | 324 | 107 | 103 | dc782d40efb60a15 |

## Counts and exposure (Experiment A)

Observed 109 recording-minutes, 1,250 poll cycles, 161 events, 970 relations = **675 relation-hours** (evidence >= the stated level). Recording gaps > 30 s: 4.

| population | episodes | events with / observed | episodes per 1,000 relation-hours | share of relation-cycles violated | top event's share | effective independent events | if none: 95% upper bound |
|---|---|---|---|---|---|---|---|
| sightings | 71 | 18 / 161 | 105.2 [29.3, 244.8] | 0.02 [0.01, 0.04] % | 52% | 3.4 | - |
| confirmed | 21 | 12 / 161 | 31.1 [12.9, 56.4] | 0.02 [0.01, 0.04] % | 19% | 9.4 | - |

Chunk-skew fingerprint: 22 of 73 sightings persisted to a later poll cycle (30%); the 51 that did not lived at most p50 = 0.10 s, p75 = 2.91 s (71% under one second).

Confirmed episodes by relation kind: chain: 2 (1 events), exhaustive: 0 (0 events), mutually_exclusive: 20 (12 events), partition: 0 (0 events)

## Executability (Experiment B)

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


P(executable | detected) by covariate - sightings, no fees (the option with the most events):


| covariate | bin | episodes | events | P(executable) | Wilson 95% | event-cluster bootstrap 95% |
|---|---|---|---|---|---|---|
| edge | <=1c | 54 | 18 | 24% | 15%-37% | 3%-37% |
| edge | 1-2c | 6 | 4 | 83% | 44%-97% | 25%-100% |
| edge | >2c | 13 | 2 | 100% | 77%-100% | 100%-100% |
| liquidity | <10 contracts | 22 | 11 | 9% | 3%-28% | 0%-21% |
| liquidity | 10-100 | 28 | 13 | 21% | 10%-40% | 0%-34% |
| liquidity | >=100 | 23 | 5 | 100% | 86%-100% | 100%-100% |
| time_to_expiry | <1h | 41 | 3 | 63% | 48%-76% | 0%-100% |
| time_to_expiry | 1-3h | 31 | 16 | 13% | 5%-29% | 3%-26% |
| time_to_expiry | 3-24h | 1 | 1 | 100% | 21%-100% | n/a |
| category | Politics | 1 | 1 | 100% | 21%-100% | n/a |
| category | Sports | 72 | 19 | 42% | 31%-53% | 7%-58% |
| book_depth | below median | 36 | 18 | 17% | 8%-32% | 5%-32% |
| book_depth | at/above median | 37 | 3 | 68% | 51%-80% | 66%-100% |


Bundle payoff vs. promise, on markets that had settled by the time of analysis:


| dataset | cost model | evidence | settled & tradable | events | realised < promised | share (Wilson) | realised $ | promised $ | worst $ |
|---|---|---|---|---|---|---|---|---|---|
| books_shortlived | baseline | DECLARED | 3 | 2 | 0 | 0% (0%-56%) | 0.01 | 0.01 | 0.00 |
| books_shortlived | baseline | UNVERIFIED | 1 | 1 | 0 | 0% (0%-79%) | 0.00 | 0.00 | 0.00 |
| books_shortlived | fee_free | PROVEN | 2 | 1 | 0 | 0% (0%-66%) | 0.42 | 0.42 | 0.12 |
| books_shortlived | fee_free | DECLARED | 18 | 7 | 0 | 0% (0%-18%) | 5.57 | 5.57 | 0.01 |
| books_shortlived | fee_free | UNVERIFIED | 19 | 9 | 0 | 0% (0%-17%) | 5.81 | 5.81 | 0.01 |

## Lifetime and edge decay (Experiment C)

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


## Latency (Experiment D)

**sightings: does the displayed edge survive D?** (bracket: optimistic/pessimistic reading)

| D (s) | n | edge still displayed | unresolved by the data | edge size retained |
|---|---|---|---|---|
| 0.001 | 72 | 79 to 100 % | 21% | 66 to 100 % |
| 0.005 | 72 | 79 to 93 % | 14% | 66 to 90 % |
| 0.01 | 72 | 78 to 90 % | 12% | 65 to 85 % |
| 0.025 | 72 | 69 to 83 % | 14% | 55 to 74 % |
| 0.05 | 72 | 67 to 83 % | 17% | 52 to 74 % |
| 0.1 | 72 | 58 to 64 % | 6% | 44 to 49 % |
| 0.25 | 72 | 50 to 53 % | 3% | 33 to 36 % |
| 0.5 | 72 | 50 to 51 % | 1% | 33 to 35 % |
| 1 | 73 | 49 to 51 % | 1% | 32 to 34 % |
| 2 | 73 | 49 to 51 % | 1% | 32 to 34 % |
| 3 | 73 | 37 to 42 % | 5% | 24 to 29 % |
| 5 | 73 | 29 to 36 % | 7% | 20 to 27 % |
| 10 | 73 | 16 to 18 % | 1% | 13 to 14 % |
| 30 | 73 | 14 to 16 % | 3% | 11 to 15 % |
| 60 | 73 | 5 to 5 % | 0% | 4 to 4 % |

sightings, tradable under **fee_free** (no fees (counterfactual, isolates the market structure)):

| D (s) | n | fills whole | net $ per opportunity | net edge retained | missed |
|---|---|---|---|---|---|
| 0.001 | 70 | 79 to 100 % | 0.56 to 1.02 | 55 to 100 % | 0-15 |
| 0.005 | 70 | 79 to 93 % | 0.56 to 0.88 | 55 to 86 % | 5-15 |
| 0.01 | 70 | 76 to 90 % | 0.55 to 0.79 | 54 to 78 % | 7-17 |
| 0.025 | 70 | 67 to 83 % | 0.42 to 0.68 | 42 to 66 % | 12-23 |
| 0.05 | 70 | 64 to 83 % | 0.38 to 0.68 | 37 to 66 % | 12-25 |
| 0.1 | 70 | 56 to 63 % | 0.29 to 0.34 | 28 to 33 % | 26-31 |
| 0.25 | 70 | 44 to 53 % | 0.13 to 0.16 | 12 to 16 % | 33-39 |
| 0.5 | 70 | 44 to 51 % | 0.13 to 0.15 | 12 to 15 % | 34-39 |
| 1 | 71 | 44 to 51 % | 0.13 to 0.15 | 12 to 14 % | 35-40 |
| 2 | 71 | 44 to 51 % | 0.13 to 0.15 | 12 to 14 % | 35-40 |
| 3 | 71 | 31 to 38 % | 0.06 to 0.12 | 6 to 11 % | 44-49 |
| 5 | 71 | 21 to 25 % | 0.03 to 0.09 | 3 to 9 % | 53-56 |
| 10 | 71 | 13 to 13 % | 0.02 to 0.02 | 2 to 2 % | 62-62 |
| 30 | 71 | 7 to 7 % | 0.02 to 0.02 | 2 to 2 % | 66-66 |
| 60 | 71 | 3 to 3 % | 0.02 to 0.02 | 2 to 2 % | 69-69 |

sightings, tradable under **baseline** (standard taker fees):

| D (s) | n | fills whole | net $ per opportunity | net edge retained | missed |
|---|---|---|---|---|---|
| 0.001 | 11 | 55 to 100 % | 0.25 to 1.06 | 24 to 100 % | 0-5 |
| 0.005 | 11 | 55 to 82 % | 0.25 to 0.72 | 24 to 68 % | 2-5 |
| 0.01 | 11 | 55 to 73 % | 0.25 to 0.45 | 24 to 43 % | 3-5 |
| 0.025 | 11 | 55 to 64 % | 0.25 to 0.31 | 24 to 29 % | 4-5 |
| 0.05 | 11 | 45 to 64 % | 0.22 to 0.31 | 21 to 29 % | 4-6 |
| 0.1 | 11 | 36 to 36 % | 0.16 to 0.16 | 15 to 15 % | 7-7 |
| 0.25 | 11 | 18 to 27 % | 0.00 to 0.00 | 0 to 0 % | 8-9 |
| 0.5 | 11 | 18 to 27 % | 0.00 to 0.00 | 0 to 0 % | 8-9 |
| 1 | 12 | 17 to 25 % | 0.00 to 0.00 | 0 to 0 % | 9-10 |
| 2 | 12 | 17 to 25 % | 0.00 to 0.00 | 0 to 0 % | 9-10 |
| 3 | 12 | 17 to 17 % | 0.00 to 0.00 | 0 to 0 % | 10-10 |
| 5 | 12 | 8 to 17 % | 0.00 to 0.00 | 0 to 0 % | 10-11 |
| 10 | 12 | 8 to 8 % | 0.00 to 0.00 | 0 to 0 % | 11-11 |
| 30 | 12 | 0 to 0 % | 0.00 to 0.00 | 0 to 0 % | 12-12 |
| 60 | 12 | 0 to 0 % | 0.00 to 0.00 | 0 to 0 % | 12-12 |

sightings: the funnel's last stage (executable at first sight -> still fillable whole D later):

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

**confirmed: does the displayed edge survive D?** (bracket: optimistic/pessimistic reading)

| D (s) | n | edge still displayed | unresolved by the data | edge size retained |
|---|---|---|---|---|
| 0.001 | 22 | 100 to 100 % | 0% | 100 to 100 % |
| 0.005 | 22 | 100 to 100 % | 0% | 100 to 100 % |
| 0.01 | 22 | 100 to 100 % | 0% | 100 to 100 % |
| 0.025 | 22 | 100 to 100 % | 0% | 100 to 100 % |
| 0.05 | 22 | 100 to 100 % | 0% | 100 to 100 % |
| 0.1 | 22 | 100 to 100 % | 0% | 100 to 100 % |
| 0.25 | 22 | 100 to 100 % | 0% | 96 to 100 % |
| 0.5 | 22 | 100 to 100 % | 0% | 96 to 100 % |
| 1 | 22 | 100 to 100 % | 0% | 96 to 100 % |
| 2 | 22 | 100 to 100 % | 0% | 96 to 100 % |
| 3 | 22 | 100 to 100 % | 0% | 96 to 100 % |
| 5 | 22 | 82 to 100 % | 18% | 79 to 96 % |
| 10 | 22 | 50 to 50 % | 0% | 50 to 50 % |
| 30 | 22 | 23 to 23 % | 0% | 25 to 25 % |
| 60 | 22 | 14 to 14 % | 0% | 17 to 17 % |

confirmed, tradable under **fee_free** (no fees (counterfactual, isolates the market structure)):

| D (s) | n | fills whole | net $ per opportunity | net edge retained | missed |
|---|---|---|---|---|---|
| 0.001 | 22 | 100 to 100 % | 0.23 to 0.23 | 100 to 100 % | 0-0 |
| 0.005 | 22 | 100 to 100 % | 0.23 to 0.23 | 100 to 100 % | 0-0 |
| 0.01 | 22 | 95 to 100 % | 0.22 to 0.23 | 100 to 100 % | 0-1 |
| 0.025 | 22 | 95 to 100 % | 0.22 to 0.23 | 100 to 100 % | 0-1 |
| 0.05 | 22 | 95 to 100 % | 0.22 to 0.23 | 100 to 100 % | 0-1 |
| 0.1 | 22 | 95 to 100 % | 0.22 to 0.23 | 100 to 100 % | 0-1 |
| 0.25 | 22 | 82 to 100 % | 0.16 to 0.23 | 72 to 100 % | 0-4 |
| 0.5 | 22 | 82 to 100 % | 0.16 to 0.23 | 72 to 100 % | 0-4 |
| 1 | 22 | 82 to 100 % | 0.16 to 0.23 | 72 to 100 % | 0-4 |
| 2 | 22 | 82 to 100 % | 0.16 to 0.23 | 72 to 100 % | 0-4 |
| 3 | 22 | 82 to 86 % | 0.16 to 0.18 | 72 to 79 % | 3-4 |
| 5 | 22 | 68 to 77 % | 0.10 to 0.12 | 44 to 52 % | 5-7 |
| 10 | 22 | 41 to 41 % | 0.08 to 0.08 | 36 to 36 % | 13-13 |
| 30 | 22 | 23 to 23 % | 0.07 to 0.07 | 33 to 33 % | 17-17 |
| 60 | 22 | 9 to 9 % | 0.06 to 0.06 | 26 to 26 % | 20-20 |

confirmed, tradable under **baseline** (standard taker fees):

| D (s) | n | fills whole | net $ per opportunity | net edge retained | missed |
|---|---|---|---|---|---|
| 0.001 | 3 | 100 to 100 % | 0.00 to 0.00 | 100 to 100 % | 0-0 |
| 0.005 | 3 | 100 to 100 % | 0.00 to 0.00 | 100 to 100 % | 0-0 |
| 0.01 | 3 | 100 to 100 % | 0.00 to 0.00 | 100 to 100 % | 0-0 |
| 0.025 | 3 | 100 to 100 % | 0.00 to 0.00 | 100 to 100 % | 0-0 |
| 0.05 | 3 | 100 to 100 % | 0.00 to 0.00 | 100 to 100 % | 0-0 |
| 0.1 | 3 | 100 to 100 % | 0.00 to 0.00 | 100 to 100 % | 0-0 |
| 0.25 | 3 | 67 to 100 % | 0.00 to 0.00 | 40 to 100 % | 0-1 |
| 0.5 | 3 | 67 to 100 % | 0.00 to 0.00 | 40 to 100 % | 0-1 |
| 1 | 3 | 67 to 100 % | 0.00 to 0.00 | 40 to 100 % | 0-1 |
| 2 | 3 | 67 to 100 % | 0.00 to 0.00 | 40 to 100 % | 0-1 |
| 3 | 3 | 67 to 67 % | 0.00 to 0.00 | 40 to 40 % | 1-1 |
| 5 | 3 | 33 to 67 % | 0.00 to 0.00 | 17 to 40 % | 1-2 |
| 10 | 3 | 33 to 33 % | 0.00 to 0.00 | 17 to 17 % | 2-2 |
| 30 | 3 | 0 to 0 % | 0.00 to 0.00 | 0 to 0 % | 3-3 |
| 60 | 3 | 0 to 0 % | 0.00 to 0.00 | 0 to 0 % | 3-3 |

confirmed: the funnel's last stage (executable at first sight -> still fillable whole D later):

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


### Engine sweep (Stage 5 engine, leg risk and realised P&L)

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


## Categories

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

## Sensitivity to the evidence threshold

| relations trusted at >= | sightings | per 1,000 rh | confirmed | per 1,000 rh | executable with fees (sightings / confirmed) | executable no fees (sightings / confirmed) |
|---|---|---|---|---|---|---|
| PROVEN | 3 | 3.5 [0.0, 12.1] | 3 | 3.5 [0.0, 12.1] | 0 / 0 | 1 / 1 |
| DECLARED | 73 | 105.2 [29.3, 244.8] | 22 | 31.1 [12.9, 56.4] | 9 / 0 | 31 / 3 |
| UNVERIFIED | 189 | 219.4 [75.3, 469.1] | 52 | 60.0 [25.8, 101.4] | 19 / 0 | 66 / 6 |

