# Stage 10 — An adaptive market maker, and what each adaptation is worth

Everything here is **paper**: simulated orders against recorded books and trade tapes. No order is
sent to Kalshi and no credential is read.

Stage 9 built a baseline market maker and found it loses money because its fills are adversely
selected (30 s markout −1.42¢, settled P&L −$0.18 per fill on the confirmatory data). Stage 10 asks
the obvious next question — *can microstructure information and time-to-resolution repair that?* — and
answers it component by component, on data the components were not fitted on. Where the answer is
"no", this document says so.

## 1. What was built

| module | what it holds |
|---|---|
| `market_making/features.py` | `MarketTracker`: causal microstructure features, computed incrementally; one class serves the offline fit and the live strategy |
| `market_making/shortterm.py` | `Ridge`: the frozen linear model the strategy carries |
| `market_making/adaptive.py` | `AdaptiveMarketMaker`: the Stage 9 baseline plus four switchable adjustments |
| `research/signal_study.py` | labelled samples from a recorded feed, ridge fitting, event-grouped cross-validation, skill scoring |
| `research/adaptive_analysis.py` | the ablation ladder, paired-by-event comparisons, per-contract rates |
| `research/adaptive_hypotheses.py` | the pre-registered hypotheses, scored mechanically |
| `scripts/stage10_adaptive_mm.py` | the driver: roles, frozen-code guard |

The Stage 9 files are untouched (their frozen fingerprint still reproduces): the adaptive maker is a
subclass.

## 2. The adaptive strategy

The baseline quotes `bid*(q) − δ` and `ask*(q) + δ` around the book mid `m` (bounded-support
Avellaneda–Stoikov, Stage 9 §3). The adaptive maker changes four things, each with one formula and
one switch. Every switch off returns the baseline quote for quote (property-tested on random feeds).

1. **Quote centre** `f = m + μ`. `μ` is a frozen ridge forecast of the mid's move over the next 5 s from
   causal features, clipped to ±5¢. Quotes are centred on `f` instead of `m`.
2. **Adverse-selection widening** `δ += κ σ̂`. `σ̂` is a frozen forecast of `E|m(t+10 s) − m(t)|`. A resting
   quote is picked off when the price jumps through it, and the expected size of that jump is what
   a spread must cover; the baseline's `(1/γ)ln(1+γ/k)` prices only the arrival rate. `κ = 1` (one
   expected move) is set from that argument, **not tuned**.
3. **Size** `n = n₀ · clip(σ_ref/σ̂, 0.2, 2)`: a constant dollar risk per quote (the loss from a fill
   scales with `n σ̂`). `σ_ref` is calibrated on the training data so that the *average* fraction is
   1, so the rule moves size from volatile to calm states without changing the amount traded — the
   comparison with the baseline is then per contract, not flattered by trading less.
4. **Time to resolution** `γ_eff = γ (1 + a e^{−τ/τ₀})`, `a = 1`, `τ₀ = 1 h`: the closer to settlement,
   the fewer chances to work inventory off passively, so skew harder. (Stage 9 found the *risk* of
   inventory is state-dependent, not clock-dependent; this is a claim about *unwinding*, not risk.)

The strategy also **re-prices on trade prints**, not only on book snapshots. Recorded books are
polled every ~3 s, so a print is the freshest price information there is; a live feed would show it
in the book at once. Whatever this buys is therefore an **emulation of a live feed**, reported apart
from genuine signal (§5). The engine applies simultaneous events as a batch, so the tracker reads the
engine's book, not the event (a bug found by the equivalence property test).

## 3. The features

All causal, dimensionless or in dollars of probability; `m` the mid, `s` the spread.

| feature | definition |
|---|---|
| `obi_top`, `obi_depth` | (Q_bid − Q_ask)/(Q_bid + Q_ask) at the touch / over three levels a side |
| `micro_dev` | microprice − m = (s/2)·`obi_top` |
| `flow_60` | signed taker volume over 60 s, `S/(V + 20)` (a thin tape is shrunk toward "no information") |
| `stale_dev` | vwap of prints in the last 10 s that are newer than the last book observation, minus m |
| `stale_out` | the part of `stale_dev` beyond the stale book's own quotes: sign·max(0, \|d\| − s/2) |
| `ret_60` | m − (the mid 60 s ago) |
| `rv_60` | Σ\|Δm\| over the last 60 s (realised movement) |
| `lv_10`, `lv_60` | ln(1 + contracts traded in the last 10 s / 60 s) |

`stale_dev` and `stale_out` are the **staleness correction**: information a live feed's book would
already contain. They are what re-pricing on prints exploits, and they are the only direction
feature that survives out of sample (§5). Everything else is a genuine microstructure signal.

## 4. Data and roles

| role | databases | use |
|---|---|---|
| **training** | `books_research_short`, `books_research_wide` (~12 h, 137 events) | the two short-horizon models are **fitted** here. Strategy results on them are *in-sample* for the models |
| **validation** | `books_shortlived`, `books_structural` (~1.8 h, 142 events, 33 with a settled market) | never seen by the fit; feature groups are admitted or rejected on it |
| **confirmatory** | `books_stage10` (recorded from 2026-09-20 07:40 UTC, after Stage 9's results were known) | opened once with the code frozen — see §9 |

The Stage 9 confirmatory databases had to become *training* data: their baseline results were public
when this stage was designed, so a claim about improving on that baseline cannot be confirmed on them.

Labelled samples: one per book snapshot or trade print (at least 1 s apart per market),
`y = m(t+h) − m(t)` from the recorded books, dropped if the market is not observed alive until `t+h`.
Models are ridge regressions on standardised features with no intercept for the fair-value shift; the
ridge penalty is chosen by event-grouped cross-validation. Skill is `1 − SSE(model)/SSE(zero forecast)`
with an event-cluster bootstrap. The horizon for the fair value is 5 s (the median Stage 9 quote lived
~5 s); the scale horizon is 10 s.

## 5. Experiment F at the market maker's horizon: what predicts the next mid move?

Skill against "the mid does not move", horizon 5 s, 95% event-cluster intervals. "CV" is cross-fitted
inside the training data; "validation" is a model fitted on all of the training data and scored on the
validation databases.

| feature group | CV (training) | validation |
|---|---|---|
| staleness (`stale_dev`, `stale_out`) | 1.17% [0.78, 1.63] | **1.36% [0.89, 1.96]** |
| order book (`obi_top`, `obi_depth`, `micro_dev`) | 0.47% [−0.04, 0.97] | −0.50% [−1.36, 0.15] |
| order flow (`flow_60`) | 0.03% [0.01, 0.05] | 0.06% [0.03, 0.10] |
| momentum (`ret_60`) | 0.58% [0.05, 1.16] | −1.18% [−2.41, −0.07] |
| all direction features | 2.15% [1.34, 2.92] | 0.04% [−0.94, 1.02] |

* **The order book, flow and momentum do not generalise.** The book and momentum groups look useful
  inside the training data and lose it — momentum significantly — on data they were not fitted to. This
  matches Stage 7/8, where book features added nothing to the outcome forecast and the microprice was
  significantly *worse* than the mid.
* **The staleness correction is the one direction signal that transfers**, at a size of ~1% of
  squared error. It is not a market insight: it is what a live feed's book would already show, which is
  why it is reported apart.
* **Admission rule**, set after this signal study and before the final development run of the
  ladder: a feature group enters the fair value only if its validation interval is above 0 *and* its
  point skill is at least 0.5% (below that the shift is under a tick). The materiality clause was added
  on seeing that order flow clears zero but is immaterial (0.06%); it is a judgement, stated here.
  Only staleness qualifies.
* **The size of the next move is forecastable; its direction is not.** Realised movement over the last
  minute (`rv_60`) predicts `E|Δm|` at 10 s with skill **10.2% [7.2, 13.3]** on validation (13.4% in
  sample). Trade intensity adds nothing.

### The widening premise fails

The widening term assumes a fill is costlier when a bigger move is expected. Tagging every baseline
fill with the forecast scale at the last quote decision (`scripts/stage10_toxicity_diagnostic.py`,
3644 fills, exploratory, no intervals claimed):

| forecast scale, tercile | 30 s markout | settled P&L per contract |
|---|---|---|
| calm (0.15–0.61¢) | −1.63¢ | −1.55¢ |
| middle | −1.66¢ | −2.86¢ |
| volatile (1.21–9.3¢) | −2.15¢ | −2.99¢ |

The markout moves by only −0.13¢ per 1¢ of forecast scale around a **constant −1.66¢**. Fills are
adversely selected in *every* volatility state; predicted volatility explains little of it. That is
why widening by `κσ̂` cannot repair the fill quality (§6).

## 6. The ablation ladder

Each rung adds one component. Rates are **per contract, in cents**, so a variant that trades less is
not flattered; "vs baseline" is a *paired* difference (events resampled together). All strategies use
the Stage 9 risk limits and the same fill model (`queue(a=1,c=.5)`, 200 ms).

### Validation databases (out-of-sample for the models; 33 settled events)

| variant | net | contracts | settled P&L / contract | 30 s markout / contract | vs baseline, settled ¢/contract |
|---|---|---|---|---|---|
| 0 baseline (Stage 9) | −$317 | 13,360 | −3.18¢ [−4.41, −2.22] | −2.18¢ | — |
| 1 + re-price on prints | −$333 | 13,538 | −3.24¢ | −2.17¢ | −0.07 [−0.55, 0.50] |
| 2 + fair value: staleness | −$318 | 13,381 | −3.33¢ | −2.18¢ | −0.16 [−1.00, 0.50] |
| 3 + fair value: book, flow, momentum | −$323 | 13,281 | −3.46¢ | −2.16¢ | −0.28 [−1.20, 0.52] |
| 4 + widening `κσ̂` | −$298 | 11,441 | −3.38¢ | −2.26¢ | −0.21 [−1.47, 1.06] |
| 5 + size by expected move | −$105 | 3,788 | −4.75¢ [−7.45, −2.35] | −2.00¢ | −1.58 [−3.64, 0.24] |
| **6 + time skew (= FULL)** | **−$112** | 3,879 | −4.78¢ [−7.27, −2.45] | −1.86¢ | −1.60 [−3.46, 0.07] |

Contrasts between adjacent rungs, settled ¢/contract: 2 vs 1 −0.09 [−0.56, 0.21]; 3 vs 2 −0.13
[−0.48, 0.22]; 5 vs 4 **−1.37 [−3.20, −0.09]**; 6 vs 5 −0.03 [−0.63, 0.60].

### Training databases (in-sample for the models; 12 h)

The same picture with a larger sample: baseline −$426 (−1.89¢/contract); FULL −$176 on 8,369
contracts (−2.33¢/contract, −0.44 [−1.05, 0.19] vs baseline); staleness fair value +0.12 [−0.15, 0.39]
over re-pricing alone; book/flow/momentum +0.09 [−0.13, 0.41] over staleness alone; widening −0.10
[−0.38, 0.19]; size −0.29 [−0.85, 0.30]. Even *in-sample*, the fitted fair value does not improve fill
quality.

### What the ladder says

1. **No component improves fill quality.** Not the staleness correction, not the genuine
   microstructure features, not widening, not the time skew. Every per-contract difference is
   statistically indistinguishable from zero except the size rule, which is *worse* (−1.37¢, interval
   excludes zero on validation).
2. **The apparent gain is exposure, not edge.** The size rule cuts the loss from −$317 to about −$110
   on validation (paired total per event +$5.4 [2.6, 8.6]) and from −$426 to −$176 in sample (+$3.1
   [1.1, 5.4]) — by trading **71%** and **63%** fewer contracts. Volatile states are where fills
   happen, so shrinking size there shrinks the strategy. A losing strategy that trades less loses
   less; nothing about the *quality* of its trades changed. Leaving the size rule out returns the
   baseline's loss (full − size: −$303, kill-switch tripped).
3. **Widening is monotonically worse per contract.** FULL's settled P&L per contract for
   `κ = 0.5, 1, 2, 4`: −4.15, −4.78, −5.39, −6.07¢. Wider quotes fill less but not *better*, as §5
   predicts.
4. **The fair-value shift is nil.** The staleness correction moves quotes by a fraction of a tick on
   average (weights 0.004 and −0.0034 per standardised unit): a real but tiny forecast gain cannot
   change a strategy whose fills are toxic for reasons the forecast does not capture.
5. **Still negative everywhere.** FULL loses money under all 10 fill-model × latency cells (−$102 to
   −$113 on validation, against −$311 to −$326 for the baseline).

## 7. The pre-registered experiments (spec §23), answered on development data

* **F — does order-book information improve short-horizon estimates?** Not out of sample. Only the
  staleness correction (a live-feed emulation) does; the size — not the direction — of the next move
  is forecastable (10%).
* **G — how does time to resolution affect inventory risk?** As in Stage 9, not through the clock:
  adding a time-dependent skew changes nothing (6 vs 5: −0.03 [−0.63, 0.60]).
* **H — which information sources materially affect market-making performance?** None improves what a
  fill is worth. Volatility information (`rv_60`) reduces exposure through sizing, at a per-contract
  cost. Order-book, flow and momentum information have no out-of-sample value.

## 8. Pre-registered hypotheses (for the confirmatory recording)

Written **after** the development run and **before** the confirmatory recording was scored. The code,
the models' training data and the driver are frozen by the fingerprint; `--expect-fingerprint` makes
the confirmatory run refuse if anything changed.

Frozen code fingerprint: **`dabbe4d46dbefeee`**. Sufficiency rule: a claim on fewer than 20
independent events is *inconclusive*, never consistent or not.

| id | hypothesis | criterion |
|---|---|---|
| **S1** | the staleness fair value beats "the mid does not move" at 5 s | 95% interval of skill above 0 |
| **S2** | adding book, flow and momentum does not beat staleness alone | point skill(all) ≤ point skill(staleness) |
| **S3** | realised movement predicts the size of the next move (10 s) | 95% interval of skill above 0 |
| **T1** | the adaptive maker still loses money | net < 0 and settled P&L < 0 |
| **T2** | it does not improve settled P&L per contract | paired difference vs baseline: lower bound ≤ 0 *(weak: a wide interval satisfies it)* |
| **T3** | any loss reduction comes from trading less | fewer contracts, paired total P&L per event > 0, per-contract change ≤ 0 |
| **T4** | the staleness correction changes per-contract P&L by nothing detectable | rung 2 vs 1: interval contains 0 |
| **T5** | book, flow and momentum in the fair value do not improve on staleness | rung 3 vs 2: point ≤ 0 |
| **T6** | the time-to-resolution skew changes nothing detectable | rung 6 vs 5: interval contains 0 |
| **T7** | its fills remain adversely selected | 30 s markout per contract: interval below 0 |
| **T8** | it loses under every fill model and latency | net < 0 in all 10 cells |

All eleven hold on the validation data (`results/stage10/dev`, `hypotheses_on_validation`) — by
construction, since they were written from it. That is **not** evidence; only fresh data tests them.
S1–S3 and T1, T3–T8 predict the null or a loss, so a confirmatory failure would be informative, not
disappointing.

## 9. Confirmatory status: not yet run

The fresh recording (`configs/books_stage10.yaml`, 6 h from 2026-09-20 07:40 UTC, ends ~13:40 UTC)
was still running when this stage was written up, and by design it is **not thin only in time**:
after 1.2 h it held 26 events, 188 of its 400 markets being one Bitcoin strike ladder and most of the
rest golf-tour markets, with only a few live baseball games. Most claims would rest on fewer than 20
independent events and come out *inconclusive* under the sufficiency rule. The decision, taken with
the user, was to close the stage on development and validation evidence, keep the recording running,
and run the confirmatory step **once** on it when it finishes, as a short addendum. A proper US-evening
recording, when MLB and NFL games are live, belongs to Stage 12's out-of-sample evaluation.

```bash
python -m data.collectors.run refresh --config configs/refresh_stage10.yaml   # settlements + trade tape
python -m scripts.stage10_adaptive_mm --role confirmatory --out results/stage10/confirm \
    --expect-fingerprint dabbe4d46dbefeee
python -m research.adaptive_hypotheses results/stage10/confirm/adaptive.json
```

**A disclosure.** To check that the confirmatory code path would not crash, I ran the pipeline once
on a *copy of the partial recording* (1.2 h, 244 baseline fills) and looked at the output: no
statistic from it was used to change any code, model, parameter or hypothesis, and the frozen
fingerprint was computed afterwards, but the smoke test should have been run on a development
database instead. The confirmatory run will use the whole recording, which contains those hours, so it
is not an untouched sample in the strictest sense. I record this so the eventual result can be read
accordingly.

## 10. Limitations

1. **Stale mid.** As in Stage 9, the maker quotes around a polled book while fills are triggered by
   timely trades. The staleness correction addresses part of this, but the deeper fact remains: a
   live-feed maker would re-centre in ~100 ms. These results describe *this* information set.
2. **Small samples.** 33 settled validation events and 137 training events, mostly live sports; the
   per-contract intervals are wide (the paired size-rule difference on validation is −1.58¢ with
   [−3.64, 0.24]). "Not detectably different from zero" is not "zero"; T2, T4 and T6 are weak claims.
3. **The training data are in-sample for the fitted models**, and the confirmatory set is thin (§9).
4. **Fair value is deliberately tiny.** A ridge on six features with strong shrinkage cannot find a
   nonlinear or regime-dependent signal; the study rejects the *linear, low-parameter* version of
   microstructure information, not every conceivable one.
5. **The size rule's effect depends on where fills occur.** Calibrating `σ_ref` on all decision times
   left the *fill-weighted* average size at about a third of the baseline's, because fills concentrate
   in volatile states. Calibrating on fills instead would trade the same volume; it was not done, to
   avoid tuning to the outcome, and the per-contract comparison is the one that does not depend on it.
6. **Parameters were not tuned**: `κ = 1`, the size band [0.2, 2], `a = 1`, `τ₀ = 1 h`, the horizons
   (5 s, 10 s) and the ridge penalties (cross-validated) were fixed from the arguments in §2 before
   the ladder was judged; the κ sweep is reported, not selected from.

## 11. What Stage 11 and 12 inherit

Nothing in Stage 10 makes the market maker profitable, and the ladder says why: fills are adversely
selected in every state, so information about *when* the price will move — the only information this
data supports — does not change what a filled quote is worth. What it can do, sizing by expected
volatility, reduces exposure. Stage 11 (paper trading on the live WebSocket feed) removes the stale-mid
handicap: the baseline and adaptive makers can be run against a genuinely live book, and the
comparison in this document is the one to repeat there. Stage 12 needs a large fresh US-evening
recording for the out-of-sample evaluation.
