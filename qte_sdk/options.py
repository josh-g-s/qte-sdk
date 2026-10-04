"""Option contract symbols.

The exchange names an option contract by its OCC option symbol, in the unpadded form
Alpaca uses: the underlying's root, the expiry as YYMMDD, `C` for a call or `P` for a
put, and the strike in thousandths of a dollar as eight digits. Its length varies with
the root, and it holds no spaces:

    >>> from qte_sdk.units import to_micros
    >>> option_symbol("SPY", date(2024, 1, 19), CALL, to_micros("470"))
    'SPY240119C00470000'
    >>> parse_option_symbol("SPY240119C00470000")
    OptionSymbol(underlying='SPY', expiry=datetime.date(2024, 1, 19), right='C', strike=470000000)

The strike is in micro-dollars, like every price in this SDK, so it can be compared with
prices from the exchange without conversion; a symbol can carry only whole thousandths of
a dollar, so a strike that is not a multiple of 1000 micro-dollars has no symbol.

`is_option_symbol` tells an option contract's id from an equity's, for example in an
account's positions, where both appear in `PositionValue.instrument`.

This module covers the symbol only. How option chains, Greeks, option books and option
orders travel on the wire is not part of the exchange's published contract yet, and this
SDK adds them once it is.
"""

import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal

__all__ = [
    "CALL",
    "PUT",
    "OptionSymbol",
    "is_option_symbol",
    "option_symbol",
    "parse_option_symbol",
]

CALL: Literal["C"] = "C"
PUT: Literal["P"] = "P"

# The OCC strike field: eight digits of thousandths of a dollar.
_MICROS_PER_STRIKE_UNIT = 1_000
_STRIKE_DIGITS = 8
_MAX_STRIKE_UNITS = 10**_STRIKE_DIGITS - 1

# A root of one to six upper-case letters or digits, starting with a letter, then the
# fixed fifteen-character suffix. The suffix has a fixed shape, so the root is whatever
# comes before it.
_ROOT = re.compile(r"[A-Z][A-Z0-9]{0,5}")
_SYMBOL = re.compile(r"([A-Z][A-Z0-9]{0,5})([0-9]{2})([0-9]{2})([0-9]{2})([CP])([0-9]{8})")


@dataclass(frozen=True)
class OptionSymbol:
    """The parts of an option contract's symbol. `strike` is in micro-dollars."""

    underlying: str
    expiry: date
    right: Literal["C", "P"]
    strike: int

    def __str__(self) -> str:
        return option_symbol(self.underlying, self.expiry, self.right, self.strike)


def option_symbol(underlying: str, expiry: date, right: str, strike: int) -> str:
    """The symbol of an option contract: `underlying` is the root, such as "SPY",
    `expiry` the expiration date, `right` `CALL` ("C") or `PUT` ("P"), and `strike` the
    strike in micro-dollars, a whole number of thousandths of a dollar from 0.001 to
    99999.999. Raises `ValueError` for anything that has no symbol."""
    if not isinstance(underlying, str) or not _ROOT.fullmatch(underlying):
        raise ValueError(
            "the underlying must be 1 to 6 upper-case letters or digits, starting with a letter"
        )
    if isinstance(expiry, datetime) or not isinstance(expiry, date):
        # A datetime is a date too, but its date can differ between UTC and New York.
        raise ValueError("the expiry must be a datetime.date, not a datetime")
    if not 2000 <= expiry.year <= 2099:
        raise ValueError("the expiry year must be between 2000 and 2099")
    if right not in (CALL, PUT):
        raise ValueError('the right must be "C" (CALL) or "P" (PUT)')
    units = _strike_units(strike)
    return f"{underlying}{expiry:%y%m%d}{right}{units:0{_STRIKE_DIGITS}d}"


def parse_option_symbol(symbol: str) -> OptionSymbol:
    """The parts of an option contract's symbol, such as "SPY240119C00470000". Raises
    `ValueError` if `symbol` is not one."""
    if not isinstance(symbol, str):
        raise ValueError("an option symbol must be a string")
    match = _SYMBOL.fullmatch(symbol)
    if match is None:
        raise ValueError(
            "not an option symbol: expected a root of 1 to 6 upper-case letters or digits, "
            "then YYMMDD, C or P, and an 8-digit strike in thousandths of a dollar"
        )
    root, yy, mm, dd, right, strike = match.groups()
    try:
        expiry = date(2000 + int(yy), int(mm), int(dd))
    except ValueError:
        raise ValueError("not an option symbol: its expiry is not a real date") from None
    units = int(strike)
    if units == 0:
        raise ValueError("not an option symbol: its strike is zero")
    return OptionSymbol(root, expiry, right, units * _MICROS_PER_STRIKE_UNIT)  # type: ignore[arg-type]


def is_option_symbol(instrument: str) -> bool:
    """True if `instrument` is an option contract's symbol rather than an equity's."""
    try:
        parse_option_symbol(instrument)
    except ValueError:
        return False
    return True


def _strike_units(strike: int) -> int:
    if isinstance(strike, bool) or not isinstance(strike, int):
        raise ValueError(
            "the strike must be an int of micro-dollars; use qte_sdk.units.to_micros to "
            'convert, such as to_micros("470")'
        )
    if strike % _MICROS_PER_STRIKE_UNIT:
        raise ValueError("the strike must be a whole number of thousandths of a dollar")
    units = strike // _MICROS_PER_STRIKE_UNIT
    if not 1 <= units <= _MAX_STRIKE_UNITS:
        raise ValueError("the strike must be from 0.001 to 99999.999 dollars")
    return units
