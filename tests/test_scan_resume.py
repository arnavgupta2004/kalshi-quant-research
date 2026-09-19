"""The universe scan is the slowest phase (~14 min live), so it must survive interruption."""

from datetime import UTC, datetime, timedelta

import pytest

from data.collectors.config import UniverseSpec
from data.collectors.universe import _rehydrate, scan_key, scan_universe
from data.storage.duckdb_store import Store
from tests.fakes import CUTOFF, FakeExchange, SimulatedCrash, make_market

D = lambda m, d, h=0: datetime(2026, m, d, h, tzinfo=UTC)  # noqa: E731
SPEC = UniverseSpec(close_after=D(9, 1), close_before=D(9, 20), max_per_series=2, seed=11)


def exchange(fx, n=60):
    live = [
        make_market(fx, f"S{i % 12}-E{i}-X", close=D(9, 2) + timedelta(hours=i)) for i in range(n)
    ]
    return FakeExchange(fx, live=live, page=5)


def listing_requests(ex):
    return [
        r for r in ex.requests if r.url.path.endswith("/markets") and "tickers" not in r.url.params
    ]


async def scan(ex, store=None, spec=SPEC, **kw):
    async with ex.rest() as rest:
        return await scan_universe(
            rest, spec, cutoff=CUTOFF, store=store, run_id="r", checkpoint_pages=2, **kw
        )


async def clean(fx):
    found, stats = await scan(exchange(fx))
    return [m.ticker for m, _ in found], stats


async def test_rest_reports_pages_and_starts_from_a_cursor(fx):
    ex = exchange(fx, 12)
    seen = []
    async with ex.rest() as rest:
        all_ = [m.ticker async for m in rest.iter_markets(status="settled", on_page=seen.append)]
        tail = [m.ticker async for m in rest.iter_markets(status="settled", start_cursor="10")]
    assert seen == ["5", "10", None]  # cursor after each page; None = exhausted
    assert tail == all_[10:]


async def test_crash_mid_scan_resumes_from_checkpoint_and_selects_identically(fx):
    expect, full = await clean(fx)
    store, ex = Store(), exchange(fx)
    ex.crash_after = 6  # six pages read, then die on the 7th
    with pytest.raises(SimulatedCrash):
        await scan(ex, store)
    saved = store.get_scan_state(scan_key(SPEC), "live")
    assert saved and not saved["done"] and saved["cursor"] == "30" and saved["n_scanned"] == 30

    ex.crash_after = None
    ex.requests.clear()
    found, stats = await scan(ex, store)
    assert [m.ticker for m, _ in found] == expect  # exactly what an uninterrupted scan selects
    assert stats.resumed == ["live"] and not stats.restarted
    assert (
        stats.scanned == full.scanned and stats.passed_filters == full.passed_filters
    )  # no double counting
    assert len(listing_requests(ex)) == 6  # pages 7..12 only, not all 12


async def test_completed_scan_is_reused_without_rescanning(fx):
    expect, _ = await clean(fx)
    store, ex = Store(), exchange(fx)
    await scan(ex, store)
    ex.requests.clear()
    found, stats = await scan(ex, store)
    assert stats.reused == ["live"] and listing_requests(ex) == []  # only tickers= rehydration
    assert [m.ticker for m, _ in found] == expect


async def test_stale_completed_scan_is_rescanned(fx):
    store, ex = Store(), exchange(fx)
    clock = [D(9, 19)]
    await scan(ex, store, now=lambda: clock[0])
    clock[0] += timedelta(hours=25)  # older than reuse_hours=24
    ex.requests.clear()
    _, stats = await scan(ex, store, now=lambda: clock[0])
    assert stats.reused == [] and len(listing_requests(ex)) == 12


async def test_cursor_silently_ignored_by_the_exchange_is_detected_and_not_double_counted(fx):
    expect, full = await clean(fx)
    store, ex = Store(), exchange(fx)
    ex.crash_after = 6
    with pytest.raises(SimulatedCrash):
        await scan(ex, store)
    st = store.get_scan_state(scan_key(SPEC), "live")
    store.set_scan_state(
        scan_key(SPEC),
        "live",
        cursor="garbage",
        n_scanned=st["n_scanned"],
        passed=st["passed"],
        first_ticker=st["first_ticker"],
        items=[[k, t] for k, t in st["items"]],
        done=False,
        run_id="r",
    )  # the real API turns "garbage" into page 1
    ex.crash_after = None
    found, stats = await scan(ex, store)
    assert stats.restarted == ["live"] and stats.resumed == []
    assert [m.ticker for m, _ in found] == expect
    assert stats.scanned == full.scanned and stats.passed_filters == full.passed_filters


async def test_changed_spec_does_not_reuse_old_progress(fx):
    store, ex = Store(), exchange(fx)
    await scan(ex, store)
    other = UniverseSpec(close_after=D(9, 1), close_before=D(9, 20), max_per_series=2, seed=12)
    assert scan_key(other) != scan_key(SPEC)
    ex.requests.clear()
    _, stats = await scan(ex, store, spec=other)
    assert stats.reused == [] and len(listing_requests(ex)) == 12


async def test_historical_early_stop_is_recorded_as_done_and_reusable(fx):
    hist = [
        make_market(
            fx,
            f"H{i % 5}-E{i}-X",
            close=D(7, 10) + timedelta(hours=i),
            created=D(7, 8) - timedelta(days=i),
        )
        for i in range(30)
    ]
    ex = FakeExchange(fx, hist=hist, page=4)
    spec = UniverseSpec(
        close_after=D(7, 1), close_before=D(7, 25), created_lookback_days=7, max_per_series=3
    )
    store = Store()
    first, s1 = await scan(ex, store, spec=spec)
    assert store.get_scan_state(scan_key(spec), "historical")["done"]
    ex.requests.clear()
    second, s2 = await scan(ex, store, spec=spec)
    assert "historical" in s2.reused and [m.ticker for m, _ in second] == [
        m.ticker for m, _ in first
    ]
    assert not [r for r in ex.requests if "tickers" not in r.url.params]


async def test_rehydrate_ignores_markets_that_were_not_asked_for(fx):
    ex = exchange(fx, 10)
    orig = ex.handler

    def noisy(request):  # a misbehaving API that ignores the tickers filter
        if request.url.path.endswith("/markets") and request.url.params.get("tickers"):
            import httpx

            return httpx.Response(200, json={"markets": ex.live, "cursor": ""})
        return orig(request)

    ex.handler = noisy
    async with ex.rest() as rest:
        got = await _rehydrate(rest, ["S0-E0-X", "S1-E1-X"], "live", None)
    assert sorted(m.ticker for m in got) == ["S0-E0-X", "S1-E1-X"]


def test_scan_state_roundtrip_and_upsert():
    store = Store()
    assert store.get_scan_state("k", "live") is None
    store.set_scan_state(
        "k",
        "live",
        cursor="c1",
        n_scanned=10,
        passed=3,
        first_ticker="A",
        items=[[5, "A"], [9, "B"]],
        done=False,
        run_id="r",
        now=D(9, 1),
    )
    store.set_scan_state(
        "k",
        "live",
        cursor=None,
        n_scanned=99,
        passed=7,
        first_ticker="A",
        items=[[5, "A"]],
        done=True,
        run_id="r",
        now=D(9, 2),
    )
    got = store.get_scan_state("k", "live")
    assert (
        got["done"]
        and got["cursor"] is None
        and got["n_scanned"] == 99
        and got["items"] == [[5, "A"]]
    )
    assert got["updated_at"] == D(9, 2)
