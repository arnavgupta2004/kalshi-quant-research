# kalshi-quant-research

A research framework for studying pricing efficiency, no-arbitrage structure, probability
calibration, execution and market making in **Kalshi binary prediction markets**.

> **Research and paper trading only.** The client is read-only by construction: the only HTTP verb
> implemented is `GET`, the WebSocket client only sends `subscribe`, order-entry channels are
> rejected, and there is no order code anywhere. Credentials live in a gitignored `.env`.

Status: **Stage 8 of 12 complete** (API client, normalised data model, historical storage, a
resumable collector, validated contract-relationship logic, a fee-exact same-event arbitrage detector, a
causal event-driven backtest engine, an arbitrage research study with a frozen-code confirmatory run, three fair-probability models, and a calibration study confirmed on a sealed holdout). See [`docs/contract_semantics.md`](docs/contract_semantics.md) for price/contract
semantics and verified API behaviour, [`docs/relationships.md`](docs/relationships.md) for the
relationship model and its verification against 14k real settled events, and [`docs/data_architecture.md`](docs/data_architecture.md) for
the storage design, resume guarantees and the known biases of the dataset. The full technical report
is written at Stage 12.

## Quick start

```bash
uv venv --python 3.12 && uv pip install -e ".[dev]"
cp .env.example .env            # optional for REST; required for the WebSocket
.venv/bin/python -m pytest      # no network or credentials needed
.venv/bin/python -m scripts.stage1_demo --markets 5 --seconds 30   # live data -> local state
```

## Layout (Stage 1)

| Package | Role |
|---|---|
| `market/` | pure domain core (+ `fees`: exact per-series Kalshi fee model in µ$), no I/O: exact units, UTC time, `OrderBook`, contracts (`Market`, `Event`, `ContractRef`, `Trade`), **`relationships`** (relations -> buy-only no-arbitrage constraints), **`semantics`** (which relations really hold, with evidence levels), `evidence` (settled-history support with exact confidence bounds) |
| `kalshi_client/` | adapter: RSA-PSS auth, async REST (rate limit, retry, pagination), resilient WebSocket, strict wire models |
| `data/normalization/` | wire -> domain mapping; sequence-checked book maintenance (`fail closed`), duplicate suppression |
| `data/schemas/`, `data/storage/` | one declarative schema -> DuckDB DDL + typed bulk ingestion; `Store` with idempotent writes, run provenance, content fingerprint, volume reconciliation |
| `backtest/` | event-driven replay engine: information-time feeds, whitelisted `MarketInfo`, latency, taker + queue-model maker fills, exact ledger, metrics; validated by a look-ahead theorem, mutation checks and a Stage 4 <-> 5 exact-equality test |
| `pricing/calibration.py` | Stage 8: reliability bins, calibration regression, ECE, Murphy decomposition, Platt/isotonic recalibrators, event-cluster bootstrap, SVG reliability diagrams |
| `pricing/` | Stage 7: point-in-time microstructure features, three fair-probability models (microstructure, historical frequencies, partition renormalisation), scoring rules, event-grouped nested evaluation, a sealed holdout for Stage 8 |
| `research/` | Stage 6: opportunity-episode scanner, event-clustered bootstrap, interval-censored lifetimes, latency brackets, category study, hypothesis scoring |
| `arbitrage/` | fee-exact execution model (book walking, VWAP, per-fill fees, profit-maximising size), classified `Opportunity` records, detector with the displayed -> liquid -> fees -> slippage -> executable funnel |
| `data/collectors/` | deterministic universe selection, resumable history collector (markets/events/trades), complete-event collector, REST book poller, WebSocket recorder, CLI |
| `tests/` | unit / integration / property-based tests; real captured fixtures plus an in-memory fake exchange that reproduces the live API's quirks |

## Collecting data (Stage 2)

```bash
# 1. settled-market history for calibration (resumable; safe to Ctrl-C and re-run)
.venv/bin/python -m data.collectors.run history --config configs/history.yaml
.venv/bin/python -m data.collectors.run verify              # trade completeness vs exchange volume
.venv/bin/python -m data.collectors.run status              # counts, coverage, fingerprint

# 2. forward order-book recording - Kalshi serves NO historical books, so leave this running
.venv/bin/python -m data.collectors.run books --config configs/books.yaml
```

The history and book jobs write to **separate DuckDB files** (`var/kalshi.duckdb`, `var/books.duckdb`)
because DuckDB allows a single writer. Later stages `ATTACH` both.

First dataset (2026-09-19): 3,000 settled markets, 1.91M trades, every market reconciled to the
exchange's reported volume. See [`docs/data_architecture.md`](docs/data_architecture.md#first-collected-dataset-2026-09-19).

## Contract relationships (Stage 3)

```bash
.venv/bin/python -m data.collectors.run events --config configs/events.yaml      # complete settled events
.venv/bin/python -m scripts.stage3_relationship_study study                       # replay relations vs real outcomes
```

Headline: every structurally inferred relation (7.5k chains, 596 exclusivity claims, 546 bucket
partitions, 10.9k ladder-equals-sum-of-buckets unions) survived 14,383 real settled events with **0
violations**, and the evidence levels are calibrated by outcome (UNVERIFIED 4.6% violated, EMPIRICAL 0.11%,
DECLARED/structural 0%). Same-market YES/NO arbitrage cannot exist in a bids-only book - see
[`docs/relationships.md`](docs/relationships.md).

## Same-event arbitrage (Stage 4)

```bash
.venv/bin/python -m scripts.stage4_arbitrage_demo worked      # book -> constraint -> fees -> net, step by step
.venv/bin/python -m scripts.stage4_arbitrage_demo replay --books-db var/books_structural.duckdb
```

Headline (535 cycles, 44 min, 100 structured events): **327 displayed violations, 0 survive fees**;
fee-free, 54 would be executable. 325 of the 327 are one artefact - a 1c minimum-tick overround on illiquid
tails of an approval-rating event. Details, the fee model and the bugs found: [`docs/arbitrage.md`](docs/arbitrage.md).

## Backtesting (Stage 5)

```bash
.venv/bin/python -m data.collectors.run refresh --config configs/refresh.yaml   # settlements + trades for recorded books
.venv/bin/python -m scripts.stage5_backtest_demo --db var/books_shortlived.duckdb
```

Headline: decisions before random cuts of the real feed are identical to the untruncated run (look-ahead
audit, thousands of orders per cut); a passive strategy's P&L ranges over **$74** across queue-position
assumptions and is unprofitable even under the most generous one. A run that first showed a +$983 phantom
arbitrage exposed an engine flaw (simultaneous events applied one at a time) - see
[`docs/backtesting.md`](docs/backtesting.md).

## Arbitrage research (Stage 6)

```bash
python -m scripts.stage6_arbitrage_research --role confirmatory \
    --db var/books_research_wide.duckdb --db var/books_research_short.duckdb \
    --out results/stage6/confirm --engine-sweep --expect-fingerprint 663d74308d808b79
```

Experiments A-D (frequency, executability, edge decay, latency) plus a category study, with 16 hypotheses and
the analysis code frozen *before* fresh confirmatory recordings were analysed once. On that data (~211 minutes,
24 standing violations in 13 events): standing violations of declared/proven relations occur at
**12.9 [5.9, 22.5] per 1,000 relation-hours**, last about **10 s**, and **1 of 24 is executable at 100
contracts with standard fees (total profit $0.14)** - fees and size, not the structure, remove the edge
(all 133 settled bundles paid at least what they promised). **11 of 15 scored hypotheses held; 4 failed**
(reported as failures). 1-250 ms latencies cannot be resolved from snapshots and are reported as brackets.
See [`docs/arbitrage_research.md`](docs/arbitrage_research.md).

## Fair-probability models (Stage 7)

```bash
python -m scripts.stage7_probability_models --out results/stage7
```

Three models of `P(X = 1 | information at t)`: **A** a microstructure correction to the market price (flow,
short-term moves, imbalance, microprice, time to expiry; ridge-shrunk to the market), **B** an independent
hierarchical historical-frequency base rate that never sees a price, **C** renormalisation of mutually exclusive
and exhaustive outcomes. On the research period (3,430 instances, 1,478 events) **none beats the market
reference** (log loss 0.352; constant base rate 0.681): A +0.0008 [-0.0003, 0.0019], recalibrated B no better than
the base rate, C nothing to fix (prices already sum to 1.003). The market price is also close to calibrated. The
calibration study proper is Stage 8, on a holdout this stage never opens (enforced in code). See
[`docs/probability_models.md`](docs/probability_models.md).

## Calibration (Stage 8)

```bash
python -m scripts.stage8_calibration --role confirmatory --out results/stage8/confirm \
    --expect-fingerprint 9c7d30b39a17ea63
```

Are the market's prices calibrated probabilities? Reliability diagrams, calibration slope/intercept, ECE and the
Murphy decomposition, broken down by category, probability range, time to resolution, liquidity and market size, with
event-clustered intervals. Hypotheses were frozen on the research period; the sealed holdout was opened once. On
1,776 holdout instances (731 events): **the market price is well calibrated** (slope 0.97 [0.86, 1.11], average gap
-1.2 points, miscalibration 0.4% of skill; 0 of 23 subgroup gaps significant) and **recalibrating it does not help**;
order-book features add nothing and the **microprice is significantly worse than the mid** (+0.016 nats). All 17
pre-specified criteria held, and a tape nondeterminism found on the way was shown not to change any verdict. See
[`docs/calibration.md`](docs/calibration.md).

## Market making (Stage 9)

```bash
python -m scripts.stage9_baseline_mm --role confirmatory --out results/stage9/confirm \
    --expect-fingerprint e036156024e9174a
```

A baseline market maker for binary contracts, **paper only** (simulated orders against recorded books and trades).
Avellaneda-Stoikov cannot be used as derived (unbounded price, constant volatility, a `(T-t)` risk clock, no settlement
jump, no YES/NO symmetry), so quoting uses the *bounded-support* form: the CARA certainty equivalent of a Bernoulli
claim gives reservation prices that stay inside (0, $1) by construction, skew against inventory, and carry the claim's
own remaining variance `p(1-p)`. Inventory risk is in settlement terms (worst / best / expected value, event-level
worst case over the admissible outcomes, position / event / portfolio limits, a drawdown kill-switch). On the
frozen confirmatory data (2,410 fills, 81 events, ~12 h): **net -$426**, 30 s markout **-1.42¢ [-1.85, -1.04]**,
hold-to-settlement P&L **-$0.18 per fill [-0.23, -0.13]**, negative under all 10 fill-model x latency cells and all
9 (gamma, k) settings. Skew cut mean inventory 9x (52 -> 6 contracts) without making money; the kill-switch capped the
drawdown at $300 (vs $600) without creating edge. All 8 pre-registered hypotheses held (one, "adverse selection
worsens near resolution", holds only by a 0.03¢ gap and is a null in substance). Caveat: the maker quotes around a
polled, ~3 s stale mid while fills come from timely trades, so this measures that information set, not a live-feed
system. See [`docs/market_making.md`](docs/market_making.md).
