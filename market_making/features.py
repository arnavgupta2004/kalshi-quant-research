"""Causal microstructure features for one market, computed incrementally from the event stream.

The SAME tracker serves the offline fit (replay a recorded feed, sample the features, regress the
subsequent mid change on them) and the live strategy (feed it the events the engine delivers), so
what the model was trained on is exactly what it sees when trading.  A feature at time ``t`` can
depend only on events delivered at or before ``t``: this is asserted by a prefix property test.

Features (dimensionless or in dollars of probability; ``m`` = book mid, ``s`` = spread):

  obi_top     (Qb - Qa) / (Qb + Qa)              queue imbalance, best bid vs best ask, in [-1, 1]
  obi_depth   the same over the best three levels a side
  micro_dev   microprice - m = (s/2) obi_top     dollars: the size-weighted touch minus the mid
  flow_60     S / (V + V0) over the last 60 s    signed taker volume: S = sum(+q if the taker bought
                                                 YES, -q if NO); V = volume; V0 shrinks a thin tape
  stale_dev   vwap(prints in the last 10 s newer than the last book) - m    dollars; 0 if none
  stale_out   the part of stale_dev outside the stale book's quotes:
              sign(d) max(0, |d| - s/2), d = stale_dev.  A print inside the spread is bid-ask
              bounce; a print beyond a stale quote means the price has moved through it
  ret_60      m - (the mid 60 s ago)             dollars; 0 if there is no history that old
  rv_60       sum |change in m| over the last 60 s   dollars: realised movement of the quoted price
  lv_10       ln(1 + contracts traded in the last 10 s)   trade intensity, short
  lv_60       ln(1 + contracts traded in the last 60 s)   trade intensity, long

``stale_dev`` deserves a warning.  Recorded books are polled every ~3 s, so a trade printed after
the last snapshot is information a LIVE feed would already show in the book.  The feature therefore
*corrects for the recorder's staleness* rather than adding a genuine signal; the study reports it
separately for that reason.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass

SEC = 1_000_000_000
PRICE_SCALE = 10_000
DIRECTION_FEATURES = (
    "obi_top", "obi_depth", "micro_dev", "flow_60", "stale_dev", "stale_out", "ret_60"
)  # fmt: skip
ACTIVITY_FEATURES = ("rv_60", "lv_10", "lv_60")
FEATURES = DIRECTION_FEATURES + ACTIVITY_FEATURES
BOOK_FEATURES = ("obi_top", "obi_depth", "micro_dev")
FLOW_WINDOW_S = 60
V0 = 20.0  # contracts: a tape with less volume than this is shrunk toward "no information"
RET_WINDOW_S = 60
STALE_WINDOW_S = 10  # prints older than this are not "newer than the book" in any useful sense
STALE_CAP = 0.15  # dollars: a print further than this from the mid is not treated as a price signal


@dataclass(frozen=True)
class Snapshot:
    """Everything the model and the strategy read at one instant, for one market."""

    ts: int
    bid: float  # YES best bid, dollars
    ask: float  # YES best ask, dollars
    mid: float
    spread: float
    x: tuple[float, ...]  # in FEATURES order

    def get(self, name: str) -> float:
        return self.x[FEATURES.index(name)]


class MarketTracker:
    """Maintains the state for one market; ``on_book`` / ``on_trade`` in time order."""

    def __init__(self) -> None:
        self.book_ts: int | None = None
        self._bid = self._ask = None  # ticks
        self._qb = self._qa = 0
        self._db = self._da = 0  # depth over three levels
        self._trades: deque[tuple[int, int, int, int]] = deque()  # (ts, yes_price, qty, sign)
        self._mids: deque[tuple[int, float]] = deque()
        self.n_trades = 0

    # ------------------------------------------------------------------ inputs
    def on_book(self, ts: int, yes_bids, no_bids) -> None:
        """``yes_bids`` / ``no_bids``: ((price_ticks, qty_centi), ...), best first."""
        self.book_ts = ts
        if not yes_bids or not no_bids:
            self._bid = self._ask = None
            return
        self._bid, self._qb = yes_bids[0]
        self._ask, self._qa = PRICE_SCALE - no_bids[0][0], no_bids[0][1]
        self._db = sum(q for _, q in yes_bids[:3])
        self._da = sum(q for _, q in no_bids[:3])
        mid = (self._bid + self._ask) / 2 / PRICE_SCALE
        self._mids.append((ts, mid))
        while len(self._mids) > 2 and self._mids[1][0] <= ts - 2 * RET_WINDOW_S * SEC:
            self._mids.popleft()

    def on_confirm(self, ts: int) -> None:
        """A poll found the book unchanged: it is current as of ``ts`` (levels untouched), so trades
        printed before ``ts`` are already reflected in it."""
        if self.book_ts is not None:
            self.book_ts = max(self.book_ts, ts)

    def on_trade(self, ts: int, yes_price: int, qty: int, taker_yes: int) -> None:
        """``taker_yes``: +1 if the taker bought YES, -1 if NO, 0 if unknown."""
        self._trades.append((ts, yes_price, qty, taker_yes))
        self.n_trades += 1
        while self._trades and self._trades[0][0] < ts - 2 * FLOW_WINDOW_S * SEC:
            self._trades.popleft()

    # ------------------------------------------------------------------ outputs
    def snapshot(self, now: int) -> Snapshot | None:
        """``None`` unless the last book is two-sided and uncrossed."""
        if self._bid is None or self._ask is None or self._ask <= self._bid:
            return None
        b, a = self._bid / PRICE_SCALE, self._ask / PRICE_SCALE
        mid, spread = (a + b) / 2, a - b
        obi_top = (self._qb - self._qa) / (self._qb + self._qa)
        obi_depth = (self._db - self._da) / (self._db + self._da)
        signed = vol = 0
        stale_q = stale_pq = 0
        for ts, px, q, sign in self._trades:
            if ts > now:
                break
            if ts > now - FLOW_WINDOW_S * SEC:
                signed += sign * q
                vol += q
            if self.book_ts is not None and ts > self.book_ts and ts > now - STALE_WINDOW_S * SEC:
                stale_q += q
                stale_pq += px * q
        stale = 0.0
        if stale_q:
            dev = stale_pq / stale_q / PRICE_SCALE - mid
            stale = dev if abs(dev) <= STALE_CAP else 0.0
        flow = (signed / 100.0) / (vol / 100.0 + V0)
        ret = 0.0
        old = None
        for ts, m in self._mids:
            if ts <= now - RET_WINDOW_S * SEC:
                old, old_ts = m, ts
            else:
                break
        # a reference older than two windows is a mid from before an outage, not a 60 s return
        if old is not None and now - old_ts <= 2 * RET_WINDOW_S * SEC:
            ret = mid - old
        rv, prev = 0.0, None
        for ts, m in self._mids:
            if ts > now:
                break
            if prev is not None and ts > now - RET_WINDOW_S * SEC:
                rv += abs(m - prev)
            prev = m
        v10 = sum(q for ts, _, q, _ in self._trades if now - 10 * SEC < ts <= now)
        x = (
            obi_top,
            obi_depth,
            spread * obi_top / 2,
            flow,
            stale,
            math.copysign(max(0.0, abs(stale) - spread / 2), stale),
            ret,
            rv,
            math.log1p(v10 / 100.0),
            math.log1p(vol / 100.0),
        )
        return Snapshot(now, b, a, mid, spread, x)
