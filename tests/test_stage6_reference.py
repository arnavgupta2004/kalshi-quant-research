"""The Stage 6 latency sweep must reproduce Stage 5's arbitrage-taker results on the same data.

Needs the recorded dataset (``var/`` is git-ignored), so it skips on a fresh checkout; on the machine
that recorded it, it pins the two implementations to each other."""

from pathlib import Path
from types import SimpleNamespace

import pytest

DB = Path("var/books_shortlived.duckdb")


@pytest.mark.skipif(not DB.exists(), reason="recorded dataset not present")
@pytest.mark.timeout(120)
def test_latency_sweep_reproduces_the_stage5_reference_numbers():
    from arbitrage.detector import build_specs
    from backtest.feed import StoreFeed
    from data.storage.duckdb_store import Store
    from market.fees import FeeBook
    from market.semantics import EvidenceLevel
    from research import latency_analysis as L
    from scripts.stage5_backtest_demo import universe

    with Store(str(DB), read_only=True) as s:
        keep = set(universe(s, 200, 30))
        bundles = [
            (e, [m for m in ms if m.ticker in keep]) for e, ms in s.read_events_with_markets()
        ]
        feed = StoreFeed(s, sorted(keep))
        len(feed)
        ds = SimpleNamespace(
            feed=feed,
            specs=build_specs([(e, ms) for e, ms in bundles if ms]),
            fees=FeeBook.from_series_meta(s.read_series_fees()),
        )
        got = {}
        for name, lvl, scale in [
            ("base", EvidenceLevel.DECLARED, 1),
            ("free", EvidenceLevel.DECLARED, 0),
        ]:
            (r,) = L.engine_latency_sweep(ds, [200], min_level=lvl, fee_scale=scale, max_size=None)
            got[name] = (r["opportunities"], round(r["net_usd"], 2))
    assert got == {"base": (10, 0.02), "free": (37, 9.69)}  # results/stage5/backtest_demo.json
