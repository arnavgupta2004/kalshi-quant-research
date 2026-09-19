# kalshi-quant-research

A research framework for studying pricing efficiency, no-arbitrage structure, probability
calibration, execution and market making in **Kalshi binary prediction markets**.

> **Research and paper trading only.** The client is read-only by construction: the only HTTP verb
> implemented is `GET`, the WebSocket client only sends `subscribe`, order-entry channels are
> rejected, and there is no order code anywhere. Credentials live in a gitignored `.env`.

Status: **Stage 1 of 12 complete** (API client + normalised data model). See
[`docs/contract_semantics.md`](docs/contract_semantics.md) for the price/contract semantics and the
verified API behaviour so far. The full technical report is written at Stage 12.

## Quick start

```bash
uv venv --python 3.12 && uv pip install -e ".[dev]"
cp .env.example .env            # optional for REST; required for the WebSocket
.venv/bin/python -m pytest      # 113 tests, no network or credentials needed
.venv/bin/python -m scripts.stage1_demo --markets 5 --seconds 30   # live data -> local state
```

## Layout (Stage 1)

| Package | Role |
|---|---|
| `market/` | pure domain core, no I/O: exact units, UTC time, `OrderBook`, contracts (`Market`, `Event`, `ContractRef`, `Trade`) |
| `kalshi_client/` | adapter: RSA-PSS auth, async REST (rate limit, retry, pagination), resilient WebSocket, strict wire models |
| `data/normalization/` | wire -> domain mapping; sequence-checked book maintenance (`fail closed`), duplicate suppression |
| `tests/` | 113 unit / integration / property-based tests (fixtures are real captured payloads) |
