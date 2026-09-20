# Stage 9 results — confirmatory

Databases: var/books_research_short.duckdb, var/books_research_wide.duckdb. Code fingerprint `e036156024e9174a`, commit `27b70915fe2ff7ac946d6d0e3dcc283b78f4972a+dirty`, seed 20260922, 1000 bootstrap draws. Central assumptions: maker queue model `queue(a=1,c=.5)`, 200 ms latency, γ=0.05, k=50.0, size 10.0 contracts.

## Headline (baseline, central assumptions)

| net P&L | gross | fees | spread captured | inventory contribution | fills | contracts |
|---|---|---|---|---|---|---|
| $-425.5 | $-413.1 | $12.5 | $-21.1 | $-392.0 | 2410 | 22415 |

* 30 s markout of fills: **-1.42 [-1.85, -1.04] ¢** (negative = adverse selection; 81 events)
* Hold-to-settlement P&L per fill (settled markets): **-0.177 [-0.229, -0.130] $** over 1901 fills, total $-335.9
* Worst database drawdown $300.2; worst settled event $-10.7; kill-switch tripped per database: [True, False]; mean |inventory| after a fill 6.0 contracts

## Per database

| database | net | fills | spread ¢/contract | max DD | worst event | Sharpe (annualised, not meaningful) | killed |
|---|---|---|---|---|---|---|---|
| books_research_short | $-280.8 | 1556 | -0.46 | $300.2 | $-5.3 | -145 | True (drawdown $300.42 >= $300.00) |
| books_research_wide | $-144.8 | 854 | 0.62 | $144.8 | $-10.7 | -104 | False |

### Execution and risk detail

**books_research_short** — orders 17522, order fill rate 0.085, quantity fill rate 0.085, cancellation rate 0.915, inventory turnover 59.5×, quote lifetime p50/p90 5.3/36.2 s, markout 5/30/300 s -1.98, -1.89, -1.80 ¢, |inventory| p50/p90/max 6.0/10.0/39.0 contracts, peak worst-case exposure $152.2, P&L vol $1.00/min, event concentration of settled P&L (effective events) 16.6.

**books_research_wide** — orders 12898, order fill rate 0.061, quantity fill rate 0.059, cancellation rate 0.902, inventory turnover 60.7×, quote lifetime p50/p90 19.3/284.0 s, markout 5/30/300 s -0.97, -1.22, -0.97 ¢, |inventory| p50/p90/max 6.0/10.0/30.0 contracts, peak worst-case exposure $471.7, P&L vol $1.25/min, event concentration of settled P&L (effective events) 4.4.

## By category

| group | fills | events | spread ¢/contract | 30 s markout ¢ | settled P&L / fill $ | settled P&L total $ |
|---|---|---|---|---|---|---|
| Climate and Weather | 314 | 5 | -0.47 | -1.35 [-2.79, 0.22] | n/a | n/a |
| Commodities | 14 | 1 | 0.86 | 0.61 | n/a | n/a |
| Crypto | 198 | 5 | 0.37 | -0.87 [-1.49, 1.51] | -0.144 | -18.6 |
| Economics | 3 | 1 | 1.33 | 1.17 | n/a | n/a |
| Entertainment | 46 | 7 | -0.01 | 0.57 [-0.37, 2.71] | n/a | n/a |
| Politics | 23 | 1 | 0.23 | -0.09 | -0.016 | -0.4 |
| Science and Technology | 7 | 1 | 0.00 | -2.00 | n/a | n/a |
| Sports | 1805 | 60 | -0.10 | -1.58 [-2.09, -1.13] | -0.181 [-0.239, -0.129] | -316.9 |

## By liquidity (contracts traded in the trailing hour)

| group | fills | events | spread ¢/contract | 30 s markout ¢ | settled P&L / fill $ | settled P&L total $ |
|---|---|---|---|---|---|---|
| none | 12 | 4 | -1.88 | -2.08 [-2.79, -0.50] | n/a | n/a |
| thin (0, 100] | 71 | 13 | 0.13 | 0.27 [-0.46, 1.72] | -0.268 [-0.500, -0.130] | -4.5 |
| busy (> 100) | 2327 | 79 | -0.09 | -1.47 [-1.91, -1.06] | -0.176 [-0.230, -0.129] | -331.3 |

## By time to resolution

| group | fills | events | spread ¢/contract | 30 s markout ¢ | settled P&L / fill $ | settled P&L total $ |
|---|---|---|---|---|---|---|
| <0.5h | 255 | 6 | 0.42 | -1.09 [-1.65, -0.68] | -0.124 [-0.200, -0.067] | -31.6 |
| 0.5-1.5h | 655 | 21 | 0.09 | -1.57 [-2.24, -1.00] | -0.173 [-0.268, -0.102] | -110.1 |
| 1.5-4h | 1023 | 51 | -0.33 | -1.73 [-2.35, -1.10] | -0.194 [-0.288, -0.115] | -192.4 |
| 4-12h | 76 | 8 | 0.13 | 0.05 [-0.65, 1.05] | -0.118 | -1.8 |
| >=12h | 401 | 17 | -0.18 | -0.87 [-2.08, 0.59] | n/a | n/a |

## By probability range (mid at the fill)

| group | fills | events | spread ¢/contract | 30 s markout ¢ | settled P&L / fill $ | settled P&L total $ |
|---|---|---|---|---|---|---|
| <5% | 34 | 19 | 0.97 | -0.01 [-2.03, 1.69] | 0.071 [-0.073, 0.227] | 2.0 |
| 5-20% | 654 | 53 | -0.02 | -1.03 [-1.38, -0.71] | -0.108 [-0.270, 0.017] | -55.9 |
| 20-50% | 925 | 63 | -0.18 | -1.50 [-2.18, -0.91] | -0.251 [-0.395, -0.084] | -182.2 |
| 50-80% | 558 | 64 | -0.22 | -2.17 [-2.87, -1.49] | -0.248 [-0.458, -0.040] | -108.5 |
| 80-95% | 222 | 31 | 0.07 | -0.69 [-1.25, 0.13] | 0.040 [-0.254, 0.321] | 7.1 |
| >=95% | 17 | 7 | 1.05 | 0.15 [-1.15, 1.86] | 0.116 [-0.023, 0.277] | 1.7 |

## Inventory risk vs time to resolution

| time to resolution | fills | events | mean \|inventory\| | remaining std $ (\|q\|√p(1−p)) | 30 s markout ¢ |
|---|---|---|---|---|---|
| <0.5h | 255 | 6 | 6.0 | 2.17 | -1.09 [-1.65, -0.68] |
| 0.5-1.5h | 655 | 21 | 5.6 | 2.39 | -1.57 [-2.24, -1.00] |
| 1.5-4h | 1023 | 51 | 6.2 | 2.52 | -1.73 [-2.35, -1.10] |
| 4-12h | 76 | 8 | 6.2 | 2.32 | 0.05 [-0.65, 1.05] |
| >=12h | 401 | 17 | 5.9 | 2.46 | -0.87 [-2.08, 0.59] |

## Ablations (each design element removed in turn)

| variant | net | fills | contracts | spread ¢ | 30 s markout ¢ | settled P&L/fill $ | mean \|inv\| | max DD | worst event | killed |
|---|---|---|---|---|---|---|---|---|---|---|
| A naive join (best bid, no model) | $-1100.6 | 14489 | 127254 | 0.29 | -0.41 [-0.56, -0.24] | -0.080 [-0.111, -0.055] | 27.9 | $858 | $-37.6 | [False, False] |
| B fixed width, no skew, no limits | $-623.3 | 2789 | 25658 | 1.31 | -0.89 [-1.47, -0.35] | -0.250 [-0.489, -0.012] | 51.8 | $566 | $-141.4 | [False, False] |
| C fixed width, no skew, limits + kill | $-489.8 | 2276 | 20522 | 1.27 | -1.00 [-1.57, -0.45] | -0.238 [-0.376, -0.118] | 22.8 | $395 | $-38.0 | [True, False] |
| D skew, no limits | $-744.4 | 4333 | 40082 | -0.18 | -1.36 [-1.75, -0.99] | -0.171 [-0.212, -0.138] | 5.8 | $600 | $-21.1 | [False, False] |
| E skew, limits, no kill-switch | $-744.4 | 4333 | 40082 | -0.18 | -1.36 [-1.75, -0.99] | -0.171 [-0.212, -0.138] | 5.8 | $600 | $-21.1 | [False, False] |
| F baseline: skew + limits + kill | $-425.5 | 2410 | 22415 | -0.09 | -1.42 [-1.85, -1.04] | -0.177 [-0.229, -0.130] | 6.0 | $300 | $-10.7 | [True, False] |

## Fill-model × latency sensitivity

| latency ms | maker fill model | net | fills | 30 s markout ¢ | settled P&L/fill $ | killed |
|---|---|---|---|---|---|---|
| 200 | pessimistic | $-458.9 | 1950 | -1.83 [-2.14, -1.42] | -0.236 [-0.301, -0.187] | [True, False] |
| 200 | queue(a=1,c=0) | $-463.2 | 2268 | -1.55 [-1.95, -1.15] | -0.211 [-0.276, -0.162] | [True, False] |
| 200 | queue(a=1,c=.5) | $-425.5 | 2410 | -1.42 [-1.89, -1.06] | -0.177 [-0.233, -0.123] | [True, False] |
| 200 | queue(a=.5,c=.5) | $-447.4 | 2737 | -1.23 [-1.60, -0.87] | -0.160 [-0.220, -0.109] | [True, False] |
| 200 | optimistic | $-488.4 | 4129 | -0.76 [-1.09, -0.48] | -0.115 [-0.174, -0.084] | [True, False] |
| 1000 | pessimistic | $-472.2 | 1990 | -1.76 [-2.06, -1.43] | -0.239 [-0.309, -0.189] | [True, False] |
| 1000 | queue(a=1,c=0) | $-444.2 | 2380 | -1.47 [-1.86, -1.11] | -0.189 [-0.247, -0.143] | [True, False] |
| 1000 | queue(a=1,c=.5) | $-443.8 | 2547 | -1.38 [-1.83, -1.04] | -0.173 [-0.225, -0.134] | [True, False] |
| 1000 | queue(a=.5,c=.5) | $-454.6 | 2857 | -1.16 [-1.50, -0.78] | -0.157 [-0.209, -0.118] | [True, False] |
| 1000 | optimistic | $-494.6 | 3944 | -0.73 [-1.06, -0.46] | -0.124 [-0.179, -0.092] | [True, False] |

## Parameter grid (γ × k)

| γ | k | net | fills | spread ¢ | 30 s markout ¢ | settled P&L/fill $ | mean \|inv\| |
|---|---|---|---|---|---|---|---|
| 0.02 | 25 | $-371.8 | 1783 | 1.45 | -1.12 [-1.69, -0.55] | -0.219 [-0.291, -0.157] | 7.3 |
| 0.02 | 50 | $-462.4 | 2602 | 0.12 | -1.41 [-1.83, -1.03] | -0.189 [-0.250, -0.135] | 7.0 |
| 0.02 | 100 | $-545.2 | 3252 | -0.23 | -1.35 [-1.83, -1.01] | -0.187 [-0.265, -0.135] | 6.9 |
| 0.05 | 25 | $-386.1 | 1854 | 0.81 | -1.22 [-1.82, -0.72] | -0.210 [-0.269, -0.156] | 6.1 |
| 0.05 | 50 | $-425.5 | 2410 | -0.09 | -1.42 [-1.89, -1.06] | -0.177 [-0.233, -0.123] | 6.0 |
| 0.05 | 100 | $-537.9 | 2855 | -0.36 | -1.37 [-1.88, -0.93] | -0.196 [-0.285, -0.146] | 5.8 |
| 0.1 | 25 | $-344.6 | 1715 | 0.68 | -1.29 [-1.87, -0.80] | -0.203 [-0.257, -0.144] | 5.5 |
| 0.1 | 50 | $-461.9 | 2505 | -0.12 | -1.27 [-1.70, -0.94] | -0.181 [-0.252, -0.136] | 5.3 |
| 0.1 | 100 | $-552.2 | 2684 | -0.57 | -1.56 [-2.20, -1.11] | -0.207 [-0.294, -0.153] | 5.4 |

## Partition evidence in the risk limits

With `EMPIRICAL`-level partitions instead of `DECLARED`: net $-425.5, fills 2410, max drawdown $300.2, mean |inventory| 6.0.

## Pre-registered hypotheses (docs/market_making.md §8)

| id | verdict | observed | criterion |
|---|---|---|---|
| M1 | consistent | -1.42 [-1.85, -1.04] (81 events) | 30 s markout of the baseline's fills (cents, bought side): 95% interval entirely below 0 |
| M2 | consistent | settled P&L/fill -0.177 [-0.229, -0.130] (47 events); net < 0 in 10/10 fill-model x latency cells | point estimate of hold-to-settlement P&L per fill < 0 AND net P&L < 0 in every cell |
| M3a | consistent | |inventory| 5.8 vs 51.8 contracts; worst event -21.1 vs -141.4 USD | skew (D) has lower mean |inventory| AND a no-worse worst-event loss than no skew (B) |
| M3b | consistent | net -744.4 (skew) vs -623.3 (no skew) USD | skew does not raise net P&L: net(D) <= net(B)  [from development data, not derived] |
| M4 | consistent | max drawdown 300.2 (kill) vs 599.6 (none) | max drawdown with the kill-switch <= without it |
| M5 | consistent | 0/9 grid cells have settled P&L/fill with lower bound > 0 | no (gamma, k) cell has a settled P&L per fill whose 95% interval lies above 0 |
| M6 | consistent | -1.44c (< 1.5 h) vs -1.41c (>= 1.5 h) | fill-weighted 30 s markout is more negative within 1.5 h of resolution than beyond (point comparison) |
| M7 | consistent | 200 ms: 1950 <= 4129; 1000 ms: 1990 <= 3944 | at each latency, fills under the optimistic queue model >= under the pessimistic one |

