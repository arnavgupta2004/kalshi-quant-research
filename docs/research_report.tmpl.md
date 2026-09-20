<!-- GENERATED from docs/research_report.tmpl.md by `python -m scripts.stage12_report`. Edit the template. -->
# Pricing efficiency, no-arbitrage and market making on Kalshi binary contracts

*A research report on a paper-trading framework. Nothing here places, or can place, a real order.*

The objective was never a bot that claims to make money. It was a framework that measures, with
honest uncertainty, whether Kalshi's binary contracts are efficiently priced, whether structural
no-arbitrage relations offer executable edge, whether prices are calibrated probabilities, and whether
a market maker can earn a spread. **The answers are: mostly efficient, no executable edge, calibrated,
and no.** Every number below is rendered from the result files the experiments wrote
(`docs/research_report.tmpl.md` → `python -m scripts.stage12_report`); a figure marked *(pending)* is
an experiment that had not finished when the report was rendered, and it fills itself in on the next
render.

## 1. Summary

| | Question | Answer |
|---|---|---|
| **A** | How often are structural probability constraints violated? | Rarely: {{n.s6_rate}} standing violations per 1,000 relation-hours (fresh recordings); every proven, lattice or declared relation held in **{{n.s3_strong_instances}}** real settled instances with **{{n.s3_strong_violated}}** violations. |
| **B** | What fraction of detected opportunities are executable after fees and liquidity? | Almost none: funnel of confirmed episodes {{n.s6_funnel}}. Stage 4: {{n.s4_displayed}} displayed violations, **{{n.s4_executable}}** survive fees ({{n.s4_fee_free_executable}} would fee-free). |
| **C** | How fast does apparent edge disappear? | Standing edges last seconds (median lifetime {{h6.C1}}); most that vanish move or are pulled rather than being taken (hypothesis C2 failed: {{h6.C2}}). |
| **D** | How does latency affect executable arbitrage? | 1–250 ms cannot be resolved from snapshots seconds apart (reported as brackets); through the engine, partial (leg-risk) bundles at 250 ms and 3 s: {{h6.D3}}; but with real fees there is almost no P&L to lose ({{h6.D4}}). |
| **E** | Are prices calibrated? | Yes: on a sealed holdout of {{n.s8_n}} instances ({{n.s8_events}} events), slope {{n.s8_slope}}, calibration-in-the-large {{n.s8_citl}}; recalibration does not help; {{score.8}} pre-registered criteria held. |
| **F** | Does order-book information improve short-horizon estimates? | Not for the outcome probability (microprice is significantly *worse* than the mid: {{h8.P5}}). At the market maker's horizon only a *staleness correction* transfers out of sample. See §6. |
| **G** | How does time to resolution affect inventory risk? | Through the state, not the clock: the remaining risk of held inventory is flat across time-to-resolution ({{n.s9_ttr_std}}), as the bounded-martingale argument predicts. |
| **H** | Which information materially affects market making? | Only a staleness correction, worth {{n.s10_c21}}¢ per contract (a live-feed emulation); order-book, flow and momentum information is worth nothing detectable, volatility information cuts exposure not loss. Nothing makes a fill profitable (§6). |

**The market-making result, in one paragraph.** A baseline maker built for a bounded (0/1) claim loses
money on fresh recordings ({{n.s9_net}} over {{n.s9_fills}} fills in {{n.s9_events}} events; 30 s markout
{{n.s9_markout}}¢), under every fill model and latency ({{n.s9_cells}} cells negative) and every risk
aversion and spread setting tried. The loss is adverse selection, not fees: fills are informed, and the
price moves through a resting quote and keeps going. Skew, limits and a kill-switch make the loss
smaller and safer, not positive. Section 6 reports what adding microstructure information to the maker
does.

## 2. What was built

| stage | what | headline |
|---|---|---|
| 1–2 | authenticated-optional REST client, read-only WebSocket client, integer-unit market model, DuckDB store, resumable collectors | GET-only client; exact µ$ accounting; every history market reconciled to the exchange's volume |
| 3 | contract semantics, relations with evidence levels | {{n.s3_strong_violated}} violations of {{n.s3_strong_instances}} proven/lattice/declared instances in {{n.s3_events}} settled events; empirical-level relations violated {{n.s3_empirical_rate}}, unverified {{n.s3_unverified_rate}} |
| 4 | same-event arbitrage detector, fee model | {{n.s4_displayed}} displayed violations in {{n.s4_minutes}} minutes, {{n.s4_executable}} after fees |
| 5 | event-driven replay engine (latency, queue models, look-ahead audit) | a passive strategy's P&L spans {{n.s5_passive_range}} across queue assumptions and is not profitable under the most generous ({{n.s5_passive_best}}) |
| 6 | arbitrage research: episodes, lifetimes, latency, categories | {{score.6}} pre-registered hypotheses held; the four that failed are reported |
| 7–8 | probability models; calibration on a sealed holdout | no model beats the market price (log loss {{n.s7_market_ll}} vs {{n.s7_A_ll}} for the microstructure model, {{n.s7_const_ll}} for a constant); the price is calibrated |
| 9 | bounded-support market maker, binary-contract inventory risk | net {{n.s9_net}}; adverse selection |
| 10 | adaptive maker, signal study, ablation ladder | see §6 |
| 11 | paper trading on live data | on three live sessions the orders, fills and equity were {{n.s11_parity}} to a backtest of the session's own tape; strategy reaction {{n.s11_reaction}} |
| 12 | this report, reproducibility manifest, final out-of-sample run | §10 |

## 3. Data, roles and splits

Kalshi serves **no historical order books**, so every book used here was recorded forward by the
project's own recorder (REST polling every 3–5 s); trades, settlements and history come from the API.
Each database plays exactly one role, and the code and hypotheses are frozen (fingerprint-checked)
before a confirmatory database is analysed.

| data | content | role |
|---|---|---|
| `kalshi.duckdb` | 3,000 settled markets, 1.91 M trades (Stage 2) | probability models and calibration (research split; a sealed holdout) |
| `relations.duckdb` | {{n.s3_events}} settled events, {{n.s3_markets}} markets | relation semantics and their real-outcome audit |
| `books_shortlived` | live sports, 177 markets / 60 events, ~0.6 h observed | development (arbitrage, market making); validation for Stage 10 |
| `books_structural` | 589 markets / 100 events, all categories, ~0.7 h, no trade tape | development; validation for Stage 10 |
| `books_research_short`, `books_research_wide` | 399 + 595 markets, 53 + 100 events; recorded concurrently over ~6 h with a **4.4 h outage**, so ~1.7 h and ~1.8 h observed | **confirmatory** for Stages 6 and 9; **training** for Stage 10 (their Stage 9 results were public before Stage 10 was designed) |
| `books_stage10` | fresh recording (sports and crypto ladders), 6 h | confirmatory for Stage 10 |
| `books_stage12_us` | fresh US-afternoon sports recording, 6 h, scheduled before any strategy saw it | **final out-of-sample** for everything frozen |

**Independence.** The unit of independence is the *event* (siblings of one event share an outcome and a
price path), so every interval is an event-cluster bootstrap and every table shows how many events
stand behind a number. The recordings are short, one window each, dominated by sports; nothing here
describes other seasons or a full market cycle.

## 4. Arbitrage: Experiments A–D

**Hypotheses** (pre-registered, frozen before the confirmatory recordings were analysed): standing
violations of declared or proven relations exist but are rare; almost none are executable after fees at
100 contracts; a standing edge lasts seconds; and legs of a bundle fail more often as latency grows.

**Method.** A *sighting* is a constraint whose displayed prices violate a relation; a *confirmed*
episode is one still there at the end of a later poll cycle (all legs re-fetched), so it cannot be two
moments stitched together by non-atomic polling. Lifetimes are interval-censored, latency is bracketed
(optimistic/pessimistic) because polling cannot resolve 1–250 ms, and outcomes are scored against
realised settlement.

**Results** (fresh recordings; {{n.s6_confirmed}} confirmed episodes, {{n.s6_sightings}} sightings):

* **A — frequency.** {{n.s6_rate}} confirmed per 1,000 relation-hours ({{n.s6_rate_sightings}} counting every
  sighting). Not stable across windows: the development data gave {{n.s6_dev_rate}}, because one live
  event supplied half of the sightings there.
* **B — executability.** Fees, then size, remove the edge; the structure is right. Whenever a relation
  settled, the bundle paid at least what it promised (`{{h6.B3}}`). The funnel, confirmed episodes at
  100 contracts:

{{t.funnel}}

* **C — decay.** Median lifetime {{h6.C1}}. What ended the confirmed episodes: {{h6.C2}} (trade-tape attribution is only as sharp as the polling interval).
* **D — latency.** Partial (leg-risk) bundles: {{h6.D3}}. Scored fully hedged bundles: {{h6.D4}}. Latencies below the polling cadence are brackets, not measurements.
* **Categories.** Only Sports has enough events to say anything; every other category is flagged and
  not interpreted.

{{t.arb_category}}

**Pre-registered predictions that failed** ({{score.6}} held): A3 ({{h6.A3}}), A4 ({{h6.A4}}), C2
({{h6.C2}}), D1 ({{h6.D1}}). They are results, not embarrassments: the chunk-skew artefact that dominated
one development event is not general; Climate & Weather shows small standing violations; trades are not
the main reason edges disappear; and the sub-cadence latency bracket is narrower than expected.

**Why the headline is negative.** A cross-market arbitrage needs 2–6 legs; each pays the quadratic fee
(up to ~1.75¢ per contract at 50¢), the displayed edge of a confirmed episode is 1–1.7¢, and the median
size at the best price is ~11 contracts. The structure is right; price and fee schedule leave nothing
for a taker.

**Limitations.** ~211 observed minutes on one Saturday, {{n.s6_confirmed}} confirmed episodes; polling
not streaming (nothing below ~1 s is measured); every latency bracket rests on a no-flicker assumption;
taker only, standard fee schedule.

## 5. Probabilities: Experiments E and F

**Hypotheses.** Kalshi prices are approximately calibrated probabilities, and no simple model built from
microstructure or an independent base rate beats the price.

**Method.** Three models of P(X=1 | information at t): a ridge-shrunk microstructure correction to the
price, an independent hierarchical base rate that never sees a price, and renormalisation of exclusive
and exhaustive outcomes. Prequential, event-grouped evaluation; the calibration study (reliability
diagrams, slope and intercept, ECE, the Murphy decomposition, five subgroup dimensions) ran **once** on
a sealed holdout whose opening is logged and enforced in code.

**Results.** On the research period no model beats the market reference (log loss {{n.s7_market_ll}}; the
microstructure model {{n.s7_A_ll}}; a constant {{n.s7_const_ll}}). On the sealed holdout ({{n.s8_n}}
instances, {{n.s8_events}} events): slope {{n.s8_slope}}, YES-minus-price {{n.s8_citl}}; {{h8.C9}}. All
{{score.8}} criteria held. Order-book information does not help the outcome forecast: {{h8.P5}}.

**Failure cases and limitations.** A tape-ordering nondeterminism in the trade loader (same-timestamp
prints ordered arbitrarily) moves the reference price's log loss by about ±0.003; it was found in
Stage 8, shown not to change any verdict, and left unpatched because the Stage 7/8 results are frozen.
The holdout is now spent: any new calibration claim needs fresh data. Calibration is measured on a
selected sample (markets that traded), so it says nothing about untraded ones.

## 6. Market making: Experiments G and H

### 6.1 The baseline (Stage 9)

Avellaneda–Stoikov, derived for an unbounded diffusion, cannot be used as it stands for a claim that
settles at 0 or $1: price unbounded, constant volatility, a deterministic risk clock, no settlement
jump, no YES/NO symmetry. The bounded-support form used here takes the CARA certainty equivalent of a
Bernoulli claim as the reservation price, so quotes stay inside (0, $1) by construction, skew against
inventory, and carry the claim's own remaining variance p(1−p). Inventory risk is written in settlement
terms (worst, best and expected value, an event-level worst case over the admissible outcomes,
position/event/portfolio limits, a drawdown kill-switch).

**Results** (fresh recordings, {{n.s9_fills}} fills, {{n.s9_events}} events):

* net **{{n.s9_net}}**: spread captured {{n.s9_spread}}, inventory contribution {{n.s9_inventory}}; fees are
  a small part. 30 s markout **{{n.s9_markout}}¢**; settled P&L per fill **{{n.s9_settled_fill}} $**.
* negative under every fill model and latency ({{n.s9_cells}} cells; {{n.s9_net_range}}); in
  {{n.s9_grid_cells}} of the risk-aversion × spread grid cells is settled P&L per fill significantly above zero.

Each design element removed in turn:

{{t.ablation9}}

Skew cuts mean inventory ({{n.s9_skew_inv}} contracts) without making money; the kill-switch caps the
drawdown ({{n.s9_kill_dd}}) without creating edge.

**By category:** {{n.s9_sports_share}} fills are Sports (markout {{n.s9_sports_markout}}¢); the other
categories have too few events to say anything.

{{t.mm_category}}

**Experiment G — time to resolution.** The remaining standard deviation of the inventory a maker is left
holding, |q|√(p(1−p)), is flat across time-to-resolution ({{n.s9_ttr_std}}): risk depends on the state
(the price), not on a (T−t) clock. Adverse selection is not worse near resolution ({{n.s9_ttr_near_markout}}
in the last half hour); pre-registered hypothesis M6 was satisfied only by a 0.03¢ gap and is a null.

### 6.2 The adaptive maker (Stage 10) and Experiment H

The Stage 9 loss is adverse selection, so the obvious question is whether information can repair it.
The adaptive maker changes four things, each one formula with a switch (all off returns the baseline
quote for quote): a **fair-value shift** from a frozen forecast of the next 5 s mid move, **widening** by
the forecast size of the next move, **size** inversely to it, and a **time-to-resolution skew**. The
forecasts are ridge models fitted on the Stage 9 confirmatory databases (now *training* data) and judged
on databases they never saw.

**Experiment F at the maker's horizon.** Of the microstructure features, only a **staleness correction**
(prints newer than the polled book: what a live feed's book would show for free) transfers: validation
skill {{n.s10_stale_skill}} against "the mid does not move". Order-book imbalance and the microprice fail
out of sample (validation skill {{n.s10_book_val}}), as do flow and momentum; adding them to the staleness
model lowers skill. The *size* of the next move is forecastable (skill {{n.s10_scale_skill}}); its direction
is not. And fill toxicity barely depends on that size: the 30 s markout of baseline fills by forecast-scale
tercile is {{n.s10_tox_table}}, around a constant of {{n.s10_tox_const}}¢ (slope {{n.s10_tox_slope}}¢ per ¢).
Fills are adversely selected in every volatility state.

**Ablation ladder** (validation databases; settled P&L per contract in cents, paired by event against the
baseline; each rung adds one component):

{{t.ladder10}}

Contrasts between rungs (¢ per contract): staleness fair value over re-pricing alone
**{{n.s10_c21}}**; book, flow and momentum on top of staleness {{n.s10_c32}}; widening {{n.s10_c42}}; sizing
{{n.s10_c54}}; time skew {{n.s10_c65}}.

**Experiment H — which information materially affects market making?**

1. **One component helps, modestly: the staleness fair value** (+0.4¢ per contract, roughly an eighth of the
   baseline's per-contract loss). It is a live-feed emulation, not a market insight.
2. **Nothing else improves fill quality.** Genuine microstructure features add nothing significant;
   widening is worse per contract and monotonically so in κ (FULL settled ¢/contract at κ = 0.5, 1, 2, 4:
   {{n.s10_kappa_pc}}); the time skew changes nothing.
3. **Sizing cuts the loss by trading less, not by trading better**: net {{n.s10_base_net}} → {{n.s10_full_net}}
   with {{n.s10_contract_cut}} fewer contracts, at a per-contract point estimate that is worse. A losing
   strategy that trades less loses less.
4. **The adaptive maker still loses under every fill model and latency** ({{n.s10_full_cells}} cells), and
   its fills remain adversely selected.

**Confirmatory status.** The hypotheses S1–S3 and T1–T8 were written from the development data and hold
there by construction ({{score.10v}}); only fresh data test them. Stage 10 confirmatory recording:
{{n.s10c_score}}. Final out-of-sample recording: {{n.s12_score}}.

**A correction that changed a conclusion.** The first version of this study said the staleness correction
was worth ~1.4% skill and changed nothing. Both were wrong. The confirmatory recordings contain a 4.4 h
outage; the trade tape was fetched afterwards, and the signal study had sampled trade prints inside the
hole, where the book is hours old and the label is exactly zero. Trained on those, every model learned
"nothing predicts anything" (85% of one database's trade samples were of this kind). After the fix the
staleness skill is four times larger and the fair value improves fill quality; the other conclusions
stand. The hypotheses T4 and T5 were rewritten *before* the confirmatory data were scored, and the reason
is recorded in the code.


## 7. Paper trading (Stage 11)

The Stage 5 engine already is a simulated exchange, so paper trading only needs a real-time feed. Live
events go to the engine through a queue in its own thread; the Stage 9/10/4 strategies run unchanged.
Every session writes its tape, and ends by replaying the tape through a fresh strategy and the plain
backtest engine: on three live sessions ({{n.s11_orders}} simulated orders) the orders, fills
and equity were {{n.s11_parity}}, with a strategy reaction of {{n.s11_reaction}}. The WebSocket path could not
be run live (no API key here) and is tested against a mock exchange. Live P&L over minutes is noise and is
not a result.

## 8. What worked, what failed, and why

**Worked.**

* **The event, not the episode or the fill, as the unit of independence.** It turned apparently large
  samples into a handful of effective events, and it explains why rates moved 2–4× between windows.
* **Freezing code and hypotheses before the data.** Predictions that failed or came out null were
  impossible to explain away because they were written first (scoreboard below).
* **Uncertainty that refuses to be precise:** interval-censored lifetimes, latency brackets, fill-model
  ranges. They declined to produce a 5 ms number the data cannot support.
* **Settlement as a free test.** Scoring realised payoffs against promises validated every declared
  relation on every event that settled.
* **A bounded-support treatment of the market-making problem**, with properties (bounds, monotone skew,
  YES/NO duality, small-γ limit) asserted rather than assumed.
* **One engine for backtest and live**, with parity checked on every session.

The pre-registered hypotheses, by experiment:

{{t.scoreboard}}

**Failed.**

* **Nothing found an edge.** Arbitrage is consumed by fees and size; prices are calibrated; no model
  beats the price; a market maker loses to informed flow.
* **Four pre-registered arbitrage predictions** (A3, A4, C2, D1) did not hold; the market-making
  "time to resolution" prediction M6 held only by a 0.03¢ gap and is a null in substance.
* **Several of my own errors**, found by the discipline above and fixed in the open: simultaneous events
  applied one at a time (a phantom +$983 arbitrage, Stage 5); a leaking event-size feature and a
  prequential fold that let siblings leak (Stage 7); trade-tape ties ordered arbitrarily (Stage 8,
  documented, not patched); a mutation-testing run whose stale bytecode hid surviving mutants; a
  confirmatory smoke test run on real data (disclosed); and **recording outages**: the confirmatory
  recordings hold ~1.8 h of observed data, not the ~6 h their wall-clock suggested, and the Stage 10
  signal study had sampled trade prints inside the outage, biasing its models toward "no signal" (§6.2).

**Why.** The market is competitive where it is liquid and structurally sound where it is constrained.
Constraints hold, so there is nothing to arbitrage except tiny standing edges that fees erase; prices are
calibrated, so there is nothing to forecast better; and a maker who quotes around a mid is, by
construction, the counterparty whom informed traders select. A live sports market moves in jumps, and
the jump takes the price through a resting quote first.

## 9. Threats to validity, and what is still open

1. **Stale books.** Recorded books are polled every 3–5 s and arrive ~0.4 s late while trades are
   timely, so every market-making backtest quotes around a stale mid. This overstates adverse selection
   relative to a live-feed maker. The WebSocket path exists to remove it and could not be run here.
2. **Short, single-window, sports-dominated recordings** — the largest limit on every claim. Event
   counts stand behind every number; claims resting on fewer than 20 events are "inconclusive".
3. **Fills are simulated** from the public tape with queue-position assumptions bracketed by five models;
   a real order changes the book it trades against.
4. **One exchange, one product family, one period.** No cross-venue comparison was made (the optional
   stretch goal), and nothing about other regimes is claimed.
5. **Out-of-sample status.** The confirmatory Stage 10 recording and the final out-of-sample recording
   (`books_stage12_us`) are the last data no design decision has touched.
   Stage 10 confirmatory: {{n.s10c_score}}. Final out-of-sample: {{n.s12_score}}.

## 10. The last two runs, pre-registered

Both were written before their data existed, and both run the code exactly as frozen.

| run | data | frozen code | what is scored |
|---|---|---|---|
| Stage 10 confirmatory | `books_stage10` (07:40 UTC 2026-09-20, 6 h: sports, crypto ladders, golf; thin) | fingerprint `6ccb2dba95a32994` | S1–S3 and T1–T8 (`research/adaptive_hypotheses.py`), event-cluster intervals, claims on fewer than 20 events reported as *inconclusive* |
| **Final out-of-sample** | `books_stage12_us` (from 21:50 IST 2026-09-20, 6 h; **sports only**: NFL, MLB, NCAAF, NBA, NHL, football, tennis, baseball, boxing, MMA; selection in `configs/books_stage12_us.yaml`, ex-ante by scheduled expiry) | fingerprint `c185c46aae99a67c` (Stage 6's 32 analysis modules pinned explicitly, the Stage 10 code, the runner) | the same 15 Stage 6 hypotheses (arbitrage replication) and the same S1–S3, T1–T8; the models are the training-fit coefficients, never refitted |

What is expected, so that a surprise is visible as one: arbitrage remains rare and almost never executable
after fees (A1, B1, B2); a sports-only recording makes the non-sports category hypothesis (A4) vacuous
rather than confirmed; the market maker and its adaptive variant remain unprofitable (T1, T8) with
adversely selected fills (T7); the staleness fair value again beats "the mid does not move" (S1) and
sizing again cuts the loss by trading less (T3). Hypotheses that the data cannot support (too few
events) are reported *inconclusive*, never counted as held.

{{t.scoreboard}}

## 11. Reproducibility

Every experiment records its seed, dataset fingerprint, markets, parameters and git commit in its result
file; stages that were pre-registered refuse to run if their code fingerprint differs from the frozen
one. `python -m scripts.stage12_manifest --check` verifies that every frozen fingerprint still
reproduces and writes `results/stage12/manifest.json`. One exception, found while writing this report: the
fingerprint stored with the Stage 6 results hashes whole packages and cannot be reproduced from the
committed tree (whole-package hashes also move whenever a new module is added). The Stage 6 development and
confirmatory analyses were therefore **re-run from scratch on the committed code, and every reported
number reproduced exactly** (`results/stage12/reproduction_stage6.json`); the final out-of-sample run pins the Stage 6 files
explicitly instead. The full test suite (>750 tests, including
property tests with independent oracles and mutation-tested analysis code) runs with `pytest`.
