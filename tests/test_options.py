import asyncio
import doctest
import logging
import threading
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from qte_sdk import errors, options, pacing
from qte_sdk.books import LatestBooks
from qte_sdk.connection import (
    Connection,
    DecodeFailed,
    Disconnected,
    Received,
    SeqGap,
    SessionInfo,
)
from qte_sdk.contract.codec import unpack
from qte_sdk.contract.v1.common_pb2 import BUY, RESIDUAL, STUDENT_TO_WALL, ReasonCodes
from qte_sdk.contract.v1.market_data_pb2 import Book, Mark, OptionTradingState, Trades
from qte_sdk.contract.v1.order_entry_pb2 import AmendOrder, NewOrder
from qte_sdk.contract.v1.order_events_pb2 import Accepted, OrderCancelled, Reject
from qte_sdk.options import (
    CALL,
    OPTION_GREEKS_VALID,
    OPTION_HALTED,
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
    is_feed_only,
    is_option_symbol,
    limit_scope,
    option_symbol,
    parse_option_symbol,
    trading_state,
    vol_to_decimal,
)
from qte_sdk.orders import reason_code_name
from qte_sdk.session import Session
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


@pytest.mark.parametrize(
    "state", [OPTION_TRADING, OPTION_REDUCING_ONLY, OPTION_SUSPENDED, OPTION_HALTED]
)
def test_a_known_trading_state_is_read_as_it_is(state):
    assert trading_state(Book(instrument="SPY261120C00665000", trading_state=state)) == state


def test_an_equity_book_has_no_trading_state():
    assert trading_state(Book(instrument="XOM")) is None


@pytest.mark.parametrize("value", [0, 5, 99])
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


def test_a_halted_option_book_decodes_by_name_and_number_and_is_held_as_halted():
    assert OptionTradingState.Name(4) == "OPTION_HALTED"
    assert OPTION_HALTED == OptionTradingState.Value("OPTION_HALTED") == 4
    payload = {
        "instrument": "SPY261120C00665000",
        "grid_time": "1000",
        "condition": "LIVE",
        "trading_state": "OPTION_HALTED",
    }
    book = unpack(payload, Book)
    assert book.HasField("trading_state") and book.trading_state == OPTION_HALTED
    # By number too.
    by_number = unpack({**payload, "trading_state": 4}, Book)
    assert trading_state(by_number) == OPTION_HALTED
    books = LatestBooks()
    assert books.update(book) is True
    assert trading_state(books.get("SPY261120C00665000")) == OPTION_HALTED
    # The Director's end of the halt republishes the book with the state in force.
    payload.update(grid_time="1100", trading_state="OPTION_TRADING")
    assert books.update(unpack(payload, Book)) is True
    assert trading_state(books.get("SPY261120C00665000")) == OPTION_TRADING


def test_the_chain_halt_reasons_decode_by_name_and_number():
    reject = unpack({"reason_code": "CONTRACT_HALTED", "receipt_time": "1"}, Reject)
    assert reject.reason_code == ReasonCodes.CONTRACT_HALTED == 1305
    assert reason_code_name(reject.reason_code) == "CONTRACT_HALTED"
    by_number = unpack({"reason_code": 1305, "receipt_time": "1"}, Reject)
    assert reason_code_name(by_number.reason_code) == "CONTRACT_HALTED"
    cancelled = unpack(
        {
            "origin": "TEAM",
            "strat_id": "s",
            "instrument": "SPY261120C00665000",
            "side": "BUY",
            "cancelled_size": "5",
            "reason_code": "CHAIN_HALTED",
            "timestamp": "1",
        },
        OrderCancelled,
    )
    assert cancelled.reason_code == ReasonCodes.CHAIN_HALTED == 1818
    assert reason_code_name(cancelled.reason_code) == "CHAIN_HALTED"
    cancelled_by_number = unpack(
        {"instrument": "SPY261120C00665000", "reason_code": 1818, "timestamp": "1"},
        OrderCancelled,
    )
    assert reason_code_name(cancelled_by_number.reason_code) == "CHAIN_HALTED"


# A Director's chain halt, as a session reads it.


@pytest.fixture
def halt_warnings(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """The warnings logged, as (code, message), caught before their thread."""
    caught: list[tuple[str, str]] = []
    monkeypatch.setattr(pacing, "_emit", lambda message, code, **_: caught.append((code, message)))
    return caught


async def _sent_nowhere(type_: str, payload: object) -> None:
    pass


def halt_session(monkeypatch: pytest.MonkeyPatch) -> Session:
    info = SessionInfo("s-1", "team-a", 1_760_000_000_000, "0.x", False)
    session = Session(Connection("ws://127.0.0.1:9"), info, [])
    monkeypatch.setattr(session.connection, "send", _sent_nowhere)
    return session


def send_option_new(session: Session, ref: str, instrument: str = "SPY261120C00665000") -> None:
    order = NewOrder(request_ref=ref, strat_id="s", instrument=instrument, side=BUY, size=1)
    asyncio.run(session.send("new", order))


def halt_reject(ref: str | None = "r") -> Received:
    # As the exchange sends it: a reject names an instrument only for UNKNOWN_INSTRUMENT.
    reject = Reject(reason_code=ReasonCodes.CONTRACT_HALTED, receipt_time=1)
    if ref is not None:
        reject.request_ref = ref
    return Received("reject", reject, None)


def halt_cancel(instrument: str = "SPY261120C00665000") -> Received:
    cancelled = OrderCancelled(
        strat_id="s",
        instrument=instrument,
        side=BUY,
        cancelled_size=5,
        reason_code=ReasonCodes.CHAIN_HALTED,
        timestamp=1,
    )
    return Received("order_cancelled", cancelled, None)


def test_a_contract_halted_reject_logs_its_code_and_what_to_do(halt_warnings, monkeypatch):
    session = halt_session(monkeypatch)
    send_option_new(session, "r")
    session._keep(halt_reject("r"))
    assert halt_warnings == [
        (
            errors.OPTIONS_CHAIN_HALTED,
            "QTE-OPTIONS-CHAIN-HALTED: the exchange rejected an order in SPY261120C00665000 "
            "(CONTRACT_HALTED): a Director has halted the options chain on SPY. No new order "
            "or amend in that chain is accepted until the Director ends the halt, and the end "
            "restores no order. Send only cancels in that chain until "
            "qte_sdk.options.trading_state of its latest book is no longer OPTION_HALTED, then "
            "place again only what that state allows.",
        )
    ]


def test_an_amend_reject_names_the_chain_the_amend_named(halt_warnings, monkeypatch):
    session = halt_session(monkeypatch)
    amend = AmendOrder(request_ref="a", instrument="GOOGL261120C00200000", side=BUY, new_size=2)
    asyncio.run(session.send("amend", amend))
    session._keep(halt_reject("a"))
    assert "halted the options chain on GOOGL" in halt_warnings[0][1]


def test_a_chain_halt_logs_once_a_minute_for_each_underlying(halt_warnings, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(options, "_monotonic", lambda: now[0])
    session = halt_session(monkeypatch)
    # The halt's start cancels every resting order in the chain at once: one warning.
    for _ in range(5):
        session._keep(halt_cancel())
    send_option_new(session, "p", "SPY261120P00600000")
    session._keep(halt_reject("p"))
    assert len(halt_warnings) == 1
    assert "cancelled an order in SPY261120C00665000 (CHAIN_HALTED)" in halt_warnings[0][1]
    # Another underlying's chain has a warning of its own, a reject's as a cancel's.
    send_option_new(session, "g", "GOOGL261120C00200000")
    session._keep(halt_reject("g"))
    assert len(halt_warnings) == 2
    assert "rejected an order in GOOGL261120C00200000" in halt_warnings[1][1]
    assert "halted the options chain on GOOGL" in halt_warnings[1][1]
    now[0] += 59
    session._keep(halt_cancel())
    assert len(halt_warnings) == 2
    now[0] += 1
    session._keep(halt_cancel())
    assert len(halt_warnings) == 3


def test_each_session_has_its_own_minute(halt_warnings, monkeypatch):
    monkeypatch.setattr(options, "_monotonic", lambda: 1000.0)
    first, second = halt_session(monkeypatch), halt_session(monkeypatch)
    first._keep(halt_cancel())
    first._keep(halt_cancel())
    second._keep(halt_cancel())
    assert len(halt_warnings) == 2
    # One session's record of what it sent is not another's.
    send_option_new(first, "r")
    second._keep(halt_reject("r"))
    assert "rejected an order in an option contract" in halt_warnings[2][1]


def test_a_halt_reject_for_an_order_the_session_did_not_send_still_logs(halt_warnings, monkeypatch):
    session = halt_session(monkeypatch)
    session._keep(halt_reject("not-sent"))
    session._keep(halt_reject(None))
    # Neither names a chain, so they share one minute.
    assert len(halt_warnings) == 1
    assert (
        "rejected an order in an option contract (CONTRACT_HALTED): a Director has halted "
        "the options chain on its underlying" in halt_warnings[0][1]
    )


def test_an_answered_order_is_forgotten(halt_warnings, monkeypatch):
    session = halt_session(monkeypatch)
    send_option_new(session, "r")
    session._keep(Received("accepted", Accepted(request_ref="r"), None))
    session._keep(halt_reject("r"))
    assert "rejected an order in an option contract" in halt_warnings[0][1]
    assert session._chain_halts._sent == {}


def test_a_request_ref_sent_again_before_its_answer_names_no_chain(halt_warnings, monkeypatch):
    # Its reject could answer either order, so naming one chain could name the wrong one.
    session = halt_session(monkeypatch)
    send_option_new(session, "r", "SPY261120C00665000")
    send_option_new(session, "r", "GOOGL261120C00200000")
    session._keep(halt_reject("r"))
    assert "rejected an order in an option contract" in halt_warnings[0][1]
    # The same contract again is no doubt, nor is an equity order sent with a fresh ref.
    halts = options._ChainHaltWarnings()
    halts.sent("new", NewOrder(request_ref="s", instrument="SPY261120C00665000"))
    halts.sent("new", NewOrder(request_ref="s", instrument="SPY261120C00665000"))
    halts.sent("new", NewOrder(request_ref="e", instrument="SPY"))
    assert halts._sent == {"s": "SPY261120C00665000"}
    halts.sent("new", NewOrder(request_ref="s", instrument="SPY"))
    assert halts._sent == {"s": None}


def test_the_record_of_sent_orders_is_bounded(monkeypatch):
    monkeypatch.setattr(options, "_SENT_LIMIT", 3)
    halts = options._ChainHaltWarnings()
    for ref in "abcd":
        halts.sent("new", NewOrder(request_ref=ref, instrument="SPY261120C00665000"))
    halts.sent("new", NewOrder(request_ref="e", instrument="SPY"))  # not an option
    halts.sent("cancel", NewOrder(request_ref="f", instrument="SPY261120C00665000"))
    assert list(halts._sent) == ["b", "c", "d"]


def test_other_rejects_and_cancels_say_nothing_of_a_halt(halt_warnings, monkeypatch):
    session = halt_session(monkeypatch)
    send_option_new(session, "r")
    suspended = halt_reject("r")
    suspended.message.reason_code = ReasonCodes.CONTRACT_SUSPENDED
    session._keep(suspended)
    closed = halt_cancel()
    closed.message.reason_code = ReasonCodes.SESSION_CLOSE
    session._keep(closed)
    assert halt_warnings == []
    assert session._chain_halts._sent == {}


def _join_warning_threads(before: set[threading.Thread]) -> None:
    for thread in set(threading.enumerate()) - before:
        if thread.name == "qte-sdk pacing warning":
            thread.join(5)


def test_a_chain_halt_warning_reaches_the_options_logger(caplog):
    before = set(threading.enumerate())
    with caplog.at_level(logging.WARNING, logger="qte_sdk.options"):
        options._ChainHaltWarnings().observe(halt_cancel())
        _join_warning_threads(before)
    records = [r for r in caplog.records if r.name == "qte_sdk.options"]
    assert len(records) == 1
    assert records[0].code == errors.OPTIONS_CHAIN_HALTED
    assert records[0].getMessage().startswith("QTE-OPTIONS-CHAIN-HALTED: the exchange ")
    assert not [r for r in caplog.records if r.name == "qte_sdk.pacing"]


def test_a_chain_halt_warning_withholds_a_token_from_its_record_fields(caplog, monkeypatch):
    # The exchange echoes the instrument an order named; a bot may have put the token there.
    token = "ab" * 16
    monkeypatch.setenv("QTE_TOKEN", token)
    before = set(threading.enumerate())
    with caplog.at_level(logging.WARNING, logger="qte_sdk.options"):
        options._ChainHaltWarnings().observe(halt_cancel(token))
        _join_warning_threads(before)
    [record] = [r for r in caplog.records if r.name == "qte_sdk.options"]
    assert record.fields["instrument"] == errors.WITHHELD
    assert token not in record.getMessage()
    assert token not in repr(vars(record))


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
    trades = unpack(payload, Trades)
    feed_only, residual, ordinary = trades.prints
    assert feed_only.kind == RESIDUAL and residual.kind == RESIDUAL
    assert ordinary.kind == STUDENT_TO_WALL
    assert feed_only.HasField("feed_only") and feed_only.feed_only
    assert not residual.HasField("feed_only")
    assert not ordinary.HasField("feed_only")
    assert [is_feed_only(p) for p in trades.prints] == [True, False, False]
