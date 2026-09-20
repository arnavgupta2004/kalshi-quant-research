"""Market-making evaluation (spec s.18): P&L, risk, execution and the required breakdowns.

Everything here is *post-hoc analysis of a finished backtest*: it may look at the whole run (and,
for markouts, at prices after a fill).  It never feeds back into a strategy.

Definitions
  spread captured   per fill, (mid at the fill instant - fill price) on the side bought, times the
                    quantity: what the resting quote earned relative to the mid the moment it
                    traded.  Positive = the quote paid a spread; negative = the market had already
                    moved through it.  (The mid is the last RECORDED book, up to ~3 s stale.)
  inventory P&L     gross P&L - spread captured: what the position earned (or lost) from prices
                    moving after it was acquired, and at settlement.
  markout (h)       mid ``h`` seconds after the fill minus the fill price, on the side bought.
                    Negative on average = **adverse selection**: the market moves against a quote
                    after it is hit.
  settled P&L/fill  ``qty * (payoff - price)``.  Summed over the fills of settled markets it is
                    EXACTLY the settled P&L before fees (a YES and a NO that net to a pair pay $1
                    either way), so any grouping of fills (category, time to resolution...)
                    attributes P&L without approximation.

Quote lifetime is measured from the strategy's own log: placement to the earlier of a fill and the
effect of a cancel request.  Fill rate is per order and per unit of quantity.
"""

from __future__ import annotations

import bisect
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace

from backtest.engine import BacktestConfig, BacktestResult, run_backtest
from backtest.feed import StoreFeed
from backtest.fills import QueueModel, TakerModel
from backtest.metrics import execution_stats, markouts, mid_series, pnl_breakdown, summarize
from data.storage.duckdb_store import Store
from market.fees import FeeBook
from market.semantics import EvidenceLevel
from market.timeutil import to_epoch_us
from market_making.baseline import BaselineParams, BinaryMarketMaker
from market_making.quoting import liquidity_half_spread
from market_making.risk import MICRO, RiskLimits, partition_worlds
from pricing.dataset import load_tapes, partition_groups, structure_stats
from pricing.microstructure import TradeTape, trade_features
from research.arbitrage_analysis import est
from research.calibration_analysis import PRICE_RANGES, TTR_BUCKETS
from research.stats import cluster_bootstrap_mean, concentration

SEC = 1_000_000_000
TICKS_PER_CENT = 100

MAKER_MODELS: dict[str, Callable[[], QueueModel]] = {
    "pessimistic": QueueModel.pessimistic,
    "queue(a=1,c=0)": lambda: QueueModel.queue(1.0, 0.0),
    "queue(a=1,c=.5)": lambda: QueueModel.queue(1.0, 0.5),
    "queue(a=.5,c=.5)": lambda: QueueModel.queue(0.5, 0.5),
    "optimistic": QueueModel.optimistic,
}


# --------------------------------------------------------------------------- data
@dataclass
class MMData:
    """Everything needed to run and score a market maker on one recorded database."""

    name: str
    feed: StoreFeed
    fees: FeeBook
    event_of: dict[str, str]
    category_of: dict[str, str]
    worlds: dict[str, list[dict[str, int]]]
    mids: dict[str, tuple[list[int], list[float]]]
    tapes: dict[str, TradeTape]
    settle: dict[str, int]  # ticker -> YES settlement (ticks), known post hoc: used only to SCORE

    @property
    def tickers(self) -> list[str]:
        return [m.ticker for m in self.feed.markets]

    @property
    def infos(self):
        return self.feed.infos


def load_mm_data(
    db: str,
    name: str,
    *,
    corpus_db: str = "var/relations.duckdb",
    partition_level: EvidenceLevel = EvidenceLevel.DECLARED,
) -> MMData:
    with Store(db, read_only=True) as s:
        start = s.query("SELECT min(recv_ts_ns) FROM book_snapshots")[0][0]
        end = max(
            s.query("SELECT max(recv_ts_ns) FROM book_snapshots")[0][0],
            s.query("SELECT max(recv_ts_ns) FROM poll_log")[0][0] or 0,
        )
        feed = StoreFeed(s, start_ns=start, end_ns=end)
        len(feed)  # materialise while the store is open
        fees = FeeBook.from_series_meta(s.read_series_fees())
        stats = structure_stats(
            corpus_db, start
        )  # exhaustiveness evidence from BEFORE the recording
        parts = partition_groups(s, stats=stats, minimum=partition_level)
        tapes = load_tapes(s, (m.ticker for m in feed.markets))
    return MMData(
        name=name,
        feed=feed,
        fees=fees,
        event_of={m.ticker: m.event_ticker for m in feed.markets},
        category_of={m.ticker: (m.category or "unknown") for m in feed.markets},
        worlds={event: partition_worlds(tickers) for event, tickers in parts.items()},
        mids=mid_series(feed),
        tapes=tapes,
        settle={
            m.ticker: m.settlement_value for m in feed.markets if m.settlement_value is not None
        },
    )


# --------------------------------------------------------------------------- running
@dataclass(frozen=True)
class RunConfig:
    params: BaselineParams = field(default_factory=BaselineParams)
    maker: str = "queue(a=1,c=.5)"
    latency_ms: float = 200.0
    fee_scale: float = 1.0
    strategy: str = "baseline"  # "baseline" | "naive_join"


@dataclass
class Run:
    data: MMData
    cfg: RunConfig
    res: BacktestResult
    mm: object


def run_mm(data: MMData, cfg: RunConfig, *, initial_cash_usd: float = 10_000.0) -> Run:
    from fractions import Fraction

    ns = int(cfg.latency_ms * 1_000_000)
    fees = (
        data.fees.scaled(Fraction(cfg.fee_scale).limit_denominator(1000))
        if cfg.fee_scale != 1
        else data.fees
    )
    bt = BacktestConfig(
        initial_cash_micro=int(initial_cash_usd * MICRO),
        latency_submit_ns=ns,
        latency_ack_ns=ns,
        latency_cancel_ns=ns,
        taker=TakerModel(max_staleness_ns=30 * SEC),
        maker=MAKER_MODELS[cfg.maker](),
        fees=fees,
        equity_sample_ns=60 * SEC,
    )
    if cfg.strategy == "naive_join":
        from backtest.strategies import PassiveQuoter

        strat = PassiveQuoter(data.tickers, qty=int(cfg.params.size * 100))
    else:
        strat = BinaryMarketMaker(
            data.tickers, cfg.params, event_of=data.event_of, worlds=data.worlds
        )
    res = run_backtest(data.feed, data.infos, strat, bt)
    return Run(data, cfg, res, strat)


# --------------------------------------------------------------------------- per-fill table
@dataclass(frozen=True)
class FillRow:
    dataset: str
    ticker: str
    event: str
    category: str
    ts: int
    side: str
    price: int
    qty: int  # centi
    fee: int
    mid_yes: float | None
    edge_micro: float | None  # spread captured vs the mid at the fill
    ttr_h: float | None
    prob: float | None  # the mid (YES probability) at the fill
    volume_1h: float
    inv_after: int  # net YES inventory in this market right after the fill (centi-contracts)
    markout30: float | None  # ticks, on the side bought
    settle_pnl: int | None  # hold-to-settlement P&L before fees, µ$ (settled markets only)

    @property
    def cluster(self) -> str:
        return f"{self.dataset}:{self.event}"


def _mid_at(mids, t: str, ts: int) -> float | None:
    times, vals = mids.get(t, ([], []))
    i = bisect.bisect_right(times, ts) - 1
    return vals[i] if i >= 0 else None


def fill_table(run: Run, horizon_s: float = 30.0) -> list[FillRow]:
    d = run.data
    rows: list[FillRow] = []
    inv: dict[str, int] = defaultdict(int)
    for f in run.res.fills:
        mid = _mid_at(d.mids, f.ticker, f.exec_ts)
        yes = f.side.value == "yes"
        inv[f.ticker] += f.qty if yes else -f.qty
        edge = None
        if mid is not None:
            edge = ((mid - f.price) if yes else ((10_000 - mid) - f.price)) * f.qty
        end = d.infos[f.ticker].scheduled_end
        ttr = None if end is None else (to_epoch_us(end) * 1000 - f.exec_ts) / (3600 * SEC)
        tape = d.tapes.get(f.ticker)
        vol = (
            trade_features(tape, f.exec_ts).volume[3600] if tape is not None and len(tape) else 0.0
        )
        later = _mid_at(d.mids, f.ticker, f.exec_ts + int(horizon_s * SEC))
        mo = None
        if later is not None and mid is not None:
            mo = (later - f.price) if yes else ((10_000 - later) - f.price)
        settle = d.settle.get(f.ticker)
        pnl = None
        if settle is not None:
            pnl = f.qty * ((settle if yes else 10_000 - settle) - f.price)
        rows.append(
            FillRow(
                d.name, f.ticker, d.event_of[f.ticker], d.category_of[f.ticker],
                f.exec_ts, f.side.value, f.price, f.qty, f.fee_micro, mid, edge, ttr,
                None if mid is None else mid / 10_000, vol, inv[f.ticker], mo, pnl,
            )
        )  # fmt: skip
    return rows


# --------------------------------------------------------------------------- one run, all metrics
def evaluate(run: Run, *, n_boot: int = 1000, seed: int = 0) -> dict:
    res, d, mm = run.res, run.data, run.mm
    fills = fill_table(run)
    pnl = pnl_breakdown(res)
    n_c = sum(f.qty for f in fills)
    spread = sum(f.edge_micro for f in fills if f.edge_micro is not None)
    gross = pnl["gross_micro"]
    summ = summarize(res)
    mo = markouts(res, d.mids, [5, 30, 300])
    # ---- P&L (µ$ -> $)
    out: dict = {
        "config": {
            "strategy": run.cfg.strategy, "maker": run.cfg.maker, "latency_ms": run.cfg.latency_ms,
            "gamma": run.cfg.params.gamma, "k": run.cfg.params.k, "size": run.cfg.params.size,
        },
        "pnl": {
            "net": pnl["net_micro"] / MICRO,
            "gross": gross / MICRO,
            "fees": pnl["fees_micro"] / MICRO,
            "realised": pnl["realised_micro"] / MICRO,
            "mark_to_market": pnl["unrealised_micro"] / MICRO,
            "spread_captured": spread / MICRO,
            "inventory_contribution": (gross - spread) / MICRO,
        },
    }  # fmt: skip
    # ---- risk
    settled_by_event: dict[str, int] = defaultdict(int)
    for _, t, _, p in res.settlements:
        settled_by_event[d.event_of[t]] += p
    inv = _inventory_paths(res)
    expo = getattr(mm, "exposure_log", [])
    out["risk"] = {
        "sharpe_annualised": summ["risk"]["sharpe_annualised"],
        "sortino_annualised": summ["risk"]["sortino_annualised"],
        "max_drawdown": summ["risk"]["max_drawdown_micro"] / MICRO,
        "pnl_volatility_per_minute": _equity_vol(res),
        "worst_event_settled": min(settled_by_event.values(), default=0) / MICRO,
        "worst_market_settled": min((p for *_, p in res.settlements), default=0) / MICRO,
        "inventory_abs_contracts": _quantiles(
            [abs(q) / 100 for path in inv.values() for q in path]
        ),
        "portfolio_worst_case_loss_max": max((e[1] for e in expo), default=None),
        "portfolio_gross_inventory_max": max((e[2] for e in expo), default=None),
        "killed": bool(getattr(getattr(mm, "risk", None), "state", None) and mm.risk.state.killed),
        "kill_reason": getattr(getattr(mm, "risk", None), "state", None) and mm.risk.state.reason,
        "event_concentration_of_settled_pnl": concentration(
            {e: abs(p) for e, p in settled_by_event.items() if p}
        ),
    }
    # ---- execution
    ex = execution_stats(res)
    lifetimes = _quote_lifetimes(run)
    out["execution"] = {
        "orders": ex["orders"],
        "order_fill_rate": ex["order_fill_rate"],
        "quantity_fill_rate": ex["quantity_fill_rate"],
        "cancellation_rate": ex["cancellation_rate"],
        "maker_fills": ex["maker_fills"],
        "contracts_traded": n_c / 100,
        "spread_captured_cents_per_contract": None if not n_c else spread / n_c / TICKS_PER_CENT,
        "inventory_turnover": _turnover(n_c / 100, expo),
        "quote_lifetime_s": _quantiles(lifetimes),
        "adverse_selection_markout_ticks": {str(h): v["mean_ticks"] for h, v in mo.items()},
    }
    out["fills"] = len(fills)
    out["_fills"] = fills
    return out


def _quantiles(xs: Sequence[float]) -> dict:
    if not xs:
        return {"n": 0}
    ys = sorted(xs)
    q = lambda p: ys[min(len(ys) - 1, int(p * len(ys)))]  # noqa: E731
    return {"n": len(ys), "p50": q(0.5), "p90": q(0.9), "max": ys[-1]}


def _inventory_paths(res: BacktestResult) -> dict[str, list[int]]:
    """Net YES inventory (centi-contracts) after each fill, per market."""
    q: dict[str, int] = defaultdict(int)
    path: dict[str, list[int]] = defaultdict(list)
    for f in sorted(res.fills, key=lambda x: x.exec_ts):
        q[f.ticker] += f.qty if f.side.value == "yes" else -f.qty
        path[f.ticker].append(q[f.ticker])
    return path


def _turnover(contracts: float, exposure_log: list[tuple]) -> float | None:
    """Contracts traded / average gross inventory held: how many times the average book was turned
    over.  Gross inventory is sampled at each of the strategy's risk checks (~1 s apart while
    events arrive)."""
    if not exposure_log:
        return None
    mean_gross = sum(e[2] for e in exposure_log) / len(exposure_log)
    return None if mean_gross <= 0 else contracts / mean_gross


def _equity_vol(res: BacktestResult) -> float | None:
    eq = [e[1] for e in res.equity]
    if len(eq) < 3:
        return None
    d = [b - a for a, b in zip(eq, eq[1:], strict=False)]
    m = sum(d) / len(d)
    return (sum((x - m) ** 2 for x in d) / (len(d) - 1)) ** 0.5 / MICRO


def _quote_lifetimes(run: Run) -> list[float]:
    mm = run.mm
    placed = getattr(mm, "placed_at", None)
    if not placed:
        return []
    first_fill: dict[int, int] = {}
    for f in run.res.fills:
        first_fill.setdefault(f.order_id, f.exec_ts)
    lat = int(run.cfg.latency_ms * 1_000_000)
    out = []
    for oid, t0 in placed.items():
        ends = []
        if oid in first_fill:
            ends.append(first_fill[oid])
        if oid in mm.cancel_requested_at:
            ends.append(mm.cancel_requested_at[oid] + lat)
        if ends:
            out.append((min(ends) - t0) / SEC)
    return out


# --------------------------------------------------------------------------- breakdowns
def _group_stats(rows: list[FillRow], *, n_boot: int, seed: int) -> dict:
    if not rows:
        return {"fills": 0}
    edge = [r for r in rows if r.edge_micro is not None]
    q = sum(r.qty for r in edge)
    settled = [r for r in rows if r.settle_pnl is not None]
    mo = [r for r in rows if r.markout30 is not None]
    row: dict = {
        "fills": len(rows),
        "events": len({r.cluster for r in rows}),
        "contracts": sum(r.qty for r in rows) / 100,
    }
    if q:
        row["spread_captured_cents_per_contract"] = (
            sum(r.edge_micro for r in edge) / q / TICKS_PER_CENT
        )
    if mo:
        e = cluster_bootstrap_mean(
            mo,
            lambda r: r.cluster,
            lambda r: r.markout30 / TICKS_PER_CENT,
            n_boot=n_boot,
            seed=seed,
        )
        row["markout_30s_cents"] = est(e)
    if settled:
        e = cluster_bootstrap_mean(
            settled,
            lambda r: r.cluster,
            lambda r: (r.settle_pnl - r.fee) / MICRO,
            n_boot=n_boot,
            seed=seed,
        )
        row["settled_pnl_per_fill_usd"] = est(e)
        row["settled_pnl_total_usd"] = sum(r.settle_pnl - r.fee for r in settled) / MICRO
        row["settled_fills"] = len(settled)
    return row


def breakdowns(fills: list[FillRow], *, n_boot: int = 1000, seed: int = 0) -> dict:
    """The spec's four breakdowns: category, liquidity, time to resolution, probability range."""

    def by(key, labels) -> dict:
        return {
            lab: _group_stats([r for r in fills if key(r) == lab], n_boot=n_boot, seed=seed)
            for lab in labels
        }

    def bucket(x, table):
        for name, lo, hi in table:
            if x is not None and lo <= x < hi:
                return name
        return None

    def liq(r):
        return (
            "none"
            if r.volume_1h <= 0
            else ("thin (0, 100]" if r.volume_1h <= 100 else "busy (> 100)")
        )

    cats = sorted({r.category for r in fills})
    return {
        "category": by(lambda r: r.category, cats),
        "liquidity_trailing_hour_volume": by(liq, ["none", "thin (0, 100]", "busy (> 100)"]),
        "time_to_resolution": by(
            lambda r: bucket(r.ttr_h, TTR_BUCKETS), [b[0] for b in TTR_BUCKETS]
        ),
        "probability_range": by(
            lambda r: bucket(r.prob, PRICE_RANGES), [b[0] for b in PRICE_RANGES]
        ),
    }


# --------------------------------------------------------------------------- ablations
UNLIMITED = 10**9


def limits_off(lim: RiskLimits, *, kill_switch: bool = False) -> RiskLimits:
    """Remove the position/event/portfolio limits (and, optionally, the drawdown kill-switch), but
    keep the data-quality gates (stale book, price band, expiry stop): those are not risk limits."""
    return replace(
        lim,
        max_position_contracts=UNLIMITED,
        max_event_loss_usd=UNLIMITED,
        max_portfolio_loss_usd=UNLIMITED,
        max_drawdown_usd=lim.max_drawdown_usd if kill_switch else UNLIMITED,
    )


def ablation_configs(base: BaselineParams, maker: str, latency_ms: float) -> dict[str, RunConfig]:
    """Every design element removed in turn.  The no-skew variants set ``gamma`` ~ 0 (reservation =
    fair price) and a fixed half-spread equal to the baseline's at p = 1/2, so only skew differs."""
    half = liquidity_half_spread(base.gamma, base.k) + base.gamma * 0.25 / 2
    flat = replace(base, gamma=1e-6, min_half_spread=half)

    def cfg(params: BaselineParams, strategy: str = "baseline") -> RunConfig:
        return RunConfig(params, maker, latency_ms, strategy=strategy)

    return {
        "A naive join (best bid, no model)": cfg(base, "naive_join"),
        "B fixed width, no skew, no limits": cfg(replace(flat, limits=limits_off(base.limits))),
        "C fixed width, no skew, limits + kill": cfg(flat),
        "D skew, no limits": cfg(replace(base, limits=limits_off(base.limits))),
        "E skew, limits, no kill-switch": cfg(
            replace(base, limits=replace(base.limits, max_drawdown_usd=UNLIMITED))
        ),
        "F baseline: skew + limits + kill": cfg(base),
    }


# ---------------------------------------------------------------------- pooling across databases
def pooled(evals: list[dict], *, n_boot: int = 1000, seed: int = 0) -> dict:
    """Combine per-database runs: P&L adds (separate accounts); rates are re-estimated over ALL
    fills with events (prefixed by database) as the independent clusters."""
    fills: list[FillRow] = [f for e in evals for f in e["_fills"]]
    return {
        "net": sum(e["pnl"]["net"] for e in evals),
        "gross": sum(e["pnl"]["gross"] for e in evals),
        "fees": sum(e["pnl"]["fees"] for e in evals),
        "spread_captured": sum(e["pnl"]["spread_captured"] for e in evals),
        "inventory_contribution": sum(e["pnl"]["inventory_contribution"] for e in evals),
        "max_drawdown_worst_db": max((e["risk"]["max_drawdown"] for e in evals), default=0.0),
        "worst_event_settled": min((e["risk"]["worst_event_settled"] for e in evals), default=0.0),
        "killed": [bool(e["risk"]["killed"]) for e in evals],
        "mean_abs_inventory_contracts": _mean_abs_inventory(fills),
        "all_fills": _group_stats(fills, n_boot=n_boot, seed=seed),
    }


def _mean_abs_inventory(fills: list[FillRow]) -> float | None:
    return sum(abs(f.inv_after) for f in fills) / len(fills) / 100 if fills else None


# ------------------------------------------------------------ inventory risk near settlement
def inventory_risk_by_ttr(fills: list[FillRow], *, n_boot: int = 1000, seed: int = 0) -> list[dict]:
    """How the risk of the inventory a maker is left holding changes with time to resolution.

    Per fill, using the inventory ``q`` (contracts, signed) right after it and the market mid ``p``:
      remaining std  |q| sqrt(p (1-p)): the P&L standard deviation of holding q to settlement
                     (a bounded martingale: state-dependent, NOT a (T-t) function)
    plus the 30 s markout of those same fills.  If inventory risk really concentrates near
    settlement it shows up as a larger remaining std and a more negative markout in the shortest
    buckets."""
    out = []
    for name, lo, hi in TTR_BUCKETS:
        rows = [f for f in fills if f.ttr_h is not None and lo <= f.ttr_h < hi and f.prob]
        if not rows:
            out.append({"bucket": name, "fills": 0})
            continue
        sd = [abs(f.inv_after) / 100 * (f.prob * (1 - f.prob)) ** 0.5 for f in rows]
        rows_m = [r for r in rows if r.markout30 is not None]
        e = (
            cluster_bootstrap_mean(
                rows_m,
                lambda r: r.cluster,
                lambda r: r.markout30 / TICKS_PER_CENT,
                n_boot=n_boot,
                seed=seed,
            )
            if rows_m
            else None
        )
        out.append(
            {
                "bucket": name,
                "fills": len(rows),
                "events": len({r.cluster for r in rows}),
                "mean_abs_inventory_contracts": sum(abs(f.inv_after) for f in rows)
                / len(rows)
                / 100,
                "mean_remaining_std_usd": sum(sd) / len(sd),
                "markout_30s_cents": None if e is None else est(e),
            }
        )
    return out


# ------------------------------------------------------------------------------------ sweeps
def run_pooled(datas: list[MMData], cfg: RunConfig, *, n_boot: int = 300, seed: int = 0) -> dict:
    """One configuration on every database, pooled.  ``_evals`` keeps the per-database results."""
    evals = [evaluate(run_mm(d, cfg), n_boot=10) for d in datas]
    out = pooled(evals, n_boot=n_boot, seed=seed)
    out["_evals"] = evals
    return out


def _row(label: dict, p: dict) -> dict:
    g = p["all_fills"]
    return {
        **label,
        "net_usd": p["net"],
        "fills": g["fills"],
        "contracts": g.get("contracts", 0.0),
        "spread_cents_per_contract": g.get("spread_captured_cents_per_contract"),
        "markout_30s_cents": g.get("markout_30s_cents"),
        "settled_pnl_per_fill_usd": g.get("settled_pnl_per_fill_usd"),
        "max_drawdown_usd": p["max_drawdown_worst_db"],
        "worst_event_settled_usd": p["worst_event_settled"],
        "mean_abs_inventory": p["mean_abs_inventory_contracts"],
        "killed": p["killed"],
    }


def sensitivity(
    datas: list[MMData], params: BaselineParams, *, latencies=(200.0, 1000.0), n_boot: int = 300
) -> list[dict]:
    """One strategy under every fill model and latency: passive P&L is a RANGE, not a number."""
    return [
        _row(
            {"latency_ms": lat, "maker": m},
            run_pooled(datas, RunConfig(params, m, lat), n_boot=n_boot),
        )
        for lat in latencies
        for m in MAKER_MODELS
    ]


def parameter_grid(
    datas: list[MMData],
    base: BaselineParams,
    maker: str,
    latency_ms: float,
    *,
    gammas=(0.02, 0.05, 0.10),
    ks=(25.0, 50.0, 100.0),
    n_boot: int = 300,
) -> list[dict]:
    """Risk aversion x liquidity-decay grid.  Half the strategy's spread comes from ``k`` (the
    smaller ``k``, the wider the quote), so this is also a spread-width sweep."""
    return [
        _row(
            {"gamma": g, "k": k},
            run_pooled(
                datas, RunConfig(replace(base, gamma=g, k=k), maker, latency_ms), n_boot=n_boot
            ),
        )
        for g in gammas
        for k in ks
    ]
