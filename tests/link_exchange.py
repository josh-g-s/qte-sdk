"""A fake exchange that speaks the exchange side of the hot-switch link protocol.

It follows qte-core's `hotswitch-link-protocol.md` at `LINK_PROTOCOL` (sections 0 to 3): the
`X-QTE-Link` check against a file holding the SHA-256 hex of the link credential (HTTP 401
when the header is wrong, 403 on a follower without it), the control connection at
`/link/control` with a `link_status` every 250 ms, and the control messages of a handover
(`stop_after_current`, `confirm`, `promote`, `abort_promote` and `resume`). What it says on a
client connection is not its business: each exchange is given a client handler (a script,
such as `test_handover.Upstream` or `test_handover.Restarting`), which reads the exchange's
`role` and `stopped` and puts `src_seq` on the messages from logged records of a link
connection. `link_drain` arrives on a client connection, so the script answers it too.

The fakes run in front of a real gateway binary (`gateway_target.py`) and, in CI, under an
in-test driver that plays the gateway's side (`test_gateway_conformance.py`).
"""

import asyncio
import hashlib
import hmac
import json
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from websockets.asyncio.server import Server, ServerConnection, serve
from websockets.exceptions import ConnectionClosed

# The protocol version these fakes implement; qte-core checks the SDK lane's reading of it.
LINK_PROTOCOL = "hotswitch-link-protocol.md v0.12 (sha256 63ee27c9)"
LINK_HEADER = "X-QTE-Link"
CONTROL_PATH = "/link/control"
STATUS_EVERY = 0.25  # seconds between link_status messages (section 2)
# The control messages that stay between the gateway and an exchange: none ever reaches a
# client.
LINK_ONLY_TYPES = frozenset(
    {
        "link_drain",
        "link_drained",
        "link_status",
        "stop_after_current",
        "stopped",
        "confirm",
        "applied",
        "promote",
        "promoted",
        "abort_promote",
        "resume_refused",
        "link_error",
    }
)


def credential_digest(credential: str) -> str:
    """The lowercase hex SHA-256 of a link credential, as the exchange's file holds it."""
    return hashlib.sha256(credential.encode()).hexdigest()


def write_credentials(folder: Path) -> tuple[str, Path, Path]:
    """Make a throwaway link credential: returns it, the gateway's file (the raw credential)
    and the exchanges' file (its SHA-256 hex). Neither file is ever printed."""
    credential = secrets.token_hex(32)
    raw = folder / "link-credential"
    digest = folder / "link-credential.sha256"
    raw.write_text(credential)
    digest.write_text(credential_digest(credential) + "\n")
    return credential, raw, digest


@dataclass(frozen=True)
class Record:
    """One record of the event log both exchanges fold. `report_seq` is the team's report
    number for a report record, None for any other record."""

    type: str
    payload: dict
    report_seq: int | None


@dataclass
class EventLog:
    """The one event log: the live appends, the follower folds what is there."""

    log_id: str
    records: list[Record] = field(default_factory=list)

    @property
    def tail(self) -> int:
        """The seq of the last record (0 for an empty log)."""
        return len(self.records)

    def append(self, type_: str, payload: dict, *, report: bool = True) -> int:
        """Append one record; returns its seq."""
        report_seq = None
        if report:
            report_seq = 1 + sum(r.report_seq is not None for r in self.records)
        self.records.append(Record(type_, payload, report_seq))
        return self.tail

    def offset(self) -> int:
        """The log's size in bytes: one JSON line per record."""
        return sum(len(json.dumps([r.type, r.payload])) + 1 for r in self.records)

    def ts(self, seq: int) -> int:
        """Record `seq`'s timestamp in engine units."""
        return 1_700_000_000_000_000_000 + seq


ClientHandler = Callable[["LinkExchange", ServerConnection], Awaitable[None]]
RequestHook = Callable[[Any, Any], Any]


@dataclass
class Behaviour:
    """How an exchange answers the gateway's control messages: the defaults complete a
    switch; the others make each abort of section 3 step 7 happen."""

    answer_confirm: bool = True  # False: never answers confirm (an abort at confirm)
    append_on_promote: bool = True  # False: abort_promote finds it before its first append
    # "now": answers promote with promoted; "never": does not answer it; "on_abort": answers
    # only abort_promote, with promoted (late promotion).
    promoted: str = "now"
    drop_control_on_promote: bool = False  # closes its control connection after appending
    refuse_control_for: float = 0.0  # then refuses control redials (HTTP 503) this long


class LinkExchange:
    """One fake exchange, `role` "live" or "follower", in deployment slot `slot`."""

    def __init__(
        self,
        name: str,
        slot: str,
        role: str,
        log: EventLog,
        digest_file: Path,
        client: ClientHandler,
        *,
        client_request: RequestHook | None = None,
        behaviour: Behaviour | None = None,
    ) -> None:
        self.name = name
        self.slot = slot
        self.role = role
        self.log = log
        self.digest = digest_file.read_text().strip()
        self.client = client
        self.client_request = client_request
        self.behaviour = behaviour or Behaviour()
        self.build = hashlib.sha256(f"build-{name}".encode()).hexdigest()
        self.stopped = False  # live, between stopped and resume: refuses every append
        self.stop_offset: int | None = None  # O, the log size at stopped
        self.appended = False  # a follower that has appended since promote
        self.refused = False  # a stopped live whose resume was refused
        self.promote_seq: int | None = None  # M, from the promote this follower was sent
        self.server: Server | None = None
        self.port = 0
        self.link_connections = 0  # client connections with a valid link header
        self.plain_connections = 0  # client connections without one
        self.control_connections = 0
        self.http_answers: list[tuple[str, int]] = []  # (path, status) answered here
        self.control_received: list[dict] = []
        self.control_sent: list[dict] = []
        self.marks: dict[str, float] = {}  # first loop time of each event, by name
        self._control_refused_until: float | None = None
        self._control_ws: ServerConnection | None = None

    # Life cycle

    async def start(self) -> None:
        self.server = await serve(
            self._handle, "127.0.0.1", 0, process_request=self._process_request
        )
        self.port = self.server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()

    @property
    def addr(self) -> str:
        return f"127.0.0.1:{self.port}"

    def mark(self, name: str) -> None:
        self.marks.setdefault(name, asyncio.get_running_loop().time())

    @property
    def accepting_orders(self) -> bool:
        """Whether order entry is taken: live and not stopped (section 3 steps 3 and 5)."""
        return self.role == "live" and not self.stopped

    def is_link(self, ws: ServerConnection) -> bool:
        """Whether `ws` carries a valid link header (checked again here, cheaply)."""
        assert ws.request is not None
        return self._valid(ws.request.headers.get(LINK_HEADER))

    def _valid(self, header: str | None) -> bool:
        if header is None:
            return False
        return hmac.compare_digest(credential_digest(header), self.digest)

    # HTTP: the link check, then the paths

    def _answer(self, connection: Any, path: str, status: int, body: str) -> Any:
        self.http_answers.append((path, status))
        return connection.respond(status, body)

    async def _process_request(self, connection: Any, request: Any) -> Any:
        path = request.path.split("?", 1)[0]
        header = request.headers.get(LINK_HEADER)
        if header is not None and not self._valid(header):
            # Section 1: a wrong link header gets 401 before any upgrade; never logged.
            return self._answer(connection, path, 401, "bad link credential\n")
        link = header is not None
        if path == CONTROL_PATH:
            if not link:
                return self._answer(connection, path, 403, "link connections only\n")
            refused_until = self._control_refused_until
            if refused_until is not None and asyncio.get_running_loop().time() < refused_until:
                return self._answer(connection, path, 503, "control unavailable\n")
            return None
        if not link and self.role == "follower":
            # Section 2: a follower serves link connections only.
            return self._answer(connection, path, 403, "follower: link connections only\n")
        if request.headers.get("Upgrade", "").lower() != "websocket":
            if path in ("/healthz", "/livez"):
                live = self.accepting_orders
                return self._answer(connection, path, 200 if live else 503, f"{self.name}\n")
            return self._answer(connection, path, 404, "not found\n")
        if self.client_request is not None:
            answer = self.client_request(connection, request)
            if asyncio.iscoroutine(answer):
                answer = await answer
            return answer
        return None

    async def _handle(self, ws: ServerConnection) -> None:
        assert ws.request is not None
        if ws.request.path.split("?", 1)[0] == CONTROL_PATH:
            await self._control(ws)
            return
        if self.is_link(ws):
            self.link_connections += 1
        else:
            self.plain_connections += 1
        await self.client(self, ws)

    # The control connection (sections 2 and 3)

    def status(self) -> dict:
        # A stopped live is not live: it takes no order until resume. This fake reports it
        # as a follower that is not ready, so a gateway never takes it for live.
        role = "follower" if self.stopped or self.refused else self.role
        return {
            "type": "link_status",
            "role": role,
            "build": self.build,
            "log_id": self.log.log_id,
            "folded_seq": self.log.tail,
            "log_tail_seq": self.log.tail,
            "ready": role == "live" or not (self.stopped or self.refused),
        }

    async def _send_control(self, ws: ServerConnection, message: dict) -> None:
        self.control_sent.append(message)
        await ws.send(json.dumps(message))

    async def _status_loop(self, ws: ServerConnection) -> None:
        try:
            while True:
                await ws.send(json.dumps(self.status()))
                await asyncio.sleep(STATUS_EVERY)
        except ConnectionClosed:
            return

    async def _control(self, ws: ServerConnection) -> None:
        self.control_connections += 1
        self._control_ws = ws
        status = asyncio.create_task(self._status_loop(ws))
        try:
            async for text in ws:
                message = json.loads(text)
                self.control_received.append(message)
                self.mark(message["type"])
                await self._on_control(ws, message)
        except ConnectionClosed:
            pass
        finally:
            status.cancel()

    async def _on_control(self, ws: ServerConnection, message: dict) -> None:
        kind = message["type"]
        b = self.behaviour
        if kind == "stop_after_current":
            if self.role != "live" or self.stopped:
                await self._send_control(ws, {"type": "error", "reason": "not_live"})
                return
            # Step 3: no further append; M is the last seq, O the log size after it.
            self.stopped = True
            self.stop_offset = self.log.offset()
            seq = self.log.tail
            self.mark("stopped")
            reply = {"type": "stopped", "seq": seq, "offset": self.stop_offset}
            await self._send_control(ws, {**reply, "ts": self.log.ts(seq)})
        elif kind == "confirm":
            if not b.answer_confirm:
                return
            if message.get("seq") != self.log.tail or message.get("offset") != self.log.offset():
                # This fake's reading: the follower names the mismatch in an error answer.
                await self._send_control(ws, {"type": "error", "reason": "offset_mismatch"})
                return
            self.mark("applied")
            await self._send_control(ws, {"type": "applied", "seq": message["seq"]})
        elif kind == "promote":
            if any(k not in message for k in ("seq", "offset", "ts")):
                await self._send_control(ws, {"type": "error", "reason": "malformed promote"})
                return
            if message["seq"] != self.log.tail or message["offset"] != self.log.offset():
                # Step 5 checks the frame at O before it opens the log as writer.
                await self._send_control(ws, {"type": "error", "reason": "offset_mismatch"})
                return
            self.promote_seq = message["seq"]
            if b.append_on_promote:
                # Step 5: the follower opens the log at O and writes its cause-4 gap start.
                self.log.append("gap_start", {"cause": 4}, report=False)
                self.appended = True
                self.role = "live"
                self.mark("appended")
            if b.drop_control_on_promote:
                if b.refuse_control_for:
                    loop = asyncio.get_running_loop()
                    self._control_refused_until = loop.time() + b.refuse_control_for
                self.mark("control_dropped")
                await ws.close()
                return
            if b.promoted == "now":
                await self._promoted(ws, message["seq"])
        elif kind == "abort_promote":
            if self.appended:
                # Late promotion: the fence decides; after its first append it stays live.
                assert self.promote_seq is not None
                await self._promoted(ws, self.promote_seq)  # M, the cut, not the log tail
            else:
                self.role = "follower"
                self.mark("stayed_follower")
        elif kind == "resume":
            if not self.stopped:
                self.mark("resumed")  # an abort before stop: nothing to reopen
            elif self.log.offset() == self.stop_offset:
                self.stopped = False  # reopen the writer from O: nobody wrote after it
                self.mark("resumed")
                self.mark("reopened")
            else:
                self.refused = True
                self.mark("resume_refused")
                await self._send_control(
                    ws, {"type": "resume_refused", "reason": "log size is not O"}
                )
        else:
            await self._send_control(ws, {"type": "error", "reason": f"unknown {kind}"})

    async def _promoted(self, ws: ServerConnection, seq: int) -> None:
        self.role = "live"
        self.stopped = False
        self.mark("promoted")
        await self._send_control(ws, {"type": "promoted", "seq": seq})
