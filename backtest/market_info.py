"""What a strategy may know about a market *before it settles*.

``MarketInfo`` is a **whitelist**, not a projection with things removed: a new field added to
``Market`` later stays invisible to strategies until someone deliberately exposes it here.

Deliberately hidden (each verified as a real leak in the collected data):

  close_time            differs from the schedule in 97% of settled markets - 2,414 of 2,988 closed
                        EARLY (doubles tennis ~216 min early): it reveals when the event ended
  expiration_time       a far-future placeholder (latest possible), uninformative
  status/result/settlement_*   known only at settlement
  volume/open_interest  lifetime totals measured after the fact
  updated_time, raw     bookkeeping that may embed any of the above

Exposed: static description and the *scheduled* end (``expected_expiration_time``).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from market.contracts import Market, PriceRange, Rules, Strike


@dataclass(frozen=True, slots=True)
class MarketInfo:
    ticker: str
    event_ticker: str
    series_ticker: str | None
    category: str | None
    title: str
    yes_sub_title: str
    no_sub_title: str
    strike: Strike
    rules: Rules
    open_time: datetime | None
    scheduled_end: datetime | None  # expected_expiration_time: the schedule, not the realised close
    price_ranges: tuple[PriceRange, ...]

    @classmethod
    def from_market(cls, m: Market) -> MarketInfo:
        return cls(
            ticker=m.ticker,
            event_ticker=m.event_ticker,
            series_ticker=m.series_ticker,
            category=m.category,
            title=m.title,
            yes_sub_title=m.yes_sub_title,
            no_sub_title=m.no_sub_title,
            strike=m.strike,
            rules=m.rules,
            open_time=m.open_time,
            scheduled_end=m.expected_expiration_time,
            price_ranges=m.price_ranges,
        )


#: Names a strategy must never be able to reach through ``MarketInfo``.
FORBIDDEN_FIELDS = frozenset(
    {
        "close_time",
        "expiration_time",
        "status",
        "result",
        "settlement_value",
        "settlement_ts",
        "volume",
        "open_interest",
        "updated_time",
        "raw",
        "is_settled",
    }
)
