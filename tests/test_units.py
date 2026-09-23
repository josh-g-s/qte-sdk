from decimal import Decimal, localcontext

import pytest

from qte_sdk.units import INT64_MAX, INT64_MIN, to_decimal, to_micros

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
