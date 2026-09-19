"""Microstructure observables at a point in time: what the tape and the book say at instant ``t``.

Everything here is a pure function of *what was knowable at t* - the causality is enforced in one
place (``TradeTape.upto``) and property-tested: features computed from a tape truncated at ``t``
equal features computed from the full tape.

Conventions (Kalshi, YES-centric; prices are probabilities in [0, 1] after ``/ PRICE_SCALE``)

  * A trade with ``taker_side == YES`` bought YES at ``yes_price`` = it lifted the **ask**.
    A trade with ``taker_side == NO`` bought NO = sold YES at ``yes_price`` = it hit the **bid**.
    So the two sides of the tape are two noisy readings of the YES ask and bid.
  * **Information time.**  A print at exchange time ``s`` reaches a live consumer at ``s + delay``;
    the same 250 ms the backtest feed uses (``FeedTiming.trade_delay_ns``).  Nothing later is
    visible.
  * The book side of the market is optional: the large trade-history dataset has trades only, the
    small recorded datasets have both.  Every feature is ``None`` when it cannot be computed, never
    a guess.

Reference price (``reference_price``): the market's own point estimate that models start from - book
mid if a two-sided, fresh book exists; else the mid of the last opposite-side prints; else the last
trade.  Its *source* is returned with it, because "the market price" means different things in each
case and calibration differs by source.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from market.contracts import Side
from market.order_book import OrderBook
from market.units import PRICE_SCALE

SEC = 1_000_000_000
DEFAULT_DELAY_NS = 250_000_000
WINDOWS_S = (300, 3600)  # 5 minutes, 1 hour
FRESH_BOOK_S = 30.0  # a snapshot older than this is not "the book now"
TMID_MAX_AGE_S = 600.0  # both sides of the tape must have printed this recently to imply a mid


@dataclass(frozen=True)
class TradeTape:
    """One market's prints, time-ordered.  ``taker``: +1 bought YES, -1 bought NO, 0 unknown."""

    ts_ns: np.ndarray  # exchange time, int64
    yes_price: np.ndarray  # ticks
    qty: np.ndarray  # centi-contracts
    taker: np.ndarray  # int8

    def __post_init__(self) -> None:
        n = len(self.ts_ns)
        if not (len(self.yes_price) == len(self.qty) == len(self.taker) == n):
            raise ValueError("tape columns differ in length")
        if n > 1 and (np.diff(self.ts_ns) < 0).any():
            raise ValueError("tape must be time-ordered")

    @classmethod
    def from_rows(cls, rows) -> TradeTape:
        """rows: (ts_ns, yes_price, qty, taker_side_or_None) with taker_side 'yes' / 'no' / None."""
        rows = sorted(rows, key=lambda r: r[0])
        side = {"yes": 1, "no": -1}
        return cls(
            np.array([r[0] for r in rows], dtype=np.int64),
            np.array([r[1] for r in rows], dtype=np.int64),
            np.array([r[2] for r in rows], dtype=np.int64),
            np.array([side.get(getattr(r[3], "value", r[3]), 0) for r in rows], dtype=np.int8),
        )

    def __len__(self) -> int:
        return len(self.ts_ns)

    def upto(self, t_ns: int, delay_ns: int = DEFAULT_DELAY_NS) -> int:
        """Number of prints visible at ``t_ns``: exchange time + delay <= t.  The only place the
        causality rule is applied."""
        return int(np.searchsorted(self.ts_ns, t_ns - delay_ns, side="right"))


@dataclass(frozen=True)
class TradeFeatures:
    n_seen: int
    last_price: float | None  # probability
    secs_since_last: float | None
    vwap: dict[int, float | None]  # window seconds -> volume-weighted YES price
    flow: dict[int, float | None]  # (YES-taker - NO-taker volume) / volume, in [-1, 1]
    volume: dict[int, float]  # contracts in the window
    ret: dict[int, float | None]  # last price - price as of (t - window)
    tmid: float | None  # mid of the last ask-side and bid-side prints
    tspread: (
        float | None
    )  # ask-side print - bid-side print (may be negative: prints are not simultaneous)


def trade_features(
    tape: TradeTape, t_ns: int, *, delay_ns: int = DEFAULT_DELAY_NS, windows_s=WINDOWS_S
) -> TradeFeatures:
    n = tape.upto(t_ns, delay_ns)
    if n == 0:
        return TradeFeatures(
            0, None, None, dict.fromkeys(windows_s), dict.fromkeys(windows_s),
            dict.fromkeys(windows_s, 0.0), dict.fromkeys(windows_s), None, None,
        )  # fmt: skip
    ts, px, q, tk = tape.ts_ns[:n], tape.yes_price[:n], tape.qty[:n], tape.taker[:n]
    now = t_ns - delay_ns  # visible-time frame: a print's age is measured from its exchange time
    vwap: dict[int, float | None] = {}
    flow: dict[int, float | None] = {}
    vol: dict[int, float] = {}
    ret: dict[int, float | None] = {}
    last = float(px[-1]) / PRICE_SCALE
    for w in windows_s:
        lo = int(np.searchsorted(ts, now - w * SEC, side="left"))
        qs = q[lo:]
        total = float(qs.sum())
        vol[w] = total / 100.0
        if total > 0:
            vwap[w] = float((px[lo:] * qs).sum()) / total / PRICE_SCALE
            signed = float((tk[lo:].astype(np.int64) * qs).sum())
            known = float(qs[tk[lo:] != 0].sum())
            flow[w] = signed / known if known > 0 else None
        else:
            vwap[w] = flow[w] = None
        j = int(np.searchsorted(ts, now - w * SEC, side="right")) - 1
        ret[w] = last - float(px[j]) / PRICE_SCALE if j >= 0 else None
    ask_i = np.flatnonzero(tk == 1)
    bid_i = np.flatnonzero(tk == -1)
    tmid = tspread = None
    if len(ask_i) and len(bid_i):
        a, b = int(ask_i[-1]), int(bid_i[-1])
        if (now - ts[a]) <= TMID_MAX_AGE_S * SEC and (now - ts[b]) <= TMID_MAX_AGE_S * SEC:
            tmid = (float(px[a]) + float(px[b])) / 2 / PRICE_SCALE
            tspread = (float(px[a]) - float(px[b])) / PRICE_SCALE
    return TradeFeatures(n, last, (now - int(ts[-1])) / SEC, vwap, flow, vol, ret, tmid, tspread)


@dataclass(frozen=True)
class BookFeatures:
    bid: float
    ask: float
    mid: float
    spread: float
    microprice: float  # (bid * ask_size + ask * bid_size) / (bid_size + ask_size)
    imb_top: float  # (bid_size - ask_size) / (bid_size + ask_size), YES-centric, in [-1, 1]
    imb_depth: float  # the same over the best three levels each side
    log_depth: float  # ln(1 + contracts resting within three levels, both sides)


def book_features(book: OrderBook | None, age_s: float | None = 0.0) -> BookFeatures | None:
    """``None`` unless the book is fresh and quoted on both sides."""
    if book is None or age_s is None or age_s > FRESH_BOOK_S:
        return None
    bid, ask = book.best_bid(), book.best_ask()
    if bid is None or ask is None:
        return None
    b, a = bid.price / PRICE_SCALE, ask.price / PRICE_SCALE
    qb, qa = bid.qty, ask.qty
    yes_lv, ask_lv = book.bids(Side.YES)[:3], book.asks(Side.YES)[:3]
    db, da = sum(lv.qty for lv in yes_lv), sum(lv.qty for lv in ask_lv)
    return BookFeatures(
        bid=b,
        ask=a,
        mid=(a + b) / 2,
        spread=a - b,
        microprice=(b * qa + a * qb) / (qa + qb),
        imb_top=(qb - qa) / (qb + qa),
        imb_depth=(db - da) / (db + da),
        log_depth=math.log1p((db + da) / 100.0),
    )


def reference_price(book: BookFeatures | None, trades: TradeFeatures) -> tuple[float | None, str]:
    """The market's own point estimate and its source: 'book_mid' | 'trade_mid' | 'last_trade'."""
    if book is not None:
        return book.mid, "book_mid"
    if trades.tmid is not None:
        return trades.tmid, "trade_mid"
    if trades.last_price is not None:
        return trades.last_price, "last_trade"
    return None, "none"


def hours_to_expiry(scheduled_end_ns: int | None, t_ns: int) -> float | None:
    """Hours until the *scheduled* end (may be negative when the game runs long).  The scheduled end
    is public at t; the realised close time is not and is never used here."""
    return None if scheduled_end_ns is None else (scheduled_end_ns - t_ns) / (3600 * SEC)
