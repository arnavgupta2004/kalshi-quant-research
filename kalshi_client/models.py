"""Typed wire models for Kalshi REST responses and WebSocket messages.

These validate *what the exchange sent* (schema + exact units + UTC timestamps).  Mapping
them onto the exchange-agnostic domain objects happens in ``data.normalization``.

Design rules
  * Prices/quantities are parsed to integer ticks/centi-contracts at the boundary.
  * The exchange uses ``""`` for "absent" in many fields; that becomes ``None``.
  * Unknown extra fields are tolerated (forward compatibility) but required fields and
    units are strict: violations raise ``MessageValidationError`` carrying the raw payload.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any, Literal, TypeVar

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, ValidationError

from kalshi_client.exceptions import MessageValidationError
from market.contracts import Side
from market.timeutil import from_epoch_ms, from_epoch_seconds, parse_iso8601
from market.units import parse_price, parse_qty

# --------------------------------------------------------------------------- field types


def _blank_to_none(fn: Callable[[Any], Any]) -> Callable[[Any], Any]:
    return lambda v: None if v is None or v == "" else fn(v)


Px = Annotated[int, BeforeValidator(parse_price)]
OptPx = Annotated[int | None, BeforeValidator(_blank_to_none(parse_price))]
Cnt = Annotated[int, BeforeValidator(parse_qty)]
SignedCnt = Annotated[int, BeforeValidator(lambda v: parse_qty(v, allow_negative=True))]
OptCnt = Annotated[int | None, BeforeValidator(_blank_to_none(parse_qty))]
OptTs = Annotated[datetime | None, BeforeValidator(_blank_to_none(parse_iso8601))]
Ts = Annotated[datetime, BeforeValidator(parse_iso8601)]
OptDollars = Annotated[Decimal | None, BeforeValidator(_blank_to_none(Decimal))]
PriceLevel = tuple[Px, Cnt]


class _Wire(BaseModel):
    model_config = ConfigDict(extra="allow", frozen=True)


# --------------------------------------------------------------------------- REST models


class ApiPriceRange(_Wire):
    start: Px
    end: Px
    step: Px


class ApiMarket(_Wire):
    ticker: str
    event_ticker: str
    status: str
    market_type: str = "binary"
    title: str = ""
    yes_sub_title: str = ""
    no_sub_title: str = ""
    rules_primary: str = ""
    rules_secondary: str = ""
    early_close_condition: str | None = None
    can_close_early: bool = False

    strike_type: str | None = None
    floor_strike: float | None = None
    cap_strike: float | None = None
    functional_strike: str | None = None
    custom_strike: dict[str, Any] | None = None

    created_time: OptTs = None
    updated_time: OptTs = None
    open_time: OptTs = None
    close_time: OptTs = None
    expected_expiration_time: OptTs = None
    expiration_time: OptTs = None
    latest_expiration_time: OptTs = None
    settlement_ts: OptTs = None

    result: str = ""
    settlement_value_dollars: OptPx = None

    price_level_structure: str | None = None
    price_ranges: list[ApiPriceRange] = Field(default_factory=list)
    exchange_index: int | None = None
    mve_collection_ticker: str | None = None

    # top-of-book / activity summary carried on the market object (a *snapshot*, not the book)
    yes_bid_dollars: OptPx = None
    yes_ask_dollars: OptPx = None
    no_bid_dollars: OptPx = None
    no_ask_dollars: OptPx = None
    yes_bid_size_fp: OptCnt = None
    yes_ask_size_fp: OptCnt = None
    last_price_dollars: OptPx = None
    volume_fp: OptCnt = None
    volume_24h_fp: OptCnt = None
    open_interest_fp: OptCnt = None
    liquidity_dollars: OptDollars = None
    notional_value_dollars: OptPx = None


class ApiSettlementSource(_Wire):
    name: str
    url: str | None = None


class ApiEvent(_Wire):
    event_ticker: str
    series_ticker: str | None = None
    title: str = ""
    sub_title: str = ""
    category: str | None = None
    mutually_exclusive: bool = False
    strike_period: str | None = None
    collateral_return_type: str | None = None
    exchange_index: int | None = None
    settlement_sources: list[ApiSettlementSource] = Field(default_factory=list)
    markets: list[ApiMarket] | None = None


class ApiTrade(_Wire):
    trade_id: str
    ticker: str
    created_time: Ts
    yes_price_dollars: Px
    no_price_dollars: Px
    count_fp: Cnt
    taker_side: str | None = None
    taker_outcome_side: str | None = None
    taker_book_side: str | None = None
    is_block_trade: bool = False


class ApiOrderbookFp(_Wire):
    yes_dollars: list[PriceLevel] = Field(default_factory=list)
    no_dollars: list[PriceLevel] = Field(default_factory=list)


class ApiOrderbook(_Wire):
    """``GET /markets/{ticker}/orderbook`` - bids only, both sides, *not* guaranteed sorted."""

    orderbook_fp: ApiOrderbookFp
    ticker: str | None = None


# --------------------------------------------------------------------------- WebSocket models


class _WsMsg(_Wire):
    """Envelope fields (from the frame) merged with the payload fields (from ``msg``)."""

    type: str
    id: int | None = None
    sid: int | None = None
    seq: int | None = None
    ts_ms: int | None = None
    #: which connection generation delivered this frame; sids/seqs are only meaningful per epoch
    epoch: int = 0
    #: local wall-clock receive time (ns) - kept separate from exchange time, never mixed
    recv_ts_ns: int = 0
    raw: str = Field(default="", repr=False)

    @property
    def exchange_ts(self) -> datetime | None:
        return None if self.ts_ms is None else from_epoch_ms(self.ts_ms)


class WsSubscribed(_WsMsg):
    type: Literal["subscribed"] = "subscribed"
    channel: str | None = None


class WsOk(_WsMsg):
    type: Literal["ok"] = "ok"


class WsUnsubscribed(_WsMsg):
    type: Literal["unsubscribed"] = "unsubscribed"


class WsError(_WsMsg):
    type: Literal["error"] = "error"
    code: int | None = None
    msg: str = ""


class WsOrderbookSnapshot(_WsMsg):
    type: Literal["orderbook_snapshot"] = "orderbook_snapshot"
    market_ticker: str
    yes_dollars_fp: list[PriceLevel] = Field(default_factory=list)
    no_dollars_fp: list[PriceLevel] = Field(default_factory=list)


class WsOrderbookDelta(_WsMsg):
    type: Literal["orderbook_delta"] = "orderbook_delta"
    market_ticker: str
    price_dollars: Px
    delta_fp: SignedCnt
    side: Side


class WsTicker(_WsMsg):
    type: Literal["ticker"] = "ticker"
    market_ticker: str
    price_dollars: OptPx = None
    yes_bid_dollars: OptPx = None
    yes_ask_dollars: OptPx = None
    volume_fp: OptCnt = None
    open_interest_fp: OptCnt = None
    yes_bid_size_fp: OptCnt = None
    yes_ask_size_fp: OptCnt = None
    last_trade_size_fp: OptCnt = None


class WsTrade(_WsMsg):
    type: Literal["trade"] = "trade"
    trade_id: str
    market_ticker: str
    yes_price_dollars: Px
    no_price_dollars: Px
    count_fp: Cnt
    taker_side: str | None = None
    taker_outcome_side: str | None = None
    taker_book_side: str | None = None
    is_block_trade: bool = False


class WsMarketLifecycle(_WsMsg):
    type: Literal["market_lifecycle_v2"] = "market_lifecycle_v2"
    market_ticker: str
    event_type: str | None = None
    open_ts: int | None = None
    close_ts: int | None = None
    determination_ts: int | None = None
    settled_ts: int | None = None
    result: str | None = None
    settlement_value: str | None = None
    is_deactivated: bool | None = None
    price_level_structure: str | None = None
    additional_metadata: dict[str, Any] | None = None

    def ts(self, name: str) -> datetime | None:
        v = getattr(self, name)
        return None if v is None else from_epoch_seconds(v)


class WsEventLifecycle(_WsMsg):
    type: Literal["event_lifecycle"] = "event_lifecycle"
    event_ticker: str
    title: str | None = None
    series_ticker: str | None = None


class WsUnknown(_WsMsg):
    """A well-formed frame of a type we do not model - preserved, never dropped."""


WsMessage = (
    WsSubscribed
    | WsOk
    | WsUnsubscribed
    | WsError
    | WsOrderbookSnapshot
    | WsOrderbookDelta
    | WsTicker
    | WsTrade
    | WsMarketLifecycle
    | WsEventLifecycle
    | WsUnknown
)

_WS_MODELS: dict[str, type[_WsMsg]] = {
    "subscribed": WsSubscribed,
    "ok": WsOk,
    "unsubscribed": WsUnsubscribed,
    "error": WsError,
    "orderbook_snapshot": WsOrderbookSnapshot,
    "orderbook_delta": WsOrderbookDelta,
    "ticker": WsTicker,
    "trade": WsTrade,
    "market_lifecycle_v2": WsMarketLifecycle,
    "event_lifecycle": WsEventLifecycle,
}


def parse_ws_message(raw: str | bytes, *, recv_ts_ns: int = 0, epoch: int = 0) -> WsMessage:
    """Validate one WebSocket text frame.  Raises ``MessageValidationError`` if malformed."""
    text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as exc:
        raise MessageValidationError(f"invalid JSON: {exc}", raw=text) from exc
    if not isinstance(obj, dict) or not isinstance(obj.get("type"), str):
        raise MessageValidationError("frame is not an object with a string 'type'", raw=text)
    body = obj.get("msg", {})
    if body is None:
        body = {}
    if not isinstance(body, dict):
        raise MessageValidationError("'msg' is not an object", raw=text)
    for key in ("sid", "seq", "id"):
        if key in obj and not isinstance(obj[key], int):
            raise MessageValidationError(f"envelope field {key!r} is not an integer", raw=text)
    envelope = {k: obj[k] for k in ("id", "sid", "seq") if obj.get(k) is not None}
    merged = {
        **envelope,
        **body,
        "type": obj["type"],
        "epoch": epoch,
        "recv_ts_ns": recv_ts_ns,
        "raw": text,
    }
    model = _WS_MODELS.get(obj["type"], WsUnknown)
    try:
        return model.model_validate(merged)
    except ValidationError as exc:
        raise MessageValidationError(f"{obj['type']}: {exc}", raw=text) from exc


# --------------------------------------------------------------------------- REST helpers

M = TypeVar("M", bound=BaseModel)


def validate_rest(model: type[M], payload: Any, *, what: str = "") -> M:
    try:
        return model.model_validate(payload)
    except ValidationError as exc:
        raise MessageValidationError(f"{what or model.__name__}: {exc}", raw=payload) from exc


__all__ = [
    "ApiEvent",
    "ApiMarket",
    "ApiOrderbook",
    "ApiOrderbookFp",
    "ApiPriceRange",
    "ApiSettlementSource",
    "ApiTrade",
    "WsError",
    "WsEventLifecycle",
    "WsMarketLifecycle",
    "WsMessage",
    "WsOk",
    "WsOrderbookDelta",
    "WsOrderbookSnapshot",
    "WsSubscribed",
    "WsTicker",
    "WsTrade",
    "WsUnknown",
    "WsUnsubscribed",
    "parse_ws_message",
    "validate_rest",
]
