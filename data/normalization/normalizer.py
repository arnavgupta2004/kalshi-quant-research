"""Map validated wire models onto the exchange-agnostic domain objects in ``market``.

All semantic decisions about *what a field means* live here, in one place:

* Settlement value: ``result == yes`` -> $1, ``no`` -> $0, ``scalar`` -> the exchange's
  ``settlement_value_dollars`` (must be present).  A settled market whose value contradicts
  its result is rejected as corrupt rather than stored.
* ``taker_book_side``: per the exchange docs, ``bid`` means the taker bought YES and ``ask``
  means the taker bought NO; the taker side is cross-checked against it.
"""

from __future__ import annotations

from datetime import datetime

from kalshi_client.exceptions import MessageValidationError
from kalshi_client.models import (
    ApiEvent,
    ApiMarket,
    ApiOrderbook,
    ApiTrade,
    WsTrade,
)
from market.contracts import (
    Event,
    Market,
    MarketStatus,
    PriceRange,
    Rules,
    SettlementResult,
    SettlementSource,
    Side,
    Strike,
    Trade,
)
from market.order_book import OrderBook
from market.timeutil import from_epoch_ms
from market.units import PRICE_SCALE, Price


def normalize_event(api: ApiEvent, raw: dict | None = None) -> Event:
    return Event(
        event_ticker=api.event_ticker,
        series_ticker=api.series_ticker,
        title=api.title,
        sub_title=api.sub_title,
        category=api.category,
        mutually_exclusive=api.mutually_exclusive,
        settlement_sources=tuple(SettlementSource(s.name, s.url) for s in api.settlement_sources),
        strike_period=api.strike_period or None,
        collateral_return_type=api.collateral_return_type or None,
        exchange_index=api.exchange_index,
        market_tickers=tuple(m.ticker for m in (api.markets or ())),
        raw=raw if raw is not None else api.model_dump(mode="json", exclude={"markets"}),
    )


def _settlement_value(api: ApiMarket) -> tuple[SettlementResult, Price | None]:
    try:
        result = SettlementResult.parse(api.result)
    except ValueError as exc:
        raise MessageValidationError(
            f"{api.ticker}: {exc}", raw=api.model_dump(mode="json")
        ) from exc
    reported = api.settlement_value_dollars
    if result is SettlementResult.UNSETTLED:
        return result, None
    if result is SettlementResult.SCALAR:
        if reported is None:
            raise MessageValidationError(f"{api.ticker}: scalar result without settlement value")
        return result, reported
    implied = PRICE_SCALE if result is SettlementResult.YES else 0
    if reported is not None and reported != implied:
        raise MessageValidationError(
            f"{api.ticker}: result={result.value} contradicts settlement_value={reported}"
        )
    return result, implied


def normalize_market(
    api: ApiMarket, *, event: Event | None = None, raw: dict | None = None
) -> Market:
    result, settlement_value = _settlement_value(api)
    return Market(
        ticker=api.ticker,
        event_ticker=api.event_ticker,
        series_ticker=event.series_ticker if event else None,
        category=event.category if event else None,
        title=api.title,
        yes_sub_title=api.yes_sub_title,
        no_sub_title=api.no_sub_title,
        market_type=api.market_type,
        status=MarketStatus.parse(api.status),
        strike=Strike(
            strike_type=api.strike_type,
            floor=api.floor_strike,
            cap=api.cap_strike,
            functional=api.functional_strike,
            custom=api.custom_strike,
        ),
        rules=Rules(
            primary=api.rules_primary,
            secondary=api.rules_secondary,
            early_close_condition=api.early_close_condition,
            can_close_early=api.can_close_early,
        ),
        open_time=api.open_time,
        close_time=api.close_time,
        expected_expiration_time=api.expected_expiration_time,
        expiration_time=api.latest_expiration_time or api.expiration_time,
        result=result,
        settlement_ts=api.settlement_ts,
        settlement_value=settlement_value,
        price_level_structure=api.price_level_structure,
        price_ranges=tuple(PriceRange(r.start, r.end, r.step) for r in api.price_ranges),
        exchange_index=api.exchange_index,
        is_multivariate=api.mve_collection_ticker is not None,
        updated_time=api.updated_time,
        volume=api.volume_fp,
        open_interest=api.open_interest_fp,
        raw=raw if raw is not None else api.model_dump(mode="json"),
    )


def _taker_side(taker_side: str | None, book_side: str | None, trade_id: str) -> Side | None:
    side = Side(taker_side) if taker_side in ("yes", "no") else None
    if book_side is not None:
        implied = {"bid": Side.YES, "ask": Side.NO}.get(book_side)
        if implied is None:
            raise MessageValidationError(f"trade {trade_id}: unknown taker_book_side {book_side!r}")
        if side is not None and side is not implied:
            raise MessageValidationError(
                f"trade {trade_id}: taker_side={side.value} contradicts taker_book_side={book_side}"
            )
        side = side or implied
    return side


def normalize_trade(api: ApiTrade) -> Trade:
    try:
        return Trade(
            trade_id=api.trade_id,
            ticker=api.ticker,
            yes_price=api.yes_price_dollars,
            no_price=api.no_price_dollars,
            count=api.count_fp,
            taker_side=_taker_side(api.taker_side, api.taker_book_side, api.trade_id),
            taker_book_side=api.taker_book_side,
            created_time=api.created_time,
            is_block_trade=api.is_block_trade,
        )
    except ValueError as exc:
        if isinstance(exc, MessageValidationError):
            raise
        raise MessageValidationError(str(exc), raw=api.model_dump(mode="json")) from exc


def normalize_ws_trade(msg: WsTrade) -> Trade:
    if msg.ts_ms is None:
        raise MessageValidationError(f"trade {msg.trade_id}: missing ts_ms", raw=msg.raw)
    try:
        return Trade(
            trade_id=msg.trade_id,
            ticker=msg.market_ticker,
            yes_price=msg.yes_price_dollars,
            no_price=msg.no_price_dollars,
            count=msg.count_fp,
            taker_side=_taker_side(msg.taker_side, msg.taker_book_side, msg.trade_id),
            taker_book_side=msg.taker_book_side,
            created_time=from_epoch_ms(msg.ts_ms),
            is_block_trade=msg.is_block_trade,
        )
    except ValueError as exc:
        if isinstance(exc, MessageValidationError):
            raise
        raise MessageValidationError(str(exc), raw=msg.raw) from exc


def orderbook_from_rest(ticker: str, api: ApiOrderbook, *, ts: datetime | None = None) -> OrderBook:
    book = OrderBook(ticker)
    book.apply_snapshot(api.orderbook_fp.yes_dollars, api.orderbook_fp.no_dollars)
    book.last_ts = ts
    return book
