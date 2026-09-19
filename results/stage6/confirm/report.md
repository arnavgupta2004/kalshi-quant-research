# Stage 6 results

role **confirmatory** - git `f9b052dc88` - code `663d74308d808b79` - seed 20260919 - 2000 bootstrap draws - evidence >= DECLARED

| dataset | span (min, incl. gaps) | markets | relations | settled | episodes (all levels) | fingerprint |
|---|---|---|---|---|---|---|
| books_research_wide | 373.4 | 595 | 892 | 43 | 33 | 84611a500eeff4b9 |
| books_research_short | 367.8 | 399 | 535 | 382 | 172 | a21d5d461e0d0897 |

## Counts and exposure (Experiment A)

Observed 211 minutes of recording (span 741 minus 530 in gaps), 3,356 poll cycles, 154 events, 1195 relations = **1862 relation-hours** (evidence >= the stated level). Recording gaps > 30 s: 2.

| population | episodes | events with / observed | episodes per 1,000 relation-hours | share of relation-cycles violated | top event's share | effective independent events | if none: 95% upper bound |
|---|---|---|---|---|---|---|---|
| sightings | 50 | 23 / 154 | 26.8 [14.5, 43.9] | 0.01 [0.00, 0.03] % | 12% | 15.8 | - |
| confirmed | 24 | 13 / 154 | 12.9 [5.9, 22.5] | 0.01 [0.00, 0.03] % | 17% | 10.3 | - |

Chunk-skew fingerprint: 24 of 50 sightings persisted to a later poll cycle (48%); the 26 that did not lived at most p50 = 3.00 s, p75 = 3.18 s (8% under one second).

Confirmed episodes by relation kind: exhaustive: 0 (0 events), mutually_exclusive: 19 (11 events), partition: 5 (2 events)

## Executability (Experiment B)

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


P(executable | detected) by covariate - sightings, no fees (the option with the most events):


| covariate | bin | episodes | events | P(executable) | Wilson 95% | event-cluster bootstrap 95% |
|---|---|---|---|---|---|---|
| edge | <=1c | 42 | 21 | 24% | 13%-39% | 9%-41% |
| edge | 1-2c | 6 | 6 | 17% | 3%-56% | 0%-50% |
| edge | >2c | 2 | 2 | 50% | 9%-91% | 0%-100% |
| liquidity | <10 contracts | 18 | 12 | 0% | 0%-18% | 0%-0% |
| liquidity | 10-100 | 22 | 11 | 9% | 3%-28% | 0%-18% |
| liquidity | >=100 | 10 | 7 | 100% | 72%-100% | 100%-100% |
| time_to_expiry | <1h | 11 | 5 | 18% | 5%-48% | 0%-33% |
| time_to_expiry | 1-3h | 31 | 16 | 32% | 19%-50% | 15%-54% |
| time_to_expiry | >=24h | 8 | 2 | 0% | 0%-32% | 0%-0% |
| category | Climate and Weather | 8 | 2 | 0% | 0%-32% | 0%-0% |
| category | Sports | 42 | 21 | 29% | 17%-44% | 14%-44% |
| book_depth | below median | 25 | 13 | 8% | 2%-25% | 0%-24% |
| book_depth | at/above median | 25 | 15 | 40% | 23%-59% | 18%-59% |


Bundle payoff vs. promise, on markets that had settled by the time of analysis:


| dataset | cost model | evidence | settled & tradable | events | realised < promised | share (Wilson) | realised $ | promised $ | worst $ |
|---|---|---|---|---|---|---|---|---|---|
| books_research_wide | fee_free | DECLARED | 4 | 1 | 0 | 0% (0%-49%) | 0.79 | 0.79 | 0.10 |
| books_research_wide | fee_free | UNVERIFIED | 14 | 2 | 0 | 0% (0%-22%) | 8.89 | 8.89 | 0.04 |
| books_research_short | baseline | DECLARED | 2 | 2 | 0 | 0% (0%-66%) | 0.29 | 0.29 | 0.14 |
| books_research_short | baseline | UNVERIFIED | 5 | 4 | 0 | 0% (0%-43%) | 2.09 | 2.09 | 0.00 |
| books_research_short | fee_free | DECLARED | 33 | 17 | 0 | 0% (0%-10%) | 19.43 | 19.43 | 0.01 |
| books_research_short | fee_free | UNVERIFIED | 82 | 22 | 0 | 0% (0%-4%) | 52.55 | 52.55 | 0.01 |

## Lifetime and edge decay (Experiment C)

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


## Latency (Experiment D)

**sightings: does the displayed edge survive D?** (bracket: optimistic/pessimistic reading)

| D (s) | n | edge still displayed | unresolved by the data | edge size retained |
|---|---|---|---|---|
| 0.001 | 50 | 96 to 100 % | 4% | 97 to 100 % |
| 0.005 | 50 | 96 to 100 % | 4% | 97 to 100 % |
| 0.01 | 50 | 96 to 100 % | 4% | 97 to 100 % |
| 0.025 | 50 | 96 to 100 % | 4% | 97 to 100 % |
| 0.05 | 50 | 96 to 100 % | 4% | 97 to 100 % |
| 0.1 | 50 | 96 to 100 % | 4% | 97 to 100 % |
| 0.25 | 50 | 96 to 100 % | 4% | 97 to 100 % |
| 0.5 | 50 | 96 to 96 % | 0% | 97 to 97 % |
| 1 | 50 | 96 to 96 % | 0% | 97 to 97 % |
| 2 | 50 | 84 to 96 % | 12% | 85 to 97 % |
| 3 | 50 | 58 to 74 % | 16% | 62 to 78 % |
| 5 | 50 | 50 to 52 % | 2% | 52 to 55 % |
| 10 | 50 | 24 to 26 % | 2% | 28 to 32 % |
| 30 | 50 | 10 to 12 % | 2% | 12 to 13 % |
| 60 | 50 | 10 to 10 % | 0% | 12 to 12 % |

sightings, tradable under **fee_free** (no fees (counterfactual, isolates the market structure)):

| D (s) | n | fills whole | net $ per opportunity | net edge retained | missed |
|---|---|---|---|---|---|
| 0.001 | 44 | 95 to 100 % | 0.45 to 0.47 | 95 to 100 % | 0-2 |
| 0.005 | 44 | 95 to 100 % | 0.45 to 0.47 | 95 to 100 % | 0-2 |
| 0.01 | 44 | 95 to 100 % | 0.45 to 0.47 | 95 to 100 % | 0-2 |
| 0.025 | 44 | 95 to 100 % | 0.45 to 0.47 | 95 to 100 % | 0-2 |
| 0.05 | 44 | 95 to 100 % | 0.45 to 0.47 | 95 to 100 % | 0-2 |
| 0.1 | 44 | 95 to 100 % | 0.45 to 0.47 | 95 to 100 % | 0-2 |
| 0.25 | 44 | 95 to 100 % | 0.45 to 0.47 | 95 to 100 % | 0-2 |
| 0.5 | 44 | 95 to 95 % | 0.45 to 0.45 | 95 to 95 % | 2-2 |
| 1 | 44 | 95 to 95 % | 0.45 to 0.45 | 95 to 95 % | 2-2 |
| 2 | 44 | 86 to 95 % | 0.40 to 0.45 | 85 to 95 % | 2-6 |
| 3 | 44 | 57 to 75 % | 0.23 to 0.35 | 49 to 74 % | 11-19 |
| 5 | 44 | 43 to 50 % | 0.20 to 0.23 | 42 to 48 % | 22-25 |
| 10 | 44 | 16 to 16 % | 0.03 to 0.03 | 7 to 7 % | 37-37 |
| 30 | 44 | 5 to 5 % | 0.01 to 0.01 | 2 to 2 % | 42-42 |
| 60 | 44 | 5 to 5 % | 0.01 to 0.01 | 2 to 2 % | 42-42 |

sightings, tradable under **baseline** (standard taker fees):

| D (s) | n | fills whole | net $ per opportunity | net edge retained | missed |
|---|---|---|---|---|---|
| 0.001 | 2 | 100 to 100 % | 0.14 to 0.14 | 100 to 100 % | 0-0 |
| 0.005 | 2 | 100 to 100 % | 0.14 to 0.14 | 100 to 100 % | 0-0 |
| 0.01 | 2 | 100 to 100 % | 0.14 to 0.14 | 100 to 100 % | 0-0 |
| 0.025 | 2 | 100 to 100 % | 0.14 to 0.14 | 100 to 100 % | 0-0 |
| 0.05 | 2 | 100 to 100 % | 0.14 to 0.14 | 100 to 100 % | 0-0 |
| 0.1 | 2 | 100 to 100 % | 0.14 to 0.14 | 100 to 100 % | 0-0 |
| 0.25 | 2 | 100 to 100 % | 0.14 to 0.14 | 100 to 100 % | 0-0 |
| 0.5 | 2 | 100 to 100 % | 0.14 to 0.14 | 100 to 100 % | 0-0 |
| 1 | 2 | 100 to 100 % | 0.14 to 0.14 | 100 to 100 % | 0-0 |
| 2 | 2 | 100 to 100 % | 0.14 to 0.14 | 100 to 100 % | 0-0 |
| 3 | 2 | 50 to 50 % | 0.07 to 0.07 | 48 to 48 % | 1-1 |
| 5 | 2 | 50 to 50 % | 0.07 to 0.07 | 48 to 48 % | 1-1 |
| 10 | 2 | 0 to 0 % | 0.00 to 0.00 | 0 to 0 % | 2-2 |
| 30 | 2 | 0 to 0 % | 0.00 to 0.00 | 0 to 0 % | 2-2 |
| 60 | 2 | 0 to 0 % | 0.00 to 0.00 | 0 to 0 % | 2-2 |

sightings: the funnel's last stage (executable at first sight -> still fillable whole D later):

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

**confirmed: does the displayed edge survive D?** (bracket: optimistic/pessimistic reading)

| D (s) | n | edge still displayed | unresolved by the data | edge size retained |
|---|---|---|---|---|
| 0.001 | 24 | 100 to 100 % | 0% | 100 to 100 % |
| 0.005 | 24 | 100 to 100 % | 0% | 100 to 100 % |
| 0.01 | 24 | 100 to 100 % | 0% | 100 to 100 % |
| 0.025 | 24 | 100 to 100 % | 0% | 100 to 100 % |
| 0.05 | 24 | 100 to 100 % | 0% | 100 to 100 % |
| 0.1 | 24 | 100 to 100 % | 0% | 100 to 100 % |
| 0.25 | 24 | 100 to 100 % | 0% | 100 to 100 % |
| 0.5 | 24 | 100 to 100 % | 0% | 100 to 100 % |
| 1 | 24 | 100 to 100 % | 0% | 100 to 100 % |
| 2 | 24 | 100 to 100 % | 0% | 100 to 100 % |
| 3 | 24 | 100 to 100 % | 0% | 103 to 103 % |
| 5 | 24 | 100 to 100 % | 0% | 103 to 103 % |
| 10 | 24 | 50 to 54 % | 4% | 59 to 66 % |
| 30 | 24 | 21 to 25 % | 4% | 24 to 28 % |
| 60 | 24 | 17 to 17 % | 0% | 21 to 21 % |

confirmed, tradable under **fee_free** (no fees (counterfactual, isolates the market structure)):

| D (s) | n | fills whole | net $ per opportunity | net edge retained | missed |
|---|---|---|---|---|---|
| 0.001 | 21 | 100 to 100 % | 0.43 to 0.43 | 100 to 100 % | 0-0 |
| 0.005 | 21 | 100 to 100 % | 0.43 to 0.43 | 100 to 100 % | 0-0 |
| 0.01 | 21 | 100 to 100 % | 0.43 to 0.43 | 100 to 100 % | 0-0 |
| 0.025 | 21 | 100 to 100 % | 0.43 to 0.43 | 100 to 100 % | 0-0 |
| 0.05 | 21 | 100 to 100 % | 0.43 to 0.43 | 100 to 100 % | 0-0 |
| 0.1 | 21 | 100 to 100 % | 0.43 to 0.43 | 100 to 100 % | 0-0 |
| 0.25 | 21 | 100 to 100 % | 0.43 to 0.43 | 100 to 100 % | 0-0 |
| 0.5 | 21 | 100 to 100 % | 0.43 to 0.43 | 100 to 100 % | 0-0 |
| 1 | 21 | 100 to 100 % | 0.43 to 0.43 | 100 to 100 % | 0-0 |
| 2 | 21 | 100 to 100 % | 0.43 to 0.43 | 100 to 100 % | 0-0 |
| 3 | 21 | 95 to 100 % | 0.45 to 0.46 | 105 to 106 % | 0-1 |
| 5 | 21 | 86 to 95 % | 0.40 to 0.45 | 93 to 105 % | 1-3 |
| 10 | 21 | 33 to 33 % | 0.07 to 0.07 | 16 to 16 % | 14-14 |
| 30 | 21 | 10 to 10 % | 0.02 to 0.02 | 5 to 5 % | 19-19 |
| 60 | 21 | 10 to 10 % | 0.02 to 0.02 | 5 to 5 % | 19-19 |

confirmed, tradable under **baseline** (standard taker fees):

| D (s) | n | fills whole | net $ per opportunity | net edge retained | missed |
|---|---|---|---|---|---|
| 0.001 | 1 | 100 to 100 % | 0.14 to 0.14 | 100 to 100 % | 0-0 |
| 0.005 | 1 | 100 to 100 % | 0.14 to 0.14 | 100 to 100 % | 0-0 |
| 0.01 | 1 | 100 to 100 % | 0.14 to 0.14 | 100 to 100 % | 0-0 |
| 0.025 | 1 | 100 to 100 % | 0.14 to 0.14 | 100 to 100 % | 0-0 |
| 0.05 | 1 | 100 to 100 % | 0.14 to 0.14 | 100 to 100 % | 0-0 |
| 0.1 | 1 | 100 to 100 % | 0.14 to 0.14 | 100 to 100 % | 0-0 |
| 0.25 | 1 | 100 to 100 % | 0.14 to 0.14 | 100 to 100 % | 0-0 |
| 0.5 | 1 | 100 to 100 % | 0.14 to 0.14 | 100 to 100 % | 0-0 |
| 1 | 1 | 100 to 100 % | 0.14 to 0.14 | 100 to 100 % | 0-0 |
| 2 | 1 | 100 to 100 % | 0.14 to 0.14 | 100 to 100 % | 0-0 |
| 3 | 1 | 100 to 100 % | 0.14 to 0.14 | 100 to 100 % | 0-0 |
| 5 | 1 | 100 to 100 % | 0.14 to 0.14 | 100 to 100 % | 0-0 |
| 10 | 1 | 0 to 0 % | 0.00 to 0.00 | 0 to 0 % | 1-1 |
| 30 | 1 | 0 to 0 % | 0.00 to 0.00 | 0 to 0 % | 1-1 |
| 60 | 1 | 0 to 0 % | 0.00 to 0.00 | 0 to 0 % | 1-1 |

confirmed: the funnel's last stage (executable at first sight -> still fillable whole D later):

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


### Engine sweep (Stage 5 engine, leg risk and realised P&L)

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


## Categories

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

## Sensitivity to the evidence threshold

| relations trusted at >= | sightings | per 1,000 rh | confirmed | per 1,000 rh | executable with fees (sightings / confirmed) | executable no fees (sightings / confirmed) |
|---|---|---|---|---|---|---|
| PROVEN | 0 | 0.0 [0.0, 0.0] | 0 | 0.0 [0.0, 0.0] | 0 / 0 | 0 / 0 |
| DECLARED | 50 | 26.8 [14.5, 43.9] | 24 | 12.9 [5.9, 22.5] | 2 / 1 | 12 / 7 |
| UNVERIFIED | 205 | 91.0 [50.0, 142.9] | 75 | 33.3 [17.2, 53.4] | 3 / 1 | 41 / 21 |

