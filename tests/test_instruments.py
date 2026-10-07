"""The exchange's `instruments` message: decoding it, keeping the latest, and reading it."""

import asyncio

import pytest
from fake_exchange import frame, serve_local
from google.protobuf import json_format
from test_calendar import calendar_frame
from test_reconnect import Clock, resume_ack
from test_resume import TERM, Scripted, book, kinds, order_state
from test_session import Server, ack, synthetic_token

from qte_sdk.connection import DecodeFailed, Received, SeqGap
from qte_sdk.contract.v1.session_pb2 import Calendar
from qte_sdk.instruments import (
    EQUITY,
    INSTRUMENT_DISABLED,
    INSTRUMENT_KIND_UNSPECIFIED,
    INSTRUMENT_REDUCING_ONLY,
    INSTRUMENT_STATUS_UNSPECIFIED,
    INSTRUMENT_TRADING,
    OPTION,
    InstrumentInfo,
    Instruments,
    can_trade,
    instrument_info,
    instruments_by_id,
    on_tick,
    sector_of,
    tradable_instruments,
)
from qte_sdk.market_data import as_market_data, market_data
from qte_sdk.options import listed_contracts, option_underlyings, strike_increment
from qte_sdk.reconnect import ReconnectingSession
from qte_sdk.session import open_session

CALL_ID = "SPY261120C00665000"
PUT_ID = "SPY261120P00665000"

# Shaped like the contract's `Instruments`: sorted by id, an equity, a disabled equity, one
# the team may not trade, and a listed option contract.
TABLE = {
    "instruments": [
        {
            "instrument": "AAPL",
            "display_name": "Apple Inc.",
            "kind": "EQUITY",
            "tick_size": "10000",
            "lot_size": "1",
            "status": "INSTRUMENT_TRADING",
            "tradable": True,
            "sector_limit": "Information Technology",
        },
        {
            "instrument": "SPY",
            "kind": "EQUITY",
            "tick_size": "10000",
            "lot_size": "1",
            "status": "INSTRUMENT_TRADING",
            "tradable": True,
            "sector_limit": "Unsectored",
        },
        {
            "instrument": CALL_ID,
            "kind": "OPTION",
            "tick_size": "50000",
            "lot_size": "1",
            "status": "INSTRUMENT_TRADING",
            "tradable": True,
            "option": {
                "underlying": "SPY",
                "expiry": "2026-11-20",
                "right": "CALL",
                "strike": "665000000",
                "multiplier": "100",
            },
        },
        {
            "instrument": "XOM",
            "kind": "EQUITY",
            "tick_size": "10000",
            "lot_size": "1",
            "status": "INSTRUMENT_DISABLED",
            "tradable": True,
        },
        {
            "instrument": "ZZZ",
            "kind": "EQUITY",
            "tick_size": "10000",
            "lot_size": "1",
            "status": "INSTRUMENT_TRADING",
            "tradable": False,
        },
    ],
    "option_underlyings": [
        {"underlying": "GOOGL", "strike_increment": "2500000"},
        {"underlying": "SPY", "strike_increment": "5000000", "contracts": [CALL_ID, PUT_ID]},
    ],
}

# The table resent after a listing change: only AAPL is left.
CHANGED = {"instruments": [TABLE["instruments"][0]], "option_underlyings": []}


def instruments(seq: int, payload: dict = TABLE) -> str:
    return frame("instruments", payload, seq)


def numbered_book(seq: int) -> str:
    return frame("book", {"instrument": "SPY", "grid_time": str(seq)}, seq)


def table(payload: dict = TABLE) -> Instruments:
    return json_format.ParseDict(payload, Instruments())


# Session: keeping the latest


async def test_the_table_is_decoded_kept_and_replaced_by_a_resend():
    server = Server(
        ack(),
        calendar_frame(2),
        instruments(3),
        numbered_book(4),
        instruments(5, CHANGED),
        hold_open=False,
    )
    async with serve_local(server) as url:
        async with await open_session(url, synthetic_token()) as session:
            assert session.instrument_table is None
            found = await session.wait_for_instrument_table(timeout=5)
            assert found == table()
            assert session.calendar is not None  # read ahead, and kept
            assert await session.wait_for_instrument_table(timeout=0) is found
            events = [event async for event in session]
            assert session.instrument_table == table(CHANGED)
    assert [(e.type, e.seq) for e in events if isinstance(e, Received)] == [
        ("calendar", 2),
        ("instruments", 3),
        ("book", 4),
        ("instruments", 5),
    ]
    assert events[1].message == table()
    # The sector arrives on the wire with each equity; the option contract has none.
    sectors = {info.instrument: info.sector_limit for info in found.instruments}
    assert sectors["AAPL"] == "Information Technology"
    assert sectors["SPY"] == "Unsectored"
    assert sectors[CALL_ID] == ""


async def test_an_exchange_that_predates_instruments_is_recognised_at_once():
    # The message after the calendar is a book: no `instruments` is coming, so the wait
    # ends there rather than at its timeout, and the connection is still open.
    server = Server(ack(), calendar_frame(2), numbered_book(3))
    async with serve_local(server) as url:
        async with await open_session(url, synthetic_token()) as session:
            async with asyncio.timeout(5):
                assert await session.wait_for_instrument_table(timeout=None) is None
            assert session.instrument_table is None
            items = []
            async with asyncio.timeout(5):
                async for event in session:
                    items.append(event)
                    if isinstance(event, Received) and event.type == "book":
                        break
    assert [e.type for e in items] == ["calendar", "book"]


async def test_no_calendar_and_no_instruments_returns_none_when_the_connection_ends():
    server = Server(ack(), numbered_book(2), hold_open=False)
    async with serve_local(server) as url:
        async with await open_session(url, synthetic_token()) as session:
            assert await session.wait_for_instrument_table(timeout=5) is None
            assert [e.type async for e in session] == ["book"]


async def test_the_wait_times_out_when_nothing_follows_the_ack():
    server = Server(ack())
    async with serve_local(server) as url:
        async with await open_session(url, synthetic_token()) as session:
            assert await session.wait_for_instrument_table(timeout=0.2) is None


async def test_an_unreadable_table_ends_the_wait_and_is_reported():
    bad = {"instruments": [{"instrument": "AAPL", "tick_size": "not a number"}]}
    server = Server(ack(), calendar_frame(2), instruments(3, bad), hold_open=False)
    async with serve_local(server) as url:
        async with await open_session(url, synthetic_token()) as session:
            assert await session.wait_for_instrument_table(timeout=5) is None
            events = [event async for event in session]
    assert isinstance(events[1], DecodeFailed) and events[1].type == "instruments"


async def test_a_gap_between_the_calendar_and_the_table_does_not_hide_it():
    server = Server(ack(), calendar_frame(2), instruments(4), hold_open=False)
    async with serve_local(server) as url:
        async with await open_session(url, synthetic_token()) as session:
            assert await session.wait_for_instrument_table(timeout=5) == table()
            events = [event async for event in session]
    assert [type(e).__name__ for e in events] == ["Received", "SeqGap", "Received"]
    assert isinstance(events[1], SeqGap)


async def test_waiting_for_the_calendar_then_the_table_reads_each_once():
    server = Server(ack(), calendar_frame(2), instruments(3), numbered_book(4), hold_open=False)
    async with serve_local(server) as url:
        async with await open_session(url, synthetic_token()) as session:
            assert isinstance(await session.wait_for_calendar(timeout=5), Calendar)
            assert await session.wait_for_instrument_table(timeout=5) == table()
            events = [event async for event in session]
    assert [e.type for e in events] == ["calendar", "instruments", "book"]


async def test_market_data_leaves_the_table_out():
    server = Server(ack(), calendar_frame(2), instruments(3), numbered_book(4), hold_open=False)
    async with serve_local(server) as url:
        async with await open_session(url, synthetic_token()) as session:
            items = [item async for item in market_data(session)]
    assert [type(item).__name__ for item in items] == ["Book"]
    assert as_market_data(Received("instruments", table(), 3)) is None


async def test_kinds_and_statuses_from_a_newer_contract_decode_as_unknown():
    newer = {
        "instruments": [
            {
                "instrument": "ESZ6",
                "kind": "FUTURE",
                "tick_size": "250000",
                "lot_size": "1",
                "status": "INSTRUMENT_AUCTION",
                "tradable": True,
            }
        ]
    }
    server = Server(ack(), calendar_frame(2), instruments(3, newer), hold_open=False)
    async with serve_local(server) as url:
        async with await open_session(url, synthetic_token()) as session:
            found = await session.wait_for_instrument_table(timeout=5)
    assert found is not None
    [info] = found.instruments
    assert info.kind == INSTRUMENT_KIND_UNSPECIFIED
    assert info.status == INSTRUMENT_STATUS_UNSPECIFIED
    assert not can_trade(info)


# ReconnectingSession


async def test_a_reconnecting_session_keeps_the_latest_table_of_the_current_session():
    exchange = Scripted(
        {
            "term": TERM,
            "before_resume": [instruments(2)],
            "answer": [resume_ack()],
            "after": [order_state(1), instruments(3, CHANGED), book(7)],
        }
    )
    async with serve_local(exchange) as url:
        rs = ReconnectingSession(url, synthetic_token(), sleep=Clock().sleep)
        assert rs.instrument_table is None
        async with rs:
            events = []
            seen = []
            async with asyncio.timeout(5):
                async for event in rs:
                    events.append(event)
                    seen.append(rs.instrument_table)
                    if isinstance(event, Received) and event.type == "book":
                        break
    assert kinds(events) == [
        "Connected",
        "calendar:None",
        "instruments:None",
        "resume_ack:None",
        "ResumeComplete",
        "order_state:1",
        "instruments:None",
        "book:None",
    ]
    # Read ahead with the calendar, so already set at `Connected`; replaced by the resend.
    assert seen[0] == table()
    assert seen[-1] == table(CHANGED)


async def test_each_new_session_starts_without_the_earlier_table():
    exchange = Scripted(
        {
            "term": TERM,
            "before_resume": [instruments(2)],
            "answer": [resume_ack()],
            "after": [order_state(1)],
            "drop": True,
        },
        {"term": TERM, "answer": [resume_ack(True, 1)], "after": [order_state(2)]},
    )
    async with serve_local(exchange) as url:
        rs = ReconnectingSession(url, synthetic_token(), sleep=Clock().sleep)
        async with rs:
            tables = {}
            async with asyncio.timeout(5):
                async for event in rs:
                    tables[type(event).__name__ + str(len(tables))] = rs.instrument_table
                    if isinstance(event, Received) and event.report_seq == 2:
                        break
            assert rs.instrument_table is None  # the second session sent none
    assert table() in tables.values()


# Reading a table


def test_instrument_info_and_instruments_by_id():
    t = table()
    info = instrument_info(t, "AAPL")
    assert info is not None
    assert info.display_name == "Apple Inc."
    assert info.kind == EQUITY and info.tick_size == 10_000 and info.lot_size == 1
    assert instrument_info(t, "MSFT") is None
    by_id = instruments_by_id(t)
    assert list(by_id) == ["AAPL", "SPY", CALL_ID, "XOM", "ZZZ"]
    assert by_id[CALL_ID].kind == OPTION
    assert by_id[CALL_ID].option.strike == 665_000_000
    assert by_id[CALL_ID].option.multiplier == 100
    assert not by_id["SPY"].HasField("option")
    assert not by_id["SPY"].HasField("display_name")


def test_each_instruments_sector_for_the_sector_limit():
    by_id = instruments_by_id(table())
    assert by_id["AAPL"].sector_limit == "Information Technology"
    assert sector_of(by_id["AAPL"]) == "Information Technology"
    assert sector_of(by_id["SPY"]) == "Unsectored"
    # An option contract, or a term with no sectors set, has none.
    assert by_id[CALL_ID].sector_limit == ""
    assert sector_of(by_id[CALL_ID]) is None
    # A sector this SDK has never heard of is returned as it is, not refused.
    assert sector_of(InstrumentInfo(instrument="A", sector_limit="Space")) == "Space"


def test_can_trade_needs_a_known_kind_trading_status_and_entitlement():
    by_id = instruments_by_id(table())
    assert can_trade(by_id["AAPL"])
    assert can_trade(by_id[CALL_ID])
    assert not can_trade(by_id["XOM"])  # disabled
    assert not can_trade(by_id["ZZZ"])  # not tradable for this team
    reducing = InstrumentInfo(
        instrument=PUT_ID, kind=OPTION, status=INSTRUMENT_REDUCING_ONLY, tradable=True
    )
    assert not can_trade(reducing)
    assert not can_trade(InstrumentInfo(instrument="A", kind=EQUITY, tradable=True))
    assert by_id["XOM"].status == INSTRUMENT_DISABLED
    assert by_id["AAPL"].status == INSTRUMENT_TRADING


def test_tradable_instruments_by_kind():
    t = table()
    assert tradable_instruments(t) == ["AAPL", "SPY", CALL_ID]
    assert tradable_instruments(t, EQUITY) == ["AAPL", "SPY"]
    assert tradable_instruments(t, OPTION) == [CALL_ID]
    assert tradable_instruments(Instruments()) == []


def test_on_tick():
    by_id = instruments_by_id(table())
    assert on_tick(by_id["AAPL"], 199_970_000)
    assert not on_tick(by_id["AAPL"], 199_975_000)
    assert on_tick(by_id[CALL_ID], 1_050_000)
    assert not on_tick(by_id[CALL_ID], 1_060_000)
    assert not on_tick(InstrumentInfo(instrument="A", tick_size=0), 100)
    for price in (199.97, "199970000", True):
        with pytest.raises(TypeError, match="micro-dollars"):
            on_tick(by_id["AAPL"], price)


def test_the_chain_bootstrap_helpers():
    t = table()
    assert option_underlyings(t) == ["GOOGL", "SPY"]
    assert listed_contracts(t, "SPY") == (CALL_ID, PUT_ID)
    assert listed_contracts(t, "GOOGL") == ()  # an underlying with nothing listed yet
    assert listed_contracts(t, "AAPL") == ()  # not an option underlying
    assert strike_increment(t, "SPY") == 5_000_000
    assert strike_increment(t, "GOOGL") == 2_500_000
    assert strike_increment(t, "AAPL") is None
    assert option_underlyings(Instruments()) == []


def test_a_kind_this_sdk_has_no_name_for_is_not_tradable():
    info = InstrumentInfo(instrument="A", kind=99, status=INSTRUMENT_TRADING, tradable=True)
    assert not can_trade(info)
    assert tradable_instruments(Instruments(instruments=[info])) == []


async def test_a_table_read_ahead_is_never_replaced_by_an_older_one():
    # Both tables are read ahead before the resume_ack, while the attempt resumes: the
    # newer is already in force at `Connected`, and delivering the older event later must
    # not bring it back.
    exchange = Scripted(
        {
            "term": TERM,
            "before_resume": [instruments(2), instruments(3, CHANGED)],
            "answer": [resume_ack()],
            "after": [order_state(1), book(7)],
        }
    )
    async with serve_local(exchange) as url:
        rs = ReconnectingSession(url, synthetic_token(), sleep=Clock().sleep)
        async with rs:
            seen = []
            async with asyncio.timeout(5):
                async for event in rs:
                    seen.append(rs.instrument_table)
                    if isinstance(event, Received) and event.type == "book":
                        break
    assert seen[0] == table(CHANGED)
    assert all(t == table(CHANGED) for t in seen)
