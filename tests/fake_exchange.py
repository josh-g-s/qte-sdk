"""A local in-process WebSocket server for tests. Never binds anything but 127.0.0.1."""

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

from websockets.asyncio.server import ServerConnection, serve

CONTRACT_VERSION = "0.x"


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
