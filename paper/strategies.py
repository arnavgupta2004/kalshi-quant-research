"""Strategy factories for paper trading.

A session needs a FRESH strategy instance at the end (to replay the tape and check parity), so
sessions take a factory, not an instance.  ``adaptive`` uses the models frozen in Stage 10
(``results/stage10/dev/adaptive.json``): the coefficients are read, never refitted.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

from arbitrage.detector import DetectorParams, build_specs
from backtest.strategies import ArbitrageTaker
from market_making.adaptive import AdaptiveMarketMaker, AdaptiveParams
from market_making.baseline import BaselineParams, BinaryMarketMaker
from market_making.shortterm import Ridge
from paper.universe import Universe

STAGE10_MODELS = "results/stage10/dev/adaptive.json"
STAGE10_KAPPA = 1.0
# live books are confirmed every second while the feed is healthy: a book unconfirmed for 5 s means
# the feed is in trouble, so the quotes come off (the backtest default of 30 s reflects 3 s polling)
LIVE_MAX_BOOK_AGE_S = 5.0


def _ridge(d: dict, target: str) -> Ridge:
    return Ridge(
        tuple(d["features"]),
        tuple(d["center"]),
        tuple(d["scale"]),
        tuple(d["weights"]),
        d["alpha"],
        d["horizon_s"],
        clip=d["clip"],
        intercept=d["intercept"],
        target=target,
    )


def adaptive_params(
    base: BaselineParams, models_path: str | Path = STAGE10_MODELS
) -> AdaptiveParams:
    """The frozen Stage 10 FULL variant: staleness fair value, widening, size, time skew."""
    m = json.loads(Path(models_path).read_text())["models"]
    return AdaptiveParams(
        base=base,
        fv=_ridge(m["fair_value_staleness"], "change"),
        scale=_ridge(m["scale"], "abs"),
        kappa=STAGE10_KAPPA,
        size_by_scale=True,
        sigma_ref=m["sigma_ref"],
        ttr_skew=1.0,
    )


def make_factory(
    kind: str, u: Universe, *, base: BaselineParams | None = None
) -> Callable[[], object]:
    base = base or BaselineParams()
    base = replace(base, limits=replace(base.limits, max_book_age_s=LIVE_MAX_BOOK_AGE_S))
    if kind == "baseline":
        return lambda: BinaryMarketMaker(u.tickers, base, event_of=u.event_of)
    if kind == "adaptive":
        params = adaptive_params(base)
        return lambda: AdaptiveMarketMaker(u.tickers, params, event_of=u.event_of)
    if kind == "arbitrage":
        specs = build_specs(u.bundles())
        det = DetectorParams(fees=u.fees)
        return lambda: ArbitrageTaker(specs, det, cooldown_ns=1_000_000_000, size_cap=1000)
    raise ValueError(f"unknown strategy {kind!r} (baseline | adaptive | arbitrage)")
