| dataset | instances | markets | events | YES rate | entropy floor (log loss) | reference source | categories |
|---|---|---|---|---|---|---|---|
| history (research) | 3430 | 1561 | 1478 | 0.42 | 0.681 | {'trade_mid': 681, 'last_trade': 2749} | {'Sports': 2490, 'Crypto': 391, 'Climate and Weather': 170, 'Commodities': 168, 'Financials': 107, 'Entertainment': 46, 'Economics': 23, 'Politics': 11} |

| training corpus | markets | corpus YES rate | kappa | mean prediction on research | research YES rate | log loss on research |
|---|---|---|---|---|---|---|
| all_markets | 142,397 | 0.30 | 30.0 | 0.20 | 0.42 | 0.850 [0.804, 0.894] |
| volume_matched | 79,358 | 0.33 | 30.0 | 0.27 | 0.42 | 0.739 [0.708, 0.771] |

| forecast | log loss | Brier | log loss minus market | Brier minus market |
|---|---|---|---|---|
| constant_base_rate | 0.6814 [0.6725, 0.6901] | 0.2442 [0.2398, 0.2485] | 0.3296 [0.3041, 0.3526] | 0.1312 [0.1213, 0.1400] |
| market_reference | 0.3518 [0.3289, 0.3769] | 0.1130 [0.1044, 0.1221] | - | - |
| last_trade | 0.3518 [0.3288, 0.3769] | 0.1133 [0.1045, 0.1224] | 0.0000 [-0.0015, 0.0017] | 0.0003 [-0.0003, 0.0010] |
| B_historical_frequency | 0.7392 [0.7083, 0.7706] | 0.2686 [0.2547, 0.2824] | 0.3874 [0.3504, 0.4234] | 0.1556 [0.1404, 0.1705] |
| B_recalibrated | 0.6767 [0.6662, 0.6877] | 0.2421 [0.2370, 0.2474] | 0.3249 [0.2994, 0.3484] | 0.1291 [0.1194, 0.1381] |
| A_microstructure | 0.3526 [0.3297, 0.3779] | 0.1134 [0.1046, 0.1225] | 0.0008 [-0.0003, 0.0019] | 0.0003 [-0.0001, 0.0008] |
| blend_market_and_B | 0.3519 [0.3275, 0.3793] | 0.1131 [0.1041, 0.1226] | 0.0001 [-0.0020, 0.0024] | 0.0001 [-0.0005, 0.0006] |

| group | n | events | market_reference | A_microstructure | B_recalibrated | A_microstructure - market | B_recalibrated - market |
|---|---|---|---|---|---|---|---|
| 24.0 | 397 | 376 | 0.445 [0.388, 0.507] | 0.446 [0.390, 0.510] | 0.669 [0.650, 0.689] | 0.0016 [-0.0008, 0.0040] | 0.2245 [0.1652, 0.2807] |
| 6.0 | 799 | 756 | 0.453 [0.419, 0.490] | 0.454 [0.420, 0.491] | 0.666 [0.653, 0.679] | 0.0010 [-0.0007, 0.0027] | 0.2133 [0.1785, 0.2465] |
| 2.0 | 860 | 810 | 0.284 [0.255, 0.315] | 0.285 [0.255, 0.317] | 0.668 [0.655, 0.680] | 0.0014 [0.0001, 0.0027] | 0.3839 [0.3525, 0.4137] |
| 1.0 | 654 | 608 | 0.282 [0.245, 0.320] | 0.283 [0.246, 0.321] | 0.687 [0.673, 0.702] | 0.0007 [-0.0007, 0.0023] | 0.4054 [0.3659, 0.4460] |
| 0.25 | 720 | 682 | 0.333 [0.295, 0.368] | 0.332 [0.294, 0.368] | 0.693 [0.681, 0.705] | -0.0006 [-0.0020, 0.0009] | 0.3605 [0.3220, 0.4009] |

| group | n | events | market_reference | A_microstructure | B_recalibrated | A_microstructure - market | B_recalibrated - market |
|---|---|---|---|---|---|---|---|
| last_trade | 2749 | 1233 | 0.339 [0.311, 0.367] | 0.340 [0.312, 0.368] | 0.672 [0.660, 0.685] | 0.0006 [-0.0006, 0.0019] | 0.3332 [0.3042, 0.3602] |
| trade_mid | 681 | 531 | 0.403 [0.369, 0.437] | 0.405 [0.370, 0.438] | 0.694 [0.678, 0.709] | 0.0013 [-0.0005, 0.0032] | 0.2911 [0.2551, 0.3289] |

**flow_1h** (mean of outcome minus market price; 0 = the price already says it all)

| bin | n | events | mean residual |
|---|---|---|---|
| [-1.01, -0.5) | 477 | 380 | 0.003 [-0.029, 0.036] |
| [-0.5, -0.01) | 259 | 245 | -0.004 [-0.047, 0.043] |
| [0.01, 0.5) | 397 | 345 | -0.003 [-0.038, 0.033] |
| [0.5, 1.01) | 1122 | 740 | -0.014 [-0.041, 0.013] |

**ret_1h** (mean of outcome minus market price; 0 = the price already says it all)

| bin | n | events | mean residual |
|---|---|---|---|
| [-1, -0.05) | 378 | 306 | -0.004 [-0.036, 0.032] |
| [-0.05, -0.001) | 239 | 213 | -0.000 [-0.048, 0.050] |
| [-0.001, 0.001) | 1486 | 752 | -0.001 [-0.026, 0.024] |
| [0.001, 0.05) | 216 | 189 | -0.021 [-0.071, 0.028] |
| [0.05, 1) | 384 | 297 | -0.016 [-0.055, 0.020] |


| term | coefficient (per std. dev.) |
|---|---|
| intercept | -0.0462 |
| flow_5m | +0.0000 |
| flow_1h | -0.0000 |
| ret_5m | +0.0000 |
| ret_1h | -0.0000 |
| log_volume_1h | +0.0000 |
| log_secs_since_trade | -0.0000 |
| last_minus_ref | +0.0000 |
| trade_spread | +0.0000 |
| hours_to_expiry | +0.0000 |
| missing:flow_5m | -0.0000 |
| missing:flow_1h | +0.0000 |
| missing:ret_5m | -0.0000 |
| missing:ret_1h | -0.0000 |
| missing:trade_spread | -0.0000 |

| price bin | n | mean price | frequency YES |
|---|---|---|---|
| [0.0, 0.1) | 933 | 0.032 | 0.025 |
| [0.1, 0.2) | 328 | 0.138 | 0.110 |
| [0.2, 0.3) | 273 | 0.245 | 0.227 |
| [0.3, 0.4) | 232 | 0.344 | 0.263 |
| [0.4, 0.5) | 285 | 0.449 | 0.467 |
| [0.5, 0.6) | 218 | 0.545 | 0.619 |
| [0.6, 0.7) | 186 | 0.644 | 0.645 |
| [0.7, 0.8) | 183 | 0.749 | 0.738 |
| [0.8, 0.9) | 178 | 0.849 | 0.860 |
| [0.9, 1.0) | 614 | 0.969 | 0.969 |

Recalibration slope on logit(price), per fold: 1.086, 1.080, 1.071, 1.099, 1.099. Recalibrated minus market log loss (out of fold): 0.0002 [-0.0020, 0.0024].

| dataset | instances | markets | events | YES rate | entropy floor (log loss) | reference source | categories |
|---|---|---|---|---|---|---|---|
| recorded books (research) | 366 | 100 | 32 | 0.43 | 0.683 | {'book_mid': 160, 'trade_mid': 83, 'last_trade': 123} | {'Sports': 365, 'Crypto': 1} |

| forecast | log loss | minus market |
|---|---|---|
| market_reference | 0.2675 [0.1989, 0.3430] | - |
| microprice | 0.2762 [0.2084, 0.3544] | 0.0086 [-0.0032, 0.0241] |
| A_trade_time_book | 0.3055 [0.2263, 0.3981] | 0.0380 [0.0148, 0.0676] |

Where a two-sided book existed (160 instances, 30 events): microprice minus mid = 0.0197 [-0.0076, 0.0560].

**Model C** (partition renormalisation): 112 instances in 14 events; the market's reference prices sum to 1.003 on average (range 0.95-1.20); log loss market 0.2841 [0.1395, 0.4300] vs normalised 0.2862 [0.1407, 0.4343]; paired difference 0.0022 [-0.0016, 0.0071].

