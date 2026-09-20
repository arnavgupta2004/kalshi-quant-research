# Stage 11 — Paper trading on live data

**No order is ever sent.** A strategy's orders go to `Backtest.submit`, a method that records an
order in a simulated book. There is no order-entry code anywhere in `kalshi_client` (its only HTTP verb
is `GET`; its WebSocket only subscribes to public channels) or in `paper/`, and
`tests/test_paper.py` scans both packages for order endpoints and write verbs to keep it that way.

## 1. The idea: one engine, two feeds

The Stage 5 engine already *is* a simulated exchange. It applies market events in time order, takes an
order after a modelled latency, fills it against the book and the tape with the queue and taker models
a backtest uses, and keeps an exact ledger. Paper trading therefore needs no new exchange, only a feed
that arrives in real time:

```
 WebSocket ─┐                          ┌─ orders, cancels ──▶ decisions.jsonl
 REST poll ─┼─▶ adapter ─▶ LiveFeed ─▶ │  Stage 5 engine  ─┬─ fills, P&L, risk ─▶ summary.json
 tape      ─┘   (events)   (thread)    └─ strategy (Stage 9 / 10 / 4, unchanged)
                               └────────▶ tape.jsonl  (every event the strategy was shown)
```

`LiveFeed` is a blocking iterator the engine pulls from in its own thread, fed by an asyncio task that
pumps a source. The strategy code is *the same objects* the backtests ran; nothing was forked or
patched, and the frozen fingerprints of Stages 9 and 10 still reproduce.

## 2. Same outputs as a backtest

The engine's clock is the events' own timestamps (local receive time), and a strategy sees nothing but
events. So a session's tape — every event handed to the engine, in order — determines everything the
strategy did. At the end of **every** session the trader replays the tape through a *fresh* strategy
instance and the plain backtest engine and compares the orders, fills, final equity and settlements.
They must be identical; the result is `summary.json → parity_with_backtest_of_the_tape`. A mismatch
would be a bug, and the CLI exits non-zero on it.

Time only advances when something arrives, so when nothing has for 100 ms the feed yields a **tick**
(a `Wake` with tag `clock`, which carries no market data and never reaches the strategy). Ticks let
scheduled work — an order reaching the simulated exchange, a fill notice, a strategy wake-up — happen on
time instead of waiting for the next market event. They are recorded in the tape, which is why replay
is exact.

## 3. Freshness: what the strategy is told, and when

A backtest gets "this book is still current" from the recorder's poll log. Live has to supply it:

| feed event | source | meaning |
|---|---|---|
| `BookUpdate` | every applied snapshot/delta (WebSocket), every changed book (REST) | the full book of one market |
| `TradeTick` | every public print, stamped with local receive time | information time, no artificial delay |
| `BookConfirm` | every 1 s per trustworthy market while the connection is healthy (WebSocket); every poll cycle (REST) | a quiet market's book is still current |
| `MarketClose`, `Settlement` | the lifecycle channel | trading stopped; the outcome |

**Fail closed.** The Stage 2 `BookStreamProcessor` enforces per-subscription sequence numbers. A gap, an
inconsistent book or an unknown market poisons the affected books, they stop being confirmed, and the
adapter asks the WebSocket client to resync (reconnect, hence a fresh snapshot). *Health* means
connected and heard from within 15 s: when it fails, confirmations stop everywhere, the books age, and
the strategy's own age limit (5 s live, against 30 s in the polled backtests) takes its quotes off the
market. An outage therefore ends in "no quotes", never in quotes on a book nobody has vouched for.
This is tested end to end over a real socket: sequence gap → resync → reconnect → fresh snapshot, with
parity against the backtest of the resulting tape.

## 4. Sources

| source | credentials | books | notes |
|---|---|---|---|
| `WsSource` | **an API key is required** (Kalshi serves no WebSocket channel without one) | live deltas | read-only, public channels only; auto-reconnect with backoff |
| `RestPollSource` | none | polled every 3 s | a live source that works today; as stale as the recordings, so it is the like-for-like live analogue of a backtest |
| `TapeSource` | none | recorded | replay |

The WebSocket path could **not** be run live in this stage: there is no key in this environment, and
creating or entering credentials is not something this project does on anyone's behalf. It is tested
against an in-process mock exchange that speaks Kalshi's frames (gaps, reconnects, malformed frames,
lifecycle events). Two assumptions could not be verified without a key: the lifecycle `event_type`
strings (`closed`) and that `settlement_value` arrives as a dollar string. Anything unrecognised is
counted and ignored, never guessed at. To run it: put `KALSHI_API_KEY_ID` and `KALSHI_PRIVATE_KEY_PATH`
in the environment or a gitignored `.env` (see `.env.example`; a read-only key is enough) and use
`--source ws`.

## 5. Strategies

`--strategy baseline` (Stage 9), `adaptive` (Stage 10's FULL variant; the coefficients are read from
`results/stage10/dev/adaptive.json`, never refitted; a test pins them to the file), `arbitrage` (the
Stage 4 detector with IOC orders, relations built from the live events with `build_specs`, live fee
schedules from the series endpoint). Risk limits and the drawdown kill-switch are the strategies' own;
a kill is logged in `decisions.jsonl` and tested live.

## 6. What a session writes

| file | content |
|---|---|
| `tape.jsonl` | every event the engine was given (books, confirms, trades, closes, settlements, ticks) |
| `decisions.jsonl` | every order (with the mid, position and equity at the time), cancel, fill and risk event |
| `universe.json` | the markets, their events, the selection rule, the source and strategy |
| `summary.json` | orders, fills, P&L, fees, positions, reaction time, incidents, and the parity check |

`reaction_ms` is the wall-clock time from an event's receipt to the strategy seeing it: queueing plus
at most one 100 ms tick (the engine peeks one event ahead to batch simultaneous ones).

## 7. Results

Live sessions on public REST data, 2026-09-20 ~15:40 IST (Sunday afternoon in India, early morning in
the US: few live sports). `--top-events 15 --max-tickers 100 --max-hours 6`: 100 markets in 11 events,
76 of them one golf-tour event, the rest football, tennis and boxing. Each session polled books every
3 s and public trades every 3 s.

| session | wall | orders | fills | cancel rate | reaction p50 / p99 | incidents | parity with the backtest of its tape |
|---|---|---|---|---|---|---|---|
| baseline (Stage 9) | 900 s | 772 | 119 (all maker) | 80% | 19 ms / 112 ms | 0 | **identical** |
| adaptive (Stage 10) | 900 s | 1,036 | 84 (all maker) | 89% | 25 ms / 115 ms | 0 | **identical** |
| arbitrage (Stage 4) | 480 s | 0 (0 opportunities detected) | 0 | — | 22 ms / 106 ms | 0 | **identical** |

* **Parity holds on real live data**, not just on scripted feeds: for each session the plain backtest
  engine, run over the session's tape with a fresh strategy, reproduced every order, fill and the final
  equity.
* **Arbitrage**: the detector, run on the live books of 11 events (relations from `build_specs`, live
  fee schedules), saw no standing opportunity in 8 minutes, consistent with Stage 6's finding that
  standing arbitrage is rare and consumed by fees. It sent no orders, so it demonstrates the wiring
  and the parity check, not an arbitrage result.
* **Reaction time** is dominated by the 100 ms clock tick (the engine peeks one event ahead), so p99
  sits just above 100 ms and p50 well below it. The strategies are not the bottleneck.
* **Quoting behaviour** matches the backtests': 20–22 of the 100 markets were ever quoted, all of them
  football and tennis matches (none of the 76 golf-tour markets was), cancels are ~80–90% of orders,
  every fill is a maker fill.
* **P&L is not reported as a result.** Fifteen minutes on 20 markets is noise (the baseline lost $17.5
  and the adaptive maker $1.5 marked at liquidation values; the two sessions saw different poll phases,
  and different fills, so the difference is not a comparison). The point of a live session is that the
  system behaves, not that it earns; the research questions belong to the backtests and to Stage 12's
  out-of-sample evaluation.

## 8. Limitations

1. **The WebSocket was not run live** (no API key here). Its path is tested against an in-process
   exchange, including sequence gaps, resync-by-reconnect and malformed frames, but two lifecycle
   assumptions (§4) are unverified against the real feed.
2. **REST-polled books are as stale as the recordings.** The live sessions therefore share the
   limitation of Stages 9 and 10 (a maker quoting around a ~3 s-old mid); they demonstrate the
   machinery, not a live-feed maker. A WebSocket session removes that handicap and is the run to make
   once a key exists.
3. **Fills are simulated**, by the same queue and taker models as the backtests, from the public tape
   and books. A paper session cannot know a real queue position, and a real order changes the book it
   trades against; neither is modelled.
4. **Universe selection is a snapshot.** Markets are chosen once at the start (the recorder's rule); a
   market that opens later is not traded. The partition evidence for event-level risk limits is not
   loaded live, so the event limit uses the conservative sum of per-market worst cases.
5. **Settlements are not observed on the REST source** (a 15-minute session rarely sees one); open
   positions are marked at liquidation values. The WebSocket source consumes lifecycle frames.
6. **Ticks make the engine peek one event ahead**, adding up to one tick (100 ms) to the strategy's
   reaction, recorded per event as `reaction_ms`.

## 9. Reproducing

```bash
python -m scripts.stage11_paper_trade --source rest --strategy baseline --duration 900 \
    --top-events 15 --max-tickers 100 --max-hours 6 --out results/stage11/rest_baseline
# needs a read-only API key in the environment / a gitignored .env:
python -m scripts.stage11_paper_trade --source ws --strategy adaptive --duration 3600 \
    --out results/stage11/ws_adaptive
```

Tests: `tests/test_paper.py` (27): the tape round trip, the live feed's clock, the adapter's health and
fail-closed rules, a live session with parity against the backtest of its own tape, the kill-switch
live, an error in the strategy surfacing, a real WebSocket client against a mock exchange through a
gap and a reconnect, the REST source, the frozen Stage 10 coefficients, universe selection, and the
static scan for order-entry code. The paper code was mutation-tested (32 mutants; 5 real test gaps found and
closed — among them a resync test in which the mock server, not the resync, caused the reconnect — and
1 equivalent survivor: the duration check, since a negative timeout also ends the loop).
