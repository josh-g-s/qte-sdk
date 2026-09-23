"""Open an authenticated session on the exchange.

    session = await open_session("ws://127.0.0.1:8080/ws")  # token from QTE_TOKEN
    async with session:
        print(session.info.team, session.info.unscored)
        async for event in session.connection:
            ...

A session is a `Connection` that has sent `auth` with your account's token and received
`session_ack`. The token comes from the `token` argument or, failing that, the `QTE_TOKEN`
environment variable. Keep it out of source files and out of the repository.

The token is sent once, in the `auth` message, and is not kept afterwards. The SDK never
logs it: the frame-level debug lines of the `websockets` library, which would show the
`auth` message, are dropped for session connections.
"""

import asyncio
import logging
import os
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

from qte_sdk.connection import (
    Connection,
    DecodeFailed,
    Received,
    SessionRejected,
)
from qte_sdk.contract.v1.session_pb2 import Auth, SessionAck

TOKEN_ENV_VAR = "QTE_TOKEN"
DEFAULT_ACK_TIMEOUT = 10.0


class MissingToken(ValueError):
    """No token was given and the `QTE_TOKEN` environment variable is unset or empty."""


class SessionNotAcknowledged(Exception):
    """The connection ended, or the acknowledgement could not be read, before the session
    was acknowledged."""


@dataclass(frozen=True)
class SessionInfo:
    """The exchange's acknowledgement of a session, field for field as `session_ack` carries it."""

    session_id: str
    team: str
    server_time: int
    contract_version: str
    unscored: bool

    @classmethod
    def from_ack(cls, ack: SessionAck) -> "SessionInfo":
        return cls(
            session_id=ack.session_id,
            team=ack.team,
            server_time=ack.server_time,
            contract_version=ack.contract_version,
            unscored=ack.unscored,
        )


@dataclass(frozen=True)
class Session:
    """An acknowledged session: the open connection and what the exchange said about it."""

    connection: Connection
    info: SessionInfo

    async def close(self) -> None:
        await self.connection.close()

    async def __aenter__(self) -> "Session":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()


def resolve_token(token: str | None = None) -> str:
    """The token to authenticate with: `token` if given, else `QTE_TOKEN`.

    Raises `MissingToken` if neither is set to a non-empty value.
    """
    if token is None:
        token = os.environ.get(TOKEN_ENV_VAR)
    if not token:
        raise MissingToken(f"no token: pass token= or set the {TOKEN_ENV_VAR} environment variable")
    return token


async def open_session(
    url: str,
    token: str | None = None,
    *,
    ack_timeout: float | None = DEFAULT_ACK_TIMEOUT,
    **connection_options: Any,
) -> Session:
    """Connect to `url`, authenticate, and wait for the exchange to acknowledge the session.

    `connection_options` are passed to `Connection` (for example `contract_version`) and on
    to `websockets.asyncio.client.connect`.

    Raises `MissingToken` before connecting if there is no token. If the exchange refuses
    the session, raises `SessionRejected` carrying the contract reason code, or
    `ContractVersionMismatch` when the exchange does not serve this contract version.
    Raises `SessionNotAcknowledged` if the connection ends first, and `TimeoutError` if no
    acknowledgement arrives within `ack_timeout` seconds. The connection is closed whenever
    no session is returned.

    Anything else the exchange sends before `session_ack` is discarded.
    """
    secret = resolve_token(token)
    del token

    base_logger = connection_options.pop("logger", None) or logging.getLogger("websockets.client")
    conn = Connection(url, logger=_WithoutFrames(base_logger), **connection_options)
    try:
        await conn.open()
        await conn.send("auth", Auth(token=secret))
        del secret
        ack = await _wait_for_ack(conn, ack_timeout)
    except BaseException:
        with suppress(Exception):
            await conn.close()
        raise
    return Session(conn, SessionInfo.from_ack(ack))


async def _wait_for_ack(conn: Connection, timeout: float | None) -> SessionAck:
    events = conn.events()
    try:
        async with asyncio.timeout(timeout):
            async for event in events:
                if isinstance(event, Received):
                    if event.type == "session_ack":
                        assert isinstance(event.message, SessionAck)
                        return event.message
                    if event.type == "reject":
                        # Before the acknowledgement, the only request in flight is `auth`.
                        message: Any = event.message
                        detail = (
                            message.reason_detail if message.HasField("reason_detail") else None
                        )
                        raise SessionRejected(message.reason_code, detail)
                elif isinstance(event, DecodeFailed) and event.type == "session_ack":
                    raise SessionNotAcknowledged(
                        "session_ack could not be decoded"
                    ) from event.error
    finally:
        await events.aclose()
    raise SessionNotAcknowledged("the connection closed before session_ack")


# The `websockets` library logs every frame at DEBUG, in lines that start "> " (sent) or
# "< " (received), and the HTTP handshake the same way. A sent frame here can be the `auth`
# message, so those lines are dropped; other records (state changes, errors) pass through.
_WIRE_PREFIXES = ("> ", "< ")


class _WithoutFrames(logging.LoggerAdapter):
    def __init__(self, logger: logging.Logger | logging.LoggerAdapter) -> None:
        super().__init__(logger, {})

    def log(self, level: int, msg: object, *args: object, **kwargs: Any) -> None:
        if isinstance(msg, str) and msg.startswith(_WIRE_PREFIXES):
            return
        super().log(level, msg, *args, **kwargs)
