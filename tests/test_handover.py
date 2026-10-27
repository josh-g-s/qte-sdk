"""What a client sees across a gateway-held exchange handover and a gateway restart (#302).

The gateway sits in front of the exchange on the same URL. For a release it holds the
team's new orders while the live build stops after report M and the standby confirms it
has applied up to M; then it sends the held orders on to the standby. It stamps the
envelope seq itself, one counter per client, so the seq runs on unbroken from the old
build's frames to the new build's. Both builds fold report_seq from the one event log, so
it runs on unbroken too; the gateway deduplicates by source seq, and a report M sent again
anyway is dropped by the session. The gateway sends no heartbeats of its own: it passes
the exchange's on, and during the hold (2 s at most) it sends nothing, so they arrive late.
A held order is answered late, with no reject. After a completed handover the market feed
has a short gap, so the next book may show an empty wall; no test assumes the book is the
same across the switch. An aborted handover goes back to the old build. A gateway restart
ends the connection with a plain TCP close and no close frame (clients see 1006). A redial
normally reaches the other gateway task; only while no gateway task is healthy does the
load balancer refuse the upgrade with HTTP 503. `ReconnectingSession` redials and resumes
from `last_report_seq`. A 1001 comes only when the exchange itself stops (the gateway
passes the frame on), and is retried the same way.

Part B cites what `test_reconnect.py` and `test_resume.py` already cover rather than
repeating it: the backoff (`test_the_backoff_doubles_up_to_its_cap`), 503 being retryable
(`test_which_errors_are_retried`), a plain drop resumed with a replay
(`test_resume.py::test_a_reconnect_replays_what_was_missed_and_completes_the_view_again`),
a 1000 close retried (`test_a_normal_close_by_the_exchange_is_a_disconnect_too`) and nothing
queued while disconnected (`test_nothing_is_sent_or_queued_while_disconnected`).

The target is chosen by one setting (G4, #311; see `gateway_target.py`): with
`QTE_GATEWAY_BIN` unset these scenarios run against the fake gateway here, `Gateway` and
`Restarting`; with it set, against that qte-gateway binary in front of two fake
link-protocol exchanges (`BinaryGateway`, and `Restarting` as exchange A's script). The
handover outcomes follow link protocol v0.12 section 3 step 7: a switch, a late promotion
(the follower had appended when the gateway gave up, so the switch completes), an abort at
drain, confirm or promote (the old live resumes), and no exchange confirmed live (the held
client is closed with 1001 and refused with 503 until one exchange is live). Tests marked
`FAKE_ONLY` check the fake gateway itself and are skipped in binary mode.
"""

import asyncio
import json
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fake_exchange import drop_connection, frame
from gateway_target import FAKE, FAKE_ONLY, HOLD_LIMIT, Rig, Tap, needs_stage_2, rig
from link_exchange import LINK_ONLY_TYPES, Behaviour, EventLog, LinkExchange
from test_reconnect import Clock, auth, resume, resume_ack
from test_resume import SCRIPTED_LOG, TERM, term_calendar
from test_session import ack, synthetic_token
from test_tickets import state_payload
from websockets.asyncio.server import ServerConnection, serve
from websockets.exceptions import ConnectionClosed, ConnectionClosedError

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
from qte_sdk.contract.v1.market_data_pb2 import EMPTY
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
    it ("" for frames the gateway makes itself, such as the ack)."""

    seq: int
    type: str
    report_seq: int | None
    build: str


class Link:
    """The gateway's side of one client connection. It stamps every frame with the next
    seq of its own per-client counter, whichever build made it."""

    def __init__(self, ws: ServerConnection) -> None:
        self.ws = ws
        # On a fake exchange behind a real gateway: whether the gateway dialled with the
        # link header, so messages from logged records carry src_seq (section 1).
        assert ws.request is not None
        self.is_link = ws.request.headers.get("X-QTE-Link") is not None
        self.seq = 0
        self.sent: list[Sent] = []
        self.frames: list[dict] = []  # every frame sent, as JSON
        self.received: list[dict] = []
        self.source = "A"  # the live build, whose heartbeats are passed on
        self.holding = False  # while set, the exchange's heartbeats wait
        self.owed = 0  # heartbeats that came due in the hold
        self.owed_at_release = 0
        self._lock = asyncio.Lock()

    async def send(
        self,
        type_: str,
        payload: dict,
        report_seq: int | None = None,
        build: str = "",
        src_seq: int | None = None,
    ) -> None:
        """Send one frame; `src_seq`, the seq of the logged record it comes from, is added
        only on a link connection."""
        async with self._lock:  # one counter: stamp and write as one step
            self.seq += 1
            self.sent.append(Sent(self.seq, type_, report_seq, build))
            extra: dict[str, Any] = {} if report_seq is None else {"report_seq": str(report_seq)}
            if src_seq is not None and self.is_link:
                extra["src_seq"] = src_seq
            text = frame(type_, payload, self.seq, **extra)
            self.frames.append(json.loads(text))
            await self.ws.send(text)

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
        """The live build's heartbeats, every `every` seconds, passed on by the gateway.
        During a hold nothing from the exchange is passed on: a heartbeat that comes due
        waits for `release`."""
        try:
            while True:
                await asyncio.sleep(every)
                if self.holding:
                    self.owed += 1
                else:
                    await self.send("heartbeat", {}, build=self.source)
        except ConnectionClosed:
            return

    async def release(self) -> None:
        """End the hold: send on the heartbeats that waited, late."""
        self.holding = False
        self.owed_at_release = self.owed
        for _ in range(self.owed):
            await self.send("heartbeat", {}, build=self.source)
        self.owed = 0

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
    after the first held order arrived (so a slow client cannot miss it). It sends nothing
    in the hold: the live build's heartbeats wait and go on at the release. Then, by
    `outcome`, it switches to B ("switch", and "late_promote", which a client cannot tell
    from it) or stays on A (the aborts in `RESUMED`), sends report M again from B if
    `repeat`, and answers the held orders from the build now live. With "no_live" it closes
    the client with 1001 at the end of the hold instead, refuses the next `refusals`
    upgrades with HTTP 503, and then serves the redial from B, which answers the resume
    with nothing to replay and sends the closing `book`. Without `handover` it answers an
    order as soon
    as it arrives. Either way it then sends ticket states from the live build and a `book`,
    which tells the client the script is over. After a completed switch the book has an
    empty wall, as after the feed gap a real handover leaves. `seq_jump` breaks the
    contract: the counter skips that many seqs at the switch. `resume` answers the client's
    resume, as a `ReconnectingSession` sends one.
    """

    def __init__(
        self,
        *,
        handover: bool = True,
        hold: float = 0.2,
        repeat: bool = False,
        outcome: str = "switch",
        seq_jump: int = 0,
        heartbeat_every: float = 0.05,
        resume: bool = False,
        refusals: int = 0,
    ) -> None:
        assert outcome in OUTCOMES, outcome
        self.handover = handover
        self.hold = hold
        self.repeat = repeat
        self.outcome = outcome
        self.refusals = refusals
        self.refused = 0
        self.closed_with: tuple[int, str] | None = None  # the close of a "no_live" outcome
        self.links: list[Link] = []
        self.clock = Clock()
        self.seq_jump = seq_jump
        self.heartbeat_every = heartbeat_every
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

    @asynccontextmanager
    async def serve(self) -> AsyncIterator[str]:
        async with serving(self, process_request=self.process_request) as url:
            yield url

    def process_request(self, connection: Any, request: Any) -> Any:
        # No exchange confirmed live: every upgrade is refused until one is (step 7).
        if self.closed_with is not None and self.refused < self.refusals:
            self.refused += 1
            return connection.respond(503, "no live exchange\n")
        return None

    async def __call__(self, ws: ServerConnection) -> None:
        if self.closed_with is not None:
            await self._served_again(Link(ws))
            return
        self.connections += 1
        link = self.link = Link(ws)
        self.links.append(link)
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
        await link.send("heartbeat", {}, build="A")
        await link.send("ticket_state", ticket("17", 10), build="A")
        await link.send("ticket_state", ticket("19", 10), build="A")
        for n in range(1, M + 1):
            if n == M and self.handover:
                # The gateway holds from here: an order sent in answer to report M is held.
                self.holding = True
            type_, payload = self.log[n - 1]
            await link.send(type_, payload, n, "A")
        if self.handover:
            link.holding = True
            await asyncio.wait_for(self.order_arrived.wait(), 5)
            assert self.hold_started is not None
            # To a deadline, since a timer may fire a little early on a coarse clock.
            deadline = self.hold_started + self.hold
            while (left := deadline - loop.time()) > 0:
                await asyncio.sleep(left)
            if self.outcome == "no_live":
                # Held to the hold limit, then closed as on any upstream loss (step 7).
                self.closed_with = (1001, "no live exchange")
                self.released_at = loop.time()
                await link.ws.close(*self.closed_with)
                return
            if self.outcome in SWITCHED:
                self.live = self.b
                link.source = "B"
                link.seq += self.seq_jump
                if self.repeat:
                    type_, payload = self.log[M - 1]
                    await link.send(type_, payload, M, "B")
            await link.release()
            self.holding = False
            self.released_at = loop.time()
        await asyncio.wait_for(self.order_arrived.wait(), 5)
        for order in self.orders:
            for n in self.live.take(order):
                type_, payload = self.log[n - 1]
                await link.send(type_, payload, n, self.live.name)
        await link.send("ticket_state", ticket("17", 20), build=self.live.name)
        await link.send("ticket_state", ticket("18", 20), build=self.live.name)
        book: dict[str, Any] = {"instrument": "AAPL", "grid_time": "1"}
        if self.live is self.b:
            book["condition"] = "EMPTY"  # the feed gap after the switch: no wall yet
        await link.send("book", book, build=self.live.name)

    async def _served_again(self, link: Link) -> None:
        """After no exchange was confirmed live, B (which had appended) reports live, and
        the gateway serves a redial from it: nothing to replay, then the closing book."""
        self.live = self.b
        self.links.append(link)
        # No replay: no report came after M, the last one read.
        await link.open([("resume_ack", payload_of(resume_ack(False, M)), None)])
        book = {"instrument": "AAPL", "grid_time": "1", "condition": "EMPTY"}
        await link.send("book", book, build="B")
        await link.record_until_closed()


# The outcomes of a handover (link protocol v0.12, section 3 step 7). To a client a late
# promotion is a completed switch, and every abort the old live resumes from is no switch.
SWITCHED = ["switch", "late_promote"]
RESUMED = ["abort_drain", "abort_confirm", "abort_promote"]
OUTCOMES = [*SWITCHED, *RESUMED, "no_live"]


class RealClock(Clock):
    """A sleep for a real gateway: it records each wait, as Clock does, and really sleeps a
    little, so a redial gives the gateway time to come back."""

    async def sleep(self, delay: float) -> None:
        self.waits.append(delay)
        await asyncio.sleep(min(delay, 0.05))


class BinaryGateway:
    """The binary-mode counterpart of `Gateway`: the same script, played by fake exchanges
    A (live) and B (follower) behind a real stage-2 gateway, through a recording `Tap`.

    A sends the opening frames and reports 1 to M-1, then starts a switch through the
    switch listener. On `link_drain` (the gateway is holding) it sends report M, waits
    `hold`, and answers `link_drained`, so an order sent in answer to report M is held. B,
    once promoted, sends report M again if `repeat` (with src_seq M, which the gateway
    drops) and answers the released order. `outcome` sets how the exchanges answer the
    control messages (`behaviours`), so each outcome of step 7 happens. It reads, for the
    tests, what the fake gateway records: `connections`, `link.sent` (the frames the gateway
    stamped), the orders each build took, `held`, `hold_started` (A's `link_drain`, at or
    after the gateway's hold start) and `released_at` (B's promoted, or A's resume)."""

    def __init__(
        self,
        *,
        handover: bool = True,
        hold: float = 0.2,
        repeat: bool = False,
        outcome: str = "switch",
        heartbeat_every: float = 0.05,
        resume: bool = False,
        refusals: int = 0,
    ) -> None:
        assert outcome in OUTCOMES, outcome
        self.handover = handover
        self.hold = hold
        self.repeat = repeat
        self.outcome = outcome
        self.heartbeat_every = heartbeat_every
        self.resume = resume
        self.refusals = refusals  # unused: the gateway refuses until B is back
        self.log = EventLog(SCRIPTED_LOG)
        for type_, payload in OPENING_LOG:
            self.log.append(type_, payload)
        self.a = Build("A", [])
        self.b = Build("B", [])
        self.links: dict[str, list[Link]] = {"A": [], "B": []}
        self.held: list[dict] = []
        self.orders: list[dict] = []
        self.link_errors: list[dict] = []  # orders a follower or a stopped live was sent
        self.clock = RealClock()
        self.rig: Rig | None = None
        self.tap: Tap | None = None
        self.start_switch: Any = None  # a coroutine function; the rig's switch by default
        self.switch_task: asyncio.Task | None = None
        self._repeated = False
        self._tasks: set[asyncio.Task] = set()

    def behaviours(self) -> tuple[Behaviour, Behaviour]:
        """How A and B answer the gateway's control messages for this outcome."""
        b = {
            "late_promote": Behaviour(promoted="on_abort"),
            "abort_confirm": Behaviour(answer_confirm=False),
            "abort_promote": Behaviour(append_on_promote=False, promoted="never"),
            "no_live": Behaviour(drop_control_on_promote=True, refuse_control_for=HOLD_LIMIT + 1),
        }.get(self.outcome, Behaviour())
        return Behaviour(), b

    def exchanges(self, folder: Path) -> tuple[LinkExchange, LinkExchange]:
        """A and B, not started, for an in-test driver with no gateway."""
        from link_exchange import write_credentials

        _, _, digest = write_credentials(folder)
        a_behaviour, b_behaviour = self.behaviours()
        a = LinkExchange("A", "a", "live", self.log, digest, self.client, behaviour=a_behaviour)
        b = LinkExchange("B", "b", "follower", self.log, digest, self.client, behaviour=b_behaviour)
        self._a, self._b = a, b
        return a, b

    @asynccontextmanager
    async def serve(self) -> AsyncIterator[str]:
        a_behaviour, b_behaviour = self.behaviours()
        with tempfile.TemporaryDirectory() as folder:
            async with rig(
                Path(folder),
                self.log,
                self.client,
                self.client,
                a_behaviour=a_behaviour,
                b_behaviour=b_behaviour,
            ) as the_rig:
                self.rig = the_rig
                self._a, self._b = the_rig.a, the_rig.b
                self.start_switch = the_rig.switch
                self.tap = Tap(the_rig.url)
                try:
                    async with self.tap.serving() as url:
                        yield url
                finally:
                    for task in self._tasks:
                        task.cancel()
                    if "link_drain" not in the_rig.a.marks and self.switch_task is not None:
                        if self.switch_task.done() and not self.switch_task.cancelled():
                            answer = self.switch_task.result()
                            raise AssertionError(
                                f"no link_drain reached A; the switch command answered "
                                f"{answer}; gateway log: {the_rig.tail()}"
                            )

    def _spawn(self, coroutine: Any) -> None:
        task = asyncio.create_task(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # What the tests read

    @property
    def connections(self) -> int:
        """Client links the gateway opened to A, the live exchange at the start."""
        return self._a.link_connections + self._a.plain_connections

    @property
    def hold_started(self) -> float | None:
        return self._a.marks.get("link_drain")

    @property
    def released_at(self) -> float | None:
        if self.outcome in SWITCHED:
            return self._b.marks.get("promoted")
        if self.outcome in RESUMED:
            return self._a.marks.get("resumed")
        return None

    @property
    def link(self) -> SimpleNamespace:
        """The frames the gateway sent the client, as `Link.sent` holds them, with the build
        that made each report: A's if A sent it, since B's repeat of a report A sent is
        dropped by the gateway."""
        assert self.tap is not None
        sent_by = {
            name: {(s.type, s.report_seq) for link in links for s in link.sent}
            for name, links in self.links.items()
        }
        sent = []
        for f in self.tap.frames:
            report_seq = int(f["report_seq"]) if "report_seq" in f else None
            key = (f["type"], report_seq)
            build = ""
            if report_seq is not None:
                build = "A" if key in sent_by["A"] else "B" if key in sent_by["B"] else "?"
            sent.append(Sent(f.get("seq", 0), f["type"], report_seq, build))
        return SimpleNamespace(sent=sent, frames=self.tap.frames)

    # The exchanges' script

    async def _send_record(self, link: Link, seq: int, build: str) -> None:
        record = self.log.records[seq - 1]
        await link.send(record.type, record.payload, record.report_seq, build, src_seq=seq)

    async def client(self, exchange: LinkExchange, ws: ServerConnection) -> None:
        link = Link(ws)
        link.source = exchange.name
        self.links[exchange.name].append(link)
        heartbeats = asyncio.create_task(link.heartbeats(self.heartbeat_every))
        try:
            while True:
                message = await link.recv()
                kind = message["type"]
                if kind == "auth":
                    await link.send("session_ack", ACK)
                    await link.send("calendar", CALENDAR)
                    if exchange.name == "A" and not self.resume:
                        self._spawn(self._opening(link))
                elif kind == "resume":
                    if exchange.name == "A":
                        await link.send("resume_ack", payload_of(resume_ack()))
                        self._spawn(self._opening(link))
                    else:
                        # A redial served by B after an outage: no report after M.
                        await link.send("resume_ack", payload_of(resume_ack(False, M)))
                        book = {"instrument": "AAPL", "grid_time": "1", "condition": "EMPTY"}
                        await link.send("book", book, build="B", src_seq=self.log.tail)
                elif kind == "link_drain":
                    # In this connection's loop, so every earlier message was handled.
                    if not await self._drain(exchange, link):
                        continue
                    await ws.send(json.dumps({"type": "link_drained", "n": message["n"]}))
                elif kind == "new":
                    self.orders.append(message)
                    if self.hold_started is not None:
                        self.held.append(message)
                    if exchange.accepting_orders:
                        await self._take(exchange, link, message)
                    else:
                        # Section 2: an internal link error, which the gateway must never
                        # pass on to the client.
                        self.link_errors.append(message)
                        error = {"type": "link_error", "reason": "not_live"}
                        await ws.send(json.dumps(error))
        except ConnectionClosed:
            return
        finally:
            heartbeats.cancel()

    async def _opening(self, link: Link) -> None:
        await link.send("heartbeat", {}, build="A")
        # The ticket states sent on connect are resent, so they carry no src_seq.
        await link.send("ticket_state", ticket("17", 10), build="A")
        await link.send("ticket_state", ticket("19", 10), build="A")
        for seq in range(1, M):
            await self._send_record(link, seq, "A")
        if not self.handover:
            await self._send_record(link, M, "A")
            return
        assert self.start_switch is not None
        self.switch_task = asyncio.create_task(self.start_switch())

    async def _drain(self, exchange: LinkExchange, link: Link) -> bool:
        """A's part of the drain: report M, then the hold. False: no link_drained."""
        exchange.mark("link_drain")
        await self._send_record(link, M, "A")
        if self.outcome == "abort_drain":
            return False  # never drained: the hold limit aborts the switch at drain
        await asyncio.sleep(self.hold)
        return True

    async def _take(self, exchange: LinkExchange, link: Link, order: dict) -> None:
        build = exchange.name
        (self.a if build == "A" else self.b).orders.append(order)
        if build == "B" and self.repeat and not self._repeated:
            self._repeated = True
            await self._send_record(link, M, "B")  # src_seq M: the gateway drops it
        payload = order["payload"]
        price, size = int(payload["price"]), int(payload["size"])
        first = self.log.tail + 1
        self.log.append("accepted", accepted(payload["request_ref"]))
        self.log.append("order_state", order_state(price, size))
        report = self.log.records[-1].report_seq
        assert report is not None
        self.log.append("execution", execution(f"x-{report + 1}", price, 20, size - 20))
        for seq in range(first, self.log.tail + 1):
            await self._send_record(link, seq, build)
        tail = self.log.tail
        await link.send("ticket_state", ticket("17", 20), build=build, src_seq=tail)
        await link.send("ticket_state", ticket("18", 20), build=build, src_seq=tail)
        book: dict[str, Any] = {"instrument": "AAPL", "grid_time": "1"}
        if build == "B":
            book["condition"] = "EMPTY"
        await link.send("book", book, build=build, src_seq=tail)


def make_gateway(**options: Any) -> Any:
    """The gateway of a handover scenario on the target `QTE_GATEWAY_BIN` picks: the fake
    here, or a stage-2 binary (skipped for a stage-1 one)."""
    needs_stage_2()
    return Gateway(**options) if FAKE else BinaryGateway(**options)


def frames_sent(gateway: Any) -> list[dict]:
    """Every frame the gateway sent the client, as JSON."""
    if FAKE:
        return [f for link in gateway.links for f in link.frames]
    return gateway.link.frames


def assert_nothing_link_only_reached_the_client(gateway: Any) -> None:
    """No src_seq and no link control message (resume_refused, abort_promote and the
    others) ever reaches a client (section 1 and step 7)."""
    frames = frames_sent(gateway)
    assert frames
    assert not [f for f in frames if "src_seq" in f]
    assert not [f["type"] for f in frames if f["type"] in LINK_ONLY_TYPES]


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


async def run(gateway: Any, kind: str, liveness_timeout: float = DEFAULT_LIVENESS_TIMEOUT) -> Seen:
    """Run a client of `kind` ("session" or "reconnecting") against `gateway`: it applies
    every event to RestingOrders, AccountReports and LatestTickets, sends one order once it
    reads report M, and stops at the `book` that ends the script."""
    seen = Seen()
    account = AccountReports()
    loop = asyncio.get_running_loop()
    ref: str | None = None
    gateway.resume = kind == "reconnecting"
    async with gateway.serve() as url:
        if kind == "session":
            client: Any = await open_session(
                url, synthetic_token(), liveness_timeout=liveness_timeout
            )
        else:
            client = ReconnectingSession(
                url,
                synthetic_token(),
                resting=seen.resting,
                sleep=gateway.clock.sleep,
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
# The longest hold a test asks for. A real gateway gives up at its 2 s hold limit, so the
# exchanges behind it answer in 1.5 s; the fake holds for exactly as long as it is told.
LONG_HOLD = MAX_HOLD if FAKE else 1.5
# How often the exchange sends a heartbeat, which the gateway passes on.
EXCHANGE_HEARTBEAT = 15.0
# The reports after the handover: the held order's acceptance, its resting remainder and
# its fill, all by the live build.
AFTER = [M + 1, M + 2, M + 3]


# Part A: the handover


@pytest.mark.parametrize("outcome", SWITCHED)
@pytest.mark.parametrize("repeat", [False, True])
@pytest.mark.parametrize("kind", CLIENTS)
async def test_no_seq_gap_disconnect_or_uncertainty_across_the_switch(kind, repeat, outcome):
    gateway = make_gateway(repeat=repeat, outcome=outcome)
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
    # The script ran to the end on the one connection: the book was read, though the
    # feed gap after the switch left it with an empty wall.
    book = received(seen)[-1]
    assert book.type == "book"
    assert book.message.condition == EMPTY and not book.message.bid_levels
    assert gateway.b.orders and not gateway.a.orders
    assert_nothing_link_only_reached_the_client(gateway)


@pytest.mark.parametrize("outcome", SWITCHED)
@pytest.mark.parametrize("kind", CLIENTS)
async def test_the_per_connection_seq_is_continuous_across_the_switch(kind, outcome):
    gateway = make_gateway(repeat=True, outcome=outcome)
    seen = await run(gateway, kind)
    assert gateway.link is not None
    sent = gateway.link.sent
    # The gateway's one counter: 1, 2, 3, ... with no jump and no repeat at M to M+1,
    # though the frames before the switch came from A and those after it from B.
    assert [s.seq for s in sent] == list(range(1, len(sent) + 1))
    # The fake gateway passes B's repeat of report M on, for the session to drop; a real
    # one drops it itself, by its src_seq (step 6).
    repeats = ["B"] if FAKE else []
    builds = [s.build for s in sent if s.report_seq is not None]
    assert builds == ["A"] * M + repeats + ["B"] * len(AFTER)
    # Every frame the client was delivered carries the seq the gateway gave it, in order;
    # the heartbeats between them were absorbed with their seqs counted, so none is a gap.
    delivered = [e.seq for e in received(seen)]
    assert delivered == sorted(set(delivered))
    by_seq = {s.seq: s for s in sent}
    assert [(by_seq[n].type, by_seq[n].report_seq) for n in delivered] == [
        (e.type, e.report_seq) for e in received(seen)
    ]
    # What was sent and not delivered: the ack, which opens the session, and (from the
    # fake) the repeat of report M, dropped as a duplicate.
    undelivered = [s for s in sent if s.type != "heartbeat" and s.seq not in delivered]
    assert [(s.type, s.report_seq, s.build) for s in undelivered] == [
        ("session_ack", None, ""),
        *[("execution", M, b) for b in repeats],
    ]
    assert not any(isinstance(e, SeqGap) for e in seen.events)


@pytest.mark.parametrize("outcome", SWITCHED)
@pytest.mark.parametrize("hold", [0.2, LONG_HOLD])
@pytest.mark.parametrize("kind", CLIENTS)
async def test_an_order_sent_in_the_hold_is_answered_late_with_no_reject(kind, hold, outcome):
    # The client runs with the default liveness timeout, as a student's does. Nothing
    # arrives in the hold, and a 2 s silence is far inside it.
    gateway = make_gateway(hold=hold, heartbeat_every=0.05, outcome=outcome)
    seen = await run(gateway, kind)
    assert uncertain(seen) == []
    assert not any(e.type in ("reject", "ticket_reject") for e in received(seen))
    # The order was sent during the hold and held, and answered only after the release.
    assert gateway.held and gateway.held == gateway.orders
    assert gateway.held[0]["payload"]["price"] == str(HELD_PRICE)
    assert seen.gateway_released_at_answer
    assert gateway.hold_started is not None and gateway.released_at is not None
    assert gateway.released_at - gateway.hold_started >= hold
    assert seen.sent_at is not None and seen.answered_at is not None
    assert seen.answered_at >= gateway.released_at
    if FAKE:
        # The fake's hold starts when the order reaches it.
        assert seen.sent_at <= gateway.hold_started
        # Answered late, by the hold, and within it, give or take a slow machine.
        assert hold <= seen.answered_at - seen.sent_at < hold + 3
        # The heartbeats that came due in the hold waited, and reached the client late, at
        # the release. No count is asserted: a slow runner owes fewer.
        assert gateway.link is not None and gateway.link.owed_at_release >= 1
    else:
        # A real gateway's hold starts before A's link_drain, which sent report M, so the
        # order was sent inside it. A late promotion holds to the 2 s hold limit.
        assert gateway.hold_started <= seen.sent_at < gateway.released_at
        assert seen.answered_at - seen.sent_at < max(hold, HOLD_LIMIT) + 3
    assert seen.heartbeats_at_answer > seen.heartbeats_at_send >= 1


@FAKE_ONLY
async def test_the_hold_is_silent_so_a_liveness_timeout_shorter_than_it_fires():
    # The control for the test above: the hold really is silent to the client, since the
    # exchange's heartbeats wait in it. A 1 s liveness timeout fires inside a 2 s hold, so
    # the test above passes because 2 s is inside the default, not because frames arrived.
    # Fake-only: link protocol v0.12 holds client messages, not the exchange's heartbeats.
    gateway = Gateway(hold=MAX_HOLD)
    with pytest.raises(LivenessTimeout):
        await run(gateway, "session", liveness_timeout=MAX_HOLD / 2)


def test_a_missed_heartbeat_plus_the_longest_hold_is_inside_the_default_liveness_timeout():
    # A static comparison, no gateway involved: the exchange heartbeats every 15 s and a
    # hold delays one by 2 s at most, so the longest quiet spell is 17 s; even a heartbeat
    # lost on top of that stays inside the 45 s before a link is presumed dead.
    assert 2 * EXCHANGE_HEARTBEAT + MAX_HOLD < DEFAULT_LIVENESS_TIMEOUT


@pytest.mark.parametrize("outcome", SWITCHED)
@pytest.mark.parametrize("kind", CLIENTS)
async def test_a_repeated_report_m_is_dropped_and_no_view_applies_a_duplicate_fill(kind, outcome):
    gateway = make_gateway(repeat=True, outcome=outcome)
    seen = await run(gateway, kind)
    if FAKE:
        # The gateway did send report M twice, the second time from B.
        assert gateway.link is not None
        assert [(s.report_seq, s.build) for s in gateway.link.sent if s.report_seq == M] == [
            (M, "A"),
            (M, "B"),
        ]
    else:
        # B did send report M again, with src_seq M, and the gateway sent it on once, A's.
        again = [f for link in gateway.links["B"] for f in link.frames if f.get("src_seq") == M]
        assert [f["report_seq"] for f in again] == [str(M)]
        assert [(s.report_seq, s.build) for s in gateway.link.sent if s.report_seq == M] == [
            (M, "A")
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


@pytest.mark.parametrize("outcome", SWITCHED)
@pytest.mark.parametrize("kind", CLIENTS)
async def test_fills_from_m_plus_1_by_the_new_build_are_read_normally(kind, outcome):
    gateway = make_gateway(outcome=outcome)
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


@pytest.mark.parametrize("outcome", RESUMED)
@pytest.mark.parametrize(
    ("kind", "hold"), [("session", 0.2), ("reconnecting", 0.2), ("session", LONG_HOLD)]
)
async def test_an_aborted_handover_changes_nothing(kind, hold, outcome):
    # An abort at drain (live not stopped), at confirm (live stopped, promote not sent) or
    # at promote (the follower had not appended): the old live resumes. The longest case
    # is a hold that runs to the 2 s abort; a real gateway always holds to its hold limit.
    aborted = make_gateway(outcome=outcome, hold=hold)
    seen = await run(aborted, kind)
    plain = await run(make_gateway(handover=False), kind)
    assert aborted.released_at is not None and aborted.hold_started is not None
    assert aborted.released_at - aborted.hold_started >= hold
    # The held order went back to A, which answered it; B saw nothing.
    assert aborted.held and aborted.a.orders == aborted.held and not aborted.b.orders
    assert aborted.link is not None
    assert {s.build for s in aborted.link.sent if s.report_seq is not None} == {"A"}
    assert [s.seq for s in aborted.link.sent] == list(range(1, len(aborted.link.sent) + 1))
    assert uncertain(seen) == []
    assert aborted.connections == 1
    # One session_ack: no second one from a resumed live.
    assert [s.type for s in aborted.link.sent].count("session_ack") == 1
    # The client saw exactly what it sees with no handover at all, only later.
    assert summary(seen) == summary(plain)
    assert_nothing_link_only_reached_the_client(aborted)
    if not FAKE:
        a, b = aborted.rig.a, aborted.rig.b
        assert "resumed" in a.marks
        # After stop, live reopened its writer at O (step 7); the follower stayed one.
        assert ("reopened" in a.marks) == (outcome != "abort_drain")
        assert b.role == "follower" and "promoted" not in b.marks
        # At promote, the abort reached B (and B had not appended, so it stayed a follower).
        assert ("abort_promote" in b.marks) == (outcome == "abort_promote")
        assert not aborted.link_errors


async def test_no_exchange_confirmed_live_closes_the_held_client_then_refuses_until_one_is():
    # Step 7: the follower had appended and lost its control connection at promote, and
    # live's resume was refused. The held client is closed with 1001 at the hold limit,
    # never silently reconnected; every redial is refused with 503 until an exchange
    # reports live (B, once its control connection is back), and is then served by it.
    # The fake holds the client for the whole hold. A real gateway holds it to its hold
    # limit itself, so A drains at once and the time goes in the failed promote.
    gateway = make_gateway(outcome="no_live", hold=MAX_HOLD if FAKE else 0.2, refusals=3)
    seen = await run(gateway, "reconnecting")
    # The close: 1001, which the SDK reads as a normal close.
    (disconnected,) = [e for e in seen.events if isinstance(e, Disconnected)]
    assert disconnected == Disconnected(None)
    retries = [e for e in seen.events if isinstance(e, Retrying)]
    assert retries[0].error is None
    refusals = [r.error for r in retries[1:]]
    assert refusals and all(
        isinstance(e, HandshakeFailed) and e.status_code == 503 for e in refusals
    )
    assert [c.reconnected for c in seen.events if isinstance(c, Connected)] == [False, True]
    # The held order was never answered, and nothing was rejected.
    assert not [e for e in received(seen) if e.type == "accepted"]
    assert not any(e.type in ("reject", "ticket_reject") for e in received(seen))
    assert seen.last_report_seq == M
    if FAKE:
        assert gateway.orders  # the fake took the order into its hold
        assert gateway.closed_with is not None and gateway.closed_with[0] == 1001
        assert gateway.released_at - gateway.hold_started >= MAX_HOLD
        assert len(refusals) == gateway.refused == 3
        served = gateway.links[-1]
    else:
        assert gateway.tap.closes[0][0] == 1001
        # Held to the hold limit, which began before A saw link_drain.
        assert gateway.tap.closed_at[0] - gateway.hold_started >= HOLD_LIMIT - 0.25
        assert not gateway.link_errors
        assert gateway.tap.refusals and set(gateway.tap.refusals) == {503}
        a, b = gateway.rig.a, gateway.rig.b
        assert "resume_refused" in a.marks and b.role == "live"
        assert not gateway.a.orders and not gateway.b.orders
        served = gateway.links["B"][-1]
    # The redial authenticated and resumed from M, on the exchange now live.
    assert [m["type"] for m in served.received] == ["auth", "resume"]
    assert served.received[1] == resume(M)
    assert_nothing_link_only_reached_the_client(gateway)


@FAKE_ONLY
@pytest.mark.parametrize("kind", CLIENTS)
async def test_a_gateway_that_jumps_the_seq_at_the_switch_shows_as_a_gap(kind):
    # Not the contract: should the gateway ever pass on the new build's own seq, or skip
    # its counter, the client sees a SeqGap, and a view that needs every message goes
    # incomplete, rather than silently trusting state it may have missed. Fake-only: a
    # real gateway cannot be made to break the contract.
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


# The closes `Restarting` can end with, by `end`: an exchange that stops, and a term change.
CLOSES = {"1001": (1001, "going away"), "4001": (4001, "term change")}


async def _idle_follower(exchange: LinkExchange, ws: ServerConnection) -> None:
    """Exchange B in the restart scenarios: a follower the gateway may subscribe to, which
    is never promoted and sends nothing."""
    await Link(ws).record_until_closed()


class Restarting:
    """A gateway that restarts under a client. The first connection opens, answers the
    resume with the empty marker, sends reports 1 to 3, takes one order it never answers
    (it is in flight when the gateway goes) and then ends with `end`: a drop with no close
    frame, which is what a gateway restart does, or a 1001 close, which the gateway passes
    on when the exchange stops (or a 4001 at a term change). The next `refusals` upgrades
    are refused with HTTP 503, as the load balancer answers while no gateway task is
    healthy. Every later connection opens and answers the resume with a replay of reports
    4 and 5, the in-flight order's acceptance and fill, which the exchange behind the
    gateway did take."""

    def __init__(self, end: str, refusals: int = 0) -> None:
        self.end = end
        self.refusals = refusals
        self.refused = 0
        self.links: list[Link] = []
        self.token = ""
        # The fake is the gateway and plays the exchange too; with a binary, this plays
        # exchange A behind it, `drop` is a SIGTERM restart of the binary, and the 503s
        # are A's, which the gateway passes on (section 0).
        self.clock: Clock = Clock() if FAKE else RealClock()
        self.rig: Rig | None = None
        self.restart_task: asyncio.Task | None = None

    @asynccontextmanager
    async def serve(self) -> AsyncIterator[str]:
        if FAKE:
            async with serving(self, process_request=self.process_request) as url:
                yield url
            return
        with tempfile.TemporaryDirectory() as folder:
            async with rig(
                Path(folder),
                EventLog(SCRIPTED_LOG),
                lambda exchange, ws: self(ws),
                _idle_follower,
                a_request=self.process_request,
            ) as the_rig:
                self.rig = the_rig
                try:
                    yield the_rig.url
                finally:
                    if self.restart_task is not None:
                        await asyncio.wait([self.restart_task], timeout=20)

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
            if self.end in CLOSES:
                await ws.close(*CLOSES[self.end])
            elif FAKE:
                await drop_connection(ws)
            else:
                assert self.rig is not None
                self.restart_task = asyncio.create_task(self.rig.restart())
                await ws.wait_closed()
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
    clock = gateway.clock
    view = RestingOrders()
    sent = False
    async with gateway.serve() as url:
        rs = ReconnectingSession(
            url, gateway.token, resting=view, sleep=clock.sleep, rng=lambda: 0.0
        )
        async with rs, asyncio.timeout(10 if FAKE else 30):
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


def _restart_refusal(error: Any) -> bool:
    """A redial refused while a gateway binary restarts: at TCP, or with HTTP 503."""
    if isinstance(error, HandshakeFailed):
        return error.status_code == 503
    return isinstance(error, OSError)


def assert_resumed_with_nothing_resent(gateway: Restarting, rs: ReconnectingSession) -> None:
    first, *_, last = gateway.links
    assert first.received[1] == resume(0)
    assert [m["type"] for m in first.received] == ["auth", "resume", "new"]
    # The new connection authenticated and resumed from the last report read, 3; the
    # order in flight at the restart was not sent again.
    assert last.received == [auth(gateway.token), resume(3)]
    assert rs.last_report_seq == 5


def assert_disconnected_as(end: str, events: list[Any]) -> None:
    """The one Disconnected tells the two ends apart: a 1001 close is a normal close, with
    no error; a drop is an error, with no close frame received."""
    (disconnected,) = [e for e in events if isinstance(e, Disconnected)]
    if end == "1001":
        assert disconnected == Disconnected(None)
    else:
        assert isinstance(disconnected.error, ConnectionClosedError)
        assert disconnected.error.rcvd is None
    # Either way the first redial follows the disconnect, with no attempt failed yet.
    first = next(e for e in events if isinstance(e, Retrying))
    assert first.error is None


def assert_replayed_then_live(events: list[Any]) -> None:
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


@pytest.mark.parametrize("end", ["drop", "1001"])
async def test_a_gateway_restart_is_retried_and_resumed_from_last_report_seq(end):
    # A gateway restart drops the connection with no close frame; a 1001 from an exchange
    # that stops is handled the same.
    gateway = Restarting(end)
    events, clock, rs = await restart_run(gateway)
    assert_disconnected_as(end, events)
    retries = [e for e in events if isinstance(e, Retrying)]
    if FAKE or end == "1001":
        assert [(r.attempt, r.delay) for r in retries] == [(1, 0.5)]
    else:
        # The binary is down while it restarts: the redials before it is back are refused
        # at TCP, each an OSError, and a stage-2 one may answer 503 until it has a live
        # exchange (section 0). Each is retried on the schedule.
        assert retries[0].attempt == 1 and retries[0].delay == 0.5
        assert retries[1:] and all(_restart_refusal(r.error) for r in retries[1:])
        assert gateway.rig is not None and gateway.rig.restarts == 1
    assert clock.waits == [r.delay for r in retries]
    assert len(gateway.links) == 2
    assert_resumed_with_nothing_resent(gateway, rs)
    assert_replayed_then_live(events)


@pytest.mark.parametrize("end", ["drop", "1001"])
async def test_a_run_of_503_refusals_is_retried_then_resumed_from_last_report_seq(end):
    gateway = Restarting(end, refusals=3)
    events, clock, rs = await restart_run(gateway)
    assert_disconnected_as(end, events)
    retries = [e for e in events if isinstance(e, Retrying)]
    refusals = [r.error for r in retries[1:] if isinstance(r.error, HandshakeFailed)]
    if FAKE or end == "1001":
        assert [(r.attempt, r.delay) for r in retries] == [(1, 0.5), (2, 1.0), (3, 2.0), (4, 4.0)]
        assert [e.status_code for e in refusals] == [503, 503, 503]
    else:
        # The refusals of the restart window (see the test above), then A's three 503s,
        # which the gateway passed on: `refused` below counts those.
        assert all(_restart_refusal(r.error) for r in retries[1:])
        assert len(refusals) >= 3 and {e.status_code for e in refusals} == {503}
        assert any(not isinstance(r.error, HandshakeFailed) for r in retries[1:])
    assert retries[0].error is None
    assert [r.attempt for r in retries] == list(range(1, len(retries) + 1))
    assert clock.waits == [r.delay for r in retries]
    assert gateway.refused == 3
    assert len(gateway.links) == 2  # the refused upgrades never reached a handler
    assert_resumed_with_nothing_resent(gateway, rs)
    connected = [e for e in events if isinstance(e, Connected)]
    assert [c.reconnected for c in connected] == [False, True]
    assert_replayed_then_live(events)
