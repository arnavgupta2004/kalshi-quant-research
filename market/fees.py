"""Kalshi trading fees, exact.

Money is integer **micro-dollars** (µ$ = 1e-6 $).  With prices in ticks (1e-4 $) and quantities in
centi-contracts (1e-2), ``price_ticks * qty_centi`` is already µ$ - so cost, payoff and fees add up
with no rounding error anywhere.

The published model (verified against the docs' own worked example, ``tests/test_fees.py``):

    fee = coefficient x multiplier x C x P x (1 - P)         C = contracts, P = price in dollars
    taker coefficient = 0.07;   maker coefficient = 0.0175 only for ``quadratic_with_maker_fees``

and ``trade_fee = ceil(fee to 6 decimals)`` **per fill**.  Whole-cent rounding is a *balance*
artefact: the excess accumulates per order and is rebated in whole increments, so it is not an
extra cost on average.  Because the exact rebate mechanics are not reproducible offline, three
rounding models are provided and every result can be re-run under each as a sensitivity check:

    EXACT_6DP       the documented trade_fee (default)
    CENT_PER_FILL   each fill rounded up to a whole cent  (conservative upper bound)
    CENT_PER_ORDER  the order's exact total rounded up to a cent

Fees are per *series*: ``fee_type``/``fee_multiplier`` come from ``GET /series/{ticker}``.  When a
series' fee is unknown the schedule falls back to the standard taker fee and is flagged
``known=False`` so results can say the fee was assumed.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from fractions import Fraction

from market.units import PRICE_SCALE, Price, Qty

MICRO_PER_DOLLAR = 1_000_000
CENT_MICRO = 10_000

TAKER_COEFFICIENT = Fraction(7, 100)
MAKER_COEFFICIENT = Fraction(7, 400)  # 0.0175
KNOWN_FEE_TYPES = ("quadratic", "quadratic_with_maker_fees")


class Rounding(StrEnum):
    EXACT_6DP = "exact_6dp"
    CENT_PER_FILL = "cent_per_fill"
    CENT_PER_ORDER = "cent_per_order"


def _ceil_div(n: int, d: int) -> int:
    return -((-n) // d)


@dataclass(frozen=True, slots=True)
class FeeSchedule:
    fee_type: str = "quadratic"
    multiplier: Fraction = Fraction(1)
    rounding: Rounding = Rounding.EXACT_6DP
    known: bool = True  # False: metadata was missing/unrecognised and the standard fee is assumed

    @classmethod
    def from_series(
        cls,
        fee_type: str | None,
        multiplier: str | Decimal | float | None,
        rounding: Rounding = Rounding.EXACT_6DP,
    ) -> FeeSchedule:
        mult = Fraction(str(multiplier)) if multiplier not in (None, "") else Fraction(1)
        if fee_type in KNOWN_FEE_TYPES and multiplier not in (None, ""):
            return cls(fee_type, mult, rounding, True)
        return cls("quadratic", mult, rounding, False)

    def with_rounding(self, rounding: Rounding) -> FeeSchedule:
        return FeeSchedule(self.fee_type, self.multiplier, rounding, self.known)

    def coefficient(self, *, maker: bool = False) -> Fraction:
        if maker:
            return (
                MAKER_COEFFICIENT * self.multiplier
                if self.fee_type == "quadratic_with_maker_fees"
                else Fraction(0)
            )
        return TAKER_COEFFICIENT * self.multiplier

    def exact_fee_micro(self, price: Price, qty: Qty, *, maker: bool = False) -> Fraction:
        """The unrounded model fee for one fill, in µ$."""
        # coeff * (qty/100) * (price/S) * ((S-price)/S) dollars  ->  x 1e6 for µ$
        return (
            self.coefficient(maker=maker)
            * qty
            * price
            * (PRICE_SCALE - price)
            * MICRO_PER_DOLLAR
            / (100 * PRICE_SCALE * PRICE_SCALE)
        )

    def fee_micro(self, price: Price, qty: Qty, *, maker: bool = False) -> int:
        """Fee for one fill under this schedule's rounding model (µ$)."""
        exact = self.exact_fee_micro(price, qty, maker=maker)
        if self.rounding is Rounding.CENT_PER_FILL:
            return _ceil_div(math.ceil(exact), CENT_MICRO) * CENT_MICRO
        return math.ceil(exact)  # EXACT_6DP; CENT_PER_ORDER is settled in order_fee_micro

    def order_fee_micro(self, fills: Sequence[tuple[Price, Qty]], *, maker: bool = False) -> int:
        """Fee for one order that filled as ``fills`` = [(price, qty), ...]."""
        if self.rounding is Rounding.CENT_PER_ORDER:
            total = sum((self.exact_fee_micro(p, q, maker=maker) for p, q in fills), Fraction(0))
            return _ceil_div(math.ceil(total), CENT_MICRO) * CENT_MICRO
        return sum(self.fee_micro(p, q, maker=maker) for p, q in fills)

    def marginal_fee_micro_per_centi(self, price: Price) -> Fraction:
        """Unrounded fee per centi-contract at ``price`` (µ$)."""
        return self.exact_fee_micro(price, 1)


def series_of_ticker(ticker: str) -> str:
    return ticker.split("-", 1)[0]


@dataclass(frozen=True)
class FeeBook:
    """Resolves the schedule for any market ticker through its series."""

    by_series: dict[str, FeeSchedule] = field(default_factory=dict)
    default: FeeSchedule = field(default_factory=lambda: FeeSchedule(known=False))

    def for_ticker(self, ticker: str) -> FeeSchedule:
        return self.by_series.get(series_of_ticker(ticker), self.default)

    @classmethod
    def from_series_meta(
        cls, meta: dict[str, tuple[str | None, str | None]], rounding: Rounding = Rounding.EXACT_6DP
    ) -> FeeBook:
        return cls(
            {s: FeeSchedule.from_series(ft, m, rounding) for s, (ft, m) in meta.items()},
            FeeSchedule(rounding=rounding, known=False),
        )

    def with_rounding(self, rounding: Rounding) -> FeeBook:
        return FeeBook(
            {s: f.with_rounding(rounding) for s, f in self.by_series.items()},
            self.default.with_rounding(rounding),
        )

    def scaled(self, factor: Fraction) -> FeeBook:
        """Sensitivity: every fee multiplied by ``factor`` (e.g. 0 = fee-free, 2 = double)."""
        return FeeBook(
            {
                s: FeeSchedule(f.fee_type, f.multiplier * factor, f.rounding, f.known)
                for s, f in self.by_series.items()
            },
            FeeSchedule(
                self.default.fee_type,
                self.default.multiplier * factor,
                self.default.rounding,
                self.default.known,
            ),
        )


FeeFn = Callable[[str], FeeSchedule]
