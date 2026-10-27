"""Gateway conformance: what a client sees through the gateway, on the target
`QTE_GATEWAY_BIN` picks, and the fake exchanges' link side under an in-test driver.

With `QTE_GATEWAY_BIN` unset (CI, Windows included) the section-0 scenarios run against the
fake exchange A directly, which is what a transparent gateway shows a client, and the link
driver tests play the gateway's side of `hotswitch-link-protocol.md` v0.12 against the
fake exchanges A and B. With it set, the section-0 scenarios run through that binary, in
front of A and B on 127.0.0.1 (`gateway_target.Rig`). A stage-1 binary (one whose `-h`
names no `-link-credential-file`) runs only the section-0 scenarios; the handover
scenarios in `test_handover.py` skip with a reason.

Section 0 of the protocol: the gateway is transparent. A close from the exchange reaches
the client with the same code and reason; with no upstream the upgrade is refused with HTTP
503 and no WebSocket opens; heartbeats pass through both ways. The restart scenarios (a
SIGTERM, seen as 1006, then a redial and a resume) are `test_handover.py` Part B, which
runs on both targets.
"""

import asyncio
import json
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from gateway_target import (
    FAKE,
    GATEWAY_BIN_ENV,
    HEALTH_ROUTE,
    NEEDS_BINARY,
    binary_stage,
    gateway_command,
    http,
    rig,
    stage_of,
)
from link_exchange import (
    CONTROL_PATH,
    LINK_HEADER,
    STATUS_EVERY,
    Behaviour,
    EventLog,
    LinkExchange,
    write_credentials,
)
from test_handover import (
    HELD_PRICE,
    OPENING_LOG,
    BinaryGateway,
    M,
    Restarting,
    restart_run,
)
from test_reconnect import auth, resume
from test_resume import SCRIPTED_LOG
from test_session import synthetic_token
from websockets.asyncio.client import ClientConnection, connect
from websockets.asyncio.server import ServerConnection, serve
from websockets.exceptions import ConnectionClosed, ConnectionClosedError, InvalidStatus

from qte_sdk.connection import HandshakeFailed
from qte_sdk.reconnect import Disconnected
from qte_sdk.session import open_session

# A bound on every wait in this module, so a hang fails fast.
WAIT = 5.0


# Section 0 through the target


class Upstream:
    """Exchange A's client side for the section-0 scenarios: it answers the first message
    with a heartbeat, then closes with `close` if one is set, or echoes heartbeats until the
    client goes."""

    def __init__(self, close: tuple[int, str] | None = None) -> None:
        self.close = close
        self.received: list[str] = []
        self.sent: list[str] = []

    async def __call__(self, exchange: LinkExchange, ws: ServerConnection) -> None:
        try:
            async for message in ws:
                assert isinstance(message, str)
                self.received.append(message)
                if self.close is not None:
                    await ws.close(*self.close)
                    return
                n = len(self.sent) + 1
                text = json.dumps({"type": "heartbeat", "seq": n, "from": f"A-{n}"})
                self.sent.append(text)
                await ws.send(text)
        except ConnectionClosed:
            return


@asynccontextmanager
async def section0(upstream: Upstream) -> AsyncIterator[tuple[str, Any]]:
    """The client's URL and the rig (None in fake mode, where A is dialled directly)."""
    with tempfile.TemporaryDirectory() as folder:
        log = EventLog(SCRIPTED_LOG)
        if FAKE:
            _, _, digest = write_credentials(Path(folder))
            a = LinkExchange("A", "a", "live", log, digest, upstream)
            await a.start()
            try:
                yield f"ws://{a.addr}", None
            finally:
                await a.stop()
            return
        async with rig(Path(folder), log, upstream, _silent) as the_rig:
            yield the_rig.url, the_rig


async def _silent(exchange: LinkExchange, ws: ServerConnection) -> None:
    try:
        async for _ in ws:
            pass
    except ConnectionClosed:
        return


AUTH = json.dumps({"type": "auth", "payload": {"token": "synthetic"}})


@pytest.mark.parametrize(
    "close", [(1001, "exchange stopping"), (4001, "term change"), (4999, "other")]
)
async def test_a_close_from_the_exchange_reaches_the_client_with_its_code_and_reason(close):
    upstream = Upstream(close=close)
    async with section0(upstream) as (url, _), asyncio.timeout(WAIT):
        async with connect(url) as ws:
            await ws.send(AUTH)
            with pytest.raises(ConnectionClosed):
                while True:
                    await ws.recv()
            assert ws.protocol.close_rcvd is not None
            assert (ws.protocol.close_rcvd.code, ws.protocol.close_rcvd.reason) == close
    assert upstream.received == [AUTH]


async def test_a_4001_through_the_gateway_makes_the_sdk_redial_and_resume_from_0():
    # The term change forgets the cursor: the redial resumes from 0, not from report 3.
    gateway = Restarting("4001")
    events, _, rs = await restart_run(gateway)
    (disconnected,) = [e for e in events if isinstance(e, Disconnected)]
    # The close the client read is the exchange's own, code and reason.
    assert isinstance(disconnected.error, ConnectionClosedError)
    rcvd = disconnected.error.rcvd
    assert rcvd is not None and (rcvd.code, rcvd.reason) == (4001, "term change")
    first, *_, last = gateway.links
    assert first.received[1] == resume(0)
    assert last.received == [auth(gateway.token), resume(0)]


async def test_with_no_upstream_the_upgrade_is_refused_with_503_and_no_websocket():
    async with no_upstream() as url, asyncio.timeout(WAIT):
        with pytest.raises(InvalidStatus) as refused:
            async with connect(url):
                pass
        assert refused.value.response.status_code == 503
        # The SDK names it, as a retryable refusal.
        with pytest.raises(HandshakeFailed) as failed:
            await open_session(url, synthetic_token())
        assert failed.value.status_code == 503


@asynccontextmanager
async def no_upstream() -> AsyncIterator[str]:
    """A gateway with no exchange behind it. Fake mode: a server that refuses every
    upgrade, as section 0 says; binary mode: the rig with A and B stopped once it is up."""
    if FAKE:

        def refuse(connection: Any, request: Any) -> Any:
            return connection.respond(503, "no upstream\n")

        async with serve(_silent_ws, "127.0.0.1", 0, process_request=refuse) as server:
            yield f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}"
        return
    with tempfile.TemporaryDirectory() as folder:
        async with rig(Path(folder), EventLog(SCRIPTED_LOG), _silent, _silent) as the_rig:
            await the_rig.a.stop()
            await the_rig.b.stop()
            yield the_rig.url


async def _silent_ws(ws: ServerConnection) -> None:
    await ws.wait_closed()


async def test_heartbeats_pass_through_both_ways():
    upstream = Upstream()
    async with section0(upstream) as (url, the_rig), asyncio.timeout(WAIT):
        async with connect(url) as ws:
            got = []
            for n in range(3):
                text = json.dumps({"type": "heartbeat", "n": n})
                await ws.send(text)
                got.append(await ws.recv())
    # The client's heartbeats reached A, and A's reached the client, in order.
    assert [json.loads(m)["n"] for m in upstream.received] == [0, 1, 2]
    if the_rig is None or the_rig.stage < 2:
        # Transparent: byte for byte.
        assert got == upstream.sent
    else:
        # A stage-2 gateway stamps its own seq: everything else is A's.
        def unstamped(texts: list) -> list:
            return [{k: v for k, v in json.loads(t).items() if k != "seq"} for t in texts]

        assert unstamped(got) == unstamped(upstream.sent)
        assert [m["from"] for m in unstamped(got)] == ["A-1", "A-2", "A-3"]


@NEEDS_BINARY
async def test_the_gateway_answers_its_own_health_route():
    async with section0(Upstream()) as (_, the_rig):
        code, _ = await asyncio.to_thread(http, "GET", f"http://{the_rig.listen}{HEALTH_ROUTE}")
    assert code == 200


@pytest.mark.skip(
    reason="the 45 s silence close (4000) takes 45 s and needs a stage-2 gateway; "
    "run by hand: hold a client silent 46 s through the binary and expect close 4000"
)
async def test_a_silent_client_is_closed_with_4000_after_45_s():
    pass


def test_stage_detection_reads_the_help_text():
    assert stage_of("flag: help requested\n") == 1
    assert stage_of("  -listen string\n  -upstream string\n") == 1
    assert stage_of("  -link-credential-file string\n  -slots string\n") == 2


def test_the_gateway_flags_live_in_one_place():
    where = {
        "listen": "127.0.0.1:1",
        "live": "127.0.0.1:2",
        "slots": {"b": "h:2", "a": "h:1"},
        "switch_addr": "127.0.0.1:3",
        "credential_file": Path("c"),
    }
    stage_1 = gateway_command("gw", 1, **where)
    assert stage_1 == ["gw", "-listen", "127.0.0.1:1", "-upstream", "http://127.0.0.1:2"]
    stage_2 = gateway_command("gw", 2, **where)
    for flag in ("-link-credential-file", "-slots", "-switch-addr", "-hold-limit"):
        assert flag in stage_2
    assert stage_2[stage_2.index("-slots") + 1] == "a=h:1,b=h:2"


@NEEDS_BINARY
def test_the_binary_stage_is_detected():
    # A sentinel for a binary run: the report names the stage the binary was taken for.
    assert binary_stage() in (1, 2), GATEWAY_BIN_ENV


# The link driver: the gateway's side of the link protocol, against the fake exchanges


class Driver:
    """Plays the gateway against A and B with no binary: client links with the link header,
    and the control connections."""

    def __init__(self, gateway: BinaryGateway, a: LinkExchange, b: LinkExchange, credential):
        self.gateway = gateway
        self.a = a
        self.b = b
        self.credential = credential
        self.switch_started = asyncio.Event()

    def headers(self) -> dict[str, str]:
        return {LINK_HEADER: self.credential}

    async def link(self, exchange: LinkExchange, path: str = "/ws") -> ClientConnection:
        return await connect(f"ws://{exchange.addr}{path}", additional_headers=self.headers())

    async def control(self, exchange: LinkExchange) -> ClientConnection:
        return await self.link(exchange, CONTROL_PATH)


@asynccontextmanager
async def driving(
    outcome: str = "switch", repeat: bool = False, b_behaviour: Behaviour | None = None
) -> AsyncIterator[Driver]:
    gateway = BinaryGateway(outcome=outcome, repeat=repeat, hold=0.05)
    with tempfile.TemporaryDirectory() as folder:
        a, b = gateway.exchanges(Path(folder))
        # The credential exchanges() wrote, which the driver sends as the gateway does.
        credential = (Path(folder) / "link-credential").read_text()
        if b_behaviour is not None:
            b.behaviour = b_behaviour
        driver = Driver(gateway, a, b, credential)

        async def start_switch() -> None:
            driver.switch_started.set()

        gateway.start_switch = start_switch
        await a.start()
        await b.start()
        try:
            async with asyncio.timeout(WAIT):
                yield driver
        finally:
            await a.stop()
            await b.stop()


async def until(ws: ClientConnection, kind: str, **fields: Any) -> dict:
    """The next message of type `kind` (with `fields`, if given) on `ws`, passing over the
    rest: link_status on a control connection, the script's frames on a client link."""
    while True:
        message = json.loads(await ws.recv())
        if message["type"] == kind and all(message.get(k) == v for k, v in fields.items()):
            return message


async def reports_until(ws: ClientConnection, n: int) -> list[dict]:
    """Every frame on a client link up to the one with report_seq `n`."""
    frames = [json.loads(await ws.recv())]
    while frames[-1].get("report_seq") != str(n):
        frames.append(json.loads(await ws.recv()))
    return frames


async def nothing_but_status(ws: ClientConnection, wait: float = 0.4) -> None:
    """Nothing but link_status arrives on a control connection for `wait` seconds."""
    try:
        async with asyncio.timeout(wait):
            while True:
                assert json.loads(await ws.recv())["type"] == "link_status"
    except TimeoutError:
        return


async def test_link_status_comes_every_250_ms_on_the_control_connection():
    window = 1.0
    async with driving() as d:
        for exchange, role in ((d.a, "live"), (d.b, "follower")):
            statuses = []
            async with await d.control(exchange) as ws:
                try:
                    async with asyncio.timeout(window):
                        while True:
                            statuses.append(json.loads(await ws.recv()))
                except TimeoutError:
                    pass
            assert {s["type"] for s in statuses} == {"link_status"}
            assert {s["role"] for s in statuses} == {role}
            assert all(s["ready"] and s["log_id"] == SCRIPTED_LOG for s in statuses)
            assert all(s["folded_seq"] == s["log_tail_seq"] == M for s in statuses)
            # One at once, then one every 250 ms: 5 in a second. A count in a window, not
            # the gaps, so a slow reader that takes two at once does not fail it.
            expected = 1 + window / STATUS_EVERY
            assert expected - 2 <= len(statuses) <= expected, len(statuses)


async def test_a_wrong_link_header_gets_401_and_a_follower_without_one_403():
    async with driving() as d:
        for exchange in (d.a, d.b):
            for path in ("/ws", CONTROL_PATH):
                with pytest.raises(InvalidStatus) as wrong:
                    await connect(
                        f"ws://{exchange.addr}{path}", additional_headers={LINK_HEADER: "nope"}
                    )
                assert wrong.value.response.status_code == 401
        with pytest.raises(InvalidStatus) as plain:
            await connect(f"ws://{d.b.addr}/ws")
        assert plain.value.response.status_code == 403
        with pytest.raises(InvalidStatus) as control:
            await connect(f"ws://{d.a.addr}{CONTROL_PATH}")
        assert control.value.response.status_code == 403
        # A live serves a plain connection (the stage-1 gateway's), with no src_seq.
        async with connect(f"ws://{d.a.addr}/ws") as ws:
            await ws.send(json.dumps({"type": "auth", "payload": {}}))
            frames = await reports_until(ws, M - 1)
        assert not any("src_seq" in f for f in frames)
        assert [f.get("report_seq") for f in frames if "report_seq" in f] == [
            str(n) for n in range(1, M)
        ]
        assert d.a.plain_connections == 1


async def test_src_seq_is_only_on_messages_from_logged_records():
    async with driving() as d:
        async with await d.link(d.a) as ws:
            await ws.send(json.dumps({"type": "auth", "payload": {}}))
            frames = await reports_until(ws, M - 1)
        assert d.a.link_connections == 1
        for f in frames:
            if "report_seq" in f:
                # A logged record: its src_seq is its seq in the event log.
                assert f["src_seq"] == int(f["report_seq"]), f
            else:
                # session_ack, calendar, heartbeats and on-connect ticket states: none.
                assert "src_seq" not in f, f
        assert {"session_ack", "calendar", "ticket_state"} <= {f["type"] for f in frames}


async def full_switch(d: Driver) -> tuple[ClientConnection, ClientConnection, list[dict]]:
    """Drive a switch from A to B as the gateway does (section 3 steps 1 to 6). Returns
    the client links to A and B, and B's frames before promotion."""
    a_link = await d.link(d.a)
    b_link = await d.link(d.b)
    a_ctl = await d.control(d.a)
    b_ctl = await d.control(d.b)
    # Before promotion the follower answers auth and subscribes, as the gateway's
    # standby link.
    await b_link.send(json.dumps({"type": "auth", "payload": {}}))
    before = [json.loads(await b_link.recv())]
    while before[-1]["type"] != "calendar":
        before.append(json.loads(await b_link.recv()))
    await a_link.send(json.dumps({"type": "auth", "payload": {}}))
    await d.switch_started.wait()
    await a_link.send(json.dumps({"type": "link_drain", "n": 1}))
    report_m = await until(a_link, "execution", report_seq=str(M))
    assert report_m["src_seq"] == M
    assert (await until(a_link, "link_drained"))["n"] == 1
    await a_ctl.send(json.dumps({"type": "stop_after_current"}))
    stopped = await until(a_ctl, "stopped")
    assert stopped["seq"] == M and stopped["ts"] == d.gateway.log.ts(M)
    await b_ctl.send(
        json.dumps({"type": "confirm", "seq": stopped["seq"], "offset": stopped["offset"]})
    )
    assert (await until(b_ctl, "applied"))["seq"] == M
    await b_ctl.send(json.dumps(promote(d)))
    assert (await until(b_ctl, "promoted"))["seq"] == M
    for ws in (a_ctl, b_ctl):
        await ws.close()
    return a_link, b_link, before


@pytest.mark.parametrize("repeat", [False, True])
async def test_a_full_switch_on_the_link_side(repeat):
    async with driving(repeat=repeat) as d:
        a_link, b_link, before = await full_switch(d)
        # B's own heartbeats may come first: the rest is the ack, then the calendar.
        assert [f["type"] for f in before if f["type"] != "heartbeat"] == [
            "session_ack",
            "calendar",
        ]
        assert d.a.stopped and not d.a.accepting_orders
        assert d.b.role == "live" and d.b.accepting_orders
        # B wrote its gap start at O, after M.
        assert d.gateway.log.records[M].type == "gap_start"
        await b_link.send(json.dumps(order()))
        frames = []
        while not frames or frames[-1]["type"] != "book":
            frames.append(json.loads(await b_link.recv()))
        reports = [f for f in frames if "report_seq" in f]
        if repeat:
            # The repeat of report M carries src_seq M, which the gateway drops (step 6).
            assert (reports[0]["report_seq"], reports[0]["src_seq"]) == (str(M), M)
            reports = reports[1:]
        assert [r["report_seq"] for r in reports] == [str(M + 1), str(M + 2), str(M + 3)]
        assert all(r["src_seq"] > M + 1 for r in reports)
        assert d.gateway.b.orders and not d.gateway.a.orders
        await a_link.close()
        await b_link.close()


def promote(d: Driver) -> dict:
    """Step 5's promote at the log's current end: seq M, offset O and M's timestamp."""
    log = d.gateway.log
    return {"type": "promote", "seq": log.tail, "offset": log.offset(), "ts": log.ts(log.tail)}


def order() -> dict:
    payload = {"request_ref": "r-1", "price": str(HELD_PRICE), "size": "50"}
    return {"type": "new", "payload": payload}


async def test_an_order_sent_to_a_follower_or_a_stopped_live_is_a_link_error():
    async with driving() as d:
        async with await d.link(d.b) as b_link:
            await b_link.send(json.dumps({"type": "auth", "payload": {}}))
            await until(b_link, "calendar")
            await b_link.send(json.dumps(order()))
            assert (await until(b_link, "link_error"))["reason"]
            async with await d.control(d.a) as a_ctl:
                await a_ctl.send(json.dumps({"type": "stop_after_current"}))
                await until(a_ctl, "stopped")
                async with await d.link(d.a) as a_link:
                    await a_link.send(json.dumps({"type": "auth", "payload": {}}))
                    await until(a_link, "calendar")
                    await a_link.send(json.dumps(order()))
                    assert (await until(a_link, "link_error"))["reason"]
        assert len(d.gateway.link_errors) == 2
        assert not d.gateway.a.orders and not d.gateway.b.orders


@pytest.mark.parametrize("stop_first", [False, True])
async def test_an_abort_resumes_the_old_live(stop_first):
    async with driving() as d:
        async with await d.control(d.a) as a_ctl:
            if stop_first:
                await a_ctl.send(json.dumps({"type": "stop_after_current"}))
                await until(a_ctl, "stopped")
                status = await until(a_ctl, "link_status")
                assert (status["role"], status["ready"]) == ("follower", False)
            await a_ctl.send(json.dumps({"type": "resume"}))
            await nothing_but_status(a_ctl)
            status = await until(a_ctl, "link_status")
        assert "resumed" in d.a.marks
        assert ("reopened" in d.a.marks) == stop_first
        assert d.a.accepting_orders and status["role"] == "live"


async def test_with_no_exchange_live_resume_is_refused_and_control_comes_back_later():
    behaviour = Behaviour(drop_control_on_promote=True, refuse_control_for=0.5)
    async with driving(outcome="no_live", b_behaviour=behaviour) as d:
        a_ctl = await d.control(d.a)
        b_ctl = await d.control(d.b)
        await a_ctl.send(json.dumps({"type": "stop_after_current"}))
        stopped = await until(a_ctl, "stopped")
        confirm = {"type": "confirm", "seq": stopped["seq"], "offset": stopped["offset"]}
        await b_ctl.send(json.dumps(confirm))
        await until(b_ctl, "applied")
        await b_ctl.send(json.dumps(promote(d)))
        # B appended and dropped its control connection with no answer.
        with pytest.raises(ConnectionClosed):
            await until(b_ctl, "promoted")
        await a_ctl.send(json.dumps({"type": "resume"}))
        assert (await until(a_ctl, "resume_refused"))["reason"]
        await a_ctl.close()
        with pytest.raises(InvalidStatus) as refused:
            await d.control(d.b)
        assert refused.value.response.status_code == 503
        await asyncio.sleep(0.6)
        async with await d.control(d.b) as again:
            status = await until(again, "link_status")
        assert (status["role"], status["ready"]) == ("live", True)
        assert d.a.refused and not d.a.accepting_orders


async def test_a_late_promotion_answers_abort_promote_with_promoted():
    async with driving(outcome="late_promote") as d:
        async with await d.control(d.b) as b_ctl:
            await b_ctl.send(json.dumps(promote(d)))
            await nothing_but_status(b_ctl)
            await b_ctl.send(json.dumps({"type": "abort_promote"}))
            assert (await until(b_ctl, "promoted"))["seq"] == M
        assert d.b.role == "live" and d.b.appended


async def test_abort_promote_before_the_first_append_leaves_a_follower():
    async with driving(outcome="abort_promote") as d:
        async with await d.control(d.b) as b_ctl:
            await b_ctl.send(json.dumps(promote(d)))
            await b_ctl.send(json.dumps({"type": "abort_promote"}))
            await nothing_but_status(b_ctl)
            status = await until(b_ctl, "link_status")
        assert status["role"] == "follower" and "stayed_follower" in d.b.marks
        assert d.gateway.log.tail == len(OPENING_LOG)


async def test_a_confirm_at_the_wrong_offset_is_answered_with_offset_mismatch():
    async with driving() as d:
        async with await d.control(d.b) as b_ctl:
            offset = d.gateway.log.offset()
            await b_ctl.send(json.dumps({"type": "confirm", "seq": M, "offset": offset + 1}))
            assert (await until(b_ctl, "error"))["reason"] == "offset_mismatch"
        assert "applied" not in d.b.marks
