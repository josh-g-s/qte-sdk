"""What a client sees across the live_slot hot switch: the gaps left by test_handover.py.

Read with the scoping note `drafts/hot-switch-client-view.v0.1.md` (qte-sdk lane). The
switch keeps the WebSocket open, as test_handover.py checks, except for the clients the
gateway closes: 1001 "exchange stopping" at the release (history past the bound, or a
replayed subscribe answered otherwise), 1008 "too many messages during a switch" for a
client that sends too much in the hold, a drop with no close frame for a client that does
not read for a quarter of the hold limit (the gateway's write to it stalls), and 1001 then
HTTP 503 for every client while no exchange slot is live. Each such client reconnects
through `ReconnectingSession` and resumes from `last_report_seq` in the same event log.

Cases that rest on the gateway's unmerged switch code (qte-platform #2828, G3 and H4) are
written against the behaviour that code's README describes, and skipped until it merges.
The rest depend only on the SDK and on plain WebSocket behaviour, and run.
"""

import asyncio
import signal
import sys
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from fake_exchange import drop_connection
from test_examples import (
    ASK,
    BID,
    EXAMPLES_DIR,
    INSTRUMENT,
    RUN_LIMIT,
    SENDS_SIGINT,
    TICK,
    FakeExchange,
    example_env,
)
from test_handover import (
    ACK,
    PRICE,
    Link,
    M,
    accepted,
    execution,
    order_state,
    payload_of,
    serving,
)
from test_reconnect import Clock, auth, resume, resume_ack, subscription
from test_resume import SCRIPTED_LOG
from test_session import synthetic_token
from websockets.asyncio.server import ServerConnection
from websockets.exceptions import ConnectionClosed, ConnectionClosedError

from qte_sdk.connection import HandshakeFailed, Received, ReportGap, ResumeComplete, SeqGap
from qte_sdk.contract.v1.common_pb2 import BUY, LIMIT
from qte_sdk.orders import send_new
from qte_sdk.reconnect import (
    Connected,
    Disconnected,
    NotConnected,
    ReconnectingSession,
    Retrying,
)
from qte_sdk.resting import RestingOrders
from qte_sdk.session import open_session

H4 = pytest.mark.skip(reason="TODO(h4): needs qte-platform #2828, G3 and H4 merged")

HOLD = 0.2  # a short hold; test_handover.py covers the longest, 2 s
HELD_PRICE = 199_950_000  # an order the client sends in the hold
END = "999"  # the grid_time of the book that ends a script
STOPPING = "exchange stopping"
OVERFLOW = "too many messages during a switch"
# The gateway's bound on what it queues for one client in the hold (H4 README:548-550).
HOLD_QUEUE_LIMIT = 1024


def book(grid_time: str = "1", condition: str = "LIVE", bid: int = PRICE) -> dict:
    return {
        "instrument": "AAPL",
        "grid_time": grid_time,
        "condition": condition,
        "bid_levels": [{"price": str(bid), "size": "300"}],
    }


def snapshot(price: int, size: int) -> dict:
    return {
        "strat_id": "mm-1",
        "instrument": "AAPL",
        "side": "BUY",
        "price": str(price),
        "remaining_size": str(size),
        "timestamp": "4",
    }


async def opening(link: Link) -> None:
    """Reports 1 to M from A: an order rests at PRICE for 100 and is filled 10, then 20."""
    await link.send("order_state", order_state(PRICE, 100), 1, "A")
    await link.send("execution", execution("x-2", PRICE, 10, 90), 2, "A")
    await link.send("execution", execution("x-3", PRICE, 20, 70), 3, "A")


async def send_order(client: Any, size: int = 50, price: int = HELD_PRICE) -> str:
    return await send_new(
        client,
        strat_id="mm-1",
        instrument="AAPL",
        side=BUY,
        order_type=LIMIT,
        size=size,
        price=price,
    )


class Closing:
    """A gateway that closes its first connection at the release of a switch, then lets
    the client reconnect to B.

    The first connection opens (the resume is answered with the empty marker), takes the
    client's subscribe if it has instruments, sends reports 1 to M from A and holds:
    for `HOLD` seconds after report M, or with `orders`, until the client has sent that
    many orders. It then ends with `end`: a close with that code and `reason`, or "drop",
    a lost connection with no close frame. The next `refusals` upgrades are refused with
    HTTP 503. Without `resume`, for an `open_session` client, no resume is awaited. Every
    later connection is B's: it opens, takes the resume and answers it
    with `answer`, takes the subscribe if any, and sends the closing book."""

    def __init__(
        self,
        end: int | str,
        reason: str = "",
        *,
        answer: list[tuple[str, dict, int | None]] | None = None,
        refusals: int = 0,
        orders: int = 0,
        instruments: bool = False,
        resume: bool = True,
    ) -> None:
        self.end = end
        self.resume = resume
        self.reason = reason
        self.answer = answer or [("resume_ack", payload_of(resume_ack(True, M)), None)]
        self.refusals = refusals
        self.refused = 0
        self.orders = orders
        self.instruments = instruments
        self.links: list[Link] = []
        self.token = ""

    def process_request(self, connection: Any, request: Any) -> Any:
        if self.links and self.refused < self.refusals:
            self.refused += 1
            return connection.respond(503, "no exchange live\n")
        return None

    async def __call__(self, ws: ServerConnection) -> None:
        link = Link(ws)
        self.links.append(link)
        if len(self.links) == 1:
            await link.open(
                [("resume_ack", payload_of(resume_ack()), None)] if self.resume else None
            )
            if self.instruments:
                assert (await link.recv())["type"] == "subscribe"
            await opening(link)
            link.holding = True
            if self.orders:
                for _ in range(self.orders):
                    assert (await link.recv())["type"] == "new"
            else:
                await asyncio.sleep(HOLD)
            if self.end == "drop":
                await drop_connection(ws)
            else:
                assert isinstance(self.end, int)
                await ws.close(self.end, self.reason)
            return
        link.source = "B"
        await link.open(self.answer)
        if self.instruments:
            assert (await link.recv())["type"] == "subscribe"
        await link.send("book", book(END), build="B")
        await link.record_until_closed()


Reaction = Callable[[ReconnectingSession, Any], Awaitable[None]]


async def drive(
    gateway: Any, *, instruments: tuple[str, ...] = (), react: Reaction | None = None
) -> tuple[list[Any], Clock, ReconnectingSession, RestingOrders, bool | None]:
    """Run a `ReconnectingSession` against `gateway` until the book that ends the script,
    passing each event to `react` first. Returns the events, the clock, the session, its
    resting view and whether that view was incomplete at the closing book (closing the
    session marks it incomplete)."""
    events: list[Any] = []
    incomplete: bool | None = None
    gateway.token = synthetic_token()
    clock = Clock()
    view = RestingOrders()
    async with serving(gateway, process_request=gateway.process_request) as url:
        rs = ReconnectingSession(
            url,
            gateway.token,
            instruments=instruments,
            resting=view,
            sleep=clock.sleep,
            rng=lambda: 0.0,
        )
        async with rs, asyncio.timeout(10):
            async for event in rs:
                events.append(event)
                if react is not None:
                    await react(rs, event)
                if isinstance(event, Received) and event.type == "book":
                    if event.message.grid_time == int(END):
                        incomplete = view.incomplete
                        break
    # Leaving `serving` waited for every handler, so each link has recorded all the client
    # sent before it closed.
    return events, clock, rs, view, incomplete


def kinds(events: list[Any]) -> list[str]:
    return [
        f"{e.type}:{e.report_seq}" if isinstance(e, Received) else type(e).__name__ for e in events
    ]


def no_gaps(events: list[Any]) -> bool:
    return not any(isinstance(e, SeqGap | ReportGap) for e in events)


def resumed_once_from(gateway: Closing, rs: ReconnectingSession, cursor: int) -> None:
    """The second connection authenticated and resumed from `cursor`, in the same log, and
    sent nothing else but the subscribe; nothing from the first was sent again."""
    assert len(gateway.links) == 2
    first, second = gateway.links
    assert first.received[:2] == [auth(gateway.token), resume(0)]
    expected = [auth(gateway.token), resume(cursor)]
    if gateway.instruments:
        expected.append(subscription("AAPL"))
    assert second.received == expected
    # One log: both acks name it, so the cursor was kept and sent in `resume`.
    assert rs.info is not None and rs.info.log_id == SCRIPTED_LOG == ACK["log_id"]


# Case 1: the 1008 close for a client that sends too much in the hold


async def test_a_1008_close_in_the_hold_is_a_disconnect_that_is_retried_and_resumed():
    # The SDK has no special case for 1008: like any close but 1000 and 1001, websockets
    # raises it as ConnectionClosedError, which is retryable. The reason text is withheld,
    # as every close reason the SDK does not know.
    # TODO(h4): whether B ever sees what was queued for a client closed 1008; this fake
    # drops it, so B replays nothing after M.
    gateway = Closing(1008, OVERFLOW, orders=1)

    async def react(rs: ReconnectingSession, event: Any) -> None:
        if isinstance(event, Received) and event.report_seq == M and len(gateway.links) == 1:
            await send_order(rs)

    events, clock, rs, view, incomplete = await drive(gateway, react=react)
    (disconnected,) = [e for e in events if isinstance(e, Disconnected)]
    assert isinstance(disconnected.error, ConnectionClosedError)
    assert disconnected.error.rcvd is not None
    assert disconnected.error.rcvd.code == 1008
    assert disconnected.error.rcvd.reason == "<withheld>"
    retries = [e for e in events if isinstance(e, Retrying)]
    assert [(r.attempt, r.delay, r.error) for r in retries] == [(1, 0.5, None)]
    assert clock.waits == [0.5]
    resumed_once_from(gateway, rs, M)
    # The held order was not sent again on B.
    assert [m["type"] for m in gateway.links[0].received] == ["auth", "resume", "new"]
    assert kinds(events)[-4:] == ["calendar:None", "resume_ack:None", "ResumeComplete", "book:None"]
    assert rs.last_report_seq == M
    assert incomplete is False
    assert no_gaps(events)


async def test_a_1008_close_ends_an_open_session_with_an_error_not_quietly():
    # Unlike 1001, which ends iteration quietly, a 1008 is raised: the open_session
    # examples stop with an error on it rather than as at a normal close.
    gateway = Closing(1008, OVERFLOW, orders=1, resume=False)
    seen: list[Any] = []
    async with serving(gateway, process_request=gateway.process_request) as url:
        session = await open_session(url, synthetic_token())
        with pytest.raises(ConnectionClosedError) as raised:
            async with session, asyncio.timeout(10):
                async for event in session:
                    seen.append(event)
                    if isinstance(event, Received) and event.report_seq == M:
                        await send_order(session)
    assert raised.value.rcvd is not None and raised.value.rcvd.code == 1008
    assert [e.report_seq for e in seen if isinstance(e, Received) and e.report_seq] == [1, 2, M]


@H4
async def test_more_than_the_hold_queue_limit_in_the_hold_is_closed_1008_and_resumed():
    # TODO(h4): the bound (1024 messages or 4 MiB) and the reason text are from the H4
    # README (548-550); the close is assumed to come once the client has sent one message
    # over the bound. The SDK sets no limit of its own: every send succeeds.
    gateway = Closing(1008, OVERFLOW, orders=HOLD_QUEUE_LIMIT + 1)
    sent: list[str] = []

    async def react(rs: ReconnectingSession, event: Any) -> None:
        if isinstance(event, Received) and event.report_seq == M and not sent:
            for n in range(HOLD_QUEUE_LIMIT + 1):
                sent.append(await send_order(rs, size=1, price=HELD_PRICE - n * 10_000))

    events, _, rs, view, incomplete = await drive(gateway, react=react)
    assert len(sent) == HOLD_QUEUE_LIMIT + 1
    (disconnected,) = [e for e in events if isinstance(e, Disconnected)]
    assert isinstance(disconnected.error, ConnectionClosedError)
    assert disconnected.error.rcvd is not None and disconnected.error.rcvd.code == 1008
    resumed_once_from(gateway, rs, M)
    assert incomplete is False


# Case 2: a 1001 at the release, then a resume on B


B_FILL = ("execution", execution("x-4", PRICE, 30, 40), M + 1)  # B fills the resting order


@pytest.mark.parametrize(
    "answer",
    [
        pytest.param(
            [("resume_ack", payload_of(resume_ack(True, M + 1)), None), B_FILL], id="replay"
        ),
        # The note infers a snapshot when the cursor is before B's live fold.
        pytest.param(
            [
                ("resume_ack", payload_of(resume_ack(False, M + 1, 1)), None),
                ("order_snapshot", snapshot(PRICE, 40), None),
            ],
            id="snapshot",
        ),
    ],
)
async def test_a_1001_at_the_release_is_resumed_on_b_in_the_same_log(answer):
    gateway = Closing(1001, STOPPING, answer=answer)
    events, clock, rs, view, incomplete = await drive(gateway)
    # A 1001 is a normal close: a disconnect with no error, retried at once.
    (disconnected,) = [e for e in events if isinstance(e, Disconnected)]
    assert disconnected == Disconnected(None)
    retries = [e for e in events if isinstance(e, Retrying)]
    assert [(r.attempt, r.delay, r.error) for r in retries] == [(1, 0.5, None)]
    resumed_once_from(gateway, rs, M)
    connected = [e for e in events if isinstance(e, Connected)]
    assert [c.reconnected for c in connected] == [False, True]
    assert connected[1].resume is not None
    assert connected[1].resume.as_of_report_seq == M + 1
    (complete,) = [e for e in events if isinstance(e, ResumeComplete)][1:]
    assert complete.as_of_report_seq == M + 1
    # Each report delivered once, and the view is right and complete: 40 left at PRICE.
    replayed = [report_seq for _, _, report_seq in answer if report_seq is not None]
    delivered = [e.report_seq for e in events if isinstance(e, Received) and e.report_seq]
    assert delivered == [1, 2, M, *replayed]
    snapshots = [e for e in events if isinstance(e, Received) and e.type == "order_snapshot"]
    assert len(snapshots) == sum(1 for type_, _, _ in answer if type_ == "order_snapshot")
    assert rs.last_report_seq == M + 1
    assert incomplete is False
    assert [(o.key.price, o.remaining_size) for o in view] == [(PRICE, 40)]
    assert no_gaps(events)


@H4
async def test_an_order_held_when_the_client_is_closed_at_the_release_is_answered_by_b():
    # TODO(h4): whether a client closed 1001 at the release has its held messages sent on
    # to B (README:494-500 says held messages go to B in arrival order; README:510-530
    # does not say for a client it closes). Assumed here that they are, so B's replay
    # carries the answer; the client never sends the order again.
    gateway = Closing(1001, STOPPING, orders=1)
    ref: list[str] = []

    async def react(rs: ReconnectingSession, event: Any) -> None:
        if isinstance(event, Received) and event.report_seq == M and not ref:
            ref.append(await send_order(rs))
            gateway.answer = [
                ("resume_ack", payload_of(resume_ack(True, M + 2)), None),
                ("accepted", accepted(ref[0]), M + 1),
                ("order_state", order_state(HELD_PRICE, 50), M + 2),
            ]

    events, _, rs, view, incomplete = await drive(gateway, react=react)
    resumed_once_from(gateway, rs, M)
    assert [m["type"] for m in gateway.links[0].received] == ["auth", "resume", "new"]
    assert rs.last_report_seq == M + 2
    assert incomplete is False
    assert view.get("AAPL", BUY, HELD_PRICE) is not None
    assert no_gaps(events)


# Case 3: market-data subscriptions across the switch


class Subscribed:
    """A gateway whose client subscribes to AAPL: A sends a book, the gateway holds for
    `HOLD` seconds, switches to B, and B sends books for AAPL, the last ending the script.
    The gateway replays the client's subscription on its own link to B, so the client
    sends nothing more."""

    def __init__(self) -> None:
        self.links: list[Link] = []
        self.token = ""

    def process_request(self, connection: Any, request: Any) -> Any:
        return None

    async def __call__(self, ws: ServerConnection) -> None:
        link = Link(ws)
        self.links.append(link)
        await link.open([("resume_ack", payload_of(resume_ack()), None)])
        assert (await link.recv())["type"] == "subscribe"
        await link.send("book", book("1"), build="A")
        link.holding = True
        await asyncio.sleep(HOLD)
        link.source = "B"
        await link.release()
        # The feed gap: B's first book may have an empty wall, then the wall is back.
        await link.send("book", {"instrument": "AAPL", "grid_time": "2", "condition": "EMPTY"})
        await link.send("book", book("3", bid=PRICE - 10_000), build="B")
        await link.send("book", book(END), build="B")
        await link.record_until_closed()


async def test_subscriptions_survive_a_held_switch_with_no_resubscribe():
    gateway = Subscribed()
    events, _, rs, _, _ = await drive(gateway, instruments=("AAPL",))
    (link,) = gateway.links
    # One subscribe, at the start; none after the switch.
    assert link.received == [auth(gateway.token), resume(0), subscription("AAPL")]
    books = [e for e in events if isinstance(e, Received) and e.type == "book"]
    assert [b.message.grid_time for b in books] == [1, 2, 3, int(END)]
    assert books[2].message.bid_levels[0].price == PRICE - 10_000
    assert not any(isinstance(e, Disconnected | Retrying) for e in events)
    assert rs.instruments == ("AAPL",)
    assert no_gaps(events)


async def test_subscriptions_are_made_again_after_a_close_at_the_release():
    gateway = Closing(1001, STOPPING, instruments=True)
    events, _, rs, _, _ = await drive(gateway, instruments=("AAPL",))
    first, _ = gateway.links
    assert first.received == [auth(gateway.token), resume(0), subscription("AAPL")]
    # The new session subscribes again, after its resume.
    resumed_once_from(gateway, rs, M)
    connected = [e for e in events if isinstance(e, Connected)]
    assert [c.instruments for c in connected] == [("AAPL",), ("AAPL",)]


# Case 4: a rollback, A to B to A, on one connection


class Rollback:
    """A gateway that switches A to B after report M1 and back to A after report M2, on one
    connection, holding for `HOLD` seconds at each cut. Each new live build sends the cut's
    report again, which the client drops. The client's order, sent on report M1, is held
    and taken by B; B's fill on it is report M2. A then fills the first order once more."""

    def __init__(self, resume: bool) -> None:
        self.resume = resume
        self.links: list[Link] = []
        self.token = ""
        self.order: dict | None = None
        self.order_arrived = asyncio.Event()
        self.order_held = False  # whether the order arrived in the first hold

    def process_request(self, connection: Any, request: Any) -> Any:
        return None

    async def take(self, link: Link) -> None:
        try:
            while True:
                message = await link.recv()
                if message["type"] == "new":
                    self.order = message
                    self.order_held = link.holding
                    self.order_arrived.set()
        except ConnectionClosed:
            return

    async def cut(self, link: Link, to: str, repeat: tuple[str, dict, int]) -> None:
        link.holding = True
        await asyncio.sleep(HOLD)
        link.source = to
        type_, payload, report_seq = repeat
        await link.send(type_, payload, report_seq, to)
        await link.release()

    async def __call__(self, ws: ServerConnection) -> None:
        link = Link(ws)
        self.links.append(link)
        await link.open([("resume_ack", payload_of(resume_ack()), None)] if self.resume else None)
        taker = asyncio.create_task(self.take(link))
        try:
            await link.send("order_state", order_state(PRICE, 100), 1, "A")
            await link.send("execution", execution("x-2", PRICE, 10, 90), 2, "A")
            # The gateway holds from here: the order sent in answer to report M is held.
            link.holding = True
            await link.send("execution", execution("x-3", PRICE, 20, 70), M, "A")
            await asyncio.wait_for(self.order_arrived.wait(), 5)
            assert self.order is not None
            ref = self.order["payload"]["request_ref"]
            m1 = ("execution", execution("x-3", PRICE, 20, 70), M)
            await self.cut(link, "B", m1)
            await link.send("accepted", accepted(ref), M + 1, "B")
            await link.send("order_state", order_state(HELD_PRICE, 50), M + 2, "B")
            m2 = ("execution", execution(f"x-{M + 3}", HELD_PRICE, 20, 30), M + 3)
            await link.send(*m2, "B")
            await self.cut(link, "A", m2)
            await link.send("execution", execution(f"x-{M + 4}", PRICE, 30, 40), M + 4, "A")
            await link.send("book", book(END), build="A")
            await ws.wait_closed()
        finally:
            taker.cancel()


@pytest.mark.parametrize("kind", ["session", "reconnecting"])
async def test_a_rollback_keeps_seq_and_report_seq_running_with_no_duplicate(kind):
    gateway = Rollback(resume=kind == "reconnecting")
    events: list[Any] = []
    view = RestingOrders()
    sent = False
    incomplete: bool | None = None
    async with serving(gateway) as url:
        token = synthetic_token()
        if kind == "session":
            client: Any = await open_session(url, token)
        else:
            client = ReconnectingSession(url, token, resting=view, sleep=Clock().sleep)
        async with client, asyncio.timeout(10):
            async for event in client:
                events.append(event)
                if kind == "session":
                    view.apply(event)
                if isinstance(event, Received) and event.report_seq == M and not sent:
                    sent = True
                    await send_order(client)
                if isinstance(event, Received) and event.type == "book":
                    incomplete = view.incomplete
                    break
        info = client.info
        last = client.last_report_seq
    (link,) = gateway.links
    assert gateway.order_held
    # The gateway's one counter ran 1, 2, 3, ... over both cuts.
    assert [s.seq for s in link.sent] == list(range(1, len(link.sent) + 1))
    builds = [(s.report_seq, s.build) for s in link.sent if s.report_seq is not None]
    assert builds == [
        (1, "A"),
        (2, "A"),
        (M, "A"),
        (M, "B"),
        (M + 1, "B"),
        (M + 2, "B"),
        (M + 3, "B"),
        (M + 3, "A"),
        (M + 4, "A"),
    ]
    # Each report delivered once: both repeats dropped.
    reports = [e.report_seq for e in events if isinstance(e, Received) and e.report_seq]
    assert reports == [1, 2, M, M + 1, M + 2, M + 3, M + 4]
    assert last == M + 4
    fills = [
        e.message.fill_size for e in events if isinstance(e, Received) and e.type == "execution"
    ]
    assert fills == [10, 20, 20, 30]
    assert sorted((o.key.price, o.remaining_size) for o in view) == [(HELD_PRICE, 30), (PRICE, 40)]
    assert incomplete is False
    # One session, one ack, one log.
    assert sum(1 for s in link.sent if s.type == "session_ack") == 1
    assert info is not None and info.log_id == SCRIPTED_LOG
    assert not any(isinstance(e, Disconnected | Retrying) for e in events)
    assert no_gaps(events)


# Case 5: no writer, then the exchange comes back


# Many more refusals than any small retry count: the backoff reaches its 30 s cap and stays.
NO_WRITER_REFUSALS = 20


@pytest.mark.parametrize(
    ("end", "answer", "resting"),
    [
        # The flip restart after the close: 1001, then 503s, then an empty snapshot, since
        # no order rests outside a session.
        pytest.param(
            1001, [("resume_ack", payload_of(resume_ack(False, M, 0)), None)], [], id="flip"
        ),
        # B crashes in the session: the connection is lost with no close frame, then 503s
        # until a writer is back, whose snapshot still holds the resting order.
        pytest.param(
            "drop",
            [
                ("resume_ack", payload_of(resume_ack(False, M, 1)), None),
                ("order_snapshot", snapshot(PRICE, 70), None),
            ],
            [(PRICE, 70)],
            id="crash",
        ),
    ],
)
async def test_with_no_writer_503s_are_retried_without_limit_and_sends_fail(end, answer, resting):
    gateway = Closing(end, STOPPING, answer=answer, refusals=NO_WRITER_REFUSALS)
    refused_sends: list[NotConnected] = []

    async def react(rs: ReconnectingSession, event: Any) -> None:
        if isinstance(event, Disconnected | Retrying):
            # Down: an order is refused at once, and nothing is queued for later.
            assert not rs.connected
            with pytest.raises(NotConnected) as raised:
                await send_order(rs)
            refused_sends.append(raised.value)

    events, clock, rs, view, incomplete = await drive(gateway, react=react)
    (disconnected,) = [e for e in events if isinstance(e, Disconnected)]
    if end == 1001:
        assert disconnected == Disconnected(None)
    else:
        assert isinstance(disconnected.error, ConnectionClosedError)
        assert disconnected.error.rcvd is None
    retries = [e for e in events if isinstance(e, Retrying)]
    assert len(retries) == NO_WRITER_REFUSALS + 1
    assert [r.attempt for r in retries] == list(range(1, NO_WRITER_REFUSALS + 2))
    delays = [0.5, 1.0, 2.0, 4.0, 8.0, 16.0] + [30.0] * (NO_WRITER_REFUSALS + 1 - 6)
    assert [r.delay for r in retries] == delays == clock.waits
    assert retries[0].error is None
    for retry in retries[1:]:
        assert isinstance(retry.error, HandshakeFailed)
        assert retry.error.status_code == 503
    assert gateway.refused == NO_WRITER_REFUSALS
    assert len(refused_sends) == 1 + len(retries)
    # Nothing sent while down reached the exchange: the new connection resumed only.
    resumed_once_from(gateway, rs, M)
    assert [m["type"] for m in gateway.links[0].received] == ["auth", "resume"]
    # The resume was answered with a snapshot; the seq starts again at 1 on the new
    # connection, which is no gap.
    assert rs.last_report_seq == M
    assert incomplete is False
    assert sorted((o.key.price, o.remaining_size) for o in view) == resting
    assert no_gaps(events)


# Case 6: an example across a held switch


class HeldSwitchExchange(FakeExchange):
    """The examples' fake exchange with a gateway hold in front of it: once `hold_after`
    books are sent and `strat_id` rests on both sides, nothing reaches the client for
    `hold` seconds, heartbeats included. What the client sends meanwhile waits unread, as
    the gateway queues it, and is answered after the release. The seq runs on with no gap,
    as the gateway restamps it. From B's third book on, the best bid is a tick lower, so
    the client must act on B's books."""

    def __init__(self, *, hold_after: int, hold: float, strat_id: str, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.hold_after = hold_after
        self.hold = hold
        self.strat_id = strat_id
        self.books_after_release = 0
        self.released: asyncio.Event | None = None
        self.hold_started: float | None = None
        self.released_at: float | None = None
        # When each message in `received` was read, on the loop's clock.
        self.received_at: list[float] = []

    def note(self, direction: str, type_: str) -> None:
        super().note(direction, type_)
        if direction == "received":
            self.received_at.append(asyncio.get_running_loop().time())

    def _release(self) -> None:
        assert self.released is not None and self.hold_started is not None
        loop = asyncio.get_running_loop()
        # To a deadline, since a timer may fire a little early on a coarse clock.
        left = self.hold_started + self.hold - loop.time()
        if left > 0:
            loop.call_later(left, self._release)
            return
        self.released_at = loop.time()
        self.released.set()

    async def send(
        self, ws: ServerConnection, type_: str, payload: dict[str, Any], **envelope: Any
    ) -> None:
        if self.released is None and self.books_sent >= self.hold_after and self.quoted():
            loop = asyncio.get_running_loop()
            self.released = asyncio.Event()
            self.hold_started = loop.time()
            loop.call_later(self.hold, self._release)
        if self.released is not None:
            await self.released.wait()
        if type_ == "book" and self.released_at is not None:
            self.books_after_release += 1
            if self.books_after_release > 2:
                moved = [{"price": str(BID - TICK), "size": "300"}]
                payload = {**payload, "bid_levels": moved, "grid_time": "2"}
        await super().send(ws, type_, payload, **envelope)

    def quoted(self) -> bool:
        sides = {
            side for (_, side, _), (strat, _) in self.resting.items() if strat == self.strat_id
        }
        return sides == {"BUY", "SELL"}


@SENDS_SIGINT
async def test_quote_both_sides_keeps_quoting_across_a_held_switch_and_cleans_up():
    # The longest hold, 2 s, once the example quotes both sides; the wall moves two books
    # after the release, so the example must act on B's books to cancel and re-enter. It
    # is stopped with Ctrl+C once it rests at the moved price, not by --seconds, so slow
    # scheduling cannot end the run before the re-entry.
    exchange = HeldSwitchExchange(
        hold_after=10, hold=2.0, strat_id="quote-test", heartbeat_every=0.2
    )
    env = example_env()
    env["QTE_TOKEN"] = synthetic_token()
    async with serving(exchange) as url:
        env["QTE_URL"] = url
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            str(EXAMPLES_DIR / "quote_both_sides.py"),
            *("--instrument", INSTRUMENT, "--strat-id", "quote-test"),
            *("--seconds", "60", "--requote-seconds", "0.1", "--drain-seconds", "5"),
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            async with asyncio.timeout(RUN_LIMIT):
                while not any(
                    side == "BUY" and price == BID and strat == "quote-test"
                    for (_, side, price), (strat, _) in exchange.resting.items()
                ):
                    await asyncio.sleep(0.01)
                process.send_signal(signal.SIGINT)
                out_bytes, err_bytes = await process.communicate()
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
    code, out, err = process.returncode, out_bytes.decode(), err_bytes.decode()
    assert code == 0, out + err
    assert exchange.hold_started is not None and exchange.released_at is not None
    assert exchange.released_at - exchange.hold_started >= exchange.hold
    assert exchange.connections == 1
    # No drop, no missed message and no stop as unreliable.
    for text in ("dropped", "missed", "reconnect", "warning", "WARNING"):
        assert text not in out, out
    orders = [
        (m["payload"]["side"], m["type"], int(m["payload"]["price"]), at)
        for m, at in zip(exchange.received, exchange.received_at, strict=True)
        if m["type"] in ("new", "cancel", "amend")
    ]
    buys = [(kind, price, at) for side, kind, price, at in orders if side == "BUY"]
    sells = [(kind, price, at) for side, kind, price, at in orders if side == "SELL"]
    first, moved = BID + TICK, BID
    assert [(kind, price) for kind, price, _ in buys] == [
        ("new", first),
        ("cancel", first),
        ("new", moved),
        ("cancel", moved),
    ]
    # The sell side was untouched by the move: quoted once, cancelled at the end.
    assert [(kind, price) for kind, price, _ in sells] == [
        ("new", ASK - TICK),
        ("cancel", ASK - TICK),
    ]
    # It quoted both sides before the hold, and again after the release, on B's books.
    assert buys[0][2] < exchange.hold_started and sells[0][2] < exchange.hold_started
    assert buys[2][2] >= exchange.released_at
    assert exchange.resting == {}
    assert "all of this example's orders are cancelled" in out
