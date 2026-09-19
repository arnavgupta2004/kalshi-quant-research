import random
from datetime import UTC, datetime, timedelta

import pytest

from data.collectors.config import UniverseSpec
from data.collectors.universe import (
    ScanOrderError,
    Selector,
    needs_partition,
    scan_universe,
    series_of,
    stable_key,
)
from kalshi_client.models import ApiMarket, validate_rest
from tests.fakes import CUTOFF, FakeExchange, make_market

D = lambda m, d: datetime(2026, m, d, tzinfo=UTC)  # noqa: E731
SPEC = dict(close_after=D(9, 1), close_before=D(9, 10))


def api(fx, ticker, **kw):
    kw.setdefault("close", D(9, 5))
    return validate_rest(ApiMarket, make_market(fx, ticker, **kw))


def test_series_and_stable_key():
    assert series_of("KXWTAMATCH-26SEP19KURJEO") == "KXWTAMATCH"
    assert stable_key(1, "A") == stable_key(1, "A") != stable_key(2, "A")


def test_eligibility_filters(fx):
    sel = Selector(UniverseSpec(**SPEC, min_volume=100))
    ok = api(fx, "S-E1-A", volume=100)
    assert sel.eligible(ok)
    assert not sel.eligible(api(fx, "S-E1-B", volume=99.99))  # thin
    assert not sel.eligible(api(fx, "S-E1-C", status="active"))  # unsettled
    assert not sel.eligible(api(fx, "S-E1-D", close=D(8, 31)))  # outside window
    assert not sel.eligible(api(fx, "S-E1-E", close=D(9, 10)))  # window is half-open
    assert not sel.eligible(
        validate_rest(
            ApiMarket, dict(make_market(fx, "S-E1-F", close=D(9, 5)), mve_collection_ticker="X")
        )
    )
    assert not Selector(UniverseSpec(**SPEC, series=["OTHER"])).eligible(ok)
    assert not Selector(UniverseSpec(**SPEC, exclude_series=["S"])).eligible(ok)


def test_selection_is_independent_of_api_order(fx):
    ms = [api(fx, f"S{i % 5}-E{i}-X", volume=200) for i in range(200)]
    results = []
    for seed in range(3):
        shuffled = ms[:]
        random.Random(seed).shuffle(shuffled)
        sel = Selector(UniverseSpec(**SPEC, sample_fraction=0.5, max_per_series=7, seed=9))
        for m in shuffled:
            sel.offer(m, "live")
        results.append([m.ticker for m, _ in sel.result()])
    assert results[0] == results[1] == results[2] and 0 < len(results[0]) <= 35


def test_per_series_cap_keeps_lowest_keys_so_no_series_swamps_the_rest(fx):
    spec = UniverseSpec(**SPEC, max_per_series=3, seed=5)
    ms = [api(fx, f"BIG-E{i}-X") for i in range(50)] + [api(fx, f"SMALL-E{i}-X") for i in range(2)]
    sel = Selector(spec)
    for m in ms:
        sel.offer(m, "live")
    got = [m.ticker for m, _ in sel.result()]
    assert (
        sum(t.startswith("SMALL") for t in got) == 2 and sum(t.startswith("BIG") for t in got) == 3
    )
    expect = sorted((m.ticker for m in ms[:50]), key=lambda t: stable_key(5, t))[:3]
    assert sorted(t for t in got if t.startswith("BIG")) == sorted(expect)


def test_global_cap_and_sampling_fraction(fx):
    ms = [api(fx, f"S{i}-E-X") for i in range(400)]
    sel = Selector(UniverseSpec(**SPEC, sample_fraction=0.25, seed=1, max_markets=40))
    for m in ms:
        sel.offer(m, "live")
    assert len(sel.result()) == 40
    sel = Selector(UniverseSpec(**SPEC, sample_fraction=0.25, seed=1))
    for m in ms:
        sel.offer(m, "live")
    assert 60 < len(sel.result()) < 140  # ~100 expected of 400


def test_duplicate_ticker_across_partitions_is_offered_once(fx):
    sel = Selector(UniverseSpec(**SPEC, max_per_series=1))
    m = api(fx, "S-E-X")
    assert sel.offer(m, "live") and not sel.offer(m, "historical")
    assert sel.result() == [(m, "live")]


def test_selection_never_reads_the_outcome(fx):
    yes, no = api(fx, "S-E-A", result="yes"), api(fx, "S-E-B", result="no")
    sel = Selector(UniverseSpec(**SPEC, seed=3))
    assert sel.eligible(yes) and sel.eligible(no)
    # same ticker -> same key regardless of result
    assert stable_key(3, "S-E-A") == stable_key(3, api(fx, "S-E-A", result="no").ticker)


def test_spec_validation():
    with pytest.raises(Exception, match="sample_fraction"):
        UniverseSpec(**SPEC, sample_fraction=0)
    with pytest.raises(Exception, match="needs close_after"):
        UniverseSpec()
    with pytest.raises(Exception, match="before"):
        UniverseSpec(close_after=D(9, 2), close_before=D(9, 1))


def test_needs_partition_windows():
    live_only = UniverseSpec(close_after=D(9, 1), close_before=D(9, 10))
    hist_only = UniverseSpec(close_after=D(6, 1), close_before=D(7, 10))
    both = UniverseSpec(close_after=D(7, 1), close_before=D(8, 1))
    assert (
        needs_partition(live_only, "live", CUTOFF),
        needs_partition(live_only, "historical", CUTOFF),
    ) == (True, False)
    assert (
        needs_partition(hist_only, "live", CUTOFF),
        needs_partition(hist_only, "historical", CUTOFF),
    ) == (False, True)
    assert needs_partition(both, "live", CUTOFF) and needs_partition(both, "historical", CUTOFF)


async def test_scan_live_uses_server_side_window(fx):
    live = [make_market(fx, f"L{i}-E-X", close=D(9, 1 + i)) for i in range(12)]  # 09-01 .. 09-12
    ex = FakeExchange(fx, live=live)
    async with ex.rest() as rest:
        found, stats = await scan_universe(rest, UniverseSpec(**SPEC), cutoff=CUTOFF)
    assert stats.partitions == ["live"] and stats.scanned["live"] < 12  # filtered server-side
    assert {m.ticker for m, _ in found} == {f"L{i}-E-X" for i in range(9)}
    assert all(p == "live" for _, p in found)
    assert ex.count("/historical/markets") == 0


async def test_scan_historical_stops_early_and_labels_partition(fx):
    hist = [
        make_market(
            fx,
            f"H{i}-E-X",
            close=D(7, 10) + timedelta(hours=i),
            created=D(7, 8) - timedelta(days=i),
        )
        for i in range(40)
    ]
    ex = FakeExchange(fx, hist=hist, page=5)
    spec = UniverseSpec(close_after=D(7, 1), close_before=D(7, 25), created_lookback_days=7)
    async with ex.rest() as rest:
        found, stats = await scan_universe(rest, spec, cutoff=CUTOFF)
    assert stats.scanned["historical"] < 40  # stopped once created < 06-24
    assert ex.count("/historical/markets") < 8
    assert found and all(p == "historical" for _, p in found)
    assert all(m.created_time >= D(6, 24) for m, _ in found)


async def test_scan_fails_loudly_if_historical_order_assumption_breaks(fx):
    hist = [make_market(fx, f"H{i}-E-X", close=D(7, 10), created=D(7, 8)) for i in range(3)]
    ex = FakeExchange(fx, hist=hist)
    orig = ex.handler

    def unsorted(request):  # serve created_time ascending instead
        resp = orig(request)
        if request.url.path.endswith("/historical/markets"):
            body = resp.json()
            body["markets"] = [
                dict(m, created_time=f"2026-07-0{i + 1}T00:00:00.000000Z")
                for i, m in enumerate(body["markets"])
            ]
            import httpx

            return httpx.Response(200, json=body)
        return resp

    ex.handler = unsorted
    async with ex.rest() as rest:
        with pytest.raises(ScanOrderError):
            await scan_universe(
                rest, UniverseSpec(close_after=D(7, 1), close_before=D(7, 25)), cutoff=CUTOFF
            )
