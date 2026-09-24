"""Open an authenticated session on the exchange.

    session = await open_session("ws://127.0.0.1:8080/ws")  # token from QTE_TOKEN
    async with session:
        print(session.info.team, session.info.unscored)
        async for event in session:
            ...

A session is a `Connection` that has sent `auth` with your account's token and received
`session_ack`. The token comes from the `token` argument or, failing that, the `QTE_TOKEN`
environment variable. Keep it out of source files and out of the repository.

The token is sent once, in the `auth` message, and is not kept afterwards. The SDK never
logs it or puts it in an exception: the connection drops the frame-level debug lines of
the `websockets` library, which would show the `auth` message (see `qte_sdk.connection`). While
the session opens, a message from the exchange that repeats the token is also kept out of
the exception raised; once the session is open a `Session` no longer holds the token, and
what the exchange sends is passed on as it arrives. A `qte_sdk.reconnect.ReconnectingSession`
keeps it, in a wrapper no repr shows, to authenticate each new session.
"""

import asyncio
import os
from collections import deque
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from google.protobuf.message import Message
from websockets.exceptions import ConnectionClosed

from qte_sdk.connection import (
    Connection,
    DecodeFailed,
    Event,
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


class AuthNotSent(SessionNotAcknowledged):
    """The `auth` message could not be encoded or sent, for a reason other than the
    connection closing, such as a bad `contract_version`. Trying again fails the same way."""


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


class Session:
    """An acknowledged session: the open connection and what the exchange said about it.

    Send on the session and iterate it, rather than its connection. Iterating the session
    delivers every event: anything the exchange sent before `session_ack` first, then the
    rest of the stream. A session is a `qte_sdk.orders.Sender`, so the functions in
    `qte_sdk.orders` and `qte_sdk.market_data` accept it.
    """

    def __init__(self, connection: Connection, info: SessionInfo, early: list[Event]) -> None:
        self.connection = connection
        self.info = info
        self._early = deque(early)

    def __repr__(self) -> str:
        return f"Session({self.connection.url!r}, {self.info!r})"

    def __aiter__(self) -> AsyncIterator[Event]:
        return self.events()

    async def events(self) -> AsyncIterator[Event]:
        while self._early:
            yield self._early.popleft()
        async for event in self.connection:
            yield event

    async def send(self, type_: str, payload: Message) -> None:
        """Send one message on this session's connection, as `Connection.send` does."""
        failure: BaseException
        try:
            await self.connection.send(type_, payload)
        except BaseException as error:
            failure = error
        else:
            return
        # The connection keeps the message out of its traceback; so does this frame, since
        # the message may be `auth`. Raised outside the handler, so nothing is chained.
        del payload
        raise failure

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

    `ack_timeout` bounds the whole opening sequence: connecting, sending `auth` and waiting
    for `session_ack` must all finish within that many seconds of the call (None for no
    limit). The `websockets` option `open_timeout` still bounds the opening handshake on its
    own, and `close_timeout` bounds closing the connection (see `Connection.close`).

    Raises `MissingToken` before connecting if there is no token. If the exchange refuses
    the session, raises `SessionRejected` carrying the contract reason code, or
    `ContractVersionMismatch` when the exchange does not serve this contract version.
    Raises `SessionNotAcknowledged` if the connection ends first, and `TimeoutError` if the
    session is not acknowledged within `ack_timeout` seconds. The connection is closed
    whenever no session is returned, which can take up to `close_timeout` seconds more.
    """
    secret = _Secret(resolve_token(token))
    del token

    # Connection keeps the token out of the websockets log itself, for any logger passed.
    conn = Connection(url, **connection_options)
    interrupted = False
    deadline = asyncio.timeout(ack_timeout)
    try:
        async with deadline:
            await conn.open()
            await _send_auth(conn, secret)
            ack, early = await _wait_for_ack(conn)
    except BaseException as error:
        interrupted = await _finish_closing(conn)
        if isinstance(error, TimeoutError) and deadline.expired():
            # A fresh error, not the one asyncio chained to the cancelled step.
            safe = TimeoutError(f"the session was not acknowledged within {ack_timeout} s")
        else:
            safe = _without_token(error, secret)
        if safe is None and not interrupted:
            raise
    else:
        return Session(conn, SessionInfo.from_ack(ack), early)
    # Raised outside the handler, so the original error, which may mention the token, is
    # not chained to it.
    if interrupted:
        raise asyncio.CancelledError
    assert safe is not None
    raise safe


async def _finish_closing(conn: Connection) -> bool:
    """Close `conn` and wait until it is closed, even if cancelled meanwhile, so no socket
    is left half closed. Returns True if a cancellation arrived, for the caller to raise."""
    return await _wait_out(asyncio.ensure_future(conn.close()))


async def _wait_out(task: "asyncio.Future[Any]") -> bool:
    """Wait for `task` to finish, even through cancellation. Returns True if a cancellation
    arrived meanwhile, for the caller to raise once the task is done."""
    interrupted = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            interrupted = True
        except Exception:
            break
    if not task.cancelled():
        task.exception()  # retrieved, so a failure is not reported as unread
    return interrupted


async def _send_auth(conn: Connection, secret: "_Secret") -> None:
    try:
        await conn.send("auth", Auth(token=secret.value))
        return
    except Exception as error:
        kind = SessionNotAcknowledged if isinstance(error, ConnectionClosed) else AuthNotSent
        replacement: BaseException = kind(
            _redact(f"could not send auth: {type(error).__name__}: {error}", secret)
        )
    except BaseException as error:
        # Cancellation and interrupts keep their type, so they behave as they otherwise would.
        replacement = type(error)(*error.args)
    # Raised outside the handler: the frames of the interrupted send hold the auth message,
    # so the original error and its traceback are not chained to this one.
    raise replacement


async def _wait_for_ack(conn: Connection) -> tuple[SessionAck, list[Event]]:
    early: list[Event] = []
    events = conn.events()
    close_code: int | None = None
    try:
        async for event in events:
            if isinstance(event, Received):
                if event.type == "session_ack":
                    assert isinstance(event.message, SessionAck)
                    return event.message, early
                if event.type == "reject":
                    # Before the acknowledgement, the only request in flight is `auth`.
                    message: Any = event.message
                    detail = message.reason_detail if message.HasField("reason_detail") else None
                    name = event.unknown_enum_names().get("reason_code")
                    raise SessionRejected(message.reason_code, detail, reason_name=name)
            elif isinstance(event, DecodeFailed) and event.type == "session_ack":
                # Not chained: the decoder's frames hold the raw payload in their locals.
                raise SessionNotAcknowledged(f"session_ack could not be decoded: {event.error}")
            early.append(event)
    except ConnectionClosed as error:
        # Only the code is kept: the close reason is server text and could echo the token.
        close_code = error.rcvd.code if error.rcvd is not None else None
    finally:
        await events.aclose()
    detail = f" (close code {close_code})" if close_code is not None else ""
    raise SessionNotAcknowledged(f"the connection closed before session_ack{detail}")


class _Secret:
    """Holds the token so that no repr, and so no traceback that shows locals, reveals it."""

    __slots__ = ("value",)

    def __init__(self, value: str) -> None:
        self.value = value

    def __repr__(self) -> str:
        return "<token withheld>"

    __str__ = __repr__


def _redact(text: str, secret: _Secret) -> str:
    return text.replace(secret.value, repr(secret))


def _without_token(error: BaseException, secret: _Secret) -> BaseException | None:
    """None if `error` and its chain never mention the token, else a replacement that does not.

    The exchange is not expected to echo a token back, but if a message from it did, it
    would otherwise reach an exception message.
    """
    seen: set[int] = set()
    link: BaseException | None = error
    while link is not None and id(link) not in seen:
        seen.add(id(link))
        if secret.value in str(link) or secret.value in repr(link):
            break
        link = link.__cause__ or link.__context__
    else:
        return None
    if isinstance(error, SessionRejected):
        detail = None if error.detail is None else _redact(error.detail, secret)
        name = _redact(error.reason_name, secret)
        return type(error)(error.reason_code, detail, reason_name=name)
    if isinstance(error, SessionNotAcknowledged):
        return type(error)(_redact(str(error), secret))
    return SessionNotAcknowledged(_redact(f"{type(error).__name__}: {error}", secret))
