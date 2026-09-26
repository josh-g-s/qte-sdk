"""The session calendar: receiving it after the ack, living without it, and the helpers."""

import asyncio

import pytest
from fake_exchange import frame, serve_local
from test_reconnect import Clock, Exchange, book, drop, session
from test_session import ack, synthetic_token
from websockets.asyncio.server import ServerConnection
from websockets.exceptions import ConnectionClosedError

from qte_sdk.calendar import next_close, next_open, next_session, session_open_at
from qte_sdk.connection import DecodeFailed, Received, SeqGap
from qte_sdk.contract import codec
from qte_sdk.contract.v1.session_pb2 import Calendar
from qte_sdk.reconnect import Connected, Disconnected, ReconnectingSession
from qte_sdk.session import TOKEN_ENV_VAR, open_session

# A synthetic term shaped like one whose configured range ends on a holiday: its last
# session is the day before `term_end`. Times are plain exchange timestamps; the helpers
# only ever compare them, so any increasing integers will do.
DAY = 1_000_000


def day(n: int, *, early: bool = False) -> dict:
    close = 600_000 if early else 800_000
    return {
        "session_date": f"2027-06-{n:02d}",
        "open_time": str(n * DAY + 400_000),
        "close_time": str(n * DAY + close),
        "early_close": early,
    }


def opens(n: int) -> int:
    return n * DAY + 400_000


def closes(n: int, *, early: bool = False) -> int:
    return n * DAY + (600_000 if early else 800_000)


# Trading days 14, 15, 17 (an early close), with a holiday on 16 and one on 18, the
# configured end of the term. (Real terms skip weekends too; the helpers do not care why a
# day has no session.)
CALENDAR_PAYLOAD = {
    "term_first_session": "2027-06-14",
    "term_last_session": "2027-06-17",
    "sessions": [day(14), day(15), day(17, early=True)],
    "holidays": [
        {"date": "2027-06-16", "name": "A midweek holiday"},
        {"date": "2027-06-18", "name": "A holiday on the last day of the term"},
    ],
    "next_open": str(opens(14)),
    "term_start": "2027-06-14",
    "term_end": "2027-06-18",
}
CALENDAR = codec.from_dict(CALENDAR_PAYLOAD, Calendar)


def calendar_frame(seq: int, payload: dict = CALENDAR_PAYLOAD) -> str:
    return frame("calendar", payload, seq)


@pytest.fixture(autouse=True)
def no_token_in_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(TOKEN_ENV_VAR, raising=False)


def scripted(*steps: str | float, hold_open: bool = False):
    """A server that receives `auth`, then sends each frame, sleeping for each number."""

    async def handler(ws: ServerConnection) -> None:
        await ws.recv()
        for step in steps:
            if isinstance(step, str):
                await ws.send(step)
            else:
                await asyncio.sleep(step)
        if hold_open:
            await ws.wait_closed()
        else:
            await ws.close()

    return handler


# The wire message


def test_the_calendar_decodes_from_the_wire_with_every_field():
    assert CALENDAR.term_first_session == "2027-06-14"
    assert CALENDAR.term_last_session == "2027-06-17"
    assert [entry.session_date for entry in CALENDAR.sessions] == [
        "2027-06-14",
        "2027-06-15",
        "2027-06-17",
    ]
    assert CALENDAR.sessions[2].early_close and not CALENDAR.sessions[0].early_close
    assert CALENDAR.sessions[0].open_time == opens(14)
    assert [holiday.date for holiday in CALENDAR.holidays] == ["2027-06-16", "2027-06-18"]
    assert CALENDAR.HasField("next_open") and CALENDAR.next_open == opens(14)
    # The configured range can run past the last session: its last day is a holiday.
    assert (CALENDAR.term_start, CALENDAR.term_end) == ("2027-06-14", "2027-06-18")


# Receiving it on a session


async def test_a_calendar_right_after_the_ack_is_delivered_and_kept_on_the_session():
    handler = scripted(ack(), calendar_frame(2), book(3))
    async with serve_local(handler) as url:
        async with await open_session(url, synthetic_token()) as sess:
            # open_session does not wait for the calendar.
            assert sess.calendar is None
            events = []
            async for event in sess:
                events.append(event)
                if isinstance(event, Received) and event.type == "calendar":
                    assert sess.calendar == CALENDAR
    assert [(event.type, event.seq) for event in events] == [("calendar", 2), ("book", 3)]
    assert events[0].message == CALENDAR
    assert sess.calendar == CALENDAR


async def test_waiting_for_the_calendar_keeps_every_event_for_iteration_in_order():
    handler = scripted(ack(), book(2), calendar_frame(3), book(4))
    async with serve_local(handler) as url:
        async with await open_session(url, synthetic_token()) as sess:
            assert await sess.wait_for_calendar(timeout=5) == CALENDAR
            assert sess.calendar == CALENDAR
            # Already here: returned at once.
            assert await sess.wait_for_calendar(timeout=0) == CALENDAR
            events = [event async for event in sess]
    assert [(event.type, event.seq) for event in events] == [
        ("book", 2),
        ("calendar", 3),
        ("book", 4),
    ]


async def test_a_calendar_sent_before_the_ack_is_kept_too():
    handler = scripted(calendar_frame(1), ack(), hold_open=True)
    async with serve_local(handler) as url:
        async with await open_session(url, synthetic_token()) as sess:
            assert sess.calendar == CALENDAR
            assert await sess.wait_for_calendar(timeout=0) == CALENDAR


async def test_a_later_calendar_on_the_same_session_replaces_the_first():
    later = {**CALENDAR_PAYLOAD, "sessions": [day(15)]}
    handler = scripted(ack(), calendar_frame(2), calendar_frame(3, later))
    async with serve_local(handler) as url:
        async with await open_session(url, synthetic_token()) as sess:
            _ = [event async for event in sess]
            assert [entry.session_date for entry in sess.calendar.sessions] == ["2027-06-15"]


# An exchange that never sends a calendar


async def test_without_a_calendar_the_session_works_and_waiting_times_out_with_none():
    # An exchange that predates the calendar message: the ack, then market data later.
    handler = scripted(ack(), 0.3, book(2), book(3))
    async with serve_local(handler) as url:
        async with await open_session(url, synthetic_token()) as sess:
            assert sess.calendar is None
            assert await sess.wait_for_calendar(timeout=0.05) is None
            # The timed-out wait lost nothing and broke nothing: the next frames still
            # arrive, with no gap reported.
            events = [event async for event in sess]
            assert sess.calendar is None
    assert [(event.type, event.seq) for event in events] == [("book", 2), ("book", 3)]
    assert not any(isinstance(event, SeqGap) for event in events)


async def test_waiting_returns_none_when_the_connection_ends_first_and_iteration_ends_normally():
    handler = scripted(ack(), book(2))
    async with serve_local(handler) as url:
        async with await open_session(url, synthetic_token()) as sess:
            assert await sess.wait_for_calendar(timeout=5) is None
            events = [event async for event in sess]
    assert [(event.type, event.seq) for event in events] == [("book", 2)]


async def test_a_drop_while_waiting_is_raised_by_iteration_after_the_kept_events():
    async def handler(ws: ServerConnection) -> None:
        await ws.recv()
        await ws.send(ack())
        await ws.send(book(2))
        await asyncio.sleep(0.1)
        ws.transport.abort()

    async with serve_local(handler) as url:
        async with await open_session(url, synthetic_token()) as sess:
            assert await sess.wait_for_calendar(timeout=5) is None
            events = []
            with pytest.raises(ConnectionClosedError):
                async for event in sess:
                    events.append(event)
    assert [(event.type, event.seq) for event in events] == [("book", 2)]


async def test_an_undecodable_calendar_ends_the_wait_and_is_reported_on_iteration():
    handler = scripted(ack(), calendar_frame(2, {"sessions": "not a list"}), hold_open=True)
    async with serve_local(handler) as url:
        async with await open_session(url, synthetic_token()) as sess:
            async with asyncio.timeout(5):
                assert await sess.wait_for_calendar(timeout=None) is None
                event = await anext(aiter(sess))
    assert isinstance(event, DecodeFailed) and event.type == "calendar"


async def test_waiting_while_another_task_reads_the_session_is_refused():
    handler = scripted(ack(), hold_open=True)
    async with serve_local(handler) as url:
        async with await open_session(url, synthetic_token()) as sess:

            async def read() -> None:
                async for _ in sess:
                    pass

            reader = asyncio.create_task(read())
            await asyncio.sleep(0.05)
            with pytest.raises(RuntimeError, match="one place"):
                await sess.wait_for_calendar(timeout=1)
            reader.cancel()
            with pytest.raises(asyncio.CancelledError):
                await reader


# The helpers: next open and close from the calendar alone


def test_before_the_term_the_next_open_and_close_are_the_first_session():
    now = opens(14) - 1
    assert next_open(CALENDAR, now) == opens(14)
    assert next_close(CALENDAR, now) == closes(14)
    assert session_open_at(CALENDAR, now) is None


def test_during_a_session_the_next_close_is_its_own_and_the_next_open_is_tomorrow():
    now = opens(14) + 1
    assert session_open_at(CALENDAR, now) == CALENDAR.sessions[0]
    assert next_close(CALENDAR, now) == closes(14)
    assert next_open(CALENDAR, now) == opens(15)


def test_at_the_open_instant_the_session_is_open_and_the_next_open_is_the_following_one():
    assert session_open_at(CALENDAR, opens(14)) == CALENDAR.sessions[0]
    assert next_open(CALENDAR, opens(14)) == opens(15)
    # At the close instant the session is over.
    assert session_open_at(CALENDAR, closes(14)) is None
    assert next_close(CALENDAR, closes(14)) == closes(15)


def test_the_next_open_skips_a_holiday():
    after_close = closes(15) + 1  # the 16th is a holiday, with no session
    assert session_open_at(CALENDAR, after_close) is None
    assert session_open_at(CALENDAR, 16 * DAY + 500_000) is None
    assert next_session(CALENDAR, after_close) == CALENDAR.sessions[2]
    assert next_open(CALENDAR, after_close) == opens(17)
    # The next session is an early close, and its close says so.
    assert next_close(CALENDAR, after_close) == closes(17, early=True)


def test_after_the_last_open_there_is_no_next_open_but_still_a_close():
    now = opens(17) + 1
    assert next_open(CALENDAR, now) is None
    assert next_session(CALENDAR, now) is None
    assert next_close(CALENDAR, now) == closes(17, early=True)


def test_after_the_last_close_nothing_opens_or_closes_up_to_term_end_and_beyond():
    # term_end is the 18th, a holiday: the range runs a day past the last session.
    for now in (closes(17, early=True), 18 * DAY + 500_000, 30 * DAY):
        assert next_open(CALENDAR, now) is None
        assert next_close(CALENDAR, now) is None
        assert session_open_at(CALENDAR, now) is None


def test_the_helpers_do_not_rely_on_the_sessions_being_in_order():
    shuffled = Calendar()
    shuffled.CopyFrom(CALENDAR)
    del shuffled.sessions[:]
    shuffled.sessions.extend([CALENDAR.sessions[2], CALENDAR.sessions[0], CALENDAR.sessions[1]])
    now = opens(14) - 1
    assert next_open(shuffled, now) == opens(14)
    assert next_close(shuffled, now) == closes(14)


def test_an_empty_calendar_has_no_next_open_or_close():
    assert next_open(Calendar(), 0) is None
    assert next_close(Calendar(), 0) is None
    assert session_open_at(Calendar(), 0) is None


# A reconnecting session


async def test_a_reconnecting_session_refreshes_the_calendar_from_each_new_session():
    second = {**CALENDAR_PAYLOAD, "sessions": [day(15), day(17, early=True)]}
    exchange = Exchange(
        session(calendar_frame(2), book(3), then=drop),
        session(calendar_frame(2, second), book(3)),
    )
    seen: list[object] = []
    calendars: list[Calendar | None] = []
    async with serve_local(exchange) as url:
        rs = ReconnectingSession(url, synthetic_token(), sleep=Clock().sleep)
        async with rs, asyncio.timeout(5):
            async for event in rs:
                seen.append(event)
                if isinstance(event, Connected):
                    calendars.append(rs.calendar)
                elif isinstance(event, Received) and event.type == "book":
                    calendars.append(rs.calendar)
                    if event.seq == 3 and len(calendars) == 4:
                        await rs.close()
    # None before the first calendar; the first session's; still the first when the
    # second session comes up; then the second session's.
    assert calendars == [None, CALENDAR, CALENDAR, codec.from_dict(second, Calendar)]
    assert any(isinstance(event, Disconnected) for event in seen)


async def test_a_reconnect_to_an_exchange_without_a_calendar_keeps_the_last_one():
    exchange = Exchange(session(calendar_frame(2), then=drop), session(book(2)))
    async with serve_local(exchange) as url:
        rs = ReconnectingSession(url, synthetic_token(), sleep=Clock().sleep)
        async with rs, asyncio.timeout(5):
            async for event in rs:
                if isinstance(event, Received) and event.type == "book":
                    break
        assert rs.calendar == CALENDAR
