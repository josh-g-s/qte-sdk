"""A message type newer than this SDK is passed over.

An exchange may add a message type the installed SDK does not know, even inside the group
it sends on authentication. These tests pin what the SDK does with one: the message
arrives as an `Unknown` event in its place in the stream, counts towards the sequence, and
disturbs nothing around it: not the calendar, the resume, market data or later events.
"""

import asyncio

import pytest
from fake_exchange import frame, serve_local
from test_calendar import calendar_frame
from test_reconnect import Clock, resume, resume_ack
from test_resume import TERM, Scripted, book, kinds, order_state
from test_session import Server, ack, synthetic_token

from qte_sdk.connection import Received, ResumeComplete, SeqGap, Unknown
from qte_sdk.contract.v1.market_data_pb2 import Book
from qte_sdk.contract.v1.session_pb2 import Calendar
from qte_sdk.market_data import as_market_data, market_data
from qte_sdk.reconnect import Connected, Disconnected, ReconnectingSession
from qte_sdk.session import open_session

# A payload of a message type newer than this SDK. Its shape does not matter: the SDK does
# not know the type, so it never reads the payload.
NEWER = {"entries": [{"id": "SPY", "flag": True}], "count": "1"}


def newer(seq: int | None = None) -> str:
    return frame("newer_kind", NEWER, seq)


def numbered_book(seq: int) -> str:
    return frame("book", {"instrument": "SPY", "grid_time": str(seq)}, seq)


# The ack, the calendar, then a newer message in the authentication group; later a book,
# the newer message again, and another book.
AUTH_GROUP = [
    ack(),
    calendar_frame(2),
    newer(3),
    numbered_book(4),
    newer(5),
    numbered_book(6),
]


async def test_the_calendar_is_still_found_and_a_newer_message_follows_it_in_order():
    server = Server(*AUTH_GROUP, hold_open=False)
    async with serve_local(server) as url:
        async with await open_session(url, synthetic_token()) as session:
            calendar = await session.wait_for_calendar(timeout=5)
            events = [event async for event in session]
    assert isinstance(calendar, Calendar)
    assert [type(e).__name__ for e in events] == [
        "Received",
        "Unknown",
        "Received",
        "Unknown",
        "Received",
    ]
    assert isinstance(events[0], Received) and isinstance(events[0].message, Calendar)
    assert events[1] == Unknown("newer_kind", NEWER, 3)
    assert events[3] == Unknown("newer_kind", NEWER, 5)
    assert not any(isinstance(e, SeqGap) for e in events)


async def test_market_data_passes_over_a_newer_message():
    server = Server(*AUTH_GROUP, hold_open=False)
    async with serve_local(server) as url:
        async with await open_session(url, synthetic_token()) as session:
            items = [item async for item in market_data(session)]
    assert items == [
        Book(instrument="SPY", grid_time=4),
        Book(instrument="SPY", grid_time=6),
    ]
    assert as_market_data(Unknown("newer_kind", NEWER, 3)) is None


async def test_a_first_connect_resumes_with_a_newer_message_before_and_after_the_resume():
    exchange = Scripted(
        {
            "term": TERM,
            "before_resume": [newer(2)],
            "answer": [resume_ack()],
            "after": [newer(3), order_state(1), book(7)],
        }
    )
    async with serve_local(exchange) as url:
        rs = ReconnectingSession(url, synthetic_token(), sleep=Clock().sleep)
        async with rs:
            events = []
            async with asyncio.timeout(5):
                async for event in rs:
                    events.append(event)
                    if isinstance(event, Received) and event.type == "book":
                        break
    assert [e for e in exchange.received if e["type"] == "resume"] == [resume(0)]
    assert kinds(events) == [
        "Connected",
        "calendar:None",
        "Unknown",
        "resume_ack:None",
        "ResumeComplete",
        "Unknown",
        "order_state:1",
        "book:None",
    ]
    assert rs.last_report_seq == 1
    assert [e for e in events if isinstance(e, Unknown)] == [
        Unknown("newer_kind", NEWER, 2),
        Unknown("newer_kind", NEWER, 3),
    ]


@pytest.mark.parametrize("where", ["before the resume_ack", "right after the resume_ack"])
async def test_a_reconnect_resumes_with_a_newer_message_in_the_auth_group(where: str):
    if where == "before the resume_ack":
        second = {
            "term": TERM,
            "before_resume": [newer(2)],
            "answer": [resume_ack(True, 2), order_state(2)],
            "after": [newer(3), order_state(3)],
        }
    else:
        second = {
            "term": TERM,
            "answer": [resume_ack(True, 2), newer(2), order_state(2)],
            "after": [newer(3), order_state(3)],
        }
    exchange = Scripted(
        {
            "term": TERM,
            "before_resume": [newer(2)],
            "answer": [resume_ack()],
            "after": [order_state(1)],
            "drop": True,
        },
        second,
    )
    async with serve_local(exchange) as url:
        rs = ReconnectingSession(url, synthetic_token(), sleep=Clock().sleep)
        async with rs:
            events = []
            async with asyncio.timeout(5):
                async for event in rs:
                    events.append(event)
                    if isinstance(event, Received) and event.report_seq == 3:
                        break
            assert rs.last_report_seq == 3
    assert [e for e in exchange.received if e["type"] == "resume"] == [resume(0), resume(1)]
    reconnected = events[events.index(next(e for e in events if isinstance(e, Disconnected))) :]
    names = kinds(reconnected)
    assert names[:3] == ["Disconnected", "Retrying", "Connected"]
    assert isinstance(reconnected[2], Connected) and reconnected[2].resume is not None
    assert reconnected[2].resume.replayed
    # Every report arrives once and in order, the resume completes, and each newer message
    # keeps its place in the stream.
    in_group = (
        ["Unknown", "resume_ack:None"]
        if where == "before the resume_ack"
        else ["resume_ack:None", "Unknown"]
    )
    assert names[3:] == [
        "calendar:None",
        *in_group,
        "order_state:2",
        "ResumeComplete",
        "Unknown",
        "order_state:3",
    ]
    assert [e for e in reconnected if isinstance(e, Unknown)] == [
        Unknown("newer_kind", NEWER, 2),
        Unknown("newer_kind", NEWER, 3),
    ]
    assert not any(isinstance(e, SeqGap) for e in events)
    assert sum(isinstance(e, ResumeComplete) for e in events) == 2
