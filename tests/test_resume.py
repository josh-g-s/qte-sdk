"""Heartbeats, liveness, report numbers and resume, against a scripted fake exchange."""

import asyncio
import json

import pytest
from fake_exchange import frame, serve_local
from test_calendar import CALENDAR, calendar_frame
from test_reconnect import EMPTY, Clock, auth, resume, resume_ack
from test_session import ack, assert_token_absent, session_reject, synthetic_token
from websockets.asyncio.server import ServerConnection
from websockets.exceptions import ConnectionClosedError

import qte_sdk.connection
from qte_sdk.connection import (
    DEFAULT_LIVENESS_TIMEOUT,
    HEARTBEAT_TIMEOUT_CLOSE_CODE,
    Connection,
    DecodeFailed,
    LivenessTimeout,
    Received,
    ReportGap,
    ResumeComplete,
    SeqGap,
    SessionRejected,
)
from qte_sdk.contract.v1.common_pb2 import BUY, RESTING, SELL, ReasonCodes
from qte_sdk.contract.v1.order_events_pb2 import OrderState, Reject
from qte_sdk.contract.v1.session_pb2 import OrderSnapshot, ResumeAck
from qte_sdk.market_data import as_market_data
from qte_sdk.reconnect import Connected, Disconnected, ReconnectingSession, is_retryable
from qte_sdk.resting import RestingOrders
from qte_sdk.session import (
    ResumeNotAcknowledged,
    ResumeRejected,
    _Reports,
    open_session,
)

PRICE = 199_970_000

# Report frames. None of them carries a seq, so the scripts need not number their frames.


def order_state(report_seq: int, price: int = PRICE, size: int = 100) -> str:
    payload = {
        "strat_id": "mm-1",
        "instrument": "AAPL",
        "side": "BUY",
        "price": str(price),
        "state": "RESTING",
        "remaining_size": str(size),
        "timestamp": "1",
    }
    return frame("order_state", payload, report_seq=str(report_seq))


def execution(report_seq: int, remaining: int, price: int = PRICE) -> str:
    payload = {
        "exec_id": f"x-{report_seq}",
        "origin": "TEAM",
        "strat_id": "mm-1",
        "instrument": "AAPL",
        "side": "BUY",
        "order_price": str(price),
        "fill_price": str(price),
        "fill_size": "10",
        "remaining_size": str(remaining),
        "timestamp": "2",
    }
    return frame("execution", payload, report_seq=str(report_seq))


def cancelled(report_seq: int, price: int = PRICE) -> str:
    payload = {
        "origin": "TEAM",
        "strat_id": "mm-1",
        "instrument": "AAPL",
        "side": "BUY",
        "price": str(price),
        "cancelled_size": "90",
        "reason_code": "REASON_CODE_UNSPECIFIED",
        "timestamp": "3",
    }
    return frame("order_cancelled", payload, report_seq=str(report_seq))


def snapshot(instrument: str, side: str, price: int, size: int) -> str:
    payload = {
        "strat_id": "mm-1",
        "instrument": instrument,
        "side": side,
        "price": str(price),
        "remaining_size": str(size),
        "timestamp": "4",
    }
    return frame("order_snapshot", payload)


def book(grid_time: int) -> str:
    return frame("book", {"instrument": "AAPL", "grid_time": str(grid_time)})


def heartbeat() -> str:
    return frame("heartbeat", {})


def resume_reject(detail: str = "a second resume") -> str:
    payload = {
        "request_type": "RESUME",
        "reason_code": "MALFORMED_MESSAGE",
        "reason_detail": detail,
    }
    return frame("reject", payload)


class Scripted:
    """Answers auth with an ack and sends `before_resume`; then, if `answer` is given,
    receives `resume` and sends `answer`; then sends `after` and holds the connection
    open. Each connection runs the next script in `scripts`, the last one repeating."""

    def __init__(self, *scripts: dict) -> None:
        self.scripts = scripts
        self.connections = 0
        self.received: list[dict] = []

    async def __call__(self, ws: ServerConnection) -> None:
        script = self.scripts[min(self.connections, len(self.scripts) - 1)]
        self.connections += 1
        self.received.append(json.loads(await ws.recv()))
        await ws.send(ack())
        for f in script.get("before_resume", ()):
            await ws.send(f)
        if script.get("answer") is not None:
            self.received.append(json.loads(await ws.recv()))
            for f in script["answer"]:
                await ws.send(f)
        for f in script.get("after", ()):
            await ws.send(f)
        if script.get("drop"):
            ws.transport.abort()
            return
        await ws.wait_closed()


async def take(events, count: int) -> list[object]:
    """The next `count` events, within 5 seconds."""
    out: list[object] = []
    async with asyncio.timeout(5):
        async for event in events:
            out.append(event)
            if len(out) == count:
                break
    return out


def kinds(events: list[object]) -> list[str]:
    return [
        f"{e.type}:{e.report_seq}" if isinstance(e, Received) else type(e).__name__ for e in events
    ]


# Heartbeats and liveness


async def test_heartbeats_are_absorbed_and_keep_a_quiet_link_alive():
    async def handler(ws: ServerConnection) -> None:
        await ws.recv()
        await ws.send(ack())
        for n in range(2, 8):
            await asyncio.sleep(0.05)
            await ws.send(frame("heartbeat", {}, n))
        await ws.send(frame("book", {"instrument": "AAPL", "grid_time": "1"}, 8))
        await ws.wait_closed()

    async with serve_local(handler) as url:
        session = await open_session(url, synthetic_token(), liveness_timeout=0.2)
        async with session:
            events = await take(session, 1)
    # 0.3 s of heartbeats outlasted the 0.2 s timeout; none of them was delivered, and
    # their seqs counted, so the book's seq 8 is not a gap.
    assert kinds(events) == ["book:None"]


async def test_silence_after_a_heartbeat_drops_the_link():
    async def handler(ws: ServerConnection) -> None:
        await ws.recv()
        await ws.send(ack())
        await ws.send(heartbeat())
        await ws.wait_closed()

    async with serve_local(handler) as url:
        session = await open_session(url, synthetic_token(), liveness_timeout=0.1)
        async with session:
            with pytest.raises(LivenessTimeout) as caught:
                await take(session, 1)
    assert caught.value.timeout == 0.1
    assert is_retryable(caught.value)
    assert caught.value.__cause__ is None and caught.value.__context__ is None


async def test_an_exchange_that_sends_no_heartbeats_is_never_dropped_for_being_quiet():
    async def handler(ws: ServerConnection) -> None:
        await ws.recv()
        await ws.send(ack())
        await asyncio.sleep(0.3)  # three times the timeout, with no heartbeat
        await ws.send(book(1))
        await ws.wait_closed()

    async with serve_local(handler) as url:
        session = await open_session(url, synthetic_token(), liveness_timeout=0.1)
        async with session:
            events = await take(session, 1)
    assert kinds(events) == ["book:None"]


def test_the_liveness_timeout_is_a_documented_choice_and_can_be_turned_off():
    assert Connection("ws://127.0.0.1:9").liveness_timeout == DEFAULT_LIVENESS_TIMEOUT
    assert Connection("ws://127.0.0.1:9", liveness_timeout=None).liveness_timeout is None
    for bad in (0, -1.0):
        with pytest.raises(ValueError):
            Connection("ws://127.0.0.1:9", liveness_timeout=bad)
    doc = " ".join((qte_sdk.connection.__doc__ or "").split())
    assert "not a value the exchange sends" in doc


async def test_a_dead_link_reconnects_and_resumes():
    exchange = Scripted(
        {"answer": [EMPTY], "after": [heartbeat(), order_state(1)]}, {"answer": [EMPTY]}
    )
    async with serve_local(exchange) as url:
        rs = ReconnectingSession(url, synthetic_token(), sleep=Clock().sleep, liveness_timeout=0.1)
        async with rs:
            events = await take(rs, 9)
    assert kinds(events) == [
        "Connected",
        "resume_ack:None",
        "ResumeComplete",
        "order_state:1",
        "Disconnected",
        "Retrying",
        "Connected",
        "resume_ack:None",
        "ResumeComplete",
    ]
    assert isinstance(events[4], Disconnected)
    assert isinstance(events[4].error, LivenessTimeout)
    assert exchange.received[3] == resume(1)


# Report numbers on one session


async def test_report_seq_is_read_from_the_envelope_and_absent_elsewhere():
    exchange = Scripted({"after": [order_state(11), book(1)]})
    async with serve_local(exchange) as url:
        async with await open_session(url, synthetic_token()) as session:
            events = await take(session, 2)
            assert session.last_report_seq == 11
    assert [getattr(e, "report_seq", None) for e in events] == [11, None]


async def test_a_report_gap_is_flagged_duplicates_are_dropped_and_the_cursor_never_skips():
    after = [order_state(1), order_state(2), order_state(4), order_state(2), order_state(3)]
    exchange = Scripted({"after": after})
    view = RestingOrders()
    async with serve_local(exchange) as url:
        async with await open_session(url, synthetic_token()) as session:
            events = await take(session, 5)
            cursor_seen = session.last_report_seq
    assert kinds(events) == [
        "order_state:1",
        "order_state:2",
        "ReportGap",
        "order_state:4",
        "order_state:3",  # the duplicate 2 was dropped
    ]
    assert events[2] == ReportGap(3, 4)
    assert cursor_seen == 4  # 3 filled the gap
    for event in events:
        view.apply(event)
    assert view.incomplete  # a gap needs a snapshot to put right
    assert as_market_data(events[2]) is None  # no market data was missed


# Resume on one session


async def test_a_full_replay_is_delivered_in_order_before_live_reports():
    exchange = Scripted(
        {
            # 11 arrives live before the ack: the replay covers it, so it is dropped.
            # 14 arrives live before the ack, and 13 during the replay: both wait.
            "before_resume": [order_state(11), order_state(14)],
            "answer": [
                resume_ack(True, 12),
                order_state(11),
                execution(13, 50),
                book(1),
                execution(12, 80),
            ],
        }
    )
    async with serve_local(exchange) as url:
        async with await open_session(url, synthetic_token()) as session:
            answer = await session.resume(10)
            events = await take(session, 7)
            assert session.last_report_seq == 14
    assert answer.replayed and answer.as_of_report_seq == 12 and answer.snapshot_count == 0
    assert exchange.received[1] == resume(10)
    assert kinds(events) == [
        "resume_ack:None",
        "order_state:11",
        "book:None",  # market data is never held
        "execution:12",
        "ResumeComplete",
        "execution:13",
        "order_state:14",
    ]
    assert events[4] == ResumeComplete(True, 12, 0)


async def test_a_replay_with_nothing_missed_completes_at_once():
    exchange = Scripted({"answer": [resume_ack(True, 7)], "after": [order_state(8)]})
    async with serve_local(exchange) as url:
        async with await open_session(url, synthetic_token()) as session:
            await session.resume(7)
            events = await take(session, 3)
    assert kinds(events) == ["resume_ack:None", "ResumeComplete", "order_state:8"]


async def test_a_resume_beyond_the_window_gets_a_snapshot_that_replaces_the_view():
    exchange = Scripted(
        {
            # Both arrive live before the ack: 49 is covered by the snapshot, 51 is not.
            "before_resume": [order_state(49), order_state(51, price=PRICE - 1)],
            "answer": [
                resume_ack(False, 50, 2),
                snapshot("AAPL", "BUY", PRICE, 70),
                snapshot("MSFT", "SELL", 400_000_000, 5),
            ],
            "after": [execution(52, 60)],
        }
    )
    view = RestingOrders()
    view.apply(OrderState(instrument="OLD", side=BUY, price=1, state=RESTING, remaining_size=1))
    view.mark_incomplete()
    seen: list[tuple[str, bool, list[str]]] = []
    async with serve_local(exchange) as url:
        async with await open_session(url, synthetic_token()) as session:
            await session.resume(3)
            events = []
            async for event in view.follow(session):
                events.append(event)
                instruments = sorted({o.key.instrument for o in view})
                seen.append((kinds([event])[0], view.incomplete, instruments))
                if isinstance(event, Received) and event.type == "execution":
                    break
            assert session.last_report_seq == 52
    assert seen == [
        # The old view stands until the snapshot is complete.
        ("resume_ack:None", True, ["OLD"]),
        ("order_snapshot:None", True, ["OLD"]),
        ("order_snapshot:None", True, ["OLD"]),
        ("ResumeComplete", False, ["AAPL", "MSFT"]),
        ("order_state:51", False, ["AAPL", "MSFT"]),
        ("execution:52", False, ["AAPL", "MSFT"]),
    ]
    entry = view.get("AAPL", BUY, PRICE)
    assert entry is not None and entry.remaining_size == 60 and entry.state == RESTING
    assert view.get("AAPL", BUY, PRICE - 1) is not None
    assert view.get("MSFT", SELL, 400_000_000) is not None


async def test_an_out_of_hours_resume_gets_the_empty_marker():
    exchange = Scripted({"answer": [resume_ack(False, 0, 0)]})
    view = RestingOrders()
    view.mark_incomplete()
    async with serve_local(exchange) as url:
        async with await open_session(url, synthetic_token()) as session:
            answer = await session.resume(0)
            events = await take(session, 2)
    for event in events:
        view.apply(event)
    assert not answer.replayed and answer.snapshot_count == 0
    assert kinds(events) == ["resume_ack:None", "ResumeComplete"]
    assert len(view) == 0 and not view.incomplete


async def test_a_seq_gap_during_a_replay_stops_waiting_for_it():
    numbered = [
        frame("resume_ack", {"replayed": True, "as_of_report_seq": "12"}, 2),
        frame("order_state", json.loads(order_state(11))["payload"], 3, report_seq="11"),
        # 12 is lost: seq 4 never arrives.
        frame("order_state", json.loads(order_state(13))["payload"], 5, report_seq="13"),
        frame("book", {"instrument": "AAPL", "grid_time": "1"}, 6),
    ]
    exchange = Scripted({"answer": numbered})
    async with serve_local(exchange) as url:
        async with await open_session(url, synthetic_token()) as session:
            await session.resume(10)
            events = await take(session, 6)
    assert kinds(events) == [
        "resume_ack:None",
        "order_state:11",
        "SeqGap",
        "ReportGap",
        "order_state:13",
        "book:None",
    ]
    assert events[2] == SeqGap(4, 5) and events[3] == ReportGap(12, 13)


async def test_an_unreadable_frame_during_a_snapshot_stops_waiting_for_it():
    exchange = Scripted(
        {
            "before_resume": [order_state(10)],
            "answer": [resume_ack(False, 9, 2), "not json", order_state(11)],
        }
    )
    async with serve_local(exchange) as url:
        async with await open_session(url, synthetic_token()) as session:
            await session.resume(0)
            events = await take(session, 4)
    # 10 was held and is released, with the gap from the cursor flagged; 11 then follows.
    assert kinds(events) == ["resume_ack:None", "DecodeFailed", "ReportGap", "order_state:10"]


async def test_a_rejected_resume_raises_and_releases_what_it_held():
    exchange = Scripted({"before_resume": [order_state(5)], "answer": [resume_reject()]})
    async with serve_local(exchange) as url:
        async with await open_session(url, synthetic_token()) as session:
            with pytest.raises(ResumeRejected) as caught:
                await session.resume(3)
            events = await take(session, 2)
    assert caught.value.reason_name == "MALFORMED_MESSAGE"
    # The cursor is back where it was before the resume: no starting point yet.
    assert kinds(events) == ["reject:None", "order_state:5"]
    assert session.last_report_seq == 5


async def test_an_unanswered_resume_times_out():
    exchange = Scripted({"answer": []})
    async with serve_local(exchange) as url:
        async with await open_session(url, synthetic_token()) as session:
            with pytest.raises(TimeoutError):
                await session.resume(0, timeout=0.1)


async def test_a_resume_cut_off_by_the_connection_raises_not_acknowledged():
    exchange = Scripted({"answer": [], "drop": True})
    async with serve_local(exchange) as url:
        async with await open_session(url, synthetic_token()) as session:
            with pytest.raises(ResumeNotAcknowledged):
                await session.resume(0)


async def test_resume_is_sent_once_with_a_valid_cursor():
    exchange = Scripted({"answer": [EMPTY]})
    async with serve_local(exchange) as url:
        async with await open_session(url, synthetic_token()) as session:
            for bad in (-1, True, 1.5):
                with pytest.raises(ValueError):
                    await session.resume(bad)  # type: ignore[arg-type]
            await session.resume(0)
            with pytest.raises(RuntimeError):
                await session.resume(0)


async def test_an_undecodable_snapshot_leaves_the_view_incomplete():
    exchange = Scripted(
        {"answer": [resume_ack(False, 9, 1), frame("order_snapshot", {"price": "not a number"})]}
    )
    view = RestingOrders()
    async with serve_local(exchange) as url:
        async with await open_session(url, synthetic_token()) as session:
            await session.resume(0)
            events = await take(session, 3)
    for event in events:
        view.apply(event)
    assert kinds(events) == ["resume_ack:None", "DecodeFailed", "ResumeComplete"]
    assert isinstance(events[1], DecodeFailed)
    assert view.incomplete


# Resume across reconnects


async def test_a_reconnect_replays_what_was_missed_and_completes_the_view_again():
    exchange = Scripted(
        # The first session starts from a snapshot of one order, then sees report 3.
        {
            "answer": [resume_ack(False, 2, 1), snapshot("AAPL", "BUY", PRICE, 100)],
            "after": [order_state(3, size=100)],
            "drop": True,
        },
        # Reports 4 and 5 were sent while disconnected; 3 is sent again and dropped.
        {"answer": [resume_ack(True, 5), order_state(3), execution(4, 40), cancelled(5)]},
    )
    view = RestingOrders()
    async with serve_local(exchange) as url:
        rs = ReconnectingSession(url, synthetic_token(), resting=view, sleep=Clock().sleep)
        async with rs:
            events = []
            async for event in rs:
                events.append(event)
                if isinstance(event, Disconnected):
                    at_disconnect = (view.incomplete, len(view))
                if isinstance(event, ResumeComplete) and event.replayed:
                    break
            assert rs.last_report_seq == 5
            at_end = (view.incomplete, len(view))
    assert [e for e in exchange.received if e["type"] == "resume"] == [resume(0), resume(3)]
    assert kinds(events) == [
        "Connected",
        "resume_ack:None",
        "order_snapshot:None",
        "ResumeComplete",
        "order_state:3",
        "Disconnected",
        "Retrying",
        "Connected",
        "resume_ack:None",
        "execution:4",
        "order_cancelled:5",
        "ResumeComplete",
    ]
    assert isinstance(events[7], Connected) and events[7].resume is not None
    assert events[7].resume.replayed
    assert at_disconnect == (True, 1)
    # The replay brought the view up to date: the order filled in part, then cancelled.
    assert at_end == (False, 0)


async def test_a_reconnect_beyond_the_window_gets_a_snapshot():
    exchange = Scripted(
        {"answer": [EMPTY], "after": [order_state(1)], "drop": True},
        {"answer": [resume_ack(False, 900, 1), snapshot("AAPL", "BUY", PRICE + 1, 3)]},
    )
    view = RestingOrders()
    async with serve_local(exchange) as url:
        rs = ReconnectingSession(url, synthetic_token(), resting=view, sleep=Clock().sleep)
        async with rs:
            async for event in rs:
                if isinstance(event, ResumeComplete) and event.as_of_report_seq == 900:
                    break
            assert rs.last_report_seq == 900
            assert not view.incomplete
            assert [o.key.price for o in view] == [PRICE + 1]


async def test_resuming_can_be_turned_off():
    exchange = Scripted({"after": [order_state(1)], "drop": True}, {"after": [order_state(4)]})
    async with serve_local(exchange) as url:
        rs = ReconnectingSession(url, synthetic_token(), resume=False, sleep=Clock().sleep)
        async with rs:
            events = await take(rs, 7)
    assert all(e["type"] == "auth" for e in exchange.received)
    # The cursor carries over, so the reports missed meanwhile are noticed.
    assert kinds(events) == [
        "Connected",
        "order_state:1",
        "Disconnected",
        "Retrying",
        "Connected",
        "ReportGap",
        "order_state:4",
    ]
    assert isinstance(events[0], Connected) and events[0].resume is None


async def test_a_refused_resume_leaves_the_session_up_without_it():
    token = synthetic_token()
    exchange = Scripted(
        {"answer": [resume_reject(f"no resume for {token}")], "after": [order_state(4)]}
    )
    async with serve_local(exchange) as url:
        rs = ReconnectingSession(url, token, sleep=Clock().sleep)
        async with rs:
            events = await take(rs, 3)
    assert exchange.connections == 1
    assert exchange.received == [auth(token), resume(0)]
    assert kinds(events) == ["Connected", "reject:None", "order_state:4"]
    assert isinstance(events[0], Connected) and events[0].resume is None
    # The reject repeated the token; the delivered copy does not.
    assert_token_absent(token, repr(events) + str(events[1].message))  # type: ignore[attr-defined]
    assert "<token withheld>" in events[1].message.reason_detail  # type: ignore[attr-defined]


async def test_an_exchange_that_serves_neither_heartbeats_nor_resume_keeps_working():
    # How a gateway that predates resume answers it: a plain MALFORMED_MESSAGE reject with
    # no request_type and no request_ref. It sends no heartbeat and no report_seq.
    unknown_type = frame(
        "reject",
        {"reason_code": "MALFORMED_MESSAGE", "reason_detail": 'unknown message type "resume"'},
    )
    plain_report = frame(
        "order_state",
        {"instrument": "AAPL", "side": "BUY", "price": str(PRICE), "state": "RESTING"},
    )
    exchange = Scripted(
        {"answer": [unknown_type], "after": [plain_report], "drop": True},
        {"answer": [unknown_type], "after": [book(1)]},
    )
    view = RestingOrders()
    async with serve_local(exchange) as url:
        rs = ReconnectingSession(
            url, synthetic_token(), resting=view, sleep=Clock().sleep, liveness_timeout=0.1
        )
        async with rs:
            events = await take(rs, 8)
            assert rs.last_report_seq is None
    assert kinds(events) == [
        "Connected",
        "reject:None",
        "order_state:None",
        "Disconnected",
        "Retrying",
        "Connected",
        "reject:None",
        "book:None",
    ]
    assert all(e.resume is None for e in events if isinstance(e, Connected))
    # As before resume existed: the view keeps what it saw and stays incomplete.
    assert view.incomplete and len(view) == 1


async def test_an_unanswered_resume_fails_the_attempt_and_is_retried():
    exchange = Scripted({"answer": []}, {"answer": [EMPTY]})
    async with serve_local(exchange) as url:
        rs = ReconnectingSession(url, synthetic_token(), ack_timeout=0.2, sleep=Clock().sleep)
        async with rs:
            events = await take(rs, 4)
    assert kinds(events) == ["Disconnected", "Retrying", "Connected", "resume_ack:None"]
    assert isinstance(events[0], Disconnected) and isinstance(events[0].error, TimeoutError)


# Edge cases a review found


async def test_an_order_reject_before_the_ack_does_not_fail_the_session():
    release_reject = frame(
        "reject",
        {"request_type": "NEW", "reason_code": "MALFORMED_MESSAGE", "request_ref": "r-1"},
        report_seq="7",
    )

    async def handler(ws: ServerConnection) -> None:
        await ws.recv()
        await ws.send(release_reject)  # a private report, sent before the ack
        await ws.send(ack())
        await ws.wait_closed()

    async with serve_local(handler) as url:
        async with await open_session(url, synthetic_token()) as session:
            events = await take(session, 1)
    assert kinds(events) == ["reject:7"]


async def test_a_session_rejected_while_resuming_is_not_retried():
    exchange = Scripted({"answer": [session_reject("TEAM_DISABLED")]})
    async with serve_local(exchange) as url:
        rs = ReconnectingSession(url, synthetic_token(), sleep=Clock().sleep)
        with pytest.raises(SessionRejected) as caught:
            async with rs:
                async for _ in rs:
                    pass
    assert not isinstance(caught.value, ResumeRejected)
    assert caught.value.reason_name == "TEAM_DISABLED"
    assert exchange.connections == 1


def test_a_view_is_incomplete_while_a_snapshot_arrives():
    view = RestingOrders()
    view.apply(ResumeAck(replayed=False, as_of_report_seq=50, snapshot_count=2))
    view.apply(OrderSnapshot(instrument="AAPL", side=BUY, price=PRICE, remaining_size=7))
    assert view.incomplete and len(view) == 0
    view.apply(OrderSnapshot(instrument="AAPL", side=BUY, price=PRICE + 1, remaining_size=7))
    view.apply(ResumeComplete(False, 50, 2))
    assert not view.incomplete and len(view) == 2


def test_a_replay_does_not_complete_while_a_gap_below_as_of_is_open():
    reports = _Reports()
    for n in (10, 12):  # 11 is missing; 12 is delivered beyond the gap
        reports.route(Received("order_state", OrderState(), None, report_seq=n))
    assert reports.cursor == 10
    reports.begin(10)
    ack_event = Received("resume_ack", ResumeAck(replayed=True, as_of_report_seq=12), None)
    assert [type(e).__name__ for e in reports.route(ack_event)] == ["Received"]
    replayed = [
        reports.route(Received("order_state", OrderState(), None, report_seq=n)) for n in (11, 12)
    ]
    assert [[type(e).__name__ for e in r] for r in replayed] == [["Received"], ["ResumeComplete"]]
    assert reports.cursor == 12


def test_a_snapshot_sets_the_cursor_exactly_even_lower_at_a_new_term():
    reports = _Reports()
    reports.begin(1000)  # a cursor from the last term
    for n in (4, 5):  # live reports of the new term, held until the snapshot completes
        reports.route(Received("order_state", OrderState(), None, report_seq=n))
    ack_event = Received(
        "resume_ack", ResumeAck(replayed=False, as_of_report_seq=3, snapshot_count=0), None
    )
    routed = reports.route(ack_event)
    assert [type(e).__name__ for e in routed] == [
        "Received",
        "ResumeComplete",
        "Received",
        "Received",
    ]
    assert [getattr(e, "report_seq", None) for e in routed[2:]] == [4, 5]
    assert reports.cursor == 5


def test_a_snapshot_forgets_reports_counted_beyond_the_old_cursor():
    reports = _Reports()
    for n in (10, 12):  # 12 is counted beyond a gap at 11, in the old term
        reports.route(Received("order_state", OrderState(), None, report_seq=n))
    reports.begin(10)
    ack_event = Received(
        "resume_ack", ResumeAck(replayed=False, as_of_report_seq=3, snapshot_count=0), None
    )
    reports.route(ack_event)
    delivered = [
        reports.route(Received("order_state", OrderState(), None, report_seq=n))
        for n in range(4, 14)
    ]
    # Every report of the new term is delivered, 12 included.
    assert all(len(d) == 1 for d in delivered)
    assert reports.cursor == 13


def test_only_an_untyped_malformed_reject_answers_a_resume():
    reports = _Reports()
    reports.begin(0)
    newer_type = Received(
        "reject",
        Reject(reason_code=ReasonCodes.MALFORMED_MESSAGE),
        None,
        {"request_type": "A_NEWER_REQUEST", "reason_code": "MALFORMED_MESSAGE"},
    )
    other_reason = Received(
        "reject", Reject(reason_code=ReasonCodes.NOT_AUTHENTICATED), None, {"reason_code": "X"}
    )
    for event in (newer_type, other_reason):
        assert reports.route(event) == [event]
        assert reports.answer is None
    untyped = Received(
        "reject",
        Reject(reason_code=ReasonCodes.MALFORMED_MESSAGE),
        None,
        {"reason_code": "MALFORMED_MESSAGE"},
    )
    reports.route(untyped)
    assert reports.answer is untyped and reports.resume is None


# Staying alive against the exchange's heartbeat rule


async def test_a_heartbeat_timeout_close_is_shown_and_retried():
    calls = 0

    async def handler(ws: ServerConnection) -> None:
        nonlocal calls
        calls += 1
        if calls > 1:
            await Scripted({"answer": [EMPTY]})(ws)
            return
        await ws.recv()
        await ws.send(ack())
        await ws.recv()  # resume
        await ws.send(EMPTY)
        await ws.close(HEARTBEAT_TIMEOUT_CLOSE_CODE, "heartbeat timeout")

    async with serve_local(handler) as url:
        rs = ReconnectingSession(url, synthetic_token(), sleep=Clock().sleep)
        async with rs:
            events = await take(rs, 7)
    assert kinds(events)[3:] == ["Disconnected", "Retrying", "Connected", "resume_ack:None"]
    error = events[3].error  # type: ignore[attr-defined]
    assert isinstance(error, ConnectionClosedError) and is_retryable(error)
    assert error.rcvd is not None
    assert (error.rcvd.code, error.rcvd.reason) == (4000, "heartbeat timeout")


async def test_a_heartbeat_between_the_ack_and_the_calendar_or_after_it_changes_nothing():
    async def handler(ws: ServerConnection) -> None:
        await ws.recv()
        await ws.send(ack())
        await ws.send(heartbeat())
        await ws.send(calendar_frame(2))
        await ws.send(heartbeat())
        await ws.send(book(1))
        await ws.wait_closed()

    async with serve_local(handler) as url:
        async with await open_session(url, synthetic_token()) as session:
            assert await session.wait_for_calendar() == CALENDAR
            events = await take(session, 2)
    assert kinds(events) == ["calendar:None", "book:None"]


async def test_a_loop_that_stops_reading_is_closed_by_the_keepalive_and_says_why():
    async def handler(ws: ServerConnection) -> None:
        await ws.recv()
        await ws.send(ack())
        for n in range(50):
            await ws.send(book(n))
        await ws.wait_closed()

    async with serve_local(handler) as url:
        session = await open_session(
            url,
            synthetic_token(),
            max_queue=1,
            ping_interval=0.05,
            ping_timeout=0.1,
            close_timeout=0.2,
        )
        async with session:
            # The loop is busy elsewhere and reads nothing until the connection has closed.
            ws = session.connection._ws  # noqa: SLF001
            assert ws is not None
            await asyncio.wait_for(ws.wait_closed(), 5)
            with pytest.raises(ConnectionClosedError) as caught:
                async with asyncio.timeout(5):
                    async for _ in session:
                        pass
    assert caught.value.sent is not None
    assert (caught.value.sent.code, caught.value.sent.reason) == (1011, "keepalive ping timeout")
    assert is_retryable(caught.value)


# The order and numbering the exchange's gateway produces


async def test_a_snapshot_in_the_gateways_order_is_applied_before_later_live_reports():
    # The gateway's order: live reports numbered before the resume (at or below as_of)
    # may come first; then resume_ack; then every order_snapshot, written ahead of any
    # queued live frame; then live reports above as_of.
    exchange = Scripted(
        {
            "before_resume": [order_state(41), execution(42, 90)],
            "answer": [
                resume_ack(False, 42, 2),
                snapshot("AAPL", "BUY", PRICE, 90),
                snapshot("MSFT", "SELL", 400_000_000, 5),
            ],
            "after": [execution(43, 40), cancelled(44, price=PRICE)],
        }
    )
    view = RestingOrders()
    seen: list[tuple[str, bool, int]] = []
    async with serve_local(exchange) as url:
        async with await open_session(url, synthetic_token()) as session:
            await session.resume(0)
            async for event in view.follow(session):
                seen.append((kinds([event])[0], view.incomplete, len(view)))
                if isinstance(event, Received) and event.type == "order_cancelled":
                    break
            assert session.last_report_seq == 44
    assert seen == [
        ("resume_ack:None", True, 0),  # 41 and 42 are covered by the snapshot: dropped
        ("order_snapshot:None", True, 0),
        ("order_snapshot:None", True, 0),
        ("ResumeComplete", False, 2),
        ("execution:43", False, 2),
        ("order_cancelled:44", False, 1),
    ]
    assert view.get("AAPL", BUY, PRICE) is None
    assert view.get("MSFT", SELL, 400_000_000) is not None


async def test_a_record_the_exchange_cannot_map_leaves_no_gap():
    # The gateway skips a record it cannot map without giving it a number, so the
    # numbering the client sees stays contiguous: no ReportGap, and the view stays
    # complete. Its effect on the team's orders is not reported at all.
    exchange = Scripted({"after": [order_state(7), execution(8, 50)]})
    view = RestingOrders()
    seen: list[tuple[str, bool]] = []
    async with serve_local(exchange) as url:
        async with await open_session(url, synthetic_token()) as session:
            async for event in view.follow(session):
                seen.append((kinds([event])[0], view.incomplete))
                if len(seen) == 2:
                    break
    assert seen == [("order_state:7", False), ("execution:8", False)]
