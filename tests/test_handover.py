"""What a client sees across a gateway-held exchange handover and a gateway restart (#302).

The gateway sits in front of the exchange on the same URL. For a release it holds the
team's new orders while the live build stops after report M and the standby confirms it
has applied up to M; then it sends the held orders on to the standby. It stamps the
envelope seq itself, one counter per client, so the seq runs on unbroken from the old
build's frames to the new build's. Both builds fold report_seq from the one event log, so
it runs on unbroken too; the gateway deduplicates by source seq, and a report M sent again
anyway is dropped by the session. During the hold the gateway answers heartbeats itself.
An aborted handover goes back to the old build. A gateway code change is a plain close
with 1001, and an upgrade refused meanwhile is HTTP 503; `ReconnectingSession` redials and
resumes from `last_report_seq`.

Part B cites what `test_reconnect.py` and `test_resume.py` already cover rather than
repeating it: the backoff (`test_the_backoff_doubles_up_to_its_cap`), 503 being retryable
(`test_which_errors_are_retried`), a plain drop resumed with a replay
(`test_resume.py::test_a_reconnect_replays_what_was_missed_and_completes_the_view_again`),
a 1000 close retried (`test_a_normal_close_by_the_exchange_is_a_disconnect_too`) and nothing
queued while disconnected (`test_nothing_is_sent_or_queued_while_disconnected`).
"""

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import pytest
from fake_exchange import drop_connection, frame
from test_reconnect import Clock, auth, resume, resume_ack
from test_resume import SCRIPTED_LOG, TERM, term_calendar
from test_session import ack, synthetic_token
from test_tickets import state_payload
from websockets.asyncio.server import ServerConnection, serve
from websockets.exceptions import ConnectionClosed

from qte_sdk.account import AccountReports
from qte_sdk.connection import (
    DEFAULT_LIVENESS_TIMEOUT,
    DataUncertain,
    HandshakeFailed,
    LivenessTimeout,
    Received,
    SeqGap,
)
from qte_sdk.contract.v1.common_pb2 import BUY, LIMIT
from qte_sdk.orders import send_new
from qte_sdk.reconnect import Connected, Disconnected, ReconnectingSession, Retrying
from qte_sdk.resting import LevelKey, RestingOrders
from qte_sdk.session import open_session
from qte_sdk.tickets import LatestTickets

PRICE = 199_970_000  # the order resting before the handover
HELD_PRICE = 199_960_000  # the order sent during the hold

# The ack names the exchange's event log, which the gateway passes on unchanged: the
# cursor is kept across a reconnect only within the one log (DL-116).
ACK = json.loads(ack(log_id=SCRIPTED_LOG))["payload"]
CALENDAR = json.loads(term_calendar(TERM))["payload"]


def payload_of(text: str) -> dict:
    return json.loads(text)["payload"]


# Report payloads, as the exchange's event log holds them.


def order_state(price: int, size: int) -> dict:
    return {
        "strat_id": "mm-1",
        "instrument": "AAPL",
        "side": "BUY",
        "price": str(price),
        "state": "RESTING",
        "remaining_size": str(size),
        "timestamp": "1",
    }


def execution(exec_id: str, price: int, size: int, remaining: int) -> dict:
    return {
        "exec_id": exec_id,
        "origin": "TEAM",
        "strat_id": "mm-1",
        "instrument": "AAPL",
        "side": "BUY",
        "order_price": str(price),
        "fill_price": str(price),
        "fill_size": str(size),
        "remaining_size": str(remaining),
        "timestamp": "2",
    }


def accepted(request_ref: str) -> dict:
    return {"request_ref": request_ref, "receipt_time": "1", "release_time": "2"}


def ticket(ticket_id: str, update_time: int) -> dict:
    return {**state_payload(update_time), "ticket_id": ticket_id}


# Reports 1 to M (M = 3) of the shared log: an order rests at PRICE for 100 and is filled
# 10, then 20. Fill sizes add up to the team's position, so a fill applied twice shows.
OPENING_LOG: list[tuple[str, dict]] = [
    ("order_state", order_state(PRICE, 100)),
    ("execution", execution("x-2", PRICE, 10, 90)),
    ("execution", execution("x-3", PRICE, 20, 70)),
]
M = len(OPENING_LOG)


@dataclass(frozen=True)
class Sent:
    """One frame the gateway sent: its envelope seq, type, report_seq and which build made
    it ("" for the gateway's own frames, such as the ack and heartbeats)."""

    seq: int
    type: str
    report_seq: int | None
    build: str


class Link:
    """The gateway's side of one client connection. It stamps every frame with the next
    seq of its own per-client counter, whichever build made it."""

    def __init__(self, ws: ServerConnection) -> None:
        self.ws = ws
        self.seq = 0
        self.sent: list[Sent] = []
        self.received: list[dict] = []
        self.paused = False  # heartbeats stop while set
        self._lock = asyncio.Lock()

    async def send(
        self, type_: str, payload: dict, report_seq: int | None = None, build: str = ""
    ) -> None:
        async with self._lock:  # one counter: stamp and write as one step
            self.seq += 1
            self.sent.append(Sent(self.seq, type_, report_seq, build))
            extra = {} if report_seq is None else {"report_seq": str(report_seq)}
            await self.ws.send(frame(type_, payload, self.seq, **extra))

    async def recv(self) -> dict:
        message = json.loads(await self.ws.recv())
        self.received.append(message)
        return message

    async def open(self, answer: list[tuple[str, dict, int | None]] | None = None) -> None:
        """Take auth, send the ack and the calendar; if `answer` is given, take the
        client's resume and send `answer` (type, payload, report_seq) to it."""
        await self.recv()
        await self.send("session_ack", ACK)
        await self.send("calendar", CALENDAR)
        if answer is not None:
            message = await self.recv()
            assert message["type"] == "resume"
            for type_, payload, report_seq in answer:
                await self.send(type_, payload, report_seq)

    async def heartbeats(self, every: float) -> None:
        """The gateway's own heartbeats, every `every` seconds, hold or no hold."""
        try:
            while True:
                await asyncio.sleep(every)
                if not self.paused:
                    await self.send("heartbeat", {})
        except ConnectionClosed:
            return

    async def record_until_closed(self) -> None:
        """Record everything the client sends until it closes."""
        try:
            while True:
                await self.recv()
        except ConnectionClosed:
            return


class Build:
    """One exchange build behind the gateway. Both fold their reports from `log`, the one
    event log, so report n is the same whichever build sends it."""

    def __init__(self, name: str, log: list[tuple[str, dict]]) -> None:
        self.name = name
        self.log = log
        self.orders: list[dict] = []

    def take(self, order: dict) -> range:
        """Run one order: log its acceptance, its resting remainder and a fill of 20.
        Returns the report numbers it added."""
        self.orders.append(order)
        payload = order["payload"]
        price, size = int(payload["price"]), int(payload["size"])
        first = len(self.log) + 1
        self.log += [
            ("accepted", accepted(payload["request_ref"])),
            ("order_state", order_state(price, size)),
            ("execution", execution(f"x-{first + 2}", price, 20, size - 20)),
        ]
        return range(first, len(self.log) + 1)


class Gateway:
    """A fake gateway in front of build A (live) and build B (standby), one connection.

    It sends a first heartbeat, so the client's liveness clock runs from the start, then
    the opening ticket states and reports 1 to M from A. With `handover`, it holds every
    order the client sends from just before report M; the hold lasts until `hold` seconds
    after the first held order arrived (so a slow client cannot miss it), with heartbeats
    all the while unless `heartbeats_in_hold` is False. Then it switches to B (or stays on
    A if `abort`), sends report M again from B if `repeat`, and answers the held orders from
    the build now live. Without `handover` it answers an order as soon as it arrives.
    Either way it then sends ticket states from the live build and a `book`, which tells
    the client the script is over. `seq_jump` breaks the contract: the counter skips that
    many seqs at the switch. `resume` answers the client's resume, as a
    `ReconnectingSession` sends one.
    """

    def __init__(
        self,
        *,
        handover: bool = True,
        hold: float = 0.2,
        repeat: bool = False,
        abort: bool = False,
        seq_jump: int = 0,
        heartbeat_every: float = 0.05,
        heartbeats_in_hold: bool = True,
        resume: bool = False,
    ) -> None:
        self.handover = handover
        self.hold = hold
        self.repeat = repeat
        self.abort = abort
        self.seq_jump = seq_jump
        self.heartbeat_every = heartbeat_every
        self.heartbeats_in_hold = heartbeats_in_hold
        self.resume = resume
        self.log = list(OPENING_LOG)
        self.a = Build("A", self.log)
        self.b = Build("B", self.log)
        self.live = self.a
        self.connections = 0
        self.link: Link | None = None
        self.holding = False
        self.held: list[dict] = []
        self.orders: list[dict] = []
        self.order_arrived = asyncio.Event()
        self.hold_started: float | None = None
        self.released_at: float | None = None

    async def __call__(self, ws: ServerConnection) -> None:
        self.connections += 1
        link = self.link = Link(ws)
        await link.open([("resume_ack", payload_of(resume_ack()), None)] if self.resume else None)
        tasks = [
            asyncio.create_task(link.heartbeats(self.heartbeat_every)),
            asyncio.create_task(self._take_orders(link)),
        ]
        try:
            await self._script(link)
            await ws.wait_closed()
        finally:
            for task in tasks:
                task.cancel()

    async def _take_orders(self, link: Link) -> None:
        try:
            while True:
                message = await link.recv()
                if message["type"] == "new":
                    if self.holding:
                        if not self.held:
                            self.hold_started = asyncio.get_running_loop().time()
                        self.held.append(message)
                    self.orders.append(message)
                    self.order_arrived.set()
        except ConnectionClosed:
            return

    async def _script(self, link: Link) -> None:
        loop = asyncio.get_running_loop()
        await link.send("heartbeat", {})
        await link.send("ticket_state", ticket("17", 10), build="A")
        await link.send("ticket_state", ticket("19", 10), build="A")
        for n in range(1, M + 1):
            if n == M and self.handover:
                # The gateway holds from here: an order sent in answer to report M is held.
                self.holding = True
            type_, payload = self.log[n - 1]
            await link.send(type_, payload, n, "A")
        if self.handover:
            link.paused = not self.heartbeats_in_hold
            await asyncio.wait_for(self.order_arrived.wait(), 5)
            assert self.hold_started is not None
            # To a deadline, since a timer may fire a little early on a coarse clock.
            deadline = self.hold_started + self.hold
            while (left := deadline - loop.time()) > 0:
                await asyncio.sleep(left)
            link.paused = False
            if not self.abort:
                self.live = self.b
                link.seq += self.seq_jump
                if self.repeat:
                    type_, payload = self.log[M - 1]
                    await link.send(type_, payload, M, "B")
            self.holding = False
            self.released_at = loop.time()
        await asyncio.wait_for(self.order_arrived.wait(), 5)
        for order in self.orders:
            for n in self.live.take(order):
                type_, payload = self.log[n - 1]
                await link.send(type_, payload, n, self.live.name)
        await link.send("ticket_state", ticket("17", 20), build=self.live.name)
        await link.send("ticket_state", ticket("18", 20), build=self.live.name)
        await link.send("book", {"instrument": "AAPL", "grid_time": "1"}, build=self.live.name)


@asynccontextmanager
async def serving(handler: Any, **options: Any) -> AsyncIterator[str]:
    async with serve(handler, "127.0.0.1", 0, **options) as server:
        yield f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}"


@dataclass
class Seen:
    """What the client saw, and the state its views were left in."""

    events: list[Any] = field(default_factory=list)
    resting: RestingOrders = field(default_factory=RestingOrders)
    position: int = 0  # the sum of the fills AccountReports applied
    fills_applied: list[int] = field(default_factory=list)  # their report_seqs
    tickets: LatestTickets = field(default_factory=LatestTickets)
    tickets_cleared: bool = False
    last_report_seq: int | None = None
    sent_at: float | None = None
    answered_at: float | None = None
    heartbeats_at_send: int = 0
    heartbeats_at_answer: int = 0
    gateway_released_at_answer: bool = False
    # Read at the closing book: closing the client marks a view it follows incomplete.
    incomplete: bool | None = None


async def run(gateway: Gateway, kind: str, liveness_timeout: float = 1.0) -> Seen:
    """Run a client of `kind` ("session" or "reconnecting") against `gateway`: it applies
    every event to RestingOrders, AccountReports and LatestTickets, sends one order once it
    reads report M, and stops at the `book` that ends the script."""
    seen = Seen()
    account = AccountReports()
    loop = asyncio.get_running_loop()
    ref: str | None = None
    gateway.resume = kind == "reconnecting"
    async with serving(gateway) as url:
        if kind == "session":
            client: Any = await open_session(
                url, synthetic_token(), liveness_timeout=liveness_timeout
            )
        else:
            client = ReconnectingSession(
                url,
                synthetic_token(),
                resting=seen.resting,
                sleep=Clock().sleep,
                liveness_timeout=liveness_timeout,
            )
        async with client, asyncio.timeout(gateway.hold + 10):
            async for event in client:
                seen.events.append(event)
                if kind == "session":
                    seen.resting.apply(event)
                for item in account.update(event):
                    if isinstance(item, Received) and item.type == "execution":
                        seen.position += item.message.fill_size
                        seen.fills_applied.append(item.report_seq)
                if seen.tickets.update(event) and isinstance(event, Disconnected):
                    seen.tickets_cleared = True
                if not isinstance(event, Received):
                    continue
                if event.report_seq == M and ref is None:
                    seen.sent_at = loop.time()
                    seen.heartbeats_at_send = client.heartbeats_received
                    ref = await send_new(
                        client,
                        strat_id="mm-1",
                        instrument="AAPL",
                        side=BUY,
                        order_type=LIMIT,
                        size=50,
                        price=HELD_PRICE,
                    )
                elif event.type == "accepted" and event.message.request_ref == ref:
                    seen.answered_at = loop.time()
                    seen.heartbeats_at_answer = client.heartbeats_received
                    seen.gateway_released_at_answer = (
                        not gateway.handover or gateway.released_at is not None
                    )
                elif event.type == "book":
                    seen.incomplete = seen.resting.incomplete
                    break
            seen.last_report_seq = client.last_report_seq
    return seen


def received(seen: Seen) -> list[Received]:
    return [e for e in seen.events if isinstance(e, Received)]


def report_seqs(seen: Seen) -> list[int]:
    return [e.report_seq for e in received(seen) if e.report_seq is not None]


def uncertain(seen: Seen) -> list[Any]:
    return [e for e in seen.events if isinstance(e, DataUncertain | Disconnected | Retrying)]


CLIENTS = ["session", "reconnecting"]
# The longest hold: the gateway aborts a handover still holding at 2 s.
MAX_HOLD = 2.0
# The reports after the handover: the held order's acceptance, its resting remainder and
# its fill, all by the live build.
AFTER = [M + 1, M + 2, M + 3]


# Part A: the handover


@pytest.mark.parametrize("repeat", [False, True])
@pytest.mark.parametrize("kind", CLIENTS)
async def test_no_seq_gap_disconnect_or_uncertainty_across_the_switch(kind, repeat):
    gateway = Gateway(repeat=repeat)
    seen = await run(gateway, kind)
    assert uncertain(seen) == []
    assert gateway.connections == 1
    if kind == "reconnecting":
        assert [type(e) for e in seen.events if isinstance(e, Connected)] == [Connected]
    assert seen.incomplete is False
    # Ticket 19, sent only before the switch, is still held: nothing cleared the tickets.
    assert not seen.tickets_cleared
    ticket_19 = seen.tickets.get("19")
    assert ticket_19 is not None and ticket_19.update_time == 10
    # The script ran to the end on the one connection: the book was read.
    assert received(seen)[-1].type == "book"
    assert gateway.b.orders and not gateway.a.orders


@pytest.mark.parametrize("kind", CLIENTS)
async def test_the_per_connection_seq_is_continuous_across_the_switch(kind):
    gateway = Gateway(repeat=True)
    seen = await run(gateway, kind)
    assert gateway.link is not None
    sent = gateway.link.sent
    # The gateway's one counter: 1, 2, 3, ... with no jump and no repeat at M to M+1,
    # though the frames before the switch came from A and those after it from B.
    assert [s.seq for s in sent] == list(range(1, len(sent) + 1))
    builds = [s.build for s in sent if s.report_seq is not None]
    assert builds == ["A"] * M + ["B"] * (1 + len(AFTER))
    # Every frame the client was delivered carries the seq the gateway gave it, in order;
    # the heartbeats between them were absorbed with their seqs counted, so none is a gap.
    delivered = [e.seq for e in received(seen)]
    assert delivered == sorted(set(delivered))
    by_seq = {s.seq: s for s in sent}
    assert [(by_seq[n].type, by_seq[n].report_seq) for n in delivered] == [
        (e.type, e.report_seq) for e in received(seen)
    ]
    # What was sent and not delivered: the ack, which opens the session, and the repeat
    # of report M, dropped as a duplicate.
    undelivered = [s for s in sent if s.type != "heartbeat" and s.seq not in delivered]
    assert [(s.type, s.report_seq, s.build) for s in undelivered] == [
        ("session_ack", None, ""),
        ("execution", M, "B"),
    ]
    assert not any(isinstance(e, SeqGap) for e in seen.events)


@pytest.mark.parametrize("hold", [0.2, MAX_HOLD])
@pytest.mark.parametrize("kind", CLIENTS)
async def test_an_order_sent_in_the_hold_is_answered_late_with_heartbeats_meanwhile(kind, hold):
    # The liveness timeout is half the longest hold, and its clock is running before the
    # hold (a heartbeat came first), so a 2 s hold is survived only because the gateway's
    # heartbeats keep arriving during it; see the next test for the same hold without them.
    gateway = Gateway(hold=hold, heartbeat_every=0.05)
    seen = await run(gateway, kind, liveness_timeout=MAX_HOLD / 2)
    assert uncertain(seen) == []
    assert not any(e.type in ("reject", "ticket_reject") for e in received(seen))
    # The order was sent during the hold and held, and answered only after the release.
    assert gateway.held and gateway.held == gateway.orders
    assert gateway.held[0]["payload"]["price"] == str(HELD_PRICE)
    assert seen.gateway_released_at_answer
    assert gateway.hold_started is not None and gateway.released_at is not None
    assert gateway.released_at - gateway.hold_started >= hold
    assert seen.sent_at is not None and seen.answered_at is not None
    assert seen.sent_at <= gateway.hold_started
    assert seen.answered_at >= gateway.released_at
    # Answered late, by the hold, and within it, give or take a slow machine.
    assert hold <= seen.answered_at - seen.sent_at < hold + 3
    # The liveness clock was running when the hold began, and heartbeats kept arriving
    # while the order waited. No count is asserted: a slow runner delivers fewer, and
    # surviving the hold with no LivenessTimeout (above) is what shows enough arrived.
    assert seen.heartbeats_at_send >= 1
    assert seen.heartbeats_at_answer > seen.heartbeats_at_send


async def test_the_same_hold_without_heartbeats_would_outlast_a_short_liveness_timeout():
    # The control for the test above: with the gateway's heartbeats stopped for the hold,
    # the 1 s liveness timeout fires inside a 2 s hold. So that test passes because the
    # heartbeats arrive, not because the liveness clock was idle.
    gateway = Gateway(hold=MAX_HOLD, heartbeats_in_hold=False)
    with pytest.raises(LivenessTimeout):
        await run(gateway, "session", liveness_timeout=MAX_HOLD / 2)


def test_the_longest_hold_is_far_below_the_default_liveness_timeout():
    # A static comparison, no gateway involved: should heartbeats ever pause, a hold ends
    # by 2 s, far inside the 45 s a link may stay quiet before it is presumed dead.
    assert DEFAULT_LIVENESS_TIMEOUT >= 20 * MAX_HOLD


@pytest.mark.parametrize("kind", CLIENTS)
async def test_a_repeated_report_m_is_dropped_and_no_view_applies_a_duplicate_fill(kind):
    gateway = Gateway(repeat=True)
    seen = await run(gateway, kind)
    # The gateway did send report M twice, the second time from B.
    assert gateway.link is not None
    assert [(s.report_seq, s.build) for s in gateway.link.sent if s.report_seq == M] == [
        (M, "A"),
        (M, "B"),
    ]
    # The session delivered it once.
    assert report_seqs(seen) == [*range(1, M + 1), *AFTER]
    assert seen.last_report_seq == M + 3
    # AccountReports applied each fill once: 10 and 20 before, 20 after.
    assert seen.fills_applied == [2, 3, M + 3]
    assert seen.position == 10 + 20 + 20
    # RestingOrders: 70 left at PRICE, 30 at the held order's price, and complete.
    assert seen.incomplete is False
    assert {(o.key.price, o.remaining_size) for o in seen.resting} == {
        (PRICE, 70),
        (HELD_PRICE, 30),
    }
    # LatestTickets kept ticket 19, sent only by A, and took B's newer states.
    assert not seen.tickets_cleared
    assert [(s.ticket_id, s.update_time) for s in seen.tickets] == [
        ("17", 20),
        ("18", 20),
        ("19", 10),
    ]


@pytest.mark.parametrize("kind", CLIENTS)
async def test_fills_from_m_plus_1_by_the_new_build_are_read_normally(kind):
    gateway = Gateway()
    seen = await run(gateway, kind)
    assert gateway.link is not None
    by_build = {s.report_seq: s.build for s in gateway.link.sent if s.report_seq is not None}
    assert [by_build[n] for n in AFTER] == ["B", "B", "B"]
    after = [e for e in received(seen) if e.report_seq in AFTER]
    assert [(e.type, e.report_seq) for e in after] == [
        ("accepted", M + 1),
        ("order_state", M + 2),
        ("execution", M + 3),
    ]
    fill = after[-1].message
    assert (fill.order_price, fill.fill_size, fill.remaining_size) == (HELD_PRICE, 20, 30)
    held = seen.resting.get("AAPL", BUY, HELD_PRICE)
    assert held is not None and held.remaining_size == 30
    assert LevelKey("AAPL", BUY, PRICE) in seen.resting
    assert seen.last_report_seq == M + 3


def summary(seen: Seen) -> tuple:
    """What a program would act on: events by kind and report, and every view's state."""
    kinds = [
        f"{e.type}:{e.report_seq}" if isinstance(e, Received) else type(e).__name__
        for e in seen.events
    ]
    return (
        kinds,
        seen.last_report_seq,
        seen.position,
        seen.fills_applied,
        seen.incomplete,
        sorted((o.key.price, o.remaining_size) for o in seen.resting),
        [(s.ticket_id, s.update_time, s.status) for s in seen.tickets],
    )


@pytest.mark.parametrize(
    ("kind", "hold"), [("session", 0.2), ("reconnecting", 0.2), ("session", MAX_HOLD)]
)
async def test_an_aborted_handover_changes_nothing(kind, hold):
    # The longest case is a hold that runs to the 2 s abort.
    aborted = Gateway(abort=True, hold=hold)
    seen = await run(aborted, kind)
    plain = await run(Gateway(handover=False), kind)
    assert aborted.released_at is not None and aborted.hold_started is not None
    assert aborted.released_at - aborted.hold_started >= hold
    # The held order went back to A, which answered it; B saw nothing.
    assert aborted.held and aborted.a.orders == aborted.held and not aborted.b.orders
    assert aborted.link is not None
    assert {s.build for s in aborted.link.sent if s.report_seq is not None} == {"A"}
    assert [s.seq for s in aborted.link.sent] == list(range(1, len(aborted.link.sent) + 1))
    assert uncertain(seen) == []
    # The client saw exactly what it sees with no handover at all, only later.
    assert summary(seen) == summary(plain)


@pytest.mark.parametrize("kind", CLIENTS)
async def test_a_gateway_that_jumps_the_seq_at_the_switch_shows_as_a_gap(kind):
    # Not the contract: should the gateway ever pass on the new build's own seq, or skip
    # its counter, the client sees a SeqGap, and a view that needs every message goes
    # incomplete, rather than silently trusting state it may have missed.
    gateway = Gateway(seq_jump=4)
    seen = await run(gateway, kind)
    gaps = [e for e in seen.events if isinstance(e, SeqGap)]
    assert len(gaps) == 1
    gap = gaps[0]
    assert gap.received == gap.expected + 4
    assert seen.incomplete is True
    # It is a gap, not a drop: the connection stays up and the reports still count once.
    assert not any(isinstance(e, Disconnected) for e in seen.events)
    assert gateway.connections == 1
    assert report_seqs(seen) == [*range(1, M + 1), *AFTER]
    assert seen.position == 10 + 20 + 20


# Part B: a gateway restart


class Restarting:
    """A gateway that restarts under a client. The first connection opens, answers the
    resume with the empty marker, sends reports 1 to 3, takes one order it never answers
    (it is in flight when the gateway goes) and then ends with `end`: a 1001 close or a
    drop. The next `refusals` upgrades are refused with HTTP 503. Every later connection
    opens and answers the resume with a replay of reports 4 and 5, the in-flight order's
    acceptance and fill, which the exchange behind the gateway did take."""

    def __init__(self, end: str, refusals: int = 0) -> None:
        self.end = end
        self.refusals = refusals
        self.refused = 0
        self.links: list[Link] = []
        self.token = ""

    def process_request(self, connection: Any, request: Any) -> Any:
        if self.links and self.refused < self.refusals:
            self.refused += 1
            return connection.respond(503, "gateway restarting\n")
        return None

    async def __call__(self, ws: ServerConnection) -> None:
        link = Link(ws)
        self.links.append(link)
        empty = payload_of(resume_ack())
        if len(self.links) == 1:
            await link.open([("resume_ack", empty, None)])
            await link.send("order_state", order_state(PRICE, 100), 1)
            await link.send("execution", execution("x-2", PRICE, 10, 90), 2)
            await link.send("execution", execution("x-3", PRICE, 20, 70), 3)
            message = await link.recv()
            assert message["type"] == "new"
            if self.end == "1001":
                await ws.close(1001, "going away")
            else:
                await drop_connection(ws)
            return
        replay = payload_of(resume_ack(True, 5))
        ref = self.links[0].received[-1]["payload"]["request_ref"]
        await link.open(
            [
                ("resume_ack", replay, None),
                ("accepted", accepted(ref), 4),
                ("execution", execution("x-5", HELD_PRICE, 50, 0), 5),
            ]
        )
        # Live again: a frame after the replay, so the client reads past its completion.
        await link.send("book", {"instrument": "AAPL", "grid_time": "1"})
        await link.record_until_closed()


async def restart_run(gateway: Restarting) -> tuple[list[Any], Clock, ReconnectingSession]:
    events: list[Any] = []
    gateway.token = synthetic_token()
    clock = Clock()
    view = RestingOrders()
    sent = False
    async with serving(gateway, process_request=gateway.process_request) as url:
        rs = ReconnectingSession(
            url, gateway.token, resting=view, sleep=clock.sleep, rng=lambda: 0.0
        )
        async with rs, asyncio.timeout(10):
            async for event in rs:
                events.append(event)
                if isinstance(event, Received) and event.report_seq == 3 and not sent:
                    sent = True
                    await send_new(
                        rs,
                        strat_id="mm-1",
                        instrument="AAPL",
                        side=BUY,
                        order_type=LIMIT,
                        size=50,
                        price=HELD_PRICE,
                    )
                if isinstance(event, Received) and event.type == "book":
                    break
    # Leaving `serving` waited for every handler, so each link has recorded all the client
    # sent before it closed.
    return events, clock, rs


def assert_resumed_with_nothing_resent(gateway: Restarting, rs: ReconnectingSession) -> None:
    first, *_, last = gateway.links
    assert first.received[1] == resume(0)
    assert [m["type"] for m in first.received] == ["auth", "resume", "new"]
    # The new connection authenticated and resumed from the last report read, 3; the
    # order in flight at the restart was not sent again.
    assert last.received == [auth(gateway.token), resume(3)]
    assert rs.last_report_seq == 5


async def test_a_gateway_restart_close_1001_is_retried_and_resumed_from_last_report_seq():
    gateway = Restarting("1001")
    events, clock, rs = await restart_run(gateway)
    # A clean close, 1001 as 1000: a plain disconnect with no error, then a redial.
    assert [e for e in events if isinstance(e, Disconnected)] == [Disconnected(None)]
    retries = [e for e in events if isinstance(e, Retrying)]
    assert retries == [Retrying(1, 0.5, None)]
    assert clock.waits == [0.5]
    assert len(gateway.links) == 2
    assert_resumed_with_nothing_resent(gateway, rs)
    tail = [
        f"{e.type}:{e.report_seq}" if isinstance(e, Received) else type(e).__name__ for e in events
    ]
    assert tail[-6:] == [
        "calendar:None",
        "resume_ack:None",
        "accepted:4",
        "execution:5",
        "ResumeComplete",
        "book:None",
    ]
    assert not any(isinstance(e, SeqGap) for e in events)


@pytest.mark.parametrize("end", ["1001", "drop"])
async def test_a_run_of_503_refusals_is_retried_then_resumed_from_last_report_seq(end):
    gateway = Restarting(end, refusals=3)
    events, clock, rs = await restart_run(gateway)
    assert sum(isinstance(e, Disconnected) for e in events) == 1
    retries = [e for e in events if isinstance(e, Retrying)]
    assert [(r.attempt, r.delay) for r in retries] == [(1, 0.5), (2, 1.0), (3, 2.0), (4, 4.0)]
    assert retries[0].error is None
    for retry in retries[1:]:
        assert isinstance(retry.error, HandshakeFailed)
        assert retry.error.status_code == 503
    assert clock.waits == [0.5, 1.0, 2.0, 4.0]
    assert gateway.refused == 3
    assert len(gateway.links) == 2  # the refused upgrades never reached a handler
    assert_resumed_with_nothing_resent(gateway, rs)
    connected = [e for e in events if isinstance(e, Connected)]
    assert [c.reconnected for c in connected] == [False, True]
