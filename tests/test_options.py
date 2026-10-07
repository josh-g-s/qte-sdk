import doctest
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from qte_sdk import options
from qte_sdk.books import LatestBooks
from qte_sdk.connection import DecodeFailed, Disconnected, SeqGap
from qte_sdk.contract.codec import unpack
from qte_sdk.contract.v1.common_pb2 import RESIDUAL, STUDENT_TO_WALL
from qte_sdk.contract.v1.market_data_pb2 import Book, Mark, Trades
from qte_sdk.options import (
    CALL,
    OPTION_GREEKS_VALID,
    OPTION_REDUCING_ONLY,
    OPTION_ROLE_ACTIVE,
    OPTION_ROLE_OBLIGATED,
    OPTION_ROLE_RETAINED,
    OPTION_SUSPENDED,
    OPTION_TRADING,
    PUT,
    LatestGreeks,
    OptionChain,
    OptionGreeks,
    OptionSymbol,
    chain_contracts,
    expiry_date,
    greek_to_decimal,
    is_option_symbol,
    limit_scope,
    option_symbol,
    parse_option_symbol,
    trading_state,
    vol_to_decimal,
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


def test_a_root_may_start_with_a_digit():
    symbol = option_symbol("1SPY", date(2026, 10, 16), CALL, 1_000)
    assert symbol == "1SPY261016C00000001"
    parts = parse_option_symbol(symbol)
    assert parts == OptionSymbol("1SPY", date(2026, 10, 16), CALL, 1_000)
    assert is_option_symbol(symbol)
    assert option_symbol(*_fields(parts)) == symbol
    assert parse_option_symbol("123456240119P00470000").underlying == "123456"


def test_the_length_varies_with_the_root_and_there_are_no_spaces():
    for root in ("A", "AB", "ABC", "ABCD", "ABCDE", "ABCDE1", "1", "999999"):
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


@pytest.mark.parametrize("root", ["", "spy", "Spy", "TOOLONG", "BRK.B", "S Y", None])
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
        "toolow240119C00470000",
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


# The day's chain.


def chain() -> OptionChain:
    chain = OptionChain(session_date="2026-10-26")
    nov = chain.expiries.add(underlying="SPY", expiry="2026-11-20")
    nov.contracts.add(instrument="SPY261120C00660000", role=OPTION_ROLE_ACTIVE)
    nov.contracts.add(instrument="SPY261120C00665000", role=OPTION_ROLE_OBLIGATED)
    nov.contracts.add(instrument="SPY261120C00700000", role=OPTION_ROLE_RETAINED)
    nov.contracts.add(instrument="SPY261120P00660000", role=OPTION_ROLE_OBLIGATED)
    dec = chain.expiries.add(underlying="SPY", expiry="2026-12-18", reducing_only=True)
    dec.contracts.add(instrument="SPY261218C00665000", role=OPTION_ROLE_ACTIVE)
    googl = chain.expiries.add(underlying="GOOGL", expiry="2026-11-20")
    googl.contracts.add(instrument="GOOGL261120P00172500", role=OPTION_ROLE_OBLIGATED)
    return chain


def ids(contracts) -> list[str]:
    return [contract.instrument for contract in contracts]


def test_chain_contracts_filters_by_underlying_expiry_and_role_in_chain_order():
    assert len(chain_contracts(chain())) == 6
    assert ids(chain_contracts(chain(), underlying="GOOGL")) == ["GOOGL261120P00172500"]
    assert ids(chain_contracts(chain(), expiry=date(2026, 12, 18))) == ["SPY261218C00665000"]
    assert ids(chain_contracts(chain(), underlying="SPY", role=OPTION_ROLE_OBLIGATED)) == [
        "SPY261120C00665000",
        "SPY261120P00660000",
    ]
    assert ids(chain_contracts(chain(), expiry=date(2026, 11, 20), role=OPTION_ROLE_RETAINED)) == [
        "SPY261120C00700000"
    ]
    assert chain_contracts(chain(), underlying="QQQ") == []
    assert chain_contracts(OptionChain()) == []


def test_every_listed_contract_is_an_option_symbol_matching_its_expiry():
    for entry in chain().expiries:
        for contract in entry.contracts:
            parts = parse_option_symbol(contract.instrument)
            assert (parts.underlying, parts.expiry) == (entry.underlying, expiry_date(entry))


def test_expiry_date_and_limit_scope():
    [nov, *_] = chain().expiries
    assert expiry_date(nov) == date(2026, 11, 20)
    assert limit_scope("SPY", date(2026, 11, 20)) == "SPY 2026-11-20"
    assert limit_scope(nov.underlying, expiry_date(nov)) == "SPY 2026-11-20"


# Greeks.


def test_greeks_convert_exactly_with_their_own_scales():
    assert greek_to_decimal(500_000_000_000) == Decimal("0.5")
    assert str(greek_to_decimal(500_000_000_000)) == "0.500000000000"
    assert greek_to_decimal(-1) == Decimal("-1E-12")
    assert greek_to_decimal(2**63 - 1) == Decimal("9223372.036854775807")
    assert vol_to_decimal(18_000_000) == Decimal(18)
    assert str(vol_to_decimal(25_000_000)) == "25.000000"
    assert vol_to_decimal(1) == Decimal("0.000001")
    for convert in (greek_to_decimal, vol_to_decimal):
        for value in (0.5, "5", True, None):
            with pytest.raises(TypeError):
                convert(value)


def greeks(instrument: str, grid_time: int, delta: int = 0) -> OptionGreeks:
    return OptionGreeks(
        instrument=instrument, grid_time=grid_time, status=OPTION_GREEKS_VALID, delta=delta
    )


def test_latest_greeks_keeps_the_newest_per_contract():
    held = LatestGreeks()
    assert held.update(greeks("SPY261120C00665000", 1000, 1)) is True
    assert held.update(greeks("SPY261120P00660000", 1000, 2)) is True
    # A subscribe snapshot repeats or predates what is held: ignored.
    assert held.update(greeks("SPY261120C00665000", 1000, 9)) is False
    assert held.update(greeks("SPY261120C00665000", 900, 9)) is False
    assert held.update(greeks("SPY261120C00665000", 1300, 3)) is True
    assert held.get("SPY261120C00665000").delta == 3
    assert held.get("SPY261120P00660000").delta == 2
    assert held.get("XOM") is None
    assert len(held) == 2 and "SPY261120P00660000" in held
    assert sorted(g.instrument for g in held) == ["SPY261120C00665000", "SPY261120P00660000"]
    # Other messages, books included, change nothing.
    assert held.update(Book(instrument="SPY261120C00665000", grid_time=5000)) is False
    assert held.update(Mark(instrument="SPY261120C00665000")) is False
    assert held.stale == frozenset()


def test_latest_greeks_marks_contracts_stale_after_missed_messages():
    held = LatestGreeks()
    held.update(greeks("SPY261120C00665000", 1000))
    held.update(DecodeFailed(type="book", error=ValueError()))
    assert held.stale == frozenset()
    held.update(DecodeFailed(type="option_greeks", error=ValueError()))
    assert held.stale == {"SPY261120C00665000"}
    # On the same connection, Greeks at the grid_time held prove nothing.
    held.update(greeks("SPY261120C00665000", 1000))
    assert held.stale == {"SPY261120C00665000"}
    held.update(greeks("SPY261120C00665000", 1300))
    assert held.stale == frozenset()
    # After a reconnect, the subscribe snapshot at the same grid_time clears it.
    held.update(Disconnected(error=None))
    assert held.stale == {"SPY261120C00665000"}
    held.update(greeks("SPY261120C00665000", 1300))
    assert held.stale == frozenset()
    held.update(SeqGap(expected=2, received=4))
    assert held.stale == {"SPY261120C00665000"}


def test_latest_books_ignores_greeks_and_keeps_option_books():
    books = LatestBooks()
    assert books.update(greeks("SPY261120C00665000", 1000)) is False
    books.update(DecodeFailed(type="option_greeks", error=ValueError()))
    option_book = Book(
        instrument="SPY261120C00665000", grid_time=1000, trading_state=OPTION_TRADING
    )
    assert books.update(option_book) is True
    assert books.get("SPY261120C00665000") is option_book


# Trading state.


@pytest.mark.parametrize("state", [OPTION_TRADING, OPTION_REDUCING_ONLY, OPTION_SUSPENDED])
def test_a_known_trading_state_is_read_as_it_is(state):
    assert trading_state(Book(instrument="SPY261120C00665000", trading_state=state)) == state


def test_an_equity_book_has_no_trading_state():
    assert trading_state(Book(instrument="XOM")) is None


@pytest.mark.parametrize("value", [0, 4, 99])
def test_an_unknown_or_unspecified_trading_state_is_read_as_suspended(value):
    book = Book(instrument="SPY261120C00665000", trading_state=value)
    assert trading_state(book) == OPTION_SUSPENDED


def test_a_trading_state_name_from_a_newer_contract_is_read_as_suspended():
    payload = {
        "instrument": "SPY261120C00665000",
        "grid_time": "1000",
        "condition": "LIVE",
        "trading_state": "OPTION_SOMETHING_NEW",
    }
    book = unpack(payload, Book)
    # The name cannot be decoded, so the field is left unset.
    assert not book.HasField("trading_state")
    assert trading_state(book) == OPTION_SUSPENDED


def test_a_feed_only_residual_print_is_told_apart_from_an_ordinary_one():
    # As the exchange sends `trades`: the flag only on the feed-only residual, never false.
    payload = {
        "instrument": "SPY261120C00665000",
        "grid_time": "1791207000000",
        "prints": [
            {
                "price": "1050000",
                "size": "2",
                "timestamp": "1",
                "kind": "RESIDUAL",
                "feed_only": True,
            },
            {"price": "1050000", "size": "1", "timestamp": "2", "kind": "RESIDUAL"},
            {"price": "1060000", "size": "3", "timestamp": "3", "kind": "STUDENT_TO_WALL"},
        ],
    }
    feed_only, residual, ordinary = unpack(payload, Trades).prints
    assert feed_only.kind == RESIDUAL and residual.kind == RESIDUAL
    assert ordinary.kind == STUDENT_TO_WALL
    assert feed_only.HasField("feed_only") and feed_only.feed_only
    assert not residual.HasField("feed_only")
    assert not ordinary.HasField("feed_only")
