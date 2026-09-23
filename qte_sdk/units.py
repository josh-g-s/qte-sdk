"""Exact conversion between wire prices and `Decimal` dollars.

Every price on the wire is a whole number of micro-dollars (10^-6 USD) in a signed 64-bit
integer, and every quantity is a whole number of shares. The generated message classes
already hold both as Python `int`, which is exact at any size. These helpers convert a
price to and from `Decimal` dollars without ever passing through `float`, which cannot
represent most prices exactly and loses whole micro-dollars above 2**53.

    >>> to_decimal(200_011_000)
    Decimal('200.011000')
    >>> to_micros("200.011")
    200011000

Quantities need no conversion: use them as they are.
"""

from decimal import Decimal, InvalidOperation

MICROS_PER_DOLLAR = 1_000_000
INT64_MIN = -(2**63)
INT64_MAX = 2**63 - 1


def to_decimal(micros: int) -> Decimal:
    """A price in micro-dollars as exact `Decimal` dollars, always with six decimal places."""
    if isinstance(micros, bool) or not isinstance(micros, int):
        raise TypeError(f"a price in micro-dollars is an int, not {type(micros).__name__}")
    # Built from the integer's own digits, so the result does not depend on the current
    # decimal context's precision or rounding.
    sign = 1 if micros < 0 else 0
    digits = tuple(int(d) for d in str(abs(micros)))
    return Decimal((sign, digits, -6))


def to_micros(dollars: Decimal | str | int) -> int:
    """A price in dollars as whole micro-dollars, for sending on the wire.

    Raises `ValueError` if the price is not a whole number of micro-dollars or does not fit
    in a signed 64-bit integer: it is never rounded. `float` is refused with `TypeError`;
    pass a `Decimal` or a decimal string such as `"199.99"` instead.
    """
    if isinstance(dollars, bool) or isinstance(dollars, float):
        raise TypeError(
            f"a price must be a Decimal, str or int, not {type(dollars).__name__}; "
            "a float cannot hold most prices exactly"
        )
    if isinstance(dollars, str):
        try:
            value = Decimal(dollars.strip())
        except InvalidOperation:
            raise ValueError(f"not a decimal number: {dollars!r}") from None
    elif isinstance(dollars, Decimal | int):
        value = Decimal(dollars)
    else:
        raise TypeError(f"a price must be a Decimal, str or int, not {type(dollars).__name__}")

    if not value.is_finite():
        raise ValueError(f"a price must be finite, not {value}")
    # Integer arithmetic on the Decimal's own digits, never Decimal arithmetic, which
    # rounds to the current context's precision and could turn a price with a stray
    # far-off digit into a whole number of micro-dollars.
    sign, digits, exponent = value.as_tuple()
    shift = int(exponent) + 6
    coefficient = int("".join(map(str, digits)))
    if coefficient == 0:
        return 0
    # Both early exits below also avoid building a power of ten with a huge exponent.
    if shift >= 0:
        if len(digits) + shift > 20:  # more than 20 digits is beyond 64 bits
            raise ValueError(f"{dollars!r} does not fit in a 64-bit micro-dollar price")
        micros = coefficient * 10**shift
    else:
        if -shift > len(digits):  # a nonzero value smaller than one micro-dollar
            raise ValueError(f"{dollars!r} is not a whole number of micro-dollars")
        micros, remainder = divmod(coefficient, 10**-shift)
        if remainder:
            raise ValueError(f"{dollars!r} is not a whole number of micro-dollars")
    if sign:
        micros = -micros
    if not INT64_MIN <= micros <= INT64_MAX:
        raise ValueError(f"{dollars!r} does not fit in a 64-bit micro-dollar price")
    return micros
