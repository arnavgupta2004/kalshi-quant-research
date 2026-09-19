"""Performance, risk and execution metrics over a ``BacktestResult``.

Everything here is *post-hoc analysis*: it may look at the whole run (and, for markouts, at prices
after a fill).  That is legitimate - it never feeds back into a strategy's decisions.

Annualisation caveat: Sharpe/Sortino are computed on an equity curve resampled to a fixed grid and
scaled by sqrt(periods per year).  Backtests over minutes or hours cover a tiny slice of one market
regime, so annualised ratios are reported only with enough observations and should be read as a
*scale-free description of the sample*, never as a forecast.
"""

from __future__ import annotations

import bisect
import math
import random
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass

from backtest.engine import BacktestResult
from backtest.events import BookUpdate, FeedEvent
from market.contracts import Side

SECONDS_PER_YEAR = 365.25 * 24 * 3600
MICRO = 1_000_000


def resample_equity(curve: Sequence[tuple[int, int, int]], step_ns: int) -> list[tuple[int, int]]:
    """Equity on a fixed time grid (last observation carried forward)."""
    if not curve:
        return []
    t0, t1 = curve[0][0], curve[-1][0]
    times = [c[0] for c in curve]
    out, t = [], t0
    while t <= t1:
        i = bisect.bisect_right(times, t) - 1
        out.append((t, curve[i][1]))
        t += step_ns
    if out[-1][0] < t1:
        out.append((t1, curve[-1][1]))
    return out


def period_returns(grid: Sequence[tuple[int, int]], base: int) -> list[float]:
    """Per-period P&L as a fraction of the starting capital ``base`` (µ$)."""
    return [(b[1] - a[1]) / base for a, b in zip(grid, grid[1:], strict=False)]


def sharpe(returns: Sequence[float], periods_per_year: float, min_obs: int = 30) -> float | None:
    if len(returns) < min_obs:
        return None
    mean = sum(returns) / len(returns)
    var = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
    if var <= 0:
        return None
    return mean / math.sqrt(var) * math.sqrt(periods_per_year)


def sortino(returns: Sequence[float], periods_per_year: float, min_obs: int = 30) -> float | None:
    if len(returns) < min_obs:
        return None
    mean = sum(returns) / len(returns)
    downside = math.sqrt(sum(min(0.0, r) ** 2 for r in returns) / len(returns))
    if downside <= 0:
        return None
    return mean / downside * math.sqrt(periods_per_year)


@dataclass(frozen=True)
class Drawdown:
    max_drawdown_micro: int
    max_drawdown_pct_of_peak: float
    peak_ts: int
    trough_ts: int


def max_drawdown(curve: Sequence[tuple[int, int]]) -> Drawdown:
    peak, peak_ts = None, 0
    best = Drawdown(0, 0.0, 0, 0)
    for ts, eq in curve:
        if peak is None or eq > peak:
            peak, peak_ts = eq, ts
        dd = peak - eq
        if dd > best.max_drawdown_micro:
            best = Drawdown(dd, dd / peak if peak > 0 else 0.0, peak_ts, ts)
    return best


def bootstrap_ci(
    values: Sequence[float],
    stat: Callable[[Sequence[float]], float] = lambda v: sum(v) / len(v),
    *,
    n_boot: int = 2000,
    block: int = 1,
    alpha: float = 0.05,
    seed: int = 0,
) -> tuple[float, float]:
    """Percentile bootstrap CI (moving blocks of ``block``; block>1 respects autocorrelation)."""
    n = len(values)
    if n == 0:
        raise ValueError("no data")
    rng = random.Random(seed)
    stats = []
    for _ in range(n_boot):
        sample: list[float] = []
        while len(sample) < n:
            start = rng.randrange(0, max(1, n - block + 1))
            sample.extend(values[start : start + block])
        stats.append(stat(sample[:n]))
    stats.sort()
    return stats[int(alpha / 2 * n_boot)], stats[min(n_boot - 1, int((1 - alpha / 2) * n_boot))]


# ---------------------------------------------------------------- P&L and execution
def pnl_breakdown(res: BacktestResult) -> dict:
    """Net P&L = realised + unrealised; ``gross`` adds back fees; per-market/liquidity views."""
    fees = res.portfolio.fees
    net = res.pnl_micro
    unrealised = net - res.portfolio.realised + fees  # equity - cash-based realised, before fees
    by_ticker: dict[str, int] = defaultdict(int)
    for _, t, _, pnl in res.settlements:
        by_ticker[t] += pnl
    fee_by_liq: dict[str, int] = defaultdict(int)
    vol_by_liq: dict[str, int] = defaultdict(int)
    for f in res.fills:
        fee_by_liq[f.liquidity] += f.fee_micro
        vol_by_liq[f.liquidity] += f.qty
    return {
        "net_micro": net,
        "gross_micro": net + fees,
        "fees_micro": fees,
        "realised_micro": res.portfolio.realised,
        "unrealised_micro": unrealised,
        "settled_pnl_by_ticker": dict(by_ticker),
        "fees_by_liquidity": dict(fee_by_liq),
        "volume_centi_by_liquidity": dict(vol_by_liq),
        "return_on_initial_cash": net / res.initial_cash_micro if res.initial_cash_micro else None,
    }


def execution_stats(res: BacktestResult) -> dict:
    orders = res.orders
    n = len(orders)
    status = Counter(o.status for o in orders)
    ordered = sum(o.qty for o in orders if o.status != "rejected")
    filled = sum(o.filled for o in orders)
    maker = [f for f in res.fills if f.liquidity == "maker"]
    submit_to_fill = []
    first_fill: dict[int, int] = {}
    for f in res.fills:
        first_fill.setdefault(f.order_id, f.exec_ts)
    for o in orders:
        if o.order_id in first_fill:
            submit_to_fill.append(first_fill[o.order_id] - o.submitted_ts)
    return {
        "orders": n,
        "status": dict(status),
        "order_fill_rate": (sum(1 for o in orders if o.filled > 0) / n) if n else None,
        "quantity_fill_rate": filled / ordered if ordered else None,
        "rejection_rate": status["rejected"] / n if n else None,
        "cancellation_rate": status["cancelled"] / n if n else None,
        "maker_fills": len(maker),
        "taker_fills": len(res.fills) - len(maker),
        "median_seconds_to_first_fill": (sorted(submit_to_fill)[len(submit_to_fill) // 2] / 1e9)
        if submit_to_fill
        else None,
        "turnover_contracts": sum(f.qty for f in res.fills) / 100,
    }


def mid_series(events: Iterable[FeedEvent]) -> dict[str, tuple[list[int], list[float]]]:
    """Per-market (times, YES mid in ticks) from the book snapshots of a feed."""
    out: dict[str, tuple[list[int], list[float]]] = defaultdict(lambda: ([], []))
    for e in events:
        if isinstance(e, BookUpdate) and e.yes_bids and e.no_bids:
            yb, ya = e.yes_bids[0][0], 10_000 - e.no_bids[0][0]
            out[e.ticker][0].append(e.ts_ns)
            out[e.ticker][1].append((yb + ya) / 2)
    return out


def markouts(
    res: BacktestResult, mids: dict[str, tuple[list[int], list[float]]], horizons_s: Sequence[float]
) -> dict[float, dict]:
    """Adverse selection: how the market moved against each fill *after* it happened.

    Positive markout = the fill was good (price moved our way).  For a YES buy at p, value is
    ``mid_yes(t+h) - p``; for a NO buy at q it is ``q_mid_no(t+h)``-based: ``(1 - mid_yes) - q``.
    Maker fills with persistently negative markout are being adversely selected."""
    out: dict[float, dict] = {}
    for h in horizons_s:
        vals, per_liq = [], defaultdict(list)
        for f in res.fills:
            times, series = mids.get(f.ticker, ([], []))
            if not times:
                continue
            i = bisect.bisect_right(times, f.exec_ts + int(h * 1e9)) - 1
            if i < 0 or times[i] <= f.exec_ts:
                continue  # no observation after the fill at that horizon
            mid_yes = series[i]
            m = (mid_yes - f.price) if f.side is Side.YES else ((10_000 - mid_yes) - f.price)
            vals.append(m)
            per_liq[f.liquidity].append(m)
        out[h] = {
            "n": len(vals),
            "mean_ticks": sum(vals) / len(vals) if vals else None,
            "by_liquidity": {k: sum(v) / len(v) for k, v in per_liq.items()},
        }
    return out


def summarize(res: BacktestResult, *, grid_step_ns: int = 60 * 10**9) -> dict:
    grid = resample_equity(res.equity, grid_step_ns)
    rets = period_returns(grid, res.initial_cash_micro)
    per_year = SECONDS_PER_YEAR / (grid_step_ns / 1e9)
    dd = max_drawdown(grid)
    return {
        "pnl": pnl_breakdown(res),
        "execution": execution_stats(res),
        "risk": {
            "periods": len(rets),
            "period_seconds": grid_step_ns / 1e9,
            "sharpe_annualised": sharpe(rets, per_year),
            "sortino_annualised": sortino(rets, per_year),
            "max_drawdown_micro": dd.max_drawdown_micro,
            "max_drawdown_pct_of_peak": dd.max_drawdown_pct_of_peak,
            "worst_settlement_micro": min((s[3] for s in res.settlements), default=None),
            "max_open_loss_micro": res.portfolio.max_loss(),
        },
        "events": res.events_processed,
        "metadata": res.metadata,
    }
