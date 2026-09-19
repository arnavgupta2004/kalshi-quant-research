from datetime import UTC, datetime, timedelta

from data.collectors.config import RefreshConfig, load_config
from data.collectors.refresh import RefreshCollector
from data.normalization.normalizer import normalize_market
from data.storage.duckdb_store import Store
from kalshi_client.models import ApiMarket, validate_rest
from tests.fakes import FakeExchange, make_market, make_trade

D = lambda d, h=0: datetime(2026, 9, d, h, tzinfo=UTC)  # noqa: E731


def recorded_db(fx):
    """A books-style DB: two markets stored while still active (no result, no trades)."""
    s = Store()
    for t in ("R-E-A", "R-E-B"):
        raw = make_market(fx, t, close=D(5, 18), status="active")
        s.upsert_markets([normalize_market(validate_rest(ApiMarket, raw))], "rec", partition="live")
    return s


def now_state(fx, a_settled=True):
    """The exchange later: A settled YES with trades, B still active with trades."""
    a = make_market(fx, "R-E-A", close=D(5, 18), result="yes")
    b = make_market(fx, "R-E-B", close=D(5, 18), status="active")
    trades = {
        "R-E-A": [make_trade("R-E-A", i, D(5, 12) + timedelta(minutes=i)) for i in range(5)],
        "R-E-B": [make_trade("R-E-B", i, D(5, 12) + timedelta(minutes=i)) for i in range(4)],
    }
    return FakeExchange(fx, live=[a, b], trades=trades, page=3)


async def refresh(ex, store, tickers=(), clock=None):
    clock = clock or D(5, 13)
    async with ex.rest() as rest:
        cfg = RefreshConfig(tickers=list(tickers), concurrency=2)
        return await RefreshCollector(store, rest, cfg, now=lambda: clock).run()


async def test_refresh_settles_recorded_markets_and_backfills_their_trades(fx):
    store, ex = recorded_db(fx), now_state(fx)
    assert all(m.settlement_value is None for m in store.read_markets())
    s = await refresh(ex, store)
    assert (s.markets, s.settled_now, s.newly_settled, s.trades_inserted, s.failures) == (
        2,
        1,
        1,
        9,
        [],
    )
    a, b = store.read_markets(["R-E-A"])[0], store.read_markets(["R-E-B"])[0]
    assert a.result.value == "yes" and a.settlement_value == 10_000 and a.settlement_ts is not None
    assert b.settlement_value is None  # still open: nothing invented
    assert store.query("SELECT ticker, count(*) FROM trades GROUP BY 1 ORDER BY 1") == [
        ("R-E-A", 5),
        ("R-E-B", 4),
    ]


async def test_refresh_is_incremental_and_idempotent(fx):
    store, ex = recorded_db(fx), now_state(fx)
    await refresh(ex, store)
    fp = store.fingerprint()["fingerprint"]
    before = ex.count("/markets/trades")
    s2 = await refresh(ex, store)
    assert store.fingerprint()["fingerprint"] == fp and s2.newly_settled == 0
    # the settled market is final: no trade re-fetch for it; only the still-open one is re-synced
    assert ex.count("/markets/trades") - before <= 2
    # later trades on the open market arrive incrementally, without duplicating the old ones
    ex.trades["R-E-B"] += [
        make_trade("R-E-B", i, D(5, 14) + timedelta(minutes=i)) for i in range(4, 7)
    ]
    s3 = await refresh(ex, store, clock=D(5, 15))  # two simulated hours later
    assert (
        s3.trades_inserted == 3
        and store.query("SELECT count(*), count(DISTINCT trade_id) FROM trades")[0][0] == 12
    )


async def test_refresh_can_be_limited_to_named_tickers(fx):
    store, ex = recorded_db(fx), now_state(fx)
    s = await refresh(ex, store, ["R-E-B"])
    assert s.markets == 1 and store.query("SELECT DISTINCT ticker FROM trades") == [("R-E-B",)]


def test_refresh_config_is_strict(tmp_path):
    p = tmp_path / "r.yaml"
    p.write_text("job: refresh\ndb_path: x.duckdb\nconcurrency: 3\n")
    assert isinstance(load_config(p), RefreshConfig) and load_config(p).concurrency == 3
    p.write_text("job: refresh\ndb_pth: x\n")
    import pytest

    from data.collectors.config import ConfigError

    with pytest.raises(ConfigError, match="unknown keys"):
        load_config(p)
