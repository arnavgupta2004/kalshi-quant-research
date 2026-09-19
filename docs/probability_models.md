# Fair-probability models (Stage 7)

Spec s.10 asks for a framework that estimates `p_t = P(X = 1 | F_t)` and **at least two distinct models**.
This stage builds three, a point-in-time evaluation harness around them, and asks two narrow questions:

1. Are they *usable*: bounded, causal, leak-free, reproducible?
2. Is there any sign they carry information that the market price does not?

It deliberately does **not** do the calibration study (reliability diagrams, calibration by category, liquidity,
time to resolution, market size): that is Stage 8, on a holdout this stage never opens (`ACCESS_LOG` is empty,
and the script refuses to finish otherwise). Code: `pricing/` (`scoring`, `microstructure`, `dataset`,
`probability_models`, `evaluation`, `report`), driver `scripts/stage7_probability_models.py`.

## 1. Setup

**An instance** is a market at a time *t*. A model sees an `Observation` (features at *t*); the label lives in a
separate `Outcome` it never receives.

* **Information time.** A print at exchange time *s* is visible from *s* + 250 ms (the backtest feed's delay).
  Book snapshots are visible from their receive time and only if a poll confirmed them within 30 s. One place
  applies this rule (`TradeTape.upto`), and a property test proves features from the full tape equal features
  from the tape truncated at *t*.
* **Static fields** come only through the Stage 5 `MarketInfo` whitelist plus the *scheduled* end
  (never `close_time`, `status`, `result`, `volume`: a test asserts `Observation` has none of them).
* **Which instants exist.** An instance is created only if the market was open at *t*
  (`open_time <= t < close_time`). A live system knows what is open now, so this is conditioning on the
  present, not a leak; the realised close time is never a feature.
* **Labels.** YES = 1, NO = 0. Scalar and void markets have no binary label and are excluded (counted).
* **Bounds.** Every model returns a probability in `[1e-4, 1 - 1e-4]` (one price tick), because a forecast of
  exactly 0 or 1 has infinite log loss when wrong; a test asserts it for every model on adversarial inputs.

### Data and the sealed holdout

| Dataset | Use | Selection (stated, not hidden) |
|---|---|---|
| trade history, markets closing before 2026-09-09 | research: 3,430 instances at 24 h, 6 h, 2 h, 1 h, 15 min before the scheduled end | chosen on **lifetime** volume >= 100 contracts (Stage 2): survivorship - every market went on to trade |
| recorded books `books_structural` + `books_shortlived` | research: 366 instances every 15 min while polled | ex ante (top by volume / expiry at recording time), but only ~100 settled markets |
| `relations.duckdb`, 142k settled markets in complete events | corpus for Model B | all markets; the volume-matched subset (79k) mirrors the history selection |
| trade history closing on/after 2026-09-09; `books_research_wide`, `books_research_short` | **sealed holdout for Stage 8** | never loaded here |

The seal is enforced in code: `history_instances` does not even load the holdout markets' trades unless
`include_holdout=True`, `book_instances` raises `SealedHoldoutError` for a holdout database, and opening it is
appended to `ACCESS_LOG`. Tests spy on the loader to prove the trades are never requested.

## 2. The models

**A - microstructure** (`MicrostructureLogit`). `logit p = logit(p_ref) + a + w . x`, where `p_ref` is the market's
own point estimate (book mid, else the mid of the last opposite-side prints, else the last trade) and `x` holds
signed trade flow (5 min, 1 h), short-term price changes, volume, staleness, the implied trade spread, time to
expiry and - when a book exists - spread, top and depth imbalance, microprice minus mid, depth. Ridge shrinkage
(chosen by nested cross-validation) pulls `w` to zero, so with no signal the model *is* the market. Missing
values sit at the training mean with an informative-missingness flag. It is an *offset* model on purpose: the
price is by far the best single predictor, and relearning it from noisy features wastes data. The question it
answers is "does anything the market *shows* but does not *say* add information?".

**B - historical frequencies** (`HistoricalFrequency`). Never sees a price. A hierarchical Beta-Binomial base
rate: global -> category -> series -> (series, strike type, event size), each level shrunk toward its parent by
`kappa` pseudo-counts, learned only from markets settled **before the prediction's UTC day**. `kappa` is chosen
*prequentially on the corpus itself* (predict each event from earlier ones), never on the instances being
scored. This is the spec's "historical frequencies" source: independent of the market's view, so its
disagreement is information and its agreement is corroboration.

**C - partition renormalisation** (`PartitionNormalizer`). For outcomes that are mutually exclusive *and*
exhaustive, exactly one is YES so the true probabilities sum to 1; dividing each price by the sum projects the
market onto the constraint (removes overround). Partitions come from the Stage 3 relation analysis at evidence
>= EMPIRICAL, with the outcome statistics restricted to events that had settled before the recording began.

**Baselines**: the market reference, last trade, microprice, and the constant base rate.

## 3. Evaluation protocol

* **Event-grouped folds**: siblings of an event share an outcome structure (one winner among exclusive
  outcomes); all instances of an event are in one fold.
* **Nested CV**: the penalty is chosen by an inner CV on the outer-training instances, never on the ones scored.
* **Event-clustered bootstrap** (`research.stats`): instances of one market at five horizons are one outcome.
* **Paired differences** against the market reference (negative = better than the market).

## 4. Bugs the real data found (fixed, with regression tests)

* **Every event looked like a single-market event.** The history database's `events.market_tickers` is empty, and
  the count was bucketed as "1"; a base-rate model then learned "single-market events resolve YES 20% of the
  time" and was confidently wrong (log loss 0.85, worse than a coin). Event sizes now come from a corpus of
  *complete* events and are `None` (unknown) otherwise.
* **Sibling leakage in choosing `kappa`.** Learning each sibling before predicting the next leaks the answer,
  because siblings settle together with jointly determined outcomes. Learning is now per whole event, with a
  test that a leaky version scores differently.
* **Population shift in the corpus.** All markets resolve YES 30% of the time, but markets that go on to trade
  ~42%; the corpus now mirrors the history dataset's own volume rule (the unfiltered version is reported).

## 5. Results (research period only)

<!-- BEGIN stage7 -->
**Data**

| dataset | instances | markets | events | YES rate | entropy floor (log loss) | reference source | categories |
|---|---|---|---|---|---|---|---|
| history (research) | 3430 | 1561 | 1478 | 0.42 | 0.681 | {'trade_mid': 681, 'last_trade': 2749} | {'Sports': 2490, 'Crypto': 391, 'Climate and Weather': 170, 'Commodities': 168, 'Financials': 107, 'Entertainment': 46, 'Economics': 23, 'Politics': 11} |

**Model B: which corpus, and the population shift**

| training corpus | markets | corpus YES rate | kappa | mean prediction on research | research YES rate | log loss on research |
|---|---|---|---|---|---|---|
| all_markets | 142,397 | 0.30 | 30.0 | 0.20 | 0.42 | 0.850 [0.804, 0.894] |
| volume_matched | 79,358 | 0.33 | 30.0 | 0.27 | 0.42 | 0.739 [0.708, 0.771] |

**Scores on the history research set (event-clustered 95% intervals)**

| forecast | log loss | Brier | log loss minus market | Brier minus market |
|---|---|---|---|---|
| constant_base_rate | 0.6814 [0.6725, 0.6901] | 0.2442 [0.2398, 0.2485] | 0.3296 [0.3041, 0.3526] | 0.1312 [0.1213, 0.1400] |
| market_reference | 0.3518 [0.3289, 0.3769] | 0.1130 [0.1044, 0.1221] | - | - |
| last_trade | 0.3518 [0.3288, 0.3769] | 0.1133 [0.1045, 0.1224] | 0.0000 [-0.0015, 0.0017] | 0.0003 [-0.0003, 0.0010] |
| B_historical_frequency | 0.7392 [0.7083, 0.7706] | 0.2686 [0.2547, 0.2824] | 0.3874 [0.3504, 0.4234] | 0.1556 [0.1404, 0.1705] |
| B_recalibrated | 0.6767 [0.6662, 0.6877] | 0.2421 [0.2370, 0.2474] | 0.3249 [0.2994, 0.3484] | 0.1291 [0.1194, 0.1381] |
| A_microstructure | 0.3526 [0.3297, 0.3779] | 0.1134 [0.1046, 0.1225] | 0.0008 [-0.0003, 0.0019] | 0.0003 [-0.0001, 0.0008] |
| blend_market_and_B | 0.3519 [0.3275, 0.3793] | 0.1131 [0.1041, 0.1226] | 0.0001 [-0.0020, 0.0024] | 0.0001 [-0.0005, 0.0006] |

**By time to the scheduled end (hours)**

| group | n | events | market_reference | A_microstructure | B_recalibrated | A_microstructure - market | B_recalibrated - market |
|---|---|---|---|---|---|---|---|
| 24.0 | 397 | 376 | 0.445 [0.388, 0.507] | 0.446 [0.390, 0.510] | 0.669 [0.650, 0.689] | 0.0016 [-0.0008, 0.0040] | 0.2245 [0.1652, 0.2807] |
| 6.0 | 799 | 756 | 0.453 [0.419, 0.490] | 0.454 [0.420, 0.491] | 0.666 [0.653, 0.679] | 0.0010 [-0.0007, 0.0027] | 0.2133 [0.1785, 0.2465] |
| 2.0 | 860 | 810 | 0.284 [0.255, 0.315] | 0.285 [0.255, 0.317] | 0.668 [0.655, 0.680] | 0.0014 [0.0001, 0.0027] | 0.3839 [0.3525, 0.4137] |
| 1.0 | 654 | 608 | 0.282 [0.245, 0.320] | 0.283 [0.246, 0.321] | 0.687 [0.673, 0.702] | 0.0007 [-0.0007, 0.0023] | 0.4054 [0.3659, 0.4460] |
| 0.25 | 720 | 682 | 0.333 [0.295, 0.368] | 0.332 [0.294, 0.368] | 0.693 [0.681, 0.705] | -0.0006 [-0.0020, 0.0009] | 0.3605 [0.3220, 0.4009] |

**By reference-price source**

| group | n | events | market_reference | A_microstructure | B_recalibrated | A_microstructure - market | B_recalibrated - market |
|---|---|---|---|---|---|---|---|
| last_trade | 2749 | 1233 | 0.339 [0.311, 0.367] | 0.340 [0.312, 0.368] | 0.672 [0.660, 0.685] | 0.0006 [-0.0006, 0.0019] | 0.3332 [0.3042, 0.3602] |
| trade_mid | 681 | 531 | 0.403 [0.369, 0.437] | 0.405 [0.370, 0.438] | 0.694 [0.678, 0.709] | 0.0013 [-0.0005, 0.0032] | 0.2911 [0.2551, 0.3289] |

**Signal check: outcome minus market price, by feature bins**

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


**Calibration preview of the market reference (research period)**

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

**Recorded books (research; small)**

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

<!-- END stage7 -->

## 6. What this says

* **The market reference is hard to beat.** Its log loss is 0.35 against 0.68 for the constant base rate,
  and it is well calibrated (s.5, calibration preview). *No* approach here improved on it.
* **A adds nothing measurable.** Cross-validation shrank its correction to zero in every outer fold; its only
  free parameter left is a small intercept, costing +0.0008 [-0.0003, 0.0019] nats. The residual (outcome minus
  price) is flat across flow and short-term-return bins. With ~1,500 independent events the interval half-width
  is ~0.001 nats, so a gain larger than ~0.002 nats is ruled out *on these data*.
* **B is a floor, not a competitor.** A base rate cannot know the score of a game. Raw it is worse than a coin
  (0.74) because base rates learned on July-September markets transfer imperfectly to a September
  traded-market sample; recalibrated on research data it merely matches the constant base rate; and blending
  it with the price gets a weight <= 0. That is a useful result: the market is doing all the work.
* **C has nothing to fix.** On the recorded events the market's prices already sum to 1.003 (0.95-1.20), so
  renormalising changes almost nothing - on 14 events, i.e. no power either way.
* **Book features overfit on 366 instances.** The book-augmented A is *worse* than the market (+0.038); the
  microprice is not better than the mid (+0.020 [-0.008, 0.056], 30 events). These datasets are too small
  to say more; Stage 8's holdout (382 settled markets in `books_research_short`) is the real test.

## 7. Pre-specified for Stage 8 (frozen before the holdout is opened)

Frozen code fingerprint **`f47b450da7120eaa`** (`pricing/` + `scripts/stage7_probability_models.py`). The
holdout is untouched (`holdout_access_log` is empty in the results JSON). Predictions on the holdout use models
**fitted on the research period only**: Model A at the penalty chosen there (`1e6`), B recalibrated with
the research-period Platt parameters, kappa = 30.

| # | Hypothesis (holdout) | Research-period basis | Consistent if |
|---|---|---|---|
| P1 | The market reference beats the constant base rate decisively | 0.352 vs 0.681 | paired log loss difference < -0.2 |
| P2 | Model A does not improve on the market | +0.0008 [-0.0003, 0.0019] | difference within +-0.005 |
| P3 | Recalibrated B is no better than the constant base rate | 0.677 vs 0.681 | difference > -0.02 |
| P4 | An independent base rate deserves ~no weight given the price | weights -0.07 to -0.11 | blend weight on B <= 0.1 |
| P5 | Microprice is no better than the mid | +0.020 [-0.008, 0.056] | difference >= -0.005 |
| P6 | Partition renormalisation gains nothing | +0.0022 [-0.0016, 0.0071] | difference within +-0.01 |
| P7 | The market price is close to calibrated, with a mild longshot bias | logit slope 1.07-1.10; low prices overestimate | slope in [0.9, 1.3]; recalibration gains < 0.005 |

P7 is a *research-period observation* (the reliability preview above was looked at while developing), so it is
a hypothesis to test, not a finding.

## 8. Limitations

* **Survivorship** in the history dataset (lifetime-volume selection) inflates how liquid these markets look
  and shifts base rates; every history result inherits it.
* **Sports-heavy**: 73% of history instances are Sports; the book research data are essentially all sports.
* **A short window**: 8 days of closes for the history research set (Sep 1-8), one morning for the book data.
* **Trade-only features** for most instances (`last_trade` is the reference for 80%): no book, so no
  imbalance or microprice, which is where a microstructure edge would most plausibly live.
* **Instances are correlated** (five horizons per market, siblings per event); intervals resample events,
  but effective sample size is still ~1,500 events, not 3,430.
* **No external information source** for Model B (sports lines, polls): the spec says not to use one unless
  contract definitions and timing make it defensible, and none was available offline.

## 9. Reproduce

```bash
python -m scripts.stage7_probability_models --out results/stage7
python -m pricing.report results/stage7/probability_models.json > results/stage7/report.md
```
