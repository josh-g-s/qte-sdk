import doctest
from datetime import UTC, date, datetime

import pytest

from qte_sdk import options
from qte_sdk.options import (
    CALL,
    PUT,
    OptionSymbol,
    is_option_symbol,
    option_symbol,
    parse_option_symbol,
)
from qte_sdk.units import to_micros


@pytest.mark.parametrize(
    ("symbol", "parts"),
    [
        ("SPY240119C00470000", OptionSymbol("SPY", date(2024, 1, 19), CALL, 470_000_000)),
        ("AAPL261218P00195500", OptionSymbol("AAPL", date(2026, 12, 18), PUT, 195_500_000)),
        ("F270115C00012500", OptionSymbol("F", date(2027, 1, 15), CALL, 12_500_000)),
        ("SPXW261016P05750000", OptionSymbol("SPXW", date(2026, 10, 16), PUT, 5_750_000_000)),
    ],
)
def test_real_looking_symbols_parse_and_rebuild(symbol, parts):
    assert parse_option_symbol(symbol) == parts
    assert option_symbol(parts.underlying, parts.expiry, parts.right, parts.strike) == symbol
    assert str(parts) == symbol
    assert is_option_symbol(symbol)


def test_the_length_varies_with_the_root_and_there_are_no_spaces():
    for root in ("A", "AB", "ABC", "ABCD", "ABCDE", "ABCDE1"):
        symbol = option_symbol(root, date(2026, 10, 16), CALL, to_micros("1"))
        assert symbol == f"{root}261016C00001000"
        assert len(symbol) == len(root) + 15
        assert " " not in symbol


@pytest.mark.parametrize(
    "parts",
    [
        OptionSymbol("Q", date(2000, 1, 1), CALL, 1_000),
        OptionSymbol("QQQ1", date(2099, 12, 31), PUT, 99_999_999_000),
        OptionSymbol("XOM", date(2028, 2, 29), PUT, 117_125_000),
    ],
)
def test_round_trip(parts):
    assert parse_option_symbol(option_symbol(*_fields(parts))) == parts


def test_boundary_strikes():
    expiry = date(2026, 10, 16)
    assert option_symbol("SPY", expiry, CALL, 1_000) == "SPY261016C00000001"
    assert option_symbol("SPY", expiry, CALL, 99_999_999_000) == "SPY261016C99999999"
    assert parse_option_symbol("SPY261016C00000001").strike == 1_000
    assert parse_option_symbol("SPY261016C99999999").strike == 99_999_999_000
    for strike in (0, -1_000, 100_000_000_000):
        with pytest.raises(ValueError, match="from 0.001 to 99999.999"):
            option_symbol("SPY", expiry, CALL, strike)


@pytest.mark.parametrize("strike", [1, 999, 470_000_001, 470_000_500])
def test_a_strike_that_is_not_whole_thousandths_is_refused(strike):
    with pytest.raises(ValueError, match="whole number of thousandths"):
        option_symbol("SPY", date(2026, 10, 16), CALL, strike)


@pytest.mark.parametrize("strike", [470.0, "470000000", True, None])
def test_a_strike_that_is_not_an_int_is_refused(strike):
    with pytest.raises(ValueError, match="int of micro-dollars"):
        option_symbol("SPY", date(2026, 10, 16), CALL, strike)


@pytest.mark.parametrize("root", ["", "spy", "1SPY", "TOOLONG", "BRK.B", "S Y", None])
def test_a_bad_root_is_refused(root):
    with pytest.raises(ValueError, match="underlying"):
        option_symbol(root, date(2026, 10, 16), CALL, 470_000_000)


@pytest.mark.parametrize("right", ["c", "CALL", "", None])
def test_a_bad_right_is_refused(right):
    with pytest.raises(ValueError, match="right"):
        option_symbol("SPY", date(2026, 10, 16), right, 470_000_000)


@pytest.mark.parametrize("expiry", [date(1999, 12, 31), date(2100, 1, 1)])
def test_an_expiry_outside_the_two_digit_years_is_refused(expiry):
    with pytest.raises(ValueError, match="between 2000 and 2099"):
        option_symbol("SPY", expiry, CALL, 470_000_000)


@pytest.mark.parametrize(
    "expiry", ["2026-10-16", 20261016, None, datetime(2026, 10, 16, 20, tzinfo=UTC)]
)
def test_an_expiry_that_is_not_a_date_is_refused(expiry):
    with pytest.raises(ValueError, match="datetime.date"):
        option_symbol("SPY", expiry, CALL, 470_000_000)


@pytest.mark.parametrize(
    "symbol",
    [
        "SPY240230C00470000",  # 30 February
        "SPY241301C00470000",  # month 13
        "SPY240100C00470000",  # day 0
        "SPY230229C00470000",  # 29 February in a common year
    ],
)
def test_an_impossible_date_is_refused(symbol):
    with pytest.raises(ValueError, match="not a real date"):
        parse_option_symbol(symbol)
    assert not is_option_symbol(symbol)


def test_a_zero_strike_is_refused():
    with pytest.raises(ValueError, match="strike is zero"):
        parse_option_symbol("SPY240119C00000000")
    assert not is_option_symbol("SPY240119C00000000")


@pytest.mark.parametrize(
    "symbol",
    [
        "",
        "SPY",
        "XOM",
        "BRK.B",
        "SPY 240119C00470000",  # the padded OCC form has spaces
        "SPY   240119C00470000",
        "spy240119C00470000",
        "SPY240119X00470000",
        "SPY240119C0047000",  # seven strike digits
        "SPY240119C004700000",  # nine strike digits
        "240119C00470000",  # no root
        "1SPY240119C00470000",
        "TOOLONG240119C00470000",
        "SPY240119C00470000\n",
        "SPY24-01-19C00470000",
        "SPY\uff12\uff14\uff10\uff11\uff11\uff19C00470000",  # full-width digits
        "SPY240119C\u0660\u0660\u0664\u0667\u0660\u0660\u0660\u0660",  # Arabic-Indic digits
    ],
)
def test_a_malformed_symbol_is_refused(symbol):
    with pytest.raises(ValueError, match="not an option symbol"):
        parse_option_symbol(symbol)
    assert not is_option_symbol(symbol)


@pytest.mark.parametrize("value", [None, 123, b"SPY240119C00470000"])
def test_a_symbol_that_is_not_a_string_is_refused(value):
    with pytest.raises(ValueError, match="must be a string"):
        parse_option_symbol(value)
    assert not is_option_symbol(value)


def test_equity_ids_are_not_option_symbols():
    for instrument in ("XOM", "CVX", "COP", "SPY", "AAPL", "BRK.B"):
        assert not is_option_symbol(instrument)


def test_option_symbol_is_frozen():
    parts = parse_option_symbol("SPY240119C00470000")
    with pytest.raises(AttributeError):
        parts.strike = 1_000  # type: ignore[misc]


def test_docstring_examples():
    assert doctest.testmod(options).failed == 0


def _fields(parts):
    return parts.underlying, parts.expiry, parts.right, parts.strike
