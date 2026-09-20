| forecast | n | events | YES rate | mean forecast | calibration in the large (YES - forecast) | intercept | slope | ECE | Brier | log loss | reliability | resolution | uncertainty |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| market_reference | 1776 | 731 | 0.368 | 0.380 | -0.012 [-0.038, 0.014] | -0.117 [-0.346, 0.120] | 0.967 [0.857, 1.109] | 0.017 [0.018, 0.048] | 0.1171 | 0.3627 | 0.0005 | 0.1160 | 0.2326 |
| last_trade | 1776 | 731 | 0.368 | 0.381 | -0.012 [-0.038, 0.014] | -0.123 [-0.351, 0.114] | 0.959 [0.850, 1.099] | 0.020 [0.019, 0.048] | 0.1170 | 0.3621 | 0.0005 | 0.1160 | 0.2326 |
| A_microstructure | 1776 | 731 | 0.368 | 0.375 | -0.007 [-0.032, 0.019] | -0.072 [-0.302, 0.165] | 0.967 [0.857, 1.110] | 0.017 [0.018, 0.048] | 0.1170 | 0.3623 | 0.0005 | 0.1148 | 0.2326 |
| B_recalibrated | 1776 | 731 | 0.368 | 0.433 | -0.065 [-0.104, -0.023] | 0.007 [-0.280, 0.280] | 2.094 [1.342, 2.891] | 0.077 [0.040, 0.115] | 0.2277 | 0.6473 | 0.0080 | 0.0123 | 0.2326 |
| blend_market_and_B | 1776 | 731 | 0.368 | 0.373 | -0.005 [-0.030, 0.021] | -0.087 [-0.317, 0.149] | 0.893 [0.791, 1.024] | 0.024 [0.020, 0.052] | 0.1174 | 0.3637 | 0.0011 | 0.1146 | 0.2326 |

| forecast bin | n | events | mean forecast | frequency YES | gap (freq - forecast) |
|---|---|---|---|---|---|
| [0.00, 0.10) | 564 | 298 | 0.032 | 0.021 [0.010, 0.035] | -0.011 |
| [0.10, 0.20) | 201 | 149 | 0.143 | 0.139 [0.086, 0.200] | -0.004 |
| [0.20, 0.30) | 154 | 111 | 0.241 | 0.247 [0.154, 0.338] | +0.006 |
| [0.30, 0.40) | 136 | 98 | 0.345 | 0.346 [0.245, 0.453] | +0.001 |
| [0.40, 0.50) | 109 | 90 | 0.446 | 0.431 [0.321, 0.535] | -0.015 |
| [0.50, 0.60) | 97 | 77 | 0.540 | 0.495 [0.383, 0.608] | -0.045 |
| [0.60, 0.70) | 89 | 71 | 0.651 | 0.629 [0.506, 0.753] | -0.022 |
| [0.70, 0.80) | 93 | 71 | 0.748 | 0.785 [0.667, 0.902] | +0.037 |
| [0.80, 0.90) | 64 | 55 | 0.849 | 0.797 [0.672, 0.900] | -0.052 |
| [0.90, 1.00) | 269 | 161 | 0.972 | 0.944 [0.905, 0.979] | -0.028 |

**category**

| group | n | events | mean price | freq YES | gap (YES - price) | slope |
|---|---|---|---|---|---|---|
| Sports | 1365 | 516 | 0.357 | 0.343 | -0.014 [-0.044, 0.014] | 0.941 [0.808, 1.118] |
| Crypto | 147 | 104 | 0.376 | 0.354 | -0.022 [-0.086, 0.048] | 0.909 [0.619, 1.351] |
| Climate and Weather | 52 | 39 | 0.333 | 0.346 | 0.014 [-0.049, 0.081] | (< 40 events) |
| Commodities | 93 | 30 | 0.480 | 0.505 | 0.025 [-0.047, 0.087] | (< 40 events) |
| Financials | 98 | 29 | 0.574 | 0.561 | -0.013 [-0.144, 0.107] | (< 40 events) |
| other | 21 | 13 | 0.676 | 0.667 | -0.009 [-0.137, 0.148] | (< 40 events) |

**probability range**

| group | n | events | mean price | freq YES | gap (YES - price) | slope |
|---|---|---|---|---|---|---|
| <5% | 404 | 244 | 0.018 | 0.012 | -0.006 [-0.016, 0.007] | (< 40 events) |
| 5-20% | 361 | 228 | 0.110 | 0.097 | -0.013 [-0.048, 0.028] | (< 40 events) |
| 20-50% | 399 | 259 | 0.332 | 0.331 | -0.002 [-0.066, 0.065] | (< 40 events) |
| 50-80% | 279 | 190 | 0.645 | 0.634 | -0.010 [-0.093, 0.062] | (< 40 events) |
| 80-95% | 115 | 90 | 0.881 | 0.809 | -0.073 [-0.167, 0.012] | (< 40 events) |
| >=95% | 218 | 134 | 0.984 | 0.972 | -0.011 [-0.045, 0.015] | (< 40 events) |

**time to resolution**

| group | n | events | mean price | freq YES | gap (YES - price) | slope |
|---|---|---|---|---|---|---|
| <0.5h | 306 | 283 | 0.432 | 0.412 | -0.020 [-0.056, 0.015] | 1.025 [0.859, 1.266] |
| 0.5-1.5h | 314 | 283 | 0.382 | 0.366 | -0.016 [-0.045, 0.017] | 1.054 [0.868, 1.364] |
| 1.5-4h | 434 | 398 | 0.365 | 0.346 | -0.020 [-0.049, 0.012] | 0.939 [0.793, 1.151] |
| 4-12h | 475 | 427 | 0.370 | 0.375 | 0.005 [-0.028, 0.041] | 0.970 [0.800, 1.212] |
| >=12h | 247 | 214 | 0.361 | 0.344 | -0.017 [-0.067, 0.031] | 0.867 [0.653, 1.212] |

**liquidity trailing hour volume**

| group | n | events | mean price | freq YES | gap (YES - price) | slope |
|---|---|---|---|---|---|---|
| volume: none | 625 | 347 | 0.353 | 0.331 | -0.022 [-0.056, 0.012] | 0.925 [0.769, 1.159] |
| volume: low (0, 411] | 557 | 393 | 0.395 | 0.379 | -0.016 [-0.053, 0.022] | 0.898 [0.759, 1.096] |
| volume: high (> 411) | 594 | 393 | 0.396 | 0.397 | 0.002 [-0.033, 0.038] | 1.100 [0.920, 1.371] |

**market size lifetime volume**

| group | n | events | mean price | freq YES | gap (YES - price) | slope |
|---|---|---|---|---|---|---|
| lifetime: low (<= 1226.39) | 643 | 292 | 0.368 | 0.347 | -0.021 [-0.054, 0.015] | 1.079 [0.916, 1.370] |
| lifetime: mid (1226.39, 10117.4] | 546 | 237 | 0.332 | 0.313 | -0.019 [-0.053, 0.020] | 1.142 [0.944, 1.429] |
| lifetime: high (> 10117.4) | 587 | 228 | 0.439 | 0.443 | 0.004 [-0.048, 0.064] | 0.733 [0.571, 0.995] |


| recalibrator | log loss minus raw price | Brier minus raw price | fitted (a, b) |
|---|---|---|---|
| platt | 0.0009 [-0.0017, 0.0039] | 0.0002 [-0.0005, 0.0010] | a=-0.029, b=1.086 |
| isotonic | -0.0000 [-0.0048, 0.0053] | 0.0011 [-0.0006, 0.0030] |  |

1501 instances in 53 events; reference source {'book_mid': 681, 'trade_mid': 218, 'last_trade': 602}

| forecast | log loss | minus market | slope | calibration in the large |
|---|---|---|---|---|
| market_reference | 0.2378 | - | 1.176 [0.963, 1.517] | -0.002 [-0.017, 0.019] |
| microprice | 0.2449 | 0.0071 [0.0011, 0.0162] | 1.132 [0.927, 1.441] | -0.004 [-0.022, 0.018] |
| A_trade_time_book | 0.2377 | -0.0000 [-0.0004, 0.0005] | 1.176 [0.963, 1.517] | 0.000 [-0.015, 0.021] |

Where a two-sided book existed (681 instances, 50 events): microprice minus mid = 0.0157 [0.0024, 0.0334].

Partition renormalisation: 743 instances, 32 events, prices sum to 1.013 on average; normalised minus market = 0.0012 [-0.0016, 0.0026].

Calibration of the reference price by where it came from:

| source | n | events | mean price | YES rate | slope | gap (YES - price) |
|---|---|---|---|---|---|---|
| book_mid | 681 | 50 | 0.351 | 0.348 | 1.108 [0.798, 1.529] | -0.003 [-0.036, 0.036] |
| trade_mid | 218 | 41 | 0.381 | 0.381 | 1.313 [0.992, 2.531] | 0.000 [-0.009, 0.013] |
| last_trade | 602 | 37 | 0.329 | 0.327 | 1.360 [no interval] | -0.002 [-0.007, 0.003] |

**Stage 7 hypotheses P1-P7**

7 of 7 consistent.

| # | verdict | observed | criterion |
|---|---|---|---|
| P1 | consistent | market minus constant = -0.302 | paired log loss difference < -0.2 |
| P2 | consistent | A minus market = -0.0004 | within +-0.005 |
| P3 | consistent | recalibrated B minus constant = -0.0171 | difference > -0.02 |
| P4 | consistent | blend weight on B fitted on this period = +0.043 | weight <= 0.1 |
| P5 | consistent | microprice minus mid = +0.0157 (681 instances) | difference >= -0.005 |
| P6 | consistent | C minus market = +0.0012 (32 events) | within +-0.01 |
| P7 | consistent | slope 0.97; Platt recalibration gain -0.0009 | slope in [0.9, 1.3] and recalibration gain < 0.005 |

**Stage 8 hypotheses C1-C10**

10 of 10 consistent.

| # | verdict | observed | criterion |
|---|---|---|---|
| C1 | consistent | YES minus price = -0.012 [-0.038, +0.014] | interval contains 0 and |gap| <= 0.03 |
| C2 | consistent | reliability / resolution = 0.0042 | < 0.05 |
| C3 | consistent | largest decile gap -0.028 (bin [0.90, 1.00), n=269) | |gap| <= 0.10 in every decile with n >= 100 |
| C4 | consistent | YES minus price for prices < 20% = -0.0093 (n=765) | negative (longshot bias: cheap contracts pay off less than their price) |
| C5 | consistent | slope in the last half hour = 1.03 | slope in [0.8, 1.4] |
| C6 | consistent | isotonic recalibration gain in log loss = +0.0000 | gain < 0.005 |
| C7 | consistent | A slope 0.97 [0.8572856869498624, 1.1095046051279878]; blend slope 0.89 [0.7909208426311726, 1.0236694932761528] | both slope intervals contain 1 |
| C8 | consistent | book-mid gap -0.003; A_book minus market -0.0000 | |book-mid gap| <= 0.05 and |A_book - market| <= 0.01 |
| C9 | consistent | 0 of 23 subgroup gaps have an interval excluding 0 | <= 3 (about 1 expected by chance at 5%; exploratory, not corrected) |
| C10 | consistent | lowest lifetime-volume tercile: YES minus price = -0.021 | negative (replication of the development-period sign) |

