# Data architecture (Stage 2)

Everything downstream (backtests, calibration, arbitrage analysis) reads **only** from the local
DuckDB file. No experiment calls the API.

```
Kalshi REST ─┐                                    ┌─ collection_runs   (provenance, coverage)
             ├─► collectors ─► normalisation ─►  ├─ events / markets  (upserted metadata)
Kalshi WS  ──┘   (resumable)   (strict, fail-loud)├─ market_status_log (state changes, local time)
                                                  ├─ trades            (dedup on trade_id)
                                                  ├─ trade_sync        (resume checkpoints)
                                                  ├─ book_snapshots    (full-depth keyframes)
                                                  ├─ book_deltas       (WS events, as received)
                                                  ├─ stream_events / poll_log (liveness, incidents)
                                                  └─ quarantine        (invalid payloads, never dropped)
```

The schema is declared once in `data/schemas/ddl.py`; the same declaration produces the DDL and the
typed column map used for ingestion.

## Two facts that shape the whole design

1. **Kalshi serves no historical order books.** `GET /historical/markets/{t}/orderbook` returns 404;
   candlesticks hold only top-of-book OHLC. Order-book history exists *only* if we record it
   ourselves, forward in time. Trade and market history, by contrast, is complete and backfillable.
2. **Data is partitioned at `GET /historical/cutoff`, and the cutoff moves.** It read 2026-07-20 when
   first probed and 2026-07-21 a day later, so data migrates from `/markets*` to `/historical/*`
   over time. `/historical/*` holds what predates the cutoff, `/markets*` what follows, and the
   live listing keeps serving markets for a while *before* the cutoff (overlap near the boundary;
   nothing older than ~2 weeks). A market straddling the cutoff has trades in both, so trade
   routing is decided per market from its open/close times, and each run records the cutoff it saw
   in `collection_runs.note`. `markets.partition` records only which listing a row was fetched from.
   `/historical/markets` ignores close-time filters and is ordered by `created_time` descending
   (asserted at runtime; the scanner aborts if that ever changes).

## Resume and idempotency

| Mechanism | Guarantee |
|---|---|
| natural primary keys + `ON CONFLICT DO NOTHING` | re-ingesting overlapping data cannot duplicate a fact |
| metadata upsert preserving `first_seen_at` | re-observing a market is harmless |
| `trade_sync` checkpoint written **after** a partition is paginated to the end | a crash mid-market repeats that market; partial rows are skipped by PK |
| finished settled markets skipped without an API call | a re-run of a completed job makes **zero** trade requests |
| deterministic selection (`blake2b(seed:ticker)`, not API order) | a resumed run selects the same universe |
| runs stuck in `running` are marked `abandoned` at next start | a hard kill is visible, never silent |

Proven by tests (`tests/test_history.py`): an interrupted-then-resumed run produces a dataset with
the **identical content fingerprint** as an uninterrupted one, including the case where a crash
left partial trade rows on disk with no checkpoint.

## The scan is resumable too

Selecting 3,000 markets meant scanning **888,096** settled markets (~14 min), and a 25-second
network outage killed the first attempt at 745k. The scan is therefore checkpointed in
`scan_state` (page cursor + the current winners as `[key, ticker]` pairs, re-fetched by ticker on
resume) and a *finished* scan is reused for 24 h. Because the exchange **silently treats an invalid
cursor as page 1**, a resume verifies the first page against the recorded one and restarts cleanly if
they match. Retry patience for long jobs is 10 attempts / 30 s backoff cap (~2.5 min); the library
default (5 attempts, ~15 s) is too short for a 14-minute job.

## Integrity checks

* **Volume reconciliation** (`python -m data.collectors.run verify`). The exchange reports each
  market's lifetime volume; for every finalized, fully-synced market the stored trade sizes must add
  up to exactly that. Shortfall = missing trades, excess = duplicates. On the collected dataset
  **all 3,000 markets reconcile to the centi-contract**, including after two `kill -9`s and a network
  outage mid-collection.
* **`quarantine`** receives every payload that fails strict validation (off-grid price, contradictory
  fields, ...) with the error and the raw text. Bad data is neither stored as good data nor lost.
* **`Store.fingerprint()`** hashes dataset *content* (independent of ingest order/run ids) so an
  experiment can record exactly which data it saw.

## Time

Two clocks are never mixed. *Exchange time* (`created_time`, `exchange_ts`, `settlement_ts`) and
*local time* (`recv_ts_ns`, `observed_at`, `ingested_at`, `*_seen_at`). Timestamps are stored as
`TIMESTAMP` in UTC. A backtest must order by exchange time for trades but may only treat a book or a
market status as known from its **local observation** time onward — otherwise it leaks the future.

## Reading order-book history correctly

* **REST poller** stores a keyframe only when the book changed (or after `heartbeat_s`). A stored
  book therefore stays valid *until the next keyframe for that ticker*, **provided the poller was
  alive**. `poll_log` (one row per cycle) and `collection_runs` (start/end) are what establish
  "alive"; a gap in `poll_log` is a hole, not "unchanged".
* Each REST snapshot carries `req_ts_ns` and `recv_ts_ns`: the true state lies somewhere in that
  interval (measured: mean 361 ms, max 1.16 s; a 3-chunk cycle over 150 markets takes ~1.1 s), and books fetched in different 50-ticker chunks are **not
  mutually atomic**. Cross-market arbitrage measured on polled data is therefore only meaningful at
  a resolution coarser than that latency; sub-second claims need the WebSocket recorder.
* **WebSocket recorder** stores deltas exactly as received. Replay them through
  `BookStreamProcessor` (which fails closed on gaps) rather than trusting live judgement.

## Dataset selection and its biases (state these in every result)

The historical universe is defined by `configs/history.yaml`. Selection never reads outcomes, but it
is not neutral:

| Choice | Effect |
|---|---|
| `min_volume` filters on **lifetime** volume | selects markets that attracted trading; thinly-traded markets (a large share of all markets) are absent. Calibration conclusions apply to *traded* markets |
| historical partition scanned by `created_time` with a lookback | markets created long before their close (long-dated) are under-represented in that partition |
| `max_per_series` cap | equalises series, so aggregate results are **not** volume-weighted; report per-category and weight explicitly |
| settled markets only | right-censoring: long-dated markets that have not yet settled are excluded |

## Performance notes

Binding Python values into DuckDB one by one (`executemany`, `unnest(?)`) ran at **~2k rows/s** on
this machine (28 s for 50k rows). Ingestion therefore writes newline-delimited JSON and lets DuckDB
parse it natively: **~450k rows/s**, with a regression test guarding against reverting.

## Complete events (Stage 3)

The history dataset samples individual markets, so it cannot say whether an event's outcomes were
exclusive or exhaustive. `data/collectors/events.py` stores complete settled events (event + every
sibling market, `market_tickers` populated) in `var/relations.duckdb` - a *separate* file so the
history dataset's definition and fingerprint are untouched. `Store.read_events_with_markets()`
rebuilds domain objects (round-trip equality is tested). 14,383 settled events / 142,709 markets from
713 series (12 most recent per series; 60 for series whose exclusivity is only exchange-declared).

## Operational caveats

* DuckDB allows one writer process and **no concurrent reader**. While a collector runs, query a
  copy (`cp var/kalshi.duckdb var/snap.duckdb`) or stop it. The CLI reports the lock clearly.
* Not stored, deliberately: the WebSocket `ticker` channel (top-of-book is derivable from books) and
  candlesticks (trades give the price path; bid/ask history exists only where books were recorded).
* The WebSocket recorder is tested against synthetic events and the documented schemas but has not
  yet run against the live authenticated stream (no credentials were available).

## First collected dataset (2026-09-19)

`configs/history.yaml` -> `var/kalshi.duckdb`, content fingerprint `2257819296b9ca2a` (the fingerprint definition became schema- and bookkeeping-independent in Stage 4, so it differs from the value first reported).

| | |
|---|---|
| settled markets | **3,000** (1,749 no / 1,239 yes / **12 scalar**), 713 series (<= 20 each), closing 2026-09-01 .. 09-12 |
| trades | **1,913,528** (median 41 / market, p90 1,167, max 42,146); 900 came from the historical partition |
| events | 2,786, categories: Sports 1,981, Crypto 380, Commodities 203, Climate 165, Financials 86, Economics 85 |
| integrity | 3,000/3,000 markets reconcile to exchange volume; 0 quarantined; 0 trades outside a market's lifetime |
| scan | 888,096 markets scanned, 184,581 eligible, 3,000 selected |
| run history | 1 ok, 1 failed (network outage), 2 abandoned (deliberate `kill -9` resume tests) |

Intended split for Stage 12: **research** = markets closing before 2026-09-08, **out-of-sample** = 09-08..09-12
(fixed now, before any modelling, so it cannot be tuned against). Note that the last pre-close trade
price separates the outcomes almost perfectly (mean 0.98 for YES vs 0.04 for NO) - calibration must
therefore be measured at earlier horizons, not at close.

A 5-minute forward book sample (`var/books_smoke.duckdb`: 150 markets in 10 mutually-exclusive events,
60 cycles, 1,075 keyframes, 88% of polls unchanged and elided) validated the recorder. It is a
demonstration, not a dataset: arbitrage research needs the recorder running for days.
