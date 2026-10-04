"""A local in-process WebSocket server for tests. Never binds anything but 127.0.0.1."""

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

import pytest
from websockets.asyncio.server import ServerConnection, serve

CONTRACT_VERSION = "0.x"


class LoopClock:
    """The running event loop's clock, which a test moves forward to make a timeout expire.

    asyncio counts every timeout and sleep by the loop's clock. A test that waits until the
    fake exchange has seen what it needs to, then calls `advance`, makes a timeout expire at
    that point however busy the machine is, where a short real timeout would race the
    exchange. Between advances the clock keeps pace with real time, so a test's own limits,
    such as `asyncio.wait_for(..., 5)`, still work. An advance brings every pending timer
    forward, those limits included, so advance only while none of them is pending.
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        loop = asyncio.get_running_loop()
        self._real = loop.time
        self._offset = 0.0
        monkeypatch.setattr(loop, "time", self.time)

    def time(self) -> float:
        return self._real() + self._offset

    def advance(self, seconds: float) -> None:
        """Move the clock `seconds` forward: whatever is due by then runs on the loop's
        next iteration."""
        self._offset += seconds


async def wait_until(task: "asyncio.Future[Any]", *events: asyncio.Event) -> None:
    """Wait until every one of `events` is set, or until `task` is done if that comes first.

    For a task bounded by a timeout of its own, so no other limit is needed. A task that
    ends first, with an error say, raises it when the test awaits it, rather than leaving
    the test waiting for the fake exchange in vain."""
    waits = [asyncio.ensure_future(event.wait()) for event in events]
    try:
        while not task.done() and not all(waiting.done() for waiting in waits):
            pending = {task, *(waiting for waiting in waits if not waiting.done())}
            await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for waiting in waits:
            waiting.cancel()


def frame(type_: str, payload: Any, seq: int | None = None, **extra: Any) -> str:
    """One server-to-client envelope as JSON text."""
    env: dict[str, Any] = {"version": CONTRACT_VERSION, "type": type_, "payload": payload, **extra}
    if seq is not None:
        env["seq"] = seq
    return json.dumps(env)


@asynccontextmanager
async def serve_local(handler: Callable[[ServerConnection], Awaitable[None]]) -> AsyncIterator[str]:
    """Run `handler` for each connection; yields the ws:// URL to connect to."""
    async with serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        yield f"ws://127.0.0.1:{port}"


@asynccontextmanager
async def exchange(frames: list[str | bytes], inbox: list[str] | None = None) -> AsyncIterator[str]:
    """A scripted server: records one client message (if `inbox` is given), sends `frames`,
    then closes normally."""

    async def handler(ws: ServerConnection) -> None:
        if inbox is not None:
            inbox.append(await ws.recv())
        for f in frames:
            await ws.send(f)
        await ws.close()

    async with serve_local(handler) as url:
        yield url


@asynccontextmanager
async def silent_server() -> AsyncIterator[str]:
    """Accepts TCP connections and never answers the opening handshake."""
    held: list[asyncio.StreamWriter] = []

    async def hold(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        held.append(writer)

    server = await asyncio.start_server(hold, "127.0.0.1", 0)
    try:
        yield f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}"
    finally:
        for writer in held:
            writer.close()
        server.close()
        await server.wait_closed()
