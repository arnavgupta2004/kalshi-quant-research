# kalshi-quant-research

A research framework for studying pricing efficiency, no-arbitrage structure, probability
calibration, execution and market making in **Kalshi binary prediction markets**.

> **Research and paper trading only.** The client is read-only by construction: the only HTTP verb
> implemented is `GET`, the WebSocket client only sends `subscribe`, order-entry channels are
> rejected, and there is no order code anywhere. Credentials live in a gitignored `.env`.

Status: **Stage 3 of 12 complete** (API client, normalised data model, historical storage, a
resumable collector, and validated contract-relationship logic). See [`docs/contract_semantics.md`](docs/contract_semantics.md) for price/contract
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
| `market/` | pure domain core, no I/O: exact units, UTC time, `OrderBook`, contracts (`Market`, `Event`, `ContractRef`, `Trade`), **`relationships`** (relations -> buy-only no-arbitrage constraints), **`semantics`** (which relations really hold, with evidence levels), `evidence` (settled-history support with exact confidence bounds) |
| `kalshi_client/` | adapter: RSA-PSS auth, async REST (rate limit, retry, pagination), resilient WebSocket, strict wire models |
| `data/normalization/` | wire -> domain mapping; sequence-checked book maintenance (`fail closed`), duplicate suppression |
| `data/schemas/`, `data/storage/` | one declarative schema -> DuckDB DDL + typed bulk ingestion; `Store` with idempotent writes, run provenance, content fingerprint, volume reconciliation |
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

Headline: every structurally inferred relation (7.5k chains, 596 exclusivity claims, 513 bucket
partitions, 10.9k ladder-equals-sum-of-buckets unions) survived 14,383 real settled events with **0
violations**, and the evidence levels are calibrated by outcome (UNVERIFIED 4.4% violated, EMPIRICAL 0.11%,
DECLARED/structural 0%). Same-market YES/NO arbitrage cannot exist in a bids-only book - see
[`docs/relationships.md`](docs/relationships.md).
