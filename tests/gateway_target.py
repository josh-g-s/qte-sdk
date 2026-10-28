"""Which gateway the conformance scenarios run against, chosen by one setting.

With `QTE_GATEWAY_BIN` unset, the scenarios run against the fake gateway inside the tests
(`test_handover.Gateway` and `test_handover.Restarting`); this is what CI runs, Windows
included. With it set to the path of a qte-gateway binary, the same scenarios run against
that binary, started on 127.0.0.1 in front of two fake link-protocol exchanges, A in slot
a (live) and B in slot b (follower), from `link_exchange.py`.

A stage-1 binary (a transparent proxy, link protocol section 0 only) runs the section-0
scenarios; the handover scenarios, which need stage 2, are skipped with a reason. A binary
is taken for stage 2 when its `-h` output names `-link-credential-file`.

Every gateway flag is set in `gateway_command`. The switch command (`SWITCH_ROUTE`) and
the stage-2 flags are as qte-core set them for G3 (qte-platform #2764) on 27 Oct 2026, to go
into link protocol v0.13.
Everything is local: no AWS, no network beyond loopback, and a throwaway link credential
and upstream header written to a temporary folder.
"""

import asyncio
import json
import os
import secrets
import signal
import socket
import subprocess
import sys
import urllib.error
import urllib.request
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import Any

import pytest
from link_exchange import (
    Behaviour,
    ClientHandler,
    EventLog,
    LinkExchange,
    RequestHook,
    write_credentials,
)
from websockets.asyncio.client import ClientConnection, connect
from websockets.asyncio.server import ServerConnection, serve
from websockets.exceptions import ConnectionClosed, InvalidStatus

GATEWAY_BIN_ENV = "QTE_GATEWAY_BIN"
GATEWAY_BIN = os.environ.get(GATEWAY_BIN_ENV) or None
FAKE = GATEWAY_BIN is None
# Set to 1, a binary that is not stage 2 fails the handover scenarios instead of skipping them.
REQUIRE_STAGE_2_ENV = "QTE_GATEWAY_REQUIRE_STAGE_2"
REQUIRE_STAGE_2 = os.environ.get(REQUIRE_STAGE_2_ENV) == "1"

# The switch command on the loopback-only -switch-addr listener (qte-core for G3, v0.13).
# It answers 200 {"result": "promoted", "seq": M}, 200 {"result": "aborted", "abort_step",
# "abort_reason", "abort_detail"}, or 409 {"result": "refused", "reason"} with nothing changed.
# The -admin-addr listener answers the same POST with 403.
SWITCH_ROUTE = "/gateway/switch"
SWITCH_BODY = {"to": "b"}
SWITCH_RESULTS = {200: {"promoted", "aborted"}, 409: {"refused"}}
STATUS_ROUTE = "/gateway/switch-status"  # section 2b
HEALTH_ROUTE = "/gateway/healthz"  # section 0
HOLD_LIMIT = 2.0  # seconds, the -hold-limit of section 3 step 7
READY_TIMEOUT = 10.0
# The stage-1 binary also needs an upstream header secret (X-QTE-Upstream, env only).
UPSTREAM_HEADER_ENV = "QTE_GATEWAY_UPSTREAM_HEADER"

NEEDS_BINARY = pytest.mark.skipif(
    FAKE, reason=f"binary mode: set {GATEWAY_BIN_ENV} to a qte-gateway binary to run it"
)
FAKE_ONLY = pytest.mark.skipif(
    not FAKE, reason=f"fake-only: checks the in-test fake gateway itself ({GATEWAY_BIN_ENV} set)"
)


def stage_of(help_text: str) -> int:
    """The gateway stage a binary's `-h` output shows: 2 once it has the link flags."""
    return 2 if "-link-credential-file" in help_text else 1


@cache
def binary_stage() -> int:
    """The stage of the binary in GATEWAY_BIN (only in binary mode)."""
    assert GATEWAY_BIN is not None
    done = subprocess.run(
        [GATEWAY_BIN, "-h"],
        capture_output=True,
        text=True,
        timeout=10,
        stdin=subprocess.DEVNULL,
        env={k: v for k, v in os.environ.items() if not k.startswith("QTE_GATEWAY_")},
    )
    return stage_of(done.stdout + done.stderr)


def needs_stage_2() -> None:
    """Skip the calling test unless the target can hold a handover: the fake always can,
    a binary only from stage 2. With `REQUIRE_STAGE_2_ENV` set to 1 it fails instead."""
    if not FAKE and binary_stage() < 2:
        reason = (
            f"needs a stage-2 gateway: {GATEWAY_BIN} has no -link-credential-file "
            "(stage 1, section 0 only)"
        )
        if REQUIRE_STAGE_2:
            pytest.fail(f"{reason}; {REQUIRE_STAGE_2_ENV}=1")
        pytest.skip(reason)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def gateway_command(
    binary: str,
    stage: int,
    *,
    listen: str,
    live: str,
    slots: dict[str, str],
    switch_addr: str,
    admin_addr: str,
    credential_file: Path,
) -> list[str]:
    """Every gateway flag, in one place. Stage 1 knows only -listen and -upstream; stage 2
    adds the flags qte-core set for G3."""
    command = [binary, "-listen", listen]
    if stage < 2:
        return [*command, "-upstream", f"http://{live}"]
    return [
        *command,
        "-link-credential-file",
        str(credential_file),
        "-slots",
        ",".join(f"{slot}={addr}" for slot, addr in sorted(slots.items())),
        "-admin-addr",
        admin_addr,
        "-switch-addr",
        switch_addr,
        "-hold-limit",
        f"{HOLD_LIMIT:g}s",
    ]


def http(method: str, url: str, body: dict | None = None) -> tuple[int, str]:
    """One loopback HTTP request, blocking: (status, body text)."""
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=5) as answer:
            return answer.status, answer.read().decode()
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode()


@dataclass
class Rig:
    """A gateway binary in front of fake exchanges A (slot a, live) and B (slot b, follower).

    A stage-1 binary is pointed at A alone. `log_path` holds the gateway's own log lines
    (it never prints the credential). `restart` stops it with SIGTERM, as ECS does, and
    starts it again on the same port after `gap` seconds."""

    a: LinkExchange
    b: LinkExchange
    folder: Path
    stage: int = field(default_factory=binary_stage)
    process: subprocess.Popen | None = None
    restarts: int = 0

    def __post_init__(self) -> None:
        self.listen = f"127.0.0.1:{free_port()}"
        self.switch_addr = f"127.0.0.1:{free_port()}"
        self.admin_addr = f"127.0.0.1:{free_port()}"
        self.log_path = self.folder / "gateway.log"
        _, self.credential_file, _ = self.credentials

    credentials: tuple[str, Path, Path] = field(default=("", Path(), Path()))

    @property
    def url(self) -> str:
        return f"ws://{self.listen}"

    def command(self) -> list[str]:
        assert GATEWAY_BIN is not None
        return gateway_command(
            GATEWAY_BIN,
            self.stage,
            listen=self.listen,
            live=self.a.addr,
            slots={"a": self.a.addr, "b": self.b.addr},
            switch_addr=self.switch_addr,
            admin_addr=self.admin_addr,
            credential_file=self.credential_file,
        )

    async def start(self) -> None:
        env = {k: v for k, v in os.environ.items() if not k.startswith("QTE_")}
        env[UPSTREAM_HEADER_ENV] = secrets.token_hex(16)
        with self.log_path.open("a") as log:
            self.process = subprocess.Popen(
                self.command(),
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                env=env,
            )
        await self.wait_ready()

    async def wait_ready(self) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + READY_TIMEOUT
        while True:
            assert self.process is not None
            if self.process.poll() is not None:
                raise RuntimeError(f"the gateway exited {self.process.returncode}: {self.tail()}")
            if await asyncio.to_thread(self._ready):
                return
            if loop.time() > deadline:
                raise TimeoutError(f"the gateway did not get ready: {self.tail()}")
            await asyncio.sleep(0.05)

    def _ready(self) -> bool:
        try:
            if self.stage < 2:
                return http("GET", f"http://{self.listen}{HEALTH_ROUTE}")[0] == 200
            status = self.switch_status()
        except OSError:
            return False
        exchanges = status.get("exchanges") or []
        return (
            status.get("live_slot") is not None
            and len(exchanges) == 2
            and all(e.get("ready") for e in exchanges)
        )

    def switch_status(self) -> dict:
        code, text = http("GET", f"http://{self.switch_addr}{STATUS_ROUTE}")
        assert code == 200, (code, text)
        return json.loads(text)

    async def switch(self, addr: str | None = None) -> tuple[int, dict]:
        """Send the switch command to slot b, on the switch listener unless `addr` says
        otherwise. A 200 or 409 reply must carry one of its listed results."""
        url = f"http://{addr or self.switch_addr}{SWITCH_ROUTE}"
        code, text = await asyncio.to_thread(http, "POST", url, SWITCH_BODY)
        reply = json.loads(text) if code in SWITCH_RESULTS else {}
        if code in SWITCH_RESULTS:
            assert reply.get("result") in SWITCH_RESULTS[code], (code, text)
        return code, reply

    async def sigterm(self) -> None:
        assert self.process is not None
        if sys.platform == "win32":
            self.process.terminate()
        else:
            self.process.send_signal(signal.SIGTERM)
        await asyncio.to_thread(self.process.wait, 15)

    async def restart(self, gap: float = 0.3) -> None:
        await self.sigterm()
        await asyncio.sleep(gap)
        self.restarts += 1
        await self.start()

    async def close(self) -> None:
        if self.process is not None and self.process.poll() is None:
            self.process.kill()
            await asyncio.to_thread(self.process.wait, 15)

    def tail(self, lines: int = 20) -> str:
        try:
            return "\n".join(self.log_path.read_text().splitlines()[-lines:])
        except OSError:
            return ""


@asynccontextmanager
async def rig(
    folder: Path,
    log: EventLog,
    a_client: ClientHandler,
    b_client: ClientHandler,
    *,
    a_request: RequestHook | None = None,
    a_behaviour: Behaviour | None = None,
    b_behaviour: Behaviour | None = None,
    start_gateway: bool = True,
) -> AsyncIterator[Rig]:
    """Start A, B and (unless told not to) the gateway; stop them all afterwards."""
    credentials = write_credentials(folder)
    digest = credentials[2]
    a = LinkExchange(
        "A", "a", "live", log, digest, a_client, client_request=a_request, behaviour=a_behaviour
    )
    b = LinkExchange("B", "b", "follower", log, digest, b_client, behaviour=b_behaviour)
    the_rig: Rig | None = None
    try:
        await a.start()
        await b.start()
        the_rig = Rig(a, b, folder, credentials=credentials)
        if start_gateway:
            await the_rig.start()
        yield the_rig
    finally:
        if the_rig is not None:
            await the_rig.close()
        await a.stop()
        await b.stop()


class Tap:
    """A recording relay between a client and the gateway, for the handover scenarios in
    binary mode: it passes every frame both ways unchanged and records the gateway's frames
    as parsed JSON (`frames`), so a test can read the seq the gateway stamped and check
    that no `src_seq` or link message reached the client. An HTTP refusal and a close are
    passed on with the same status, code and reason; a close with no close frame is passed
    on as one."""

    def __init__(self, upstream: str) -> None:
        self.upstream = upstream
        self.frames: list[dict] = []
        self.refusals: list[int] = []
        self.closes: list[tuple[int | None, str | None]] = []
        self.closed_at: list[float] = []  # loop time of each close, as `closes`
        self._dialled: dict[int, ClientConnection] = {}

    async def _process_request(self, connection: Any, request: Any) -> Any:
        try:
            upstream = await connect(self.upstream + request.path, open_timeout=5)
        except InvalidStatus as error:
            status = error.response.status_code
            self.refusals.append(status)
            body = error.response.body.decode(errors="replace") if error.response.body else ""
            return connection.respond(status, body)
        self._dialled[id(connection)] = upstream
        return None

    async def _handle(self, ws: ServerConnection) -> None:
        upstream = self._dialled.pop(id(ws))

        async def down() -> None:
            try:
                async for message in upstream:
                    if isinstance(message, str):
                        self.frames.append(json.loads(message))
                    await ws.send(message)
            except ConnectionClosed:
                pass
            rcvd = upstream.protocol.close_rcvd
            self.closes.append((rcvd.code, rcvd.reason) if rcvd else (None, None))
            self.closed_at.append(asyncio.get_running_loop().time())
            if rcvd is None:
                ws.transport.write_eof()  # no close frame: the client sees 1006
                await ws.wait_closed()
            else:
                await ws.close(rcvd.code, rcvd.reason)

        async def up() -> None:
            try:
                async for message in ws:
                    await upstream.send(message)
            except ConnectionClosed:
                pass
            await upstream.close()

        tasks = [asyncio.create_task(down()), asyncio.create_task(up())]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            await asyncio.wait(tasks, timeout=2)
        finally:
            for task in tasks:
                task.cancel()

    @asynccontextmanager
    async def serving(self) -> AsyncIterator[str]:
        try:
            async with serve(
                self._handle, "127.0.0.1", 0, process_request=self._process_request
            ) as server:
                yield f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}"
        finally:
            # An upstream dialled for a client whose own upgrade then failed: no handler
            # ran to close it.
            for upstream in self._dialled.values():
                await upstream.close()
            self._dialled.clear()
