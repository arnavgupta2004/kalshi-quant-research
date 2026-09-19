import json
from pathlib import Path

from data.collectors.config import EventsConfig
from data.collectors.events import EventCollector
from data.storage.duckdb_store import Store
from market.contracts import SettlementResult
from market.semantics import EvidenceLevel, analyze_event
from tests.fakes import FakeExchange

FIX = Path(__file__).parent / "fixtures" / "events"


def event_payload(name, series=None):
    d = json.loads((FIX / f"{name}.json").read_text())
    ev = dict(d["event"])
    ev["markets"] = d["markets"]
    if series:
        ev["series_ticker"] = series
    return ev


def exchange(fx, names):
    ex = FakeExchange(fx, page=3)
    ex.event_list = [event_payload(n) for n in names]
    return ex


async def collect(ex, store, series, per_series=5):
    cfg = EventsConfig(per_series=per_series, concurrency=2, requests_per_second=1e6)
    async with ex.rest() as rest:
        return await EventCollector(store, rest, cfg, series).run()


async def test_complete_events_roundtrip_through_the_database_and_analyse_identically(fx):
    ex = exchange(fx, ["nyc_high", "epl_3way", "mlb_spread"])
    store = Store()
    s = await collect(ex, store, ["KXHIGHNY", "KXEPLGAME", "KXMLBSPREAD"])
    assert s.events == 3 and s.markets == 6 + 3 + 6 and not s.failures
    bundles = store.read_events_with_markets()
    assert {e.series_ticker: len(ms) for e, ms in bundles} == {
        "KXHIGHNY": 6,
        "KXEPLGAME": 3,
        "KXMLBSPREAD": 6,
    }
    for ev, ms in bundles:  # completeness metadata survives, so partition claims stay possible
        assert set(ev.market_tickers) == {m.ticker for m in ms}
    nyc = next(b for b in bundles if b[0].series_ticker == "KXHIGHNY")
    assert any(
        a.level is EvidenceLevel.LATTICE and a.relation.kind.value == "partition"
        for a in analyze_event(*nyc).assessments
    )  # same result as from the API objects


async def test_domain_objects_survive_the_database_exactly(fx):
    from data.normalization.normalizer import normalize_event, normalize_market
    from kalshi_client.models import ApiEvent, validate_rest

    ex = exchange(fx, ["epl_3way"])
    store = Store()
    await collect(ex, store, ["KXEPLGAME"])
    ((ev, ms),) = store.read_events_with_markets()
    api = validate_rest(ApiEvent, event_payload("epl_3way"))
    want_ev = normalize_event(api)
    want = {m.ticker: normalize_market(m, event=want_ev) for m in api.markets}
    assert ev == want_ev
    for m in ms:
        w = want[m.ticker]
        assert (m.strike, m.rules, m.result, m.settlement_value, m.close_time, m.status) == (
            w.strike,
            w.rules,
            w.result,
            w.settlement_value,
            w.close_time,
            w.status,
        )
    assert {m.result for m in ms} == {SettlementResult.YES, SettlementResult.NO}


async def test_rerun_skips_complete_series_and_is_idempotent(fx):
    ex = exchange(fx, ["nyc_high", "epl_3way"])
    store = Store()
    await collect(ex, store, ["KXHIGHNY", "KXEPLGAME"], per_series=1)
    fp = store.fingerprint()["fingerprint"]
    before = len(ex.requests)
    s2 = await collect(ex, store, ["KXHIGHNY", "KXEPLGAME"], per_series=1)
    assert s2.series_skipped == 2 and s2.series_fetched == 0 and len(ex.requests) == before
    assert store.fingerprint()["fingerprint"] == fp


async def test_one_failing_series_does_not_stop_the_others(fx):
    ex = exchange(fx, ["nyc_high", "epl_3way"])
    orig = ex.handler

    def flaky(request):
        import httpx

        if (
            request.url.path == "/trade-api/v2/events"
            and request.url.params.get("series_ticker") == "KXEPLGAME"
        ):
            return httpx.Response(500, json={"error": "boom"})
        return orig(request)

    ex.handler = flaky
    store = Store()
    s = await collect(ex, store, ["KXHIGHNY", "KXEPLGAME"])
    assert s.series_fetched == 1 and len(s.failures) == 1 and "KXEPLGAME" in s.failures[0]
    assert store.query("SELECT count(*) FROM events") == [(1,)]
