"""Exact fixed-point units for prices and quantities.

Kalshi transmits prices as dollar strings with four decimals (``"0.0300"``) and
contract counts as strings with two decimals (``"75.00"``).  Floats are never used
for accounting: prices are integers in units of 1/10,000 dollar ("ticks") and
quantities are integers in units of 1/100 contract.

Parsing is *strict*: a value that does not land exactly on the grid raises
``InvalidUnitError`` instead of being silently rounded, so a schema change on the
exchange side surfaces as a loud failure rather than as corrupted data.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

PRICE_SCALE = 10_000  # ticks per $1 (== payout of one contract)
QTY_SCALE = 100  # centi-contracts per contract

Price = int
Qty = int


class InvalidUnitError(ValueError):
    """A price or quantity could not be represented exactly on the fixed-point grid."""


def _to_decimal(value: object) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, str | Decimal):
        # ints/floats are ambiguous (dollars? cents? ticks?) so they are rejected outright.
        raise InvalidUnitError(f"expected str or Decimal, got {type(value).__name__}: {value!r}")
    try:
        d = Decimal(value) if isinstance(value, str) else value
    except InvalidOperation as exc:
        raise InvalidUnitError(f"not a decimal number: {value!r}") from exc
    if not d.is_finite():
        raise InvalidUnitError(f"non-finite number: {value!r}")
    return d


def _scaled(value: object, scale: int, what: str) -> int:
    scaled = _to_decimal(value) * scale
    integral = scaled.to_integral_value()
    if scaled != integral:
        raise InvalidUnitError(f"{what} {value!r} is not exactly representable at 1/{scale} units")
    return int(integral)


def parse_price(value: object) -> Price:
    """Parse a dollar price in ``[0, 1]`` (e.g. ``"0.0300"``) into ticks."""
    ticks = _scaled(value, PRICE_SCALE, "price")
    if not 0 <= ticks <= PRICE_SCALE:
        raise InvalidUnitError(f"price {value!r} outside [0, 1]")
    return ticks


def parse_qty(value: object, *, allow_negative: bool = False) -> Qty:
    """Parse a contract count (e.g. ``"75.00"``) into centi-contracts."""
    q = _scaled(value, QTY_SCALE, "quantity")
    if q < 0 and not allow_negative:
        raise InvalidUnitError(f"negative quantity {value!r}")
    return q


def price_to_decimal(price: Price) -> Decimal:
    return Decimal(price) / PRICE_SCALE


def price_to_float(price: Price) -> float:
    """Lossy conversion for analytics only - never use for accounting."""
    return price / PRICE_SCALE


def qty_to_float(qty: Qty) -> float:
    return qty / QTY_SCALE


def format_price(price: Price) -> str:
    """Render ticks in the exchange's wire format, e.g. ``300 -> '0.0300'``."""
    return f"{price // PRICE_SCALE}.{price % PRICE_SCALE:04d}"


def format_qty(qty: Qty) -> str:
    sign = "-" if qty < 0 else ""
    q = abs(qty)
    return f"{sign}{q // QTY_SCALE}.{q % QTY_SCALE:02d}"


def complement(price: Price) -> Price:
    """Price of the opposite outcome: a YES bid at ``p`` is a NO ask at ``1 - p``."""
    return PRICE_SCALE - price
