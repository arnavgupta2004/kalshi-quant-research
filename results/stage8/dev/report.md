| forecast | n | events | YES rate | mean forecast | calibration in the large (YES - forecast) | intercept | slope | ECE | Brier | log loss | reliability | resolution | uncertainty |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| market_reference | 3430 | 1478 | 0.424 | 0.429 | -0.006 [-0.024, 0.012] | -0.029 [-0.192, 0.126] | 1.087 [0.992, 1.207] | 0.019 [0.018, 0.038] | 0.1130 | 0.3518 | 0.0010 | 0.1312 | 0.2442 |
| last_trade | 3430 | 1478 | 0.424 | 0.429 | -0.005 [-0.023, 0.012] | -0.031 [-0.193, 0.125] | 1.073 [0.980, 1.190] | 0.018 [0.016, 0.038] | 0.1133 | 0.3518 | 0.0008 | 0.1308 | 0.2442 |
| A_microstructure | 3430 | 1478 | 0.424 | 0.424 | -0.000 [-0.018, 0.017] | 0.021 [-0.144, 0.178] | 1.087 [0.992, 1.207] | 0.021 [0.017, 0.039] | 0.1130 | 0.3517 | 0.0009 | 0.1314 | 0.2442 |
| B_recalibrated | 3430 | 1478 | 0.424 | 0.424 | 0.000 [-0.029, 0.030] | -0.000 [-0.199, 0.238] | 1.000 [0.473, 1.662] | 0.034 [0.016, 0.064] | 0.2415 | 0.6754 | 0.0028 | 0.0062 | 0.2442 |
| blend_market_and_B | 3430 | 1478 | 0.424 | 0.424 | 0.000 [-0.018, 0.017] | 0.001 [-0.164, 0.157] | 1.003 [0.915, 1.114] | 0.017 [0.015, 0.036] | 0.1128 | 0.3510 | 0.0008 | 0.1310 | 0.2442 |

| forecast bin | n | events | mean forecast | frequency YES | gap (freq - forecast) |
|---|---|---|---|---|---|
| [0.00, 0.10) | 933 | 533 | 0.032 | 0.025 [0.013, 0.039] | -0.007 |
| [0.10, 0.20) | 328 | 254 | 0.138 | 0.110 [0.074, 0.151] | -0.028 |
| [0.20, 0.30) | 273 | 198 | 0.245 | 0.227 [0.168, 0.295] | -0.017 |
| [0.30, 0.40) | 232 | 172 | 0.344 | 0.263 [0.188, 0.349] | -0.082 |
| [0.40, 0.50) | 285 | 191 | 0.449 | 0.467 [0.384, 0.546] | +0.017 |
| [0.50, 0.60) | 218 | 167 | 0.545 | 0.619 [0.541, 0.696] | +0.074 |
| [0.60, 0.70) | 186 | 150 | 0.644 | 0.645 [0.559, 0.727] | +0.001 |
| [0.70, 0.80) | 183 | 134 | 0.749 | 0.738 [0.646, 0.829] | -0.012 |
| [0.80, 0.90) | 178 | 135 | 0.849 | 0.860 [0.797, 0.920] | +0.010 |
| [0.90, 1.00) | 614 | 353 | 0.969 | 0.969 [0.948, 0.987] | -0.000 |

**category**

| group | n | events | mean price | freq YES | gap (YES - price) | slope |
|---|---|---|---|---|---|---|
| Sports | 2490 | 972 | 0.423 | 0.420 | -0.002 [-0.026, 0.020] | 1.067 [0.946, 1.221] |
| Crypto | 391 | 264 | 0.446 | 0.427 | -0.019 [-0.057, 0.019] | 1.194 [0.974, 1.507] |
| Climate and Weather | 170 | 112 | 0.435 | 0.435 | 0.000 [-0.059, 0.062] | 1.036 [0.772, 1.648] |
| Commodities | 168 | 57 | 0.480 | 0.452 | -0.028 [-0.087, 0.033] | 1.110 [0.887, 1.638] |
| Financials | 107 | 33 | 0.521 | 0.495 | -0.026 [-0.119, 0.069] | (< 40 events) |
| other | 104 | 40 | 0.335 | 0.346 | 0.011 [-0.013, 0.046] | 5.796 [no interval] |

**probability range**

| group | n | events | mean price | freq YES | gap (YES - price) | slope |
|---|---|---|---|---|---|---|
| <5% | 668 | 424 | 0.017 | 0.007 | -0.009 [-0.016, 0.000] | (< 40 events) |
| 5-20% | 593 | 388 | 0.108 | 0.091 | -0.017 [-0.043, 0.013] | (< 40 events) |
| 20-50% | 790 | 499 | 0.348 | 0.324 | -0.024 [-0.070, 0.022] | (< 40 events) |
| 50-80% | 587 | 392 | 0.640 | 0.664 | 0.024 [-0.026, 0.078] | (< 40 events) |
| 80-95% | 306 | 218 | 0.879 | 0.882 | 0.003 [-0.041, 0.042] | (< 40 events) |
| >=95% | 486 | 295 | 0.982 | 0.984 | 0.001 [-0.020, 0.016] | (< 40 events) |

**time to resolution**

| group | n | events | mean price | freq YES | gap (YES - price) | slope |
|---|---|---|---|---|---|---|
| <0.5h | 720 | 682 | 0.496 | 0.469 | -0.027 [-0.052, -0.000] | 1.130 [0.986, 1.339] |
| 0.5-1.5h | 654 | 608 | 0.468 | 0.460 | -0.008 [-0.031, 0.016] | 1.109 [0.973, 1.342] |
| 1.5-4h | 860 | 810 | 0.393 | 0.397 | 0.003 [-0.017, 0.024] | 1.219 [1.063, 1.431] |
| 4-12h | 799 | 756 | 0.396 | 0.397 | 0.000 [-0.026, 0.028] | 1.078 [0.928, 1.288] |
| >=12h | 397 | 376 | 0.386 | 0.393 | 0.007 [-0.031, 0.046] | 0.867 [0.709, 1.124] |

**liquidity trailing hour volume**

| group | n | events | mean price | freq YES | gap (YES - price) | slope |
|---|---|---|---|---|---|---|
| volume: none | 1153 | 603 | 0.392 | 0.389 | -0.004 [-0.029, 0.021] | 1.043 [0.879, 1.257] |
| volume: low (0, 411] | 1139 | 812 | 0.446 | 0.438 | -0.008 [-0.032, 0.018] | 1.035 [0.915, 1.184] |
| volume: high (> 411) | 1138 | 780 | 0.450 | 0.445 | -0.005 [-0.030, 0.020] | 1.213 [1.062, 1.404] |

**market size lifetime volume**

| group | n | events | mean price | freq YES | gap (YES - price) | slope |
|---|---|---|---|---|---|---|
| lifetime: low (<= 1226.39) | 1144 | 575 | 0.408 | 0.385 | -0.023 [-0.047, -0.001] | 1.258 [1.093, 1.504] |
| lifetime: mid (1226.39, 10117.4] | 1143 | 477 | 0.435 | 0.445 | 0.010 [-0.020, 0.038] | 1.111 [0.955, 1.345] |
| lifetime: high (> 10117.4) | 1143 | 453 | 0.444 | 0.441 | -0.003 [-0.041, 0.034] | 0.921 [0.778, 1.114] |


| recalibrator | log loss minus raw price | Brier minus raw price | fitted (a, b) |
|---|---|---|---|
| platt | -0.0003 [-0.0023, 0.0018] | 0.0000 [-0.0005, 0.0005] | (-0.05, 1.09), (-0.02, 1.10), (-0.04, 1.08), (+0.03, 1.08), (-0.06, 1.09) |
| isotonic | 0.0046 [-0.0035, 0.0161] | 0.0000 [-0.0013, 0.0013] |  |

366 instances in 32 events; reference source {'book_mid': 160, 'trade_mid': 83, 'last_trade': 123}

| forecast | log loss | minus market | slope | calibration in the large |
|---|---|---|---|---|
| market_reference | 0.2675 | - | 1.259 [0.973, 1.797] | -0.003 [-0.055, 0.052] |
| microprice | 0.2762 | 0.0086 [-0.0036, 0.0245] | 1.206 [0.955, 1.685] | 0.002 [-0.055, 0.060] |
| A_trade_time_book | 0.2675 | -0.0000 [-0.0014, 0.0014] | 1.259 [0.973, 1.797] | -0.000 [-0.052, 0.055] |

Where a two-sided book existed (160 instances, 30 events): microprice minus mid = 0.0197 [-0.0081, 0.0570].

Partition renormalisation: 112 instances, 14 events, prices sum to 1.003 on average; normalised minus market = 0.0022 [-0.0014, 0.0070].

Calibration of the reference price by where it came from:

| source | n | events | mean price | YES rate | slope | gap (YES - price) |
|---|---|---|---|---|---|---|
| book_mid | 160 | 30 | 0.409 | 0.419 | 1.191 [0.820, 1.827] | 0.009 [-0.074, 0.091] |
| trade_mid | 83 | 21 | 0.454 | 0.434 | 1.199 [0.886, 3.084] | -0.020 [-0.081, 0.047] |
| last_trade | 123 | 20 | 0.445 | 0.439 | 1.678 [no interval] | -0.006 [-0.036, 0.022] |

**Stage 7 hypotheses P1-P7**

7 of 7 consistent.

| # | verdict | observed | criterion |
|---|---|---|---|
| P1 | consistent | market minus constant = -0.330 | paired log loss difference < -0.2 |
| P2 | consistent | A minus market = -0.0001 | within +-0.005 |
| P3 | consistent | recalibrated B minus constant = -0.0060 | difference > -0.02 |
| P4 | consistent | blend weight on B fitted on this period = -0.085 | weight <= 0.1 |
| P5 | consistent | microprice minus mid = +0.0197 (160 instances) | difference >= -0.005 |
| P6 | consistent | C minus market = +0.0022 (14 events) | within +-0.01 |
| P7 | consistent | slope 1.09; Platt recalibration gain +0.0003 | slope in [0.9, 1.3] and recalibration gain < 0.005 |

**Stage 8 hypotheses C1-C10**

10 of 10 consistent.

| # | verdict | observed | criterion |
|---|---|---|---|
| C1 | consistent | YES minus price = -0.006 [-0.024, +0.012] | interval contains 0 and |gap| <= 0.03 |
| C2 | consistent | reliability / resolution = 0.0073 | < 0.05 |
| C3 | consistent | largest decile gap -0.082 (bin [0.30, 0.40), n=232) | |gap| <= 0.10 in every decile with n >= 100 |
| C4 | consistent | YES minus price for prices < 20% = -0.0127 (n=1261) | negative (longshot bias: cheap contracts pay off less than their price) |
| C5 | consistent | slope in the last half hour = 1.13 | slope in [0.8, 1.4] |
| C6 | consistent | isotonic recalibration gain in log loss = -0.0046 | gain < 0.005 |
| C7 | consistent | A slope 1.09 [0.9917473948388834, 1.2067384864487563]; blend slope 1.00 [0.9150992707118623, 1.1139127132403646] | both slope intervals contain 1 |
| C8 | consistent | book-mid gap +0.009; A_book minus market -0.0000 | |book-mid gap| <= 0.05 and |A_book - market| <= 0.01 |
| C9 | consistent | 2 of 23 subgroup gaps have an interval excluding 0 | <= 3 (about 1 expected by chance at 5%; exploratory, not corrected) |
| C10 | consistent | lowest lifetime-volume tercile: YES minus price = -0.023 | negative (replication of the development-period sign) |

