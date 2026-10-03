import doctest
import random
from datetime import UTC, date, datetime, timedelta, timezone, tzinfo
from decimal import Decimal, localcontext
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pytest

from qte_sdk import units
from qte_sdk.units import (
    EPOCH,
    INT64_MAX,
    INT64_MIN,
    to_datetime,
    to_decimal,
    to_micros,
    to_timedelta,
    to_timestamp,
)

ABOVE_2_53 = 2**53 + 1  # the first integer a float cannot hold


@pytest.mark.parametrize(
    ("micros", "dollars"),
    [
        (0, "0.000000"),
        (1, "0.000001"),
        (-1, "-0.000001"),
        (199_990_000, "199.990000"),
        (200_011_000, "200.011000"),
        (ABOVE_2_53, "9007199254.740993"),
        (INT64_MAX, "9223372036854.775807"),
        (INT64_MIN, "-9223372036854.775808"),
    ],
)
def test_micros_convert_to_exact_decimal_dollars_and_back(micros, dollars):
    assert to_decimal(micros) == Decimal(dollars)
    assert str(to_decimal(micros)) == dollars
    assert to_micros(to_decimal(micros)) == micros
    assert to_micros(dollars) == micros


def test_a_float_would_have_lost_the_value_the_decimal_keeps():
    assert float(ABOVE_2_53) == float(ABOVE_2_53 - 1)
    assert to_decimal(ABOVE_2_53) != to_decimal(ABOVE_2_53 - 1)


def test_conversion_does_not_depend_on_the_decimal_context():
    with localcontext() as ctx:
        ctx.prec = 3
        assert to_decimal(INT64_MAX) == Decimal("9223372036854.775807")
        assert to_micros("9223372036854.775807") == INT64_MAX


@pytest.mark.parametrize(
    ("dollars", "micros"),
    [
        (Decimal("200.011"), 200_011_000),
        ("199.99", 199_990_000),
        (" 199.99 ", 199_990_000),
        (200, 200_000_000),
        (Decimal("1E+2"), 100_000_000),
        (Decimal("0.0000010000"), 1),
        ("-0", 0),
        (Decimal("0E-50"), 0),
        ("1." + "0" * 5000, 1_000_000),  # more digits than int() accepts from a string
        (Decimal("1." + "0" * 5000), 1_000_000),
        ("12345.678900000000000000000000000000", 12_345_678_900),
    ],
)
def test_prices_convert_to_whole_micros(dollars, micros):
    assert to_micros(dollars) == micros


@pytest.mark.parametrize(
    "dollars",
    [
        "0.0000001",
        Decimal("199.9900001"),
        "1.00000000000000000000000000001",  # a stray digit beyond default decimal precision
        "0." + "0" * 5000 + "1",
        "1E-999999999",
        "NaN",
        "Infinity",
        "-Infinity",
        "not a price",
        "",
        "9223372036854.775808",  # one micro-dollar above the 64-bit range
        "-9223372036854.775809",
        "1E+999999999",
    ],
)
def test_a_price_that_is_not_a_whole_64_bit_micro_amount_is_refused_never_rounded(dollars):
    with pytest.raises(ValueError):
        to_micros(dollars)


@pytest.mark.parametrize("bad", [199.99, 1.0, True, None, [1]])
def test_to_micros_refuses_float_and_other_non_decimal_types(bad):
    with pytest.raises(TypeError):
        to_micros(bad)


@pytest.mark.parametrize("bad", [1.0, Decimal("1"), "1", True, None])
def test_to_decimal_takes_only_integer_micros(bad):
    with pytest.raises(TypeError):
        to_decimal(bad)


# Exchange timestamps: whole milliseconds since the Unix epoch, UTC.

MS = timedelta(milliseconds=1)
FIRST_MS = -62_135_596_800_000  # 0001-01-01 00:00:00.000 UTC, the earliest datetime
LAST_MS = 253_402_300_799_999  # 9999-12-31 23:59:59.999 UTC, the latest whole millisecond
NEXT_OPEN = 1_791_207_000_000  # 2026-10-05 13:30 UTC
SERVER_TIME = 1_791_062_220_000  # 2026-10-03 21:17 UTC


def test_the_module_examples_hold():
    assert doctest.testmod(units).failed == 0


@pytest.mark.parametrize(
    ("timestamp", "expected"),
    [
        (0, datetime(1970, 1, 1, tzinfo=UTC)),
        (1, datetime(1970, 1, 1, 0, 0, 0, 1000, tzinfo=UTC)),
        (-1, datetime(1969, 12, 31, 23, 59, 59, 999_000, tzinfo=UTC)),
        (-86_400_001, datetime(1969, 12, 30, 23, 59, 59, 999_000, tzinfo=UTC)),
        (NEXT_OPEN, datetime(2026, 10, 5, 13, 30, tzinfo=UTC)),
        (SERVER_TIME, datetime(2026, 10, 3, 21, 17, tzinfo=UTC)),
        (FIRST_MS, datetime(1, 1, 1, tzinfo=UTC)),
        (LAST_MS, datetime(9999, 12, 31, 23, 59, 59, 999_000, tzinfo=UTC)),
    ],
)
def test_a_timestamp_is_an_exact_utc_datetime_and_back(timestamp, expected):
    when = to_datetime(timestamp)
    assert when == expected
    assert when.tzinfo is UTC and when.utcoffset() == timedelta(0)
    assert to_timestamp(when) == timestamp


@pytest.mark.parametrize(
    "timestamp", [FIRST_MS - 1, LAST_MS + 1, 2**53 + 1, -(2**53) - 1, INT64_MAX, INT64_MIN]
)
def test_a_timestamp_outside_the_years_a_datetime_holds_is_refused(timestamp):
    # Every value beyond 2**53 ms (about 285,000 years) is outside the years 1 to 9999.
    with pytest.raises(ValueError, match="outside the years 1 to 9999"):
        to_datetime(timestamp)


def test_timestamps_round_trip_across_the_whole_datetime_range():
    draw = random.Random(90)
    for _ in range(2_000):
        timestamp = draw.randint(FIRST_MS, LAST_MS)
        assert to_timestamp(to_datetime(timestamp)) == timestamp


@pytest.mark.parametrize(
    ("milliseconds", "expected"),
    [
        (0, timedelta(0)),
        (1, timedelta(microseconds=1000)),
        (-1, -timedelta(microseconds=1000)),
        (NEXT_OPEN - SERVER_TIME, timedelta(hours=40, minutes=13)),
        (SERVER_TIME - NEXT_OPEN, -timedelta(hours=40, minutes=13)),
        (86_399_999_999_999_999, timedelta.max - timedelta(microseconds=999)),
        (-86_399_999_913_600_000, timedelta.min),
    ],
)
def test_a_difference_of_timestamps_is_an_exact_timedelta(milliseconds, expected):
    assert to_timedelta(milliseconds) == expected


def test_a_difference_beyond_2_53_ms_stays_exact():
    above = 2**53 + 1  # a float would make this 2**53
    assert float(above) == float(above - 1)
    for value in (above, -above):
        exact = to_timedelta(value)
        assert exact // MS == value
        assert exact % MS == timedelta(0)
        assert exact != to_timedelta(value - 1)


@pytest.mark.parametrize(
    "milliseconds",
    [86_399_999_999_999_999 + 1, -86_399_999_913_600_000 - 1, INT64_MAX, INT64_MAX - INT64_MIN],
)
def test_a_difference_beyond_what_a_timedelta_holds_is_refused(milliseconds):
    # The difference of two 64-bit timestamps can be up to 2**64 - 1 ms.
    with pytest.raises(ValueError, match="beyond the range of a timedelta"):
        to_timedelta(milliseconds)


@pytest.mark.parametrize("bad", [1.0, 1e12, Decimal("1"), "1", True, None])
def test_timestamps_and_differences_are_taken_only_as_int(bad):
    with pytest.raises(TypeError):
        to_datetime(bad)
    with pytest.raises(TypeError):
        to_timedelta(bad)


@pytest.mark.parametrize(
    ("when", "timestamp"),
    [
        # Below the millisecond, the time rounds down to the start of its millisecond.
        (datetime(1970, 1, 1, 0, 0, 0, 999, tzinfo=UTC), 0),
        (datetime(1970, 1, 1, 0, 0, 0, 1000, tzinfo=UTC), 1),
        (datetime(1970, 1, 1, 0, 0, 0, 1500, tzinfo=UTC), 1),
        (datetime(1970, 1, 1, 0, 0, 0, 1999, tzinfo=UTC), 1),
        # Down, not towards zero: half a millisecond before the epoch is in millisecond -1.
        (datetime(1969, 12, 31, 23, 59, 59, 999_500, tzinfo=UTC), -1),
        (datetime(1969, 12, 31, 23, 59, 59, 999_999, tzinfo=UTC), -1),
        (datetime(1969, 12, 31, 23, 59, 59, 998_999, tzinfo=UTC), -2),
        (datetime(9999, 12, 31, 23, 59, 59, 999_999, tzinfo=UTC), LAST_MS),
    ],
)
def test_a_datetime_rounds_down_to_whole_milliseconds(when, timestamp):
    assert to_timestamp(when) == timestamp


def test_a_datetime_in_another_zone_gives_the_same_instant():
    new_york_summer = timezone(timedelta(hours=-4))
    assert to_timestamp(datetime(2026, 10, 5, 9, 30, tzinfo=new_york_summer)) == NEXT_OPEN
    tokyo = timezone(timedelta(hours=9))
    assert to_timestamp(datetime(2026, 10, 5, 22, 30, tzinfo=tokyo)) == NEXT_OPEN
    # Just after midnight UTC on 1 January 1, one hour east: before the first datetime.
    assert to_timestamp(datetime(1, 1, 1, 0, 30, tzinfo=timezone(timedelta(hours=1)))) == (
        FIRST_MS - 30 * 60_000
    )


def test_a_datetime_in_a_named_zone_follows_its_daylight_saving():
    try:
        new_york = ZoneInfo("America/New_York")
    except ZoneInfoNotFoundError:
        pytest.skip("no time zone data here (on Windows, install the tzdata package)")
    assert to_timestamp(datetime(2026, 10, 5, 9, 30, tzinfo=new_york)) == NEXT_OPEN
    january_open = datetime(2026, 1, 5, 14, 30, tzinfo=UTC)
    assert to_timestamp(datetime(2026, 1, 5, 9, 30, tzinfo=new_york)) == to_timestamp(january_open)
    assert to_datetime(NEXT_OPEN).astimezone(new_york).hour == 9


class NoOffset(tzinfo):
    """A time zone that does not know its offset, which makes a datetime naive."""

    def utcoffset(self, dt):
        return None

    def dst(self, dt):
        return None


@pytest.mark.parametrize(
    "naive", [datetime(2026, 10, 5, 13, 30), datetime(2026, 10, 5, 13, 30, tzinfo=NoOffset())]
)
def test_a_datetime_with_no_time_zone_is_refused(naive):
    with pytest.raises(ValueError, match="no time zone"):
        to_timestamp(naive)


@pytest.mark.parametrize("bad", [date(2026, 10, 5), "2026-10-05T13:30Z", NEXT_OPEN, 1.0, None])
def test_to_timestamp_takes_only_a_datetime(bad):
    with pytest.raises(TypeError):
        to_timestamp(bad)


def test_the_epoch_is_timestamp_zero():
    assert EPOCH == datetime(1970, 1, 1, tzinfo=UTC)
    assert to_timestamp(EPOCH) == 0
