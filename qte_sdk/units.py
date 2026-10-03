"""Exact conversion of the wire's prices and timestamps.

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

Every timestamp on the wire, such as `session_ack.server_time`, a `grid_time` or a
calendar session's `open_time`, is a whole number of milliseconds since the Unix epoch,
in UTC, in a signed 64-bit integer. The difference of two timestamps is a number of
milliseconds. These helpers convert both with integer arithmetic only:

    >>> to_datetime(1_791_207_000_000)
    datetime.datetime(2026, 10, 5, 13, 30, tzinfo=datetime.timezone.utc)
    >>> to_timedelta(1_791_207_000_000 - 1_791_062_220_000)
    datetime.timedelta(days=1, seconds=58380)
    >>> to_timestamp(datetime(2026, 10, 5, 13, 30, tzinfo=UTC))
    1791207000000

For "now", use the exchange's clock, `session.info.server_time` or a later exchange
timestamp, not your computer's: the exchange's clock is the one that opens and closes the
market.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation

MICROS_PER_DOLLAR = 1_000_000
INT64_MIN = -(2**63)
INT64_MAX = 2**63 - 1

EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
"""Exchange timestamp 0: midnight UTC at the start of 1 January 1970."""

_ONE_MILLISECOND = timedelta(milliseconds=1)


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
    if not any(digits):
        return 0
    # Trailing zeros carry no value ("1.000000000" is 1), so drop them first. The last
    # digit left is then nonzero, and any digit below the micro-dollar place makes the
    # price a fraction of a micro-dollar.
    significant = len(digits)
    while digits[significant - 1] == 0:
        significant -= 1
    shift = int(exponent) + len(digits) - significant + 6
    if shift < 0:
        raise ValueError(f"{dollars!r} is not a whole number of micro-dollars")
    if significant + shift > 20:  # more than 20 digits is beyond 64 bits
        raise ValueError(f"{dollars!r} does not fit in a 64-bit micro-dollar price")
    micros = int("".join(map(str, digits[:significant]))) * 10**shift
    if sign:
        micros = -micros
    if not INT64_MIN <= micros <= INT64_MAX:
        raise ValueError(f"{dollars!r} does not fit in a 64-bit micro-dollar price")
    return micros


def _whole_milliseconds(value: int, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{what} is an int of milliseconds, not {type(value).__name__}")
    return value


def to_timedelta(milliseconds: int) -> timedelta:
    """A number of milliseconds, such as the difference of two exchange timestamps, as an
    exact `timedelta`.

    Raises `ValueError` if it is beyond what a `timedelta` holds (about 2.7 million
    years either way), which a difference of two 64-bit timestamps can be.
    """
    milliseconds = _whole_milliseconds(milliseconds, "a duration")
    # Whole days and the milliseconds left over, so every part passed on is an int.
    days, rest = divmod(milliseconds, 86_400_000)
    try:
        return timedelta(days=days, milliseconds=rest)
    except OverflowError:
        raise ValueError(f"{milliseconds} ms is beyond the range of a timedelta") from None


def to_datetime(timestamp: int) -> datetime:
    """An exchange timestamp as a timezone-aware `datetime` in UTC, exact to the
    millisecond.

    Raises `ValueError` if the timestamp is outside the years 1 to 9999, the range of a
    `datetime`. That holds every real exchange time, but not every 64-bit integer.
    """
    timestamp = _whole_milliseconds(timestamp, "an exchange timestamp")
    try:
        return EPOCH + to_timedelta(timestamp)
    except (OverflowError, ValueError):
        raise ValueError(
            f"exchange timestamp {timestamp} is outside the years 1 to 9999 a datetime holds"
        ) from None


def to_timestamp(when: datetime) -> int:
    """A timezone-aware `datetime` as an exchange timestamp: whole milliseconds since the
    Unix epoch, in UTC.

    A `datetime` with no time zone is refused with `ValueError`, since it could mean any
    zone's time: give it one, for example `datetime(2026, 10, 5, 13, 30, tzinfo=UTC)`.
    Below the millisecond the time is rounded down, to the start of the millisecond it
    falls in, so a time just before the epoch gives -1, not 0.
    """
    if not isinstance(when, datetime):
        raise TypeError(f"a time must be a datetime, not {type(when).__name__}")
    if when.utcoffset() is None:
        raise ValueError(f"{when.isoformat()} has no time zone; give it one, such as tzinfo=UTC")
    # Subtracting two aware datetimes gives an exact timedelta, and dividing it by one
    # millisecond floors it to whole milliseconds, in integers throughout.
    return (when - EPOCH) // _ONE_MILLISECOND
