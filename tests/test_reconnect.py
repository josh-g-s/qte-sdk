import asyncio
import contextlib
import json
import ssl
import traceback
from collections.abc import Awaitable, Callable
from contextlib import aclosing

import pytest
from fake_exchange import frame, serve_local
from test_session import ack, assert_token_absent, session_reject, synthetic_token
from websockets.asyncio.client import ClientConnection
from websockets.asyncio.server import ServerConnection
from websockets.exceptions import ConnectionClosedError
from websockets.protocol import State

import qte_sdk.reconnect
from qte_sdk.connection import (
    Connection,
    ContractVersionMismatch,
    HandshakeFailed,
    Received,
    SeqGap,
    SessionRejected,
)
from qte_sdk.contract.v1.common_pb2 import BUY, ReasonCodes
from qte_sdk.contract.v1.session_pb2 import Auth
from qte_sdk.market_data import subscribe, unsubscribe
from qte_sdk.reconnect import (
    Backoff,
    Connected,
    Disconnected,
    NotConnected,
    ReconnectingSession,
    Retrying,
    is_retryable,
)
from qte_sdk.resting import RestingOrders
from qte_sdk.session import TOKEN_ENV_VAR, AuthNotSent, MissingToken, SessionNotAcknowledged

# The fake exchange. Each connection runs the next script in turn; the last one repeats.

Script = Callable[[ServerConnection, "Exchange"], Awaitable[None]]


class Exchange:
    def __init__(self, *scripts: Script) -> None:
        self.scripts = scripts
        self.connections = 0
        self.received: list[dict] = []
        self.closed = asyncio.Event()

    async def __call__(self, ws: ServerConnection) -> None:
        index = self.connections
        self.connections += 1
        await self.scripts[min(index, len(self.scripts) - 1)](ws, self)

    async def recv(self, ws: ServerConnection) -> dict:
        message = json.loads(await ws.recv())
        self.received.append(message)
        return message


def book(seq: int, instrument: str = "AAPL") -> str:
    return frame("book", {"instrument": instrument, "grid_time": str(seq)}, seq)


def resting_order(seq: int) -> str:
    payload = {
        "strat_id": "mm-1",
        "instrument": "AAPL",
        "side": "BUY",
        "price": "199970000",
        "state": "RESTING",
        "remaining_size": "100",
        "timestamp": "1",
    }
    return frame("order_state", payload, seq)


async def drop(ws: ServerConnection, exchange: Exchange) -> None:
    ws.transport.abort()


async def close_normally(ws: ServerConnection, exchange: Exchange) -> None:
    await ws.close()


async def hold(ws: ServerConnection, exchange: Exchange) -> None:
    await ws.wait_closed()
    exchange.closed.set()


def session(
    *frames: str, subscribed: bool = False, then: Script = hold, recv_after: int = 0
) -> Script:
    """Receive auth, acknowledge it, receive the subscription if one is expected, send
    `frames`, receive `recv_after` more messages, then end with `then`."""

    async def script(ws: ServerConnection, exchange: Exchange) -> None:
        await exchange.recv(ws)
        await ws.send(ack())
        if subscribed:
            await exchange.recv(ws)
        for f in frames:
            await ws.send(f)
        for _ in range(recv_after):
            await exchange.recv(ws)
        await then(ws, exchange)

    return script


def refused(reply: str) -> Script:
    async def script(ws: ServerConnection, exchange: Exchange) -> None:
        await exchange.recv(ws)
        await ws.send(reply)
        await ws.wait_closed()

    return script


async def closed_before_ack(ws: ServerConnection, exchange: Exchange) -> None:
    await exchange.recv(ws)
    await ws.close()


def shown(error: BaseException) -> str:
    """What a traceback showing local variables could print for `error` and its chain,
    leaving out this module's own frames, which hold the token by design."""
    parts = [str(error), repr(error)]
    pending = [traceback.TracebackException.from_exception(error, capture_locals=True)]
    seen: set[int] = set()
    while pending:
        link = pending.pop()
        if id(link) in seen:
            continue
        seen.add(id(link))
        parts.extend(link.format_exception_only())
        for summary in link.stack:
            if summary.filename != __file__:
                parts.append(f"{summary.filename}:{summary.lineno} {summary.locals}")
        pending.extend(n for n in (link.__cause__, link.__context__) if n is not None)
    return "\n".join(parts)


class Clock:
    """A stand-in for asyncio.sleep that records each wait and returns at once."""

    def __init__(self) -> None:
        self.waits: list[float] = []

    async def sleep(self, delay: float) -> None:
        self.waits.append(delay)
        await asyncio.sleep(0)


def auth(token: str) -> dict:
    return {"version": "0.x", "type": "auth", "payload": {"token": token}}


def subscription(*instruments: str) -> dict:
    return {"version": "0.x", "type": "subscribe", "payload": {"instruments": list(instruments)}}


@pytest.fixture(autouse=True)
def no_token_in_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(TOKEN_ENV_VAR, raising=False)


# A disconnect, in order: uncertainty, then a fresh session, auth and subscription


async def test_a_drop_flags_uncertainty_then_reconnects_with_fresh_seq_auth_and_subscriptions():
    token = synthetic_token()
    view = RestingOrders()
    clock = Clock()
    exchange = Exchange(
        # Seqs 1 to 3, then a second subscription arrives and the socket drops.
        session(book(2), resting_order(3), subscribed=True, recv_after=1, then=drop),
        # A new session numbers its messages from 1 again.
        session(book(2), subscribed=True),
    )
    seen: list[object] = []
    at_disconnect: dict[str, object] = {}
    async with serve_local(exchange) as url:
        rs = ReconnectingSession(
            url, token, instruments=["AAPL"], resting=view, sleep=clock.sleep, rng=lambda: 0.0
        )
        async with rs:
            async for event in rs:
                seen.append(event)
                if isinstance(event, Connected) and not event.reconnected:
                    await subscribe(rs, ["MSFT"])
                elif isinstance(event, Disconnected):
                    at_disconnect = {
                        "incomplete": view.incomplete,
                        "connected": rs.connected,
                        "connections": exchange.connections,
                        "waits": list(clock.waits),
                    }
                elif isinstance(event, Received) and len(exchange.received) == 5:
                    await rs.close()
        await asyncio.wait_for(exchange.closed.wait(), 5)

    kinds = [type(event).__name__ for event in seen]
    assert kinds == [
        "Connected",
        "Received",
        "Received",
        "Disconnected",
        "Retrying",
        "Connected",
        "Received",
    ]
    first, second = seen[0], seen[5]
    assert isinstance(first, Connected) and first.instruments == ("AAPL",)
    assert first.reconnected is False
    # The data-uncertainty event came first: the view was already incomplete, and no new
    # connection had been attempted.
    assert at_disconnect == {
        "incomplete": True,
        "connected": False,
        "connections": 1,
        "waits": [],
    }
    disconnected = seen[3]
    assert isinstance(disconnected, Disconnected)
    assert isinstance(disconnected.error, ConnectionClosedError)
    assert seen[4] == Retrying(1, 0.5, None)
    # Then a new session: authenticated again and subscribed to the same instruments.
    assert exchange.received == [
        auth(token),
        subscription("AAPL"),
        subscription("MSFT"),
        auth(token),
        subscription("AAPL", "MSFT"),
    ]
    assert isinstance(second, Connected) and second.reconnected is True
    assert second.instruments == ("AAPL", "MSFT")
    # Sequence tracking started afresh: seq 2 on the new session is not a gap.
    assert not any(isinstance(event, SeqGap) for event in seen)
    assert seen[6] == Received("book", seen[1].message, 2)  # type: ignore[attr-defined]
    # The view saw the resting order and stays incomplete: no resume, no snapshot.
    assert view.get("AAPL", BUY, 199_970_000) is not None
    assert view.incomplete


async def test_a_normal_close_by_the_exchange_is_a_disconnect_too():
    clock = Clock()
    exchange = Exchange(session(then=close_normally), session())
    events: list[object] = []
    async with serve_local(exchange) as url:
        async with ReconnectingSession(url, synthetic_token(), sleep=clock.sleep) as rs:
            async for event in rs:
                events.append(event)
                if isinstance(event, Connected) and event.reconnected:
                    await rs.close()
    assert [type(e) for e in events] == [Connected, Disconnected, Retrying, Connected]
    assert events[1] == Disconnected(None)
    assert exchange.received == [auth(exchange.received[0]["payload"]["token"])] * 2


async def test_unsubscribed_instruments_are_not_subscribed_again():
    clock = Clock()
    exchange = Exchange(session(subscribed=True, recv_after=1, then=drop), session(subscribed=True))
    async with serve_local(exchange) as url:
        rs = ReconnectingSession(
            url, synthetic_token(), instruments=["AAPL", "MSFT"], sleep=clock.sleep
        )
        async with rs:
            async for event in rs:
                if isinstance(event, Connected):
                    if event.reconnected:
                        break
                    await unsubscribe(rs, ["AAPL"])
    assert exchange.received[-1] == subscription("MSFT")
    assert rs.instruments == ("MSFT",)


async def test_nothing_is_sent_or_queued_while_disconnected():
    clock = Clock()
    exchange = Exchange(session(subscribed=True, then=drop), session(subscribed=True))
    failures: list[BaseException] = []
    async with serve_local(exchange) as url:
        rs = ReconnectingSession(url, synthetic_token(), instruments=["AAPL"], sleep=clock.sleep)
        with pytest.raises(NotConnected):
            await subscribe(rs, ["MSFT"])  # before the first session
        async with rs:
            async for event in rs:
                if isinstance(event, Disconnected):
                    try:
                        await subscribe(rs, ["MSFT"])
                    except NotConnected as error:
                        failures.append(error)
                if isinstance(event, Connected) and event.reconnected:
                    break
    assert len(failures) == 1
    # Neither refused subscription was recorded or sent later.
    assert exchange.received[-1] == subscription("AAPL")
    assert rs.instruments == ("AAPL",)


# Backoff


def test_the_backoff_doubles_up_to_its_cap():
    backoff = Backoff(initial=0.5, maximum=5.0, factor=2.0, jitter=0.5)
    assert [backoff.delay(n) for n in range(1, 9)] == [0.5, 1, 2, 4, 5, 5, 5, 5]
    # A very late attempt is still capped rather than overflowing.
    assert backoff.delay(100_000) == 5.0


def test_jitter_only_ever_shortens_the_wait():
    backoff = Backoff(initial=1.0, maximum=8.0, jitter=0.5)
    assert backoff.delay(4, 0.0) == 8.0
    assert backoff.delay(4, 0.5) == 6.0
    assert 4.0 < backoff.delay(4, 0.999999) < 4.001
    assert Backoff(jitter=0.0).delay(3, 0.9) == 2.0


@pytest.mark.parametrize(
    "options",
    [
        {"initial": -1},
        {"initial": 10, "maximum": 5},
        {"factor": 0.5},
        {"jitter": 1.5},
        {"max_attempts": 0},
    ],
)
def test_a_backoff_that_makes_no_sense_is_refused(options: dict):
    with pytest.raises(ValueError):
        Backoff(**options)


async def test_reconnect_attempts_wait_with_capped_jittered_backoff_and_can_give_up():
    clock = Clock()
    draws = iter([0.0, 0.5, 0.0, 0.0, 0.2])
    backoff = Backoff(initial=1.0, maximum=4.0, factor=2.0, jitter=0.5, max_attempts=5)
    exchange = Exchange(session(then=drop), closed_before_ack)
    events: list[object] = []
    async with serve_local(exchange) as url:
        rs = ReconnectingSession(
            url, synthetic_token(), backoff=backoff, sleep=clock.sleep, rng=lambda: next(draws)
        )
        with pytest.raises(SessionNotAcknowledged):
            async with rs:
                async for event in rs:
                    events.append(event)
    assert clock.waits == [1.0, 1.5, 4.0, 4.0, 3.6]
    retries = [event for event in events if isinstance(event, Retrying)]
    assert [r.attempt for r in retries] == [1, 2, 3, 4, 5]
    assert retries[0].error is None
    assert all(isinstance(r.error, SessionNotAcknowledged) for r in retries[1:])
    assert exchange.connections == 6  # the first session and five failed attempts


async def test_the_first_connection_retries_without_waiting_first():
    clock = Clock()
    exchange = Exchange(closed_before_ack, closed_before_ack, session())
    async with serve_local(exchange) as url:
        async with ReconnectingSession(
            url, synthetic_token(), sleep=clock.sleep, rng=lambda: 0.0
        ) as rs:
            async for event in rs:
                if isinstance(event, Connected):
                    assert event.reconnected is False
                    break
    assert clock.waits == [0.5, 1.0]


async def test_a_failed_attempt_resets_after_a_session_comes_up():
    clock = Clock()
    exchange = Exchange(
        session(then=drop),
        closed_before_ack,
        session(then=drop),
        session(),
    )
    async with serve_local(exchange) as url:
        async with ReconnectingSession(
            url, synthetic_token(), sleep=clock.sleep, rng=lambda: 0.0
        ) as rs:
            connected = 0
            async for event in rs:
                if isinstance(event, Connected):
                    connected += 1
                    if connected == 3:
                        break
    assert clock.waits == [0.5, 1.0, 0.5]


async def test_an_unreachable_exchange_is_retried_until_max_attempts():
    async with serve_local(Exchange(hold)) as url:
        pass  # the server is gone, so its port refuses connections
    clock = Clock()
    rs = ReconnectingSession(
        url,
        synthetic_token(),
        backoff=Backoff(initial=0.5, maximum=2.0, max_attempts=6),
        sleep=clock.sleep,
        rng=lambda: 0.0,
    )
    with pytest.raises(OSError):
        async for _ in rs:
            pass
    assert clock.waits == [0.5, 1.0, 2.0, 2.0, 2.0]


# Reconnecting turned off


async def test_with_reconnecting_off_iteration_ends_after_the_disconnect():
    view = RestingOrders()
    clock = Clock()
    exchange = Exchange(session(book(2), then=drop), session())
    async with serve_local(exchange) as url:
        rs = ReconnectingSession(
            url, synthetic_token(), resting=view, backoff=None, sleep=clock.sleep
        )
        async with rs:
            events = [event async for event in rs]
    assert [type(e) for e in events] == [Connected, Received, Disconnected]
    assert view.incomplete
    assert exchange.connections == 1
    assert clock.waits == []


async def test_with_reconnecting_off_a_first_connection_failure_is_raised_at_once():
    clock = Clock()
    exchange = Exchange(closed_before_ack, session())
    async with serve_local(exchange) as url:
        rs = ReconnectingSession(url, synthetic_token(), backoff=None, sleep=clock.sleep)
        with pytest.raises(SessionNotAcknowledged):
            async for _ in rs:
                pass
    assert exchange.connections == 1
    assert clock.waits == []


# Failures that will not fix themselves stop the session


@pytest.mark.parametrize(
    ("reply", "error_type"),
    [
        (session_reject("NOT_AUTHENTICATED", "unknown credentials"), SessionRejected),
        (session_reject("TEAM_DISABLED"), SessionRejected),
        (session_reject("VERSION_MISMATCH"), ContractVersionMismatch),
        (session_reject("A_REASON_FROM_A_NEWER_CONTRACT"), SessionRejected),
    ],
)
async def test_a_rejected_reconnect_is_raised_and_not_retried(reply: str, error_type: type):
    clock = Clock()
    exchange = Exchange(session(then=drop), refused(reply), session())
    events: list[object] = []
    async with serve_local(exchange) as url:
        rs = ReconnectingSession(url, synthetic_token(), sleep=clock.sleep)
        with pytest.raises(error_type):
            async with rs:
                async for event in rs:
                    events.append(event)
    assert [type(e) for e in events] == [Connected, Disconnected, Retrying]
    assert exchange.connections == 2
    assert len(clock.waits) == 1


async def test_a_rejected_first_connection_is_raised_and_not_retried():
    clock = Clock()
    exchange = Exchange(refused(session_reject("NOT_AUTHENTICATED")), session())
    async with serve_local(exchange) as url:
        rs = ReconnectingSession(url, synthetic_token(), sleep=clock.sleep)
        with pytest.raises(SessionRejected):
            async for _ in rs:
                pass
    assert exchange.connections == 1
    assert clock.waits == []


async def test_a_session_rejected_while_open_is_flagged_then_raised():
    view = RestingOrders()
    clock = Clock()
    rejected = frame("session_reject", {"reason_code": "TEAM_DISABLED"}, 2)
    exchange = Exchange(session(rejected), session())
    events: list[object] = []
    async with serve_local(exchange) as url:
        rs = ReconnectingSession(url, synthetic_token(), resting=view, sleep=clock.sleep)
        with pytest.raises(SessionRejected) as caught:
            async with rs:
                async for event in rs:
                    events.append(event)
    assert [type(e) for e in events] == [Connected, Disconnected]
    assert events[1] == Disconnected(caught.value)
    assert caught.value.reason_code == ReasonCodes.TEAM_DISABLED
    assert view.incomplete
    assert exchange.connections == 1


@pytest.mark.parametrize(
    ("error", "retryable"),
    [
        (ConnectionClosedError(None, None), True),
        (ConnectionRefusedError(), True),
        (ssl.SSLCertVerificationError(), False),
        (TimeoutError(), True),
        (SessionNotAcknowledged("closed"), True),
        (AuthNotSent("could not send auth"), False),
        (HandshakeFailed("InvalidMessage", None), True),
        (HandshakeFailed("InvalidHeader", None), False),
        (HandshakeFailed("NegotiationError", None), False),
        (HandshakeFailed("InvalidStatus", 503), True),
        (HandshakeFailed("InvalidStatus", 429), True),
        (HandshakeFailed("InvalidStatus", 404), False),
        (HandshakeFailed("InvalidStatus", 401), False),
        (SessionRejected(ReasonCodes.NOT_AUTHENTICATED, None), False),
        (SessionRejected(ReasonCodes.EXCHANGE_OUTAGE, None), False),
        (ContractVersionMismatch(ReasonCodes.VERSION_MISMATCH, None), False),
        (ValueError("a bug"), False),
        (asyncio.CancelledError(), False),
    ],
)
def test_which_errors_are_retried(error: BaseException, retryable: bool):
    assert is_retryable(error) is retryable


# Closing and cancelling


async def test_cancelling_during_a_backoff_wait_stops_without_another_attempt():
    waiting = asyncio.Event()

    async def stalled_sleep(delay: float) -> None:
        waiting.set()
        await asyncio.Event().wait()

    view = RestingOrders()
    exchange = Exchange(session(then=drop), session())
    async with serve_local(exchange) as url:
        rs = ReconnectingSession(url, synthetic_token(), resting=view, sleep=stalled_sleep)

        async def consume() -> None:
            async for _ in rs:
                pass

        task = asyncio.create_task(consume())
        await asyncio.wait_for(waiting.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await rs.close()
    assert exchange.connections == 1
    assert view.incomplete


async def test_closing_during_a_backoff_wait_ends_iteration_without_another_attempt():
    exchange = Exchange(session(then=drop), session())
    async with serve_local(exchange) as url:
        rs = ReconnectingSession(url, synthetic_token())

        async def close_then_wait(delay: float) -> None:
            await rs.close()

        rs._sleep = close_then_wait
        events = [event async for event in rs]
    assert [type(e) for e in events] == [Connected, Disconnected, Retrying]
    assert exchange.connections == 1


async def test_leaving_the_loop_early_and_closing_closes_the_socket():
    view = RestingOrders()
    exchange = Exchange(session(book(2)))
    async with serve_local(exchange) as url:
        async with ReconnectingSession(url, synthetic_token(), resting=view) as rs:
            async with aclosing(rs.events()) as events:
                async for event in events:
                    if isinstance(event, Received):
                        break
            assert view.incomplete  # the view gets no more events
        await asyncio.wait_for(exchange.closed.wait(), 5)
    assert not rs.connected


async def test_cancelling_the_consumer_mid_session_closes_the_socket():
    view = RestingOrders()
    up = asyncio.Event()
    exchange = Exchange(session())
    async with serve_local(exchange) as url:
        rs = ReconnectingSession(url, synthetic_token(), resting=view)

        async def consume() -> None:
            async for event in rs:
                if isinstance(event, Connected):
                    up.set()

        task = asyncio.create_task(consume())
        await asyncio.wait_for(up.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.wait_for(exchange.closed.wait(), 5)
    assert view.incomplete
    assert not rs.connected


async def test_closing_the_iterator_early_closes_the_socket():
    exchange = Exchange(session(book(2)))
    async with serve_local(exchange) as url:
        rs = ReconnectingSession(url, synthetic_token())
        async with aclosing(rs.events()) as events:
            async for event in events:
                if isinstance(event, Received):
                    break
        await asyncio.wait_for(exchange.closed.wait(), 5)


async def test_closing_while_the_exchange_has_not_acknowledged_stops_the_attempt():
    received_auth = asyncio.Event()

    async def never_ack(ws: ServerConnection, exchange: Exchange) -> None:
        await exchange.recv(ws)
        received_auth.set()
        await hold(ws, exchange)

    exchange = Exchange(never_ack)
    async with serve_local(exchange) as url:
        rs = ReconnectingSession(url, synthetic_token(), ack_timeout=None)
        consumer = asyncio.create_task(asyncio.wait_for(anext(rs.events(), None), 5))
        await asyncio.wait_for(received_auth.wait(), 5)
        await rs.close()
        # The socket is closed by the time close() returns, not only once the consumer runs.
        await asyncio.wait_for(exchange.closed.wait(), 5)
        assert await consumer is None  # iteration ended, with nothing delivered


async def test_closing_while_handling_retrying_starts_no_wait():
    async def forever(delay: float) -> None:
        await asyncio.Event().wait()

    exchange = Exchange(session(then=drop), session())
    events: list[object] = []
    async with serve_local(exchange) as url:
        rs = ReconnectingSession(url, synthetic_token(), sleep=forever)

        async def consume() -> None:
            async for event in rs:
                events.append(event)
                if isinstance(event, Retrying):
                    await rs.close()

        await asyncio.wait_for(consume(), 5)
    assert [type(e) for e in events] == [Connected, Disconnected, Retrying]
    assert exchange.connections == 1


async def test_closing_ends_iteration_in_a_task_that_once_caught_a_cancellation():
    exchange = Exchange(session(then=drop), session())
    events: list[object] = []
    async with serve_local(exchange) as url:
        rs = ReconnectingSession(
            url, synthetic_token(), backoff=Backoff(initial=3600, maximum=3600)
        )
        started = asyncio.Event()

        async def consume() -> None:
            started.set()
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                pass  # caught, and the task carries on
            async for event in rs:
                events.append(event)
                if isinstance(event, Retrying):
                    asyncio.get_running_loop().call_soon(asyncio.ensure_future, rs.close())

        task = asyncio.create_task(consume())
        await started.wait()
        task.cancel()
        await asyncio.wait_for(task, 5)
    assert [type(e) for e in events] == [Connected, Disconnected, Retrying]


async def test_cancelling_just_as_a_session_opens_closes_that_session(monkeypatch):
    real_open = qte_sdk.reconnect.open_session
    consumer: asyncio.Task | None = None

    async def open_then_cancel(*args, **kwargs):
        opened = await real_open(*args, **kwargs)
        # The consumer is cancelled after the session opened but before it resumes.
        assert consumer is not None
        asyncio.get_running_loop().call_soon(consumer.cancel)
        return opened

    monkeypatch.setattr(qte_sdk.reconnect, "open_session", open_then_cancel)
    exchange = Exchange(session())
    async with serve_local(exchange) as url:
        rs = ReconnectingSession(url, synthetic_token())
        consumer = asyncio.create_task(anext(rs.events()))
        with pytest.raises(asyncio.CancelledError):
            await consumer
        await asyncio.wait_for(exchange.closed.wait(), 5)


async def test_closing_cuts_a_backoff_wait_short():
    view = RestingOrders()
    exchange = Exchange(session(then=drop), session())
    events: list[object] = []
    async with serve_local(exchange) as url:
        rs = ReconnectingSession(
            url, synthetic_token(), resting=view, backoff=Backoff(initial=3600, maximum=3600)
        )

        async def consume() -> None:
            async for event in rs:
                events.append(event)
                if isinstance(event, Retrying):
                    asyncio.get_running_loop().call_soon(asyncio.ensure_future, rs.close())

        await asyncio.wait_for(consume(), 5)
    assert [type(e) for e in events] == [Connected, Disconnected, Retrying]
    assert exchange.connections == 1


async def test_closing_marks_the_view_incomplete_at_once():
    view = RestingOrders()
    exchange = Exchange(session(book(2)))
    async with serve_local(exchange) as url:
        rs = ReconnectingSession(url, synthetic_token(), resting=view)
        events = rs.events()
        assert isinstance(await anext(events), Connected)
        assert not view.incomplete
        await rs.close()  # the iterator is left suspended
        assert view.incomplete
        await events.aclose()


async def test_overlapping_closes_all_wait_for_one_shutdown_even_if_one_is_cancelled():
    received_auth = asyncio.Event()

    async def never_ack(ws: ServerConnection, exchange: Exchange) -> None:
        await exchange.recv(ws)
        received_auth.set()
        await hold(ws, exchange)

    exchange = Exchange(never_ack)
    async with serve_local(exchange) as url:
        rs = ReconnectingSession(url, synthetic_token(), ack_timeout=None)
        consumer = asyncio.create_task(anext(rs.events(), None))
        await asyncio.wait_for(received_auth.wait(), 5)
        first = asyncio.create_task(rs.close())
        second = asyncio.create_task(rs.close())
        await asyncio.sleep(0)
        first.cancel()
        await asyncio.wait_for(second, 5)
        await asyncio.wait_for(exchange.closed.wait(), 5)
        assert await consumer is None


async def test_no_event_is_delivered_or_applied_after_close():
    view = RestingOrders()
    acknowledged = json.loads(ack())
    acknowledged["seq"] = 2

    async def order_then_ack(ws: ServerConnection, exchange: Exchange) -> None:
        await exchange.recv(ws)
        await ws.send(resting_order(1))  # arrives before the ack, so it is buffered
        await ws.send(json.dumps(acknowledged))
        await hold(ws, exchange)

    exchange = Exchange(order_then_ack)
    events: list[object] = []
    async with serve_local(exchange) as url:
        rs = ReconnectingSession(url, synthetic_token(), resting=view)
        async for event in rs:
            events.append(event)
            if isinstance(event, Connected):
                await rs.close()
    assert [type(e) for e in events] == [Connected]
    assert len(view) == 0 and view.incomplete


async def test_an_auth_message_that_cannot_be_encoded_is_not_retried():
    clock = Clock()
    exchange = Exchange(session())
    async with serve_local(exchange) as url:
        rs = ReconnectingSession(url, synthetic_token(), sleep=clock.sleep, contract_version=123)
        with pytest.raises(AuthNotSent):
            async for _ in rs:
                pass
    assert exchange.connections == 1
    assert clock.waits == []


async def test_close_waits_for_a_socket_the_iterator_is_still_closing():
    exchange = Exchange(session())
    async with serve_local(exchange) as url:
        rs = ReconnectingSession(url, synthetic_token())
        events = rs.events()
        assert isinstance(await anext(events), Connected)
        ws = rs._session.connection._ws  # type: ignore[union-attr]
        closing = asyncio.create_task(events.aclose())
        await asyncio.sleep(0)
        closing.cancel()  # the iterator's own cleanup is interrupted
        await rs.close()
        assert ws.state is State.CLOSED
        with contextlib.suppress(asyncio.CancelledError):
            await closing


async def test_close_does_not_interrupt_a_cancelled_attempt_that_is_closing(monkeypatch):
    received_auth = asyncio.Event()
    closes: list[str] = []
    real_close = Connection.close

    async def slow_close(self: Connection) -> None:
        closes.append("started")
        await asyncio.sleep(0.05)  # a close handshake that takes a moment
        await real_close(self)
        closes.append("finished")

    async def never_ack(ws: ServerConnection, exchange: Exchange) -> None:
        await exchange.recv(ws)
        received_auth.set()
        await hold(ws, exchange)

    monkeypatch.setattr(Connection, "close", slow_close)
    exchange = Exchange(never_ack)
    async with serve_local(exchange) as url:
        rs = ReconnectingSession(url, synthetic_token(), ack_timeout=None)
        consumer = asyncio.create_task(anext(rs.events()))
        await asyncio.wait_for(received_auth.wait(), 5)
        consumer.cancel()  # the attempt starts closing its connection
        await asyncio.sleep(0.01)
        await rs.close()  # must wait for that close, not cut it short
        assert closes and closes.count("started") == closes.count("finished")
        with pytest.raises(asyncio.CancelledError):
            await consumer


async def test_closing_while_handling_disconnected_delivers_nothing_more():
    exchange = Exchange(session(then=drop), session())
    events: list[object] = []
    async with serve_local(exchange) as url:
        rs = ReconnectingSession(url, synthetic_token())
        async for event in rs:
            events.append(event)
            if isinstance(event, Disconnected):
                await rs.close()
    assert [type(e) for e in events] == [Connected, Disconnected]
    assert exchange.connections == 1


async def test_a_session_is_iterated_only_once():
    exchange = Exchange(session())
    async with serve_local(exchange) as url:
        async with ReconnectingSession(url, synthetic_token()) as rs:
            async for _ in rs:
                break
            with pytest.raises(RuntimeError):
                async for _ in rs:
                    pass


# The token


async def test_a_missing_token_fails_before_any_connection():
    exchange = Exchange(session())
    async with serve_local(exchange) as url:
        with pytest.raises(MissingToken):
            ReconnectingSession(url)
    assert exchange.connections == 0


async def test_the_token_is_resolved_once_from_the_environment(monkeypatch):
    token = synthetic_token()
    monkeypatch.setenv(TOKEN_ENV_VAR, token)
    exchange = Exchange(session(then=drop), session())
    async with serve_local(exchange) as url:
        rs = ReconnectingSession(url, sleep=Clock().sleep)
        monkeypatch.setenv(TOKEN_ENV_VAR, synthetic_token())
        async with rs:
            async for event in rs:
                if isinstance(event, Connected) and event.reconnected:
                    break
    assert exchange.received == [auth(token), auth(token)]


async def test_the_session_object_never_shows_the_token():
    token = synthetic_token()
    exchange = Exchange(session())
    async with serve_local(exchange) as url:
        rs = ReconnectingSession(url, token)
        async with rs:
            async for _ in rs:
                break
            text = "\n".join([repr(rs), str(rs), repr(vars(rs)), str(vars(rs))])
    assert_token_absent(token, text)


async def test_a_failed_send_does_not_show_what_was_sent(monkeypatch):
    token = synthetic_token()

    async def failing_send(self: ClientConnection, message: object) -> None:
        raise RuntimeError("send failed")

    exchange = Exchange(session())
    async with serve_local(exchange) as url:
        async with ReconnectingSession(url, synthetic_token()) as rs:
            async for event in rs:
                if isinstance(event, Connected):
                    break
            monkeypatch.setattr(ClientConnection, "send", failing_send)
            with pytest.raises(RuntimeError) as caught:
                await rs.send("auth", Auth(token=token))
    assert "reconnect.py" in shown(caught.value)
    assert_token_absent(token, shown(caught.value))


async def test_sending_while_not_connected_does_not_show_what_was_sent():
    token = synthetic_token()
    rs = ReconnectingSession("ws://127.0.0.1:9", synthetic_token())
    with pytest.raises(NotConnected) as caught:
        await rs.send("auth", Auth(token=token))
    assert "reconnect.py" in shown(caught.value)
    assert_token_absent(token, shown(caught.value))


async def test_a_token_echoed_on_a_reconnect_is_withheld_from_events_and_errors():
    token = synthetic_token()

    async def close_with_token(ws: ServerConnection, exchange: Exchange) -> None:
        await ws.close(4000, token)

    reject = session_reject("NOT_AUTHENTICATED", f"bad token {token}")
    exchange = Exchange(session(then=close_with_token), closed_before_ack, refused(reject))
    events: list[object] = []
    async with serve_local(exchange) as url:
        rs = ReconnectingSession(url, token, sleep=Clock().sleep)
        with pytest.raises(SessionRejected) as caught:
            async with rs:
                async for event in rs:
                    events.append(event)
    assert caught.value.detail == "bad token <token withheld>"
    assert "reconnect.py" in shown(caught.value)  # the SDK's own frames are included
    assert_token_absent(token, shown(caught.value))
    assert_token_absent(token, "\n".join(repr(event) for event in events))
    for event in events:
        error = getattr(event, "error", None)
        if error is not None:
            assert_token_absent(token, shown(error))


async def test_a_token_echoed_in_a_rejection_while_open_is_withheld():
    token = synthetic_token()
    rejected = frame("session_reject", {"reason_code": token, "reason_detail": f"token {token}"}, 2)
    exchange = Exchange(session(rejected))
    events: list[object] = []
    async with serve_local(exchange) as url:
        rs = ReconnectingSession(url, token)
        with pytest.raises(SessionRejected) as caught:
            async with rs:
                async for event in rs:
                    events.append(event)
    assert caught.value.reason_name == "<token withheld>"
    assert caught.value.detail == "token <token withheld>"
    assert_token_absent(token, shown(caught.value))
    assert_token_absent(token, "\n".join(repr(event) for event in events))


# What the documentation promises


def test_the_docstrings_say_fills_during_a_disconnect_are_not_recovered():
    module_doc = " ".join((qte_sdk.reconnect.__doc__ or "").split())
    assert "Fills, order events and market data sent while the connection was down are not " in (
        module_doc
    )
    assert "recovered" in module_doc and "resume" in module_doc
    disconnected_doc = " ".join((Disconnected.__doc__ or "").split())
    assert "Fills" in disconnected_doc and "not recovered" in disconnected_doc
