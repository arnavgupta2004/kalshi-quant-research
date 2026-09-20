# Stage 9 results — development

Databases: var/books_shortlived.duckdb, var/books_structural.duckdb. Code fingerprint `e036156024e9174a`, commit `27b70915fe2ff7ac946d6d0e3dcc283b78f4972a+dirty`, seed 20260922, 1000 bootstrap draws. Central assumptions: maker queue model `queue(a=1,c=.5)`, 200 ms latency, γ=0.05, k=50.0, size 10.0 contracts.

## Headline (baseline, central assumptions)

| net P&L | gross | fees | spread captured | inventory contribution | fills | contracts |
|---|---|---|---|---|---|---|
| $-317.4 | $-317.4 | $0.0 | $-49.2 | $-268.2 | 1402 | 13360 |

* 30 s markout of fills: **-2.07 [-2.86, -1.40] ¢** (negative = adverse selection; 46 events)
* Hold-to-settlement P&L per fill (settled markets): **-0.301 [-0.420, -0.211] $** over 914 fills, total $-275.0
* Worst database drawdown $334.7; worst settled event $-16.9; kill-switch tripped per database: [True, False]; mean |inventory| after a fill 5.8 contracts

## Per database

| database | net | fills | spread ¢/contract | max DD | worst event | Sharpe (annualised, not meaningful) | killed |
|---|---|---|---|---|---|---|---|
| books_shortlived | $-315.7 | 1383 | -0.36 | $334.7 | $-16.9 | -311 | True (drawdown $300.20 >= $300.00) |
| books_structural | $-1.7 | 19 | -1.08 | $1.7 | $0.0 | -162 | False |

### Execution and risk detail

**books_shortlived** — orders 12016, order fill rate 0.111, quantity fill rate 0.110, cancellation rate 0.889, inventory turnover 36.2×, quote lifetime p50/p90 5.2/44.2 s, markout 5/30/300 s -1.99, -2.30, -2.46 ¢, |inventory| p50/p90/max 5.5/10.0/40.0 contracts, peak worst-case exposure $268.2, P&L vol $1.67/min, event concentration of settled P&L (effective events) 3.7.

**books_structural** — orders 5620, order fill rate 0.003, quantity fill rate 0.003, cancellation rate 0.897, inventory turnover 17.1×, quote lifetime p50/p90 19.0/169.0 s, markout 5/30/300 s -2.08, -1.34, -1.21 ¢, |inventory| p50/p90/max 10.0/10.0/10.0 contracts, peak worst-case exposure $11.5, P&L vol $0.13/min, event concentration of settled P&L (effective events) n/a.

## By category

| group | fills | events | spread ¢/contract | 30 s markout ¢ | settled P&L / fill $ | settled P&L total $ |
|---|---|---|---|---|---|---|
| Climate and Weather | 3 | 1 | -1.17 | -2.00 | n/a | n/a |
| Crypto | 16 | 3 | -1.06 | -1.22 [-1.38, -0.25] | n/a | n/a |
| Sports | 1383 | 42 | -0.36 | -2.08 [-2.85, -1.38] | -0.301 [-0.420, -0.211] | -275.0 |

## By liquidity (contracts traded in the trailing hour)

| group | fills | events | spread ¢/contract | 30 s markout ¢ | settled P&L / fill $ | settled P&L total $ |
|---|---|---|---|---|---|---|
| none | 21 | 5 | -1.10 | -1.31 [-1.61, -0.86] | -0.100 | -0.2 |
| thin (0, 100] | 11 | 5 | -1.59 | -4.41 [-7.50, 0.50] | -0.887 [-2.362, -0.020] | -7.1 |
| busy (> 100) | 1370 | 40 | -0.35 | -2.06 [-2.88, -1.38] | -0.296 [-0.417, -0.201] | -267.7 |

## By time to resolution

| group | fills | events | spread ¢/contract | 30 s markout ¢ | settled P&L / fill $ | settled P&L total $ |
|---|---|---|---|---|---|---|
| <0.5h | 16 | 2 | 1.87 | -11.69 [-15.50, -0.25] | -1.675 | -20.1 |
| 0.5-1.5h | 415 | 7 | -0.27 | -2.37 [-3.82, -0.69] | -0.335 [-0.437, -0.153] | -110.8 |
| 1.5-4h | 952 | 37 | -0.43 | -1.79 [-2.57, -1.30] | -0.252 [-0.423, -0.154] | -144.0 |
| 4-12h | 11 | 2 | -0.91 | -1.45 [-2.50, -1.35] | n/a | n/a |
| >=12h | 8 | 3 | -1.31 | -1.19 [-2.00, -0.25] | n/a | n/a |

## By probability range (mid at the fill)

| group | fills | events | spread ¢/contract | 30 s markout ¢ | settled P&L / fill $ | settled P&L total $ |
|---|---|---|---|---|---|---|
| <5% | 15 | 5 | 1.03 | -1.23 [-8.15, 1.04] | -0.440 [-2.582, 0.465] | -6.6 |
| 5-20% | 234 | 31 | 0.02 | -1.70 [-2.34, -0.89] | -0.502 [-0.891, -0.097] | -96.3 |
| 20-50% | 549 | 34 | -0.58 | -2.93 [-4.60, -1.84] | -0.318 [-0.681, -0.081] | -98.7 |
| 50-80% | 450 | 35 | -0.67 | -1.34 [-2.31, -0.55] | -0.016 [-0.343, 0.382] | -4.2 |
| 80-95% | 148 | 22 | 0.54 | -1.90 [-4.34, -0.16] | -0.576 [-1.501, -0.057] | -69.7 |
| >=95% | 6 | 4 | 1.40 | 1.17 [-1.00, 2.25] | 0.083 [-0.167, 0.440] | 0.5 |

## Inventory risk vs time to resolution

| time to resolution | fills | events | mean \|inventory\| | remaining std $ (\|q\|√p(1−p)) | 30 s markout ¢ |
|---|---|---|---|---|---|
| <0.5h | 16 | 2 | 9.9 | 4.45 | -11.69 [-15.50, -0.25] |
| 0.5-1.5h | 415 | 7 | 6.2 | 2.48 | -2.37 [-3.82, -0.69] |
| 1.5-4h | 952 | 37 | 5.6 | 2.43 | -1.79 [-2.57, -1.30] |
| 4-12h | 11 | 2 | 4.5 | 1.81 | -1.45 [-2.50, -1.35] |
| >=12h | 8 | 3 | 6.2 | 2.59 | -1.19 [-2.00, -0.25] |

## Ablations (each design element removed in turn)

| variant | net | fills | contracts | spread ¢ | 30 s markout ¢ | settled P&L/fill $ | mean \|inv\| | max DD | worst event | killed |
|---|---|---|---|---|---|---|---|---|---|---|
| A naive join (best bid, no model) | $-253.8 | 5268 | 47901 | 0.24 | -0.65 [-0.95, -0.31] | -0.019 [-0.109, 0.097] | 24.9 | $190 | $-16.4 | [False, False] |
| B fixed width, no skew, no limits | $-93.3 | 1344 | 12621 | 1.57 | -1.97 [-3.51, -0.50] | -0.341 [-0.731, -0.009] | 45.1 | $300 | $-43.5 | [False, False] |
| C fixed width, no skew, limits + kill | $-135.2 | 1077 | 9715 | 1.53 | -2.18 [-3.65, -0.63] | -0.256 [-0.565, -0.012] | 22.9 | $200 | $-41.1 | [False, False] |
| D skew, no limits | $-443.4 | 2223 | 21238 | -0.27 | -1.76 [-2.39, -1.25] | -0.241 [-0.331, -0.161] | 5.8 | $442 | $-16.9 | [False, False] |
| E skew, limits, no kill-switch | $-443.4 | 2223 | 21238 | -0.27 | -1.76 [-2.39, -1.25] | -0.241 [-0.331, -0.161] | 5.8 | $442 | $-16.9 | [False, False] |
| F baseline: skew + limits + kill | $-317.4 | 1402 | 13360 | -0.37 | -2.07 [-2.86, -1.40] | -0.301 [-0.420, -0.211] | 5.8 | $335 | $-16.9 | [True, False] |

## Fill-model × latency sensitivity

| latency ms | maker fill model | net | fills | 30 s markout ¢ | settled P&L/fill $ | killed |
|---|---|---|---|---|---|---|
| 200 | pessimistic | $-323.9 | 1121 | -2.35 [-3.20, -1.67] | -0.368 [-0.481, -0.256] | [True, False] |
| 200 | queue(a=1,c=0) | $-323.9 | 1379 | -2.14 [-2.89, -1.48] | -0.308 [-0.423, -0.218] | [True, False] |
| 200 | queue(a=1,c=.5) | $-317.4 | 1402 | -2.07 [-2.80, -1.43] | -0.301 [-0.410, -0.213] | [True, False] |
| 200 | queue(a=.5,c=.5) | $-319.1 | 1459 | -1.97 [-2.95, -1.34] | -0.287 [-0.401, -0.195] | [True, False] |
| 200 | optimistic | $-326.0 | 1657 | -1.76 [-2.48, -1.23] | -0.247 [-0.355, -0.164] | [True, False] |
| 1000 | pessimistic | $-311.4 | 1188 | -1.99 [-2.85, -1.39] | -0.365 [-0.468, -0.267] | [True, False] |
| 1000 | queue(a=1,c=0) | $-318.6 | 1403 | -2.00 [-2.67, -1.47] | -0.311 [-0.423, -0.231] | [True, False] |
| 1000 | queue(a=1,c=.5) | $-315.2 | 1415 | -2.02 [-2.75, -1.53] | -0.321 [-0.450, -0.230] | [True, False] |
| 1000 | queue(a=.5,c=.5) | $-325.5 | 1514 | -1.89 [-2.63, -1.33] | -0.308 [-0.425, -0.212] | [True, False] |
| 1000 | optimistic | $-324.1 | 1666 | -1.74 [-2.29, -1.27] | -0.262 [-0.364, -0.185] | [True, False] |

## Parameter grid (γ × k)

| γ | k | net | fills | spread ¢ | 30 s markout ¢ | settled P&L/fill $ | mean \|inv\| |
|---|---|---|---|---|---|---|---|
| 0.02 | 25 | $-223.6 | 915 | 1.82 | -1.57 [-2.76, -0.37] | -0.343 [-0.545, -0.141] | 8.0 |
| 0.02 | 50 | $-331.2 | 1360 | -0.12 | -2.09 [-2.91, -1.20] | -0.377 [-0.495, -0.266] | 7.2 |
| 0.02 | 100 | $-347.2 | 1374 | -0.65 | -2.10 [-3.00, -1.28] | -0.369 [-0.498, -0.265] | 7.2 |
| 0.05 | 25 | $-242.2 | 1001 | 0.99 | -1.78 [-3.00, -0.78] | -0.299 [-0.473, -0.161] | 6.2 |
| 0.05 | 50 | $-317.4 | 1402 | -0.37 | -2.07 [-2.80, -1.43] | -0.301 [-0.410, -0.213] | 5.8 |
| 0.05 | 100 | $-311.5 | 1437 | -0.54 | -2.00 [-2.57, -1.34] | -0.309 [-0.423, -0.225] | 5.8 |
| 0.1 | 25 | $-246.7 | 1004 | 0.51 | -1.94 [-3.03, -1.07] | -0.308 [-0.422, -0.182] | 5.5 |
| 0.1 | 50 | $-293.3 | 1446 | -0.21 | -1.69 [-2.31, -1.18] | -0.247 [-0.333, -0.168] | 5.5 |
| 0.1 | 100 | $-301.2 | 1363 | -0.57 | -1.99 [-2.46, -1.37] | -0.277 [-0.361, -0.213] | 5.4 |

## Partition evidence in the risk limits

With `EMPIRICAL`-level partitions instead of `DECLARED`: net $-317.4, fills 1402, max drawdown $334.7, mean |inventory| 5.8.

## Pre-registered hypotheses (docs/market_making.md §8)

| id | verdict | observed | criterion |
|---|---|---|---|
| M1 | consistent | -2.07 [-2.86, -1.40] (46 events) | 30 s markout of the baseline's fills (cents, bought side): 95% interval entirely below 0 |
| M2 | consistent | settled P&L/fill -0.301 [-0.420, -0.211] (25 events); net < 0 in 10/10 fill-model x latency cells | point estimate of hold-to-settlement P&L per fill < 0 AND net P&L < 0 in every cell |
| M3a | consistent | |inventory| 5.8 vs 45.1 contracts; worst event -16.9 vs -43.5 USD | skew (D) has lower mean |inventory| AND a no-worse worst-event loss than no skew (B) |
| M3b | consistent | net -443.4 (skew) vs -93.3 (no skew) USD | skew does not raise net P&L: net(D) <= net(B)  [from development data, not derived] |
| M4 | consistent | max drawdown 334.7 (kill) vs 441.7 (none) | max drawdown with the kill-switch <= without it |
| M5 | consistent | 0/9 grid cells have settled P&L/fill with lower bound > 0 | no (gamma, k) cell has a settled P&L per fill whose 95% interval lies above 0 |
| M6 | consistent | -2.71c (< 1.5 h) vs -1.79c (>= 1.5 h) | fill-weighted 30 s markout is more negative within 1.5 h of resolution than beyond (point comparison) |
| M7 | consistent | 200 ms: 1121 <= 1657; 1000 ms: 1188 <= 1666 | at each latency, fills under the optimistic queue model >= under the pessimistic one |

