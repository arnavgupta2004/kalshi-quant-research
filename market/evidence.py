"""Empirical support for relations: how often did settled events of a series break them?

A relation the exchange merely *declares* (``mutually_exclusive``) or that cannot be proven from
strikes (three-way soccer results are exhaustive only because "tie" exists) is checked against
history: for every settled event, how many of its markets resolved YES?

    0 YES  -> the outcomes were not exhaustive (or the event was void)
    1 YES  -> consistent with a partition
    2+ YES -> the outcomes were not mutually exclusive

Events containing a ``scalar`` (fractional / void) settlement are counted separately as
*resolution risk* and excluded from the clean counts.  Rates are reported with one-sided exact
(Clopper-Pearson) upper bounds: "0 violations in 40 events" still allows a rate of ~7%.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace

from market.contracts import Market, SettlementResult


def clopper_pearson_upper(k: int, n: int, confidence: float = 0.95) -> float:
    """One-sided upper confidence bound for a binomial proportion, k successes in n trials.

    The smallest p with P(X <= k | n, p) <= 1 - confidence, found by bisection on the exact cdf
    (no scipy needed at runtime)."""
    if n <= 0:
        return 1.0
    if k >= n:
        return 1.0
    alpha = 1.0 - confidence

    def cdf(p: float) -> float:
        if p <= 0:
            return 1.0
        if p >= 1:
            return 0.0
        lp, lq = math.log(p), math.log1p(-p)
        total = 0.0
        for i in range(k + 1):
            total += math.exp(
                math.lgamma(n + 1)
                - math.lgamma(i + 1)
                - math.lgamma(n - i + 1)
                + i * lp
                + (n - i) * lq
            )
        return total

    lo, hi = 0.0, 1.0
    for _ in range(80):
        mid = (lo + hi) / 2
        if cdf(mid) > alpha:
            lo = mid  # cdf still too large -> the bound is higher
        else:
            hi = mid
    return hi


@dataclass
class SeriesStats:
    series: str
    n_events: int = 0  # settled events observed
    n_scalar: int = 0  # ... containing a fractional/void settlement (resolution risk)
    n_clean: int = 0  # ... where every market resolved plainly yes/no
    zero_yes: int = 0
    one_yes: int = 0
    multi_yes: int = 0

    def add(self, markets: Sequence[Market]) -> bool:
        """Record one event; returns False (and records nothing) if it is not fully settled."""
        if not markets or any(m.settlement_value is None for m in markets):
            return False
        self.n_events += 1
        if any(m.result is SettlementResult.SCALAR for m in markets):
            self.n_scalar += 1
            return True
        self.n_clean += 1
        n_yes = sum(m.result is SettlementResult.YES for m in markets)
        if n_yes == 0:
            self.zero_yes += 1
        elif n_yes == 1:
            self.one_yes += 1
        else:
            self.multi_yes += 1
        return True

    def remove(self, markets: Sequence[Market]) -> bool:
        """Exact inverse of ``add`` (leave-one-out: judge an event by *other* events' history)."""
        if not markets or any(m.settlement_value is None for m in markets):
            return False
        self.n_events -= 1
        if any(m.result is SettlementResult.SCALAR for m in markets):
            self.n_scalar -= 1
            return True
        self.n_clean -= 1
        n_yes = sum(m.result is SettlementResult.YES for m in markets)
        if n_yes == 0:
            self.zero_yes -= 1
        elif n_yes == 1:
            self.one_yes -= 1
        else:
            self.multi_yes -= 1
        return True

    def multi_yes_upper(self, confidence: float = 0.95) -> float:
        return clopper_pearson_upper(self.multi_yes, self.n_clean, confidence)

    def zero_yes_upper(self, confidence: float = 0.95) -> float:
        return clopper_pearson_upper(self.zero_yes, self.n_clean, confidence)

    @property
    def scalar_rate(self) -> float:
        return self.n_scalar / self.n_events if self.n_events else 0.0


class StatsBook:
    """Per-series outcome statistics."""

    def __init__(self) -> None:
        self._by_series: dict[str, SeriesStats] = {}

    def add_event(self, series: str, markets: Sequence[Market]) -> bool:
        return self._by_series.setdefault(series, SeriesStats(series)).add(markets)

    def get(self, series: str) -> SeriesStats | None:
        return self._by_series.get(series)

    def without(self, series: str, markets: Sequence[Market]) -> StatsBook:
        """A view of this book with one event's contribution removed."""
        view = StatsBook()
        view._by_series = dict(self._by_series)
        if (s := self._by_series.get(series)) is not None:
            copy = replace(s)
            copy.remove(markets)
            view._by_series[series] = copy
        return view

    def __iter__(self):
        return iter(self._by_series.values())

    def __len__(self) -> int:
        return len(self._by_series)

    @classmethod
    def from_events(cls, events: Iterable[tuple[str, Sequence[Market]]]) -> StatsBook:
        book = cls()
        for series, markets in events:
            book.add_event(series, markets)
        return book
