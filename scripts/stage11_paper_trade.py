"""Stage 11: paper trading on live data.  NO ORDER IS EVER SENT: orders go to a simulated exchange.

    # public REST polling (no credentials needed; books ~3 s stale, like the recordings)
    python -m scripts.stage11_paper_trade --source rest --strategy baseline --duration 600 \\
        --out results/stage11/rest_baseline

    # the WebSocket feed (needs a READ-ONLY API key in the environment / a gitignored .env)
    python -m scripts.stage11_paper_trade --source ws --strategy adaptive --duration 3600 \\
        --out results/stage11/ws_adaptive

Strategies: ``baseline`` (Stage 9), ``adaptive`` (Stage 10, coefficients read from
results/stage10/dev/adaptive.json, never refitted), ``arbitrage`` (Stage 4 detector, IOC orders).

Outputs in ``--out``: ``tape.jsonl`` (every event the strategy was shown), ``decisions.jsonl``
(orders, cancels, fills, risk events), ``universe.json`` and ``summary.json``, whose
``parity_with_backtest_of_the_tape`` says whether the plain backtest engine, run over the tape,
reproduces the session's orders and fills exactly.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from backtest.engine import BacktestConfig
from backtest.fills import QueueModel, TakerModel
from data.collectors.config import BookSelection
from kalshi_client.config import KalshiConfig
from kalshi_client.exceptions import AuthenticationError, ConfigurationError
from kalshi_client.rest import KalshiRestClient
from kalshi_client.websocket import KalshiWebSocket
from paper.adapter import WsAdapter
from paper.sources import RestPollSource, WsSource
from paper.strategies import make_factory
from paper.trader import PaperTrader
from paper.universe import load_universe

SEC = 1_000_000_000
BANNER = "PAPER TRADING: no order is ever sent to any exchange."


def config(latency_ms: float, fees, staleness_s: float) -> BacktestConfig:
    ns = int(latency_ms * 1_000_000)
    return BacktestConfig(
        initial_cash_micro=10_000 * 1_000_000,
        latency_submit_ns=ns,
        latency_ack_ns=ns,
        latency_cancel_ns=ns,
        taker=TakerModel(max_staleness_ns=int(staleness_s * SEC)),
        maker=QueueModel.queue(1.0, 0.5),
        fees=fees,
        equity_sample_ns=10 * SEC,
    )


async def main_async(a: argparse.Namespace) -> int:
    print(BANNER)
    sel = BookSelection(
        top_events=a.top_events,
        max_tickers=a.max_tickers,
        min_event_markets=a.min_event_markets,
        max_hours_to_expiry=a.max_hours,
    )
    kcfg = KalshiConfig.from_env()
    if a.source == "ws" and kcfg.auth is None:
        print(
            "The WebSocket needs an API key (even for public channels).  Set KALSHI_API_KEY_ID and\n"
            "KALSHI_PRIVATE_KEY_PATH in the environment or a gitignored .env (see .env.example).\n"
            "A read-only key is enough; nothing here places orders.  Use --source rest to run today.",
            file=sys.stderr,
        )
        return 2
    out = Path(a.out)
    async with KalshiRestClient(kcfg) as rest:
        print("loading the universe (one pass over the open markets, ~1 min)...", flush=True)
        u = await load_universe(rest, sel)
        if not u.tickers:
            print("no markets matched the selection", file=sys.stderr)
            return 1
        out.mkdir(parents=True, exist_ok=True)
        (out / "universe.json").write_text(
            json.dumps(
                {
                    "tickers": u.tickers,
                    "event_of": u.event_of,
                    "selection": vars(sel),
                    "source": a.source,
                    "strategy": a.strategy,
                },
                indent=1,
            )
        )
        print(
            f"{len(u.tickers)} markets in {len(u.events)} events; strategy {a.strategy}", flush=True
        )
        cfg = config(a.latency_ms, u.fees, 5.0 if a.source == "ws" else 30.0)
        trader = PaperTrader(u.infos, make_factory(a.strategy, u), cfg, out)
        if a.source == "rest":
            source = RestPollSource(rest, u.tickers, interval_s=a.interval)
        else:
            ws = KalshiWebSocket(
                kcfg,
                channels=["orderbook_delta", "trade", "market_lifecycle_v2"],
                market_tickers=u.tickers,
                stale_timeout=20.0,
            )
            source = WsSource(ws, WsAdapter(u.tickers))
        summary = await trader.run(source, duration_s=a.duration)
    keep = {
        k: summary[k]
        for k in (
            "mode", "wall_seconds", "markets", "orders", "fills", "net_pnl_usd", "fees_usd",
            "reaction_ms", "n_incidents", "kill_switch", "parity_with_backtest_of_the_tape",
        )
    }  # fmt: skip
    print(json.dumps(keep, indent=1, default=str))
    return 0 if summary["parity_with_backtest_of_the_tape"]["identical"] else 3


def main() -> None:
    ap = argparse.ArgumentParser(description=BANNER)
    ap.add_argument("--source", choices=["rest", "ws"], default="rest")
    ap.add_argument("--strategy", choices=["baseline", "adaptive", "arbitrage"], default="baseline")
    ap.add_argument("--duration", type=float, default=600.0, help="seconds of live data")
    ap.add_argument("--out", required=True)
    ap.add_argument("--top-events", type=int, default=10)
    ap.add_argument("--max-tickers", type=int, default=60)
    ap.add_argument("--min-event-markets", type=int, default=1)
    ap.add_argument("--max-hours", type=float, default=3.0)
    ap.add_argument("--latency-ms", type=float, default=100.0)
    ap.add_argument("--interval", type=float, default=3.0, help="REST poll interval (s)")
    args = ap.parse_args()
    try:
        raise SystemExit(asyncio.run(main_async(args)))
    except (AuthenticationError, ConfigurationError) as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
