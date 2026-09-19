"""API parsing + normalisation, against payloads captured from the live exchange."""

import json
from decimal import Decimal

import pytest

from data.normalization.normalizer import (
    normalize_event,
    normalize_market,
    normalize_trade,
    normalize_ws_trade,
    orderbook_from_rest,
)
from kalshi_client.exceptions import MessageValidationError
from kalshi_client.models import (
    ApiEvent,
    ApiMarket,
    ApiOrderbook,
    ApiTrade,
    WsError,
    WsMarketLifecycle,
    WsOrderbookDelta,
    WsOrderbookSnapshot,
    WsSubscribed,
    WsTicker,
    WsTrade,
    WsUnknown,
    parse_ws_message,
    validate_rest,
)
from market.contracts import MarketStatus, SettlementResult, Side
from market.units import PRICE_SCALE, parse_price


# ------------------------------------------------------------------ markets
def test_active_market_fields_and_blank_handling(fx):
    raw = fx("market_active.json")
    api = validate_rest(ApiMarket, raw)
    m = normalize_market(api, raw=raw)
    assert m.status is MarketStatus.ACTIVE and m.status.is_tradeable
    assert m.result is SettlementResult.UNSETTLED and m.settlement_value is None
    assert api.yes_bid_dollars == parse_price(raw["yes_bid_dollars"])
    assert m.rules.primary and m.close_time is not None and m.close_time.tzinfo is not None
    assert m.payoff(Side.YES) is None
    assert m.price_ranges and m.price_ranges[0].step > 0
    assert not m.is_multivariate


def test_settled_yes_no_scalar_payoffs(fx):
    yes = normalize_market(validate_rest(ApiMarket, fx("market_settled_yes.json")))
    no = normalize_market(validate_rest(ApiMarket, fx("market_settled_no.json")))
    sc_raw = fx("market_settled_scalar.json")
    sc = normalize_market(validate_rest(ApiMarket, sc_raw))
    assert yes.payoff(Side.YES) == PRICE_SCALE and yes.payoff(Side.NO) == 0
    assert no.payoff(Side.YES) == 0 and no.payoff(Side.NO) == PRICE_SCALE
    # settlement is NOT always {0,1}: scalar markets pay a fraction and YES+NO still sums to $1
    assert sc.result is SettlementResult.SCALAR
    assert sc.settlement_value == parse_price(sc_raw["settlement_value_dollars"])
    assert 0 < sc.settlement_value < PRICE_SCALE
    assert sc.payoff(Side.YES) + sc.payoff(Side.NO) == PRICE_SCALE


def test_result_contradicting_settlement_value_is_rejected(fx):
    raw = dict(fx("market_settled_yes.json"), settlement_value_dollars="0.0000")
    with pytest.raises(MessageValidationError, match="contradicts"):
        normalize_market(validate_rest(ApiMarket, raw))


def test_scalar_without_value_is_rejected(fx):
    raw = dict(fx("market_settled_scalar.json"), settlement_value_dollars="")
    with pytest.raises(MessageValidationError, match="scalar"):
        normalize_market(validate_rest(ApiMarket, raw))


def test_unknown_result_string_is_rejected(fx):
    raw = dict(fx("market_settled_yes.json"), result="voided?")
    with pytest.raises(MessageValidationError):
        normalize_market(validate_rest(ApiMarket, raw))


def test_unknown_status_maps_to_unknown_not_crash(fx):
    m = normalize_market(validate_rest(ApiMarket, dict(fx("market_active.json"), status="frozen")))
    assert m.status is MarketStatus.UNKNOWN


def test_multivariate_flagged(fx):
    assert normalize_market(validate_rest(ApiMarket, fx("market_mve.json"))).is_multivariate


def test_market_missing_required_field_or_bad_units(fx):
    raw = fx("market_active.json")
    with pytest.raises(MessageValidationError):
        validate_rest(ApiMarket, {k: v for k, v in raw.items() if k != "ticker"})
    with pytest.raises(MessageValidationError):
        validate_rest(
            ApiMarket, dict(raw, yes_bid_dollars="0.123456")
        )  # off-grid: no silent rounding
    with pytest.raises(MessageValidationError):
        validate_rest(ApiMarket, dict(raw, close_time="2026-09-19T06:31:35"))  # naive timestamp


def test_liquidity_is_dollars_not_a_price(fx):
    api = validate_rest(ApiMarket, dict(fx("market_active.json"), liquidity_dollars="12345.6789"))
    assert api.liquidity_dollars == Decimal("12345.6789")


# ------------------------------------------------------------------ events
def test_event_normalisation(fx):
    raw = fx("event.json")
    api = validate_rest(ApiEvent, raw)
    ev = normalize_event(api)
    assert ev.event_ticker == raw["event_ticker"]
    assert ev.mutually_exclusive is raw["mutually_exclusive"]
    assert len(ev.settlement_sources) == len(raw["settlement_sources"])
    assert ev.market_tickers == tuple(m["ticker"] for m in raw["markets"])
    m = normalize_market(api.markets[0], event=ev)
    assert m.series_ticker == ev.series_ticker and m.category == ev.category


# ------------------------------------------------------------------ order book (REST)
def test_rest_orderbook_is_sorted_by_us_not_trusted(fx):
    raw = fx("orderbook.json")
    api = validate_rest(ApiOrderbook, raw)
    book = orderbook_from_rest("T", api)
    # the exchange returned levels worst->best in practice; we must not depend on that
    for side in Side:
        prices = [lv.price for lv in book.bids(side)]
        assert prices == sorted(prices, reverse=True)
    yb, nb = book.best_bid(Side.YES), book.best_bid(Side.NO)
    assert book.best_ask(Side.YES).price == PRICE_SCALE - nb.price
    assert book.best_ask(Side.NO).price == PRICE_SCALE - yb.price


def test_rest_orderbook_bad_shape_raises():
    with pytest.raises(MessageValidationError):
        validate_rest(ApiOrderbook, {"orderbook": {"yes": [[50, 10]]}})


# ------------------------------------------------------------------ trades
def test_rest_trades(fx):
    trades = [normalize_trade(validate_rest(ApiTrade, t)) for t in fx("trades.json")["trades"]]
    assert trades
    for t in trades:
        assert t.yes_price + t.no_price == PRICE_SCALE
        assert t.created_time.tzinfo is not None
        # per docs: taker_book_side bid == YES taker, ask == NO taker
        assert t.taker_side is {"bid": Side.YES, "ask": Side.NO}[t.taker_book_side]


def test_trade_side_contradiction_rejected(fx):
    raw = dict(fx("trades.json")["trades"][0], taker_side="yes", taker_book_side="ask")
    with pytest.raises(MessageValidationError, match="contradicts"):
        normalize_trade(validate_rest(ApiTrade, raw))


def test_trade_prices_must_sum_to_one(fx):
    raw = dict(
        fx("trades.json")["trades"][0], yes_price_dollars="0.1000", no_price_dollars="0.1000"
    )
    with pytest.raises(MessageValidationError):
        normalize_trade(validate_rest(ApiTrade, raw))


# ------------------------------------------------------------------ websocket frames
SNAPSHOT = {
    "type": "orderbook_snapshot",
    "sid": 2,
    "seq": 2,
    "msg": {
        "market_ticker": "FED-23DEC-T3.00",
        "market_id": "9b0f",
        "yes_dollars_fp": [["0.0800", "300.00"]],
        "no_dollars_fp": [["0.5400", "20.00"]],
    },
}
DELTA = {
    "type": "orderbook_delta",
    "sid": 2,
    "seq": 3,
    "msg": {
        "market_ticker": "FED-23DEC-T3.00",
        "market_id": "9b0f",
        "price_dollars": "0.9600",
        "delta_fp": "-54.00",
        "side": "yes",
        "ts_ms": 1669149841000,
    },
}
TICKER = {
    "type": "ticker",
    "sid": 11,
    "msg": {
        "market_ticker": "FED-23DEC-T3.00",
        "price_dollars": "0.4800",
        "yes_bid_dollars": "0.4500",
        "yes_ask_dollars": "0.5300",
        "volume_fp": "33896.00",
        "open_interest_fp": "20422.00",
        "dollar_volume": 16948,
        "yes_bid_size_fp": "300.00",
        "yes_ask_size_fp": "150.00",
        "last_trade_size_fp": "25.00",
        "ts": 1669149841,
        "ts_ms": 1669149841000,
    },
}
TRADE = {
    "type": "trade",
    "sid": 11,
    "seq": 2,
    "msg": {
        "trade_id": "d91b",
        "market_ticker": "HIGHNY-22DEC23-B53.5",
        "yes_price_dollars": "0.3600",
        "no_price_dollars": "0.6400",
        "count_fp": "136.00",
        "taker_side": "no",
        "taker_outcome_side": "no",
        "taker_book_side": "ask",
        "is_block_trade": False,
        "ts": 1669149841,
        "ts_ms": 1669149841000,
    },
}
LIFECYCLE = {
    "type": "market_lifecycle_v2",
    "sid": 13,
    "seq": 3,
    "msg": {
        "market_ticker": "INXD-23SEP14-B4487",
        "event_type": "created",
        "open_ts": 1694635200,
        "close_ts": 1694721600,
        "price_level_structure": "linear_cent",
        "additional_metadata": {"title": "x"},
    },
}


def ws(obj, **kw):
    return parse_ws_message(json.dumps(obj), **kw)


def test_ws_snapshot_and_delta():
    s = ws(SNAPSHOT, recv_ts_ns=5, epoch=3)
    assert isinstance(s, WsOrderbookSnapshot)
    assert (s.sid, s.seq, s.epoch, s.recv_ts_ns) == (2, 2, 3, 5)
    assert s.yes_dollars_fp == [(800, 30000)] and s.no_dollars_fp == [(5400, 2000)]
    d = ws(DELTA)
    assert isinstance(d, WsOrderbookDelta)
    assert (d.price_dollars, d.delta_fp, d.side) == (9600, -5400, Side.YES)
    assert d.exchange_ts.year == 2022 and d.exchange_ts.tzinfo is not None


def test_ws_ticker_trade_lifecycle():
    t = ws(TICKER)
    assert isinstance(t, WsTicker) and (t.yes_bid_dollars, t.yes_ask_dollars) == (4500, 5300)
    tr = ws(TRADE)
    assert isinstance(tr, WsTrade)
    trade = normalize_ws_trade(tr)
    assert trade.taker_side is Side.NO and trade.count == 13600
    assert trade.created_time.isoformat().startswith("2022-11-22T20:44:01")
    lc = ws(LIFECYCLE)
    assert isinstance(lc, WsMarketLifecycle) and lc.ts("open_ts").year == 2023


def test_ws_control_frames_and_unknown_types_preserved():
    sub = ws({"id": 1, "type": "subscribed", "msg": {"channel": "orderbook_delta", "sid": 7}})
    assert isinstance(sub, WsSubscribed) and (sub.sid, sub.id, sub.channel) == (
        7,
        1,
        "orderbook_delta",
    )
    err = ws({"id": 1, "type": "error", "msg": {"code": 6, "msg": "Already subscribed"}})
    assert isinstance(err, WsError) and err.code == 6 and err.msg == "Already subscribed"
    unk = ws({"type": "brand_new_channel", "sid": 1, "msg": {"x": 1}})
    assert isinstance(unk, WsUnknown) and unk.type == "brand_new_channel" and '"x": 1' in unk.raw


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        "[1,2,3]",
        '{"no_type": 1}',
        '{"type": "ticker", "msg": 5}',
        '{"type": "orderbook_delta", "sid": "2", "seq": 1, "msg": {}}',  # non-int envelope
        json.dumps({**DELTA, "msg": {**DELTA["msg"], "side": "maybe"}}),  # bad enum
        json.dumps({**DELTA, "msg": {**DELTA["msg"], "price_dollars": "0.96001"}}),  # off-grid
        json.dumps(
            {**DELTA, "msg": {k: v for k, v in DELTA["msg"].items() if k != "market_ticker"}}
        ),
    ],
)
def test_ws_malformed_raises_with_raw_attached(raw):
    with pytest.raises(MessageValidationError) as ei:
        parse_ws_message(raw)
    assert ei.value.raw == raw
