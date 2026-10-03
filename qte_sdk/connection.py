"""A WebSocket connection that carries QTE contract envelopes.

    async with Connection("ws://127.0.0.1:8080/ws") as conn:
        await conn.send("subscribe", Subscribe(instruments=["AAPL"]))
        async for event in conn:
            ...

A connection is single-use: it does not authenticate, reconnect or resubscribe. Iteration
ends when the server closes the connection normally and raises
`websockets.exceptions.ConnectionClosedError` when it drops.

Heartbeats: the exchange sends a `heartbeat` message at a regular interval, at any hour, so
that a live link is never silent for long. A connection absorbs them: they count for
sequence tracking and for liveness, and are never delivered as events. The client sends
nothing in return. Once the first heartbeat has arrived, if nothing at all arrives for
`liveness_timeout` seconds, the link is presumed dead: the connection is dropped and
iteration raises `LivenessTimeout`. Until then the check is off, so a connection to an
exchange that does not send heartbeats is never dropped for being quiet. The default,
`DEFAULT_LIVENESS_TIMEOUT`, is this SDK's choice, not a value the exchange sends; see its
description.

Staying alive: the exchange also sends WebSocket pings, and closes a connection that has
sent it no complete frame for a while, with close code `HEARTBEAT_TIMEOUT_CLOSE_CODE`
(4000) and reason `heartbeat timeout`. The client sends no messages of its own to stay
connected: in the background, the `websockets` library answers each ping with a pong, and
with its default `ping_interval` it also sends a ping of its own every 20 seconds. It can
only answer a ping, or see the pong to its own, while it is reading the socket, and it
pauses reading once more than `max_queue` frames (16 by default) are waiting for your
loop. If your loop falls that far behind and stays there, its own pings go unanswered and
it closes the connection after `ping_timeout` (close code 1011, reason `keepalive ping
timeout`). With `ping_interval=None` it sends no pings, the exchange's go unanswered, and
the exchange closes the connection with 4000 instead. Either way the error is a
`ConnectionClosedError`, which `ReconnectingSession` retries. Keep your loop reading
promptly, and do slow work elsewhere. You can raise `max_queue` (a `websockets` connect
option) to absorb bursts, at the cost of memory.

Term change: when one term ends and the next begins, the exchange closes every connection
with close code `TERM_CHANGE_CLOSE_CODE` (4001) and reason `term change`, so no connection
stays open across terms. Iteration raises a `ConnectionClosedError`, which
`ReconnectingSession` retries like any other drop. The new session's calendar names the
new term, in which report numbers start again (see `qte_sdk.reconnect`).

Report numbers: each of the team's private order reports that the exchange can replay
carries a `report_seq` on its envelope, which is the event's `report_seq` here (None on
every other message). A connection only reports it; `qte_sdk.session.Session` keeps the
count and uses it to resume.

Credentials: the SDK never itself writes the session token it holds into a log record, an
exception message, attribute or chain, or a traceback local variable that it creates or
lets escape. Text a server reflects back is withheld on these paths: close reasons, the
opening handshake and response headers, and redirects, which are not followed. Frame
traces are not logged, and log records carry only a snapshot of the connection's id and
address. A frame that fails to decode is reported without its text, since the parser's
error keeps it. Server-supplied protocol content (a rejection's reason_detail) is passed
through as-is, because it is what a caller needs to understand a failure; the QTE gateway
never echoes credentials. A token placed in the caller's own URL or headers is the
caller's configuration.
"""

import asyncio
import logging
import sys
from collections.abc import AsyncIterator, Iterator, MutableMapping
from dataclasses import dataclass, field
from typing import Any

from google.protobuf.message import Message
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, ConnectionClosedOK, InvalidHandshake
from websockets.frames import Close, Frame

from qte_sdk.contract import codec
from qte_sdk.contract.registry import CONTRACT_VERSION, INBOUND
from qte_sdk.contract.v1.common_pb2 import ReasonCodes
from qte_sdk.contract.v1.session_pb2 import ResumeAck, SessionAck

DEFAULT_LIVENESS_TIMEOUT = 45.0
"""Seconds with no message from the exchange after which a connection presumes the link
dead. This is a choice the SDK makes, not a value the exchange sends: it equals the silence
after which the exchange's documentation currently says a client should give up, which is
several of the exchange's heartbeat intervals, so a healthy link never stays quiet this
long. Pass `liveness_timeout` to `Connection` (or through `open_session` and
`ReconnectingSession`) to choose another, or None to turn the check off. The check
starts with the first heartbeat on a connection."""


@dataclass(frozen=True)
class _LoggedConnection:
    """What a log record may say about the connection it came from."""

    id: Any
    remote_address: Any

    @classmethod
    def of(cls, connection: Any) -> "_LoggedConnection":
        try:
            return cls(getattr(connection, "id", None), getattr(connection, "remote_address", None))
        except ReferenceError:
            return cls(None, None)


class _WithoutCredentials(logging.LoggerAdapter):
    """Keeps the session token out of every log record the websockets library writes.

    Any frame on this connection can carry the token, whole or in pieces (fragments,
    control frames, truncated traces), so frame traces are dropped outright rather than
    inspected. Exceptions are logged by type only, because their messages and tracebacks
    can hold frame data.
    """

    def process(
        self, msg: Any, kwargs: MutableMapping[str, Any]
    ) -> tuple[Any, MutableMapping[str, Any]]:
        # websockets attaches the live connection to each record, from which a handler could
        # reach close reasons and response headers; attach a snapshot of safe fields instead.
        extra = kwargs.get("extra")
        if extra and "websocket" in extra:
            kwargs["extra"] = {**extra, "websocket": _LoggedConnection.of(extra["websocket"])}
        return msg, kwargs

    def log(self, level: int, msg: Any, *args: Any, **kwargs: Any) -> None:
        if not self.isEnabledFor(level):
            return
        if any(isinstance(arg, Frame) for arg in args):
            return
        args = _without_handshake_values(msg, args)
        args = tuple(type(arg).__name__ if isinstance(arg, BaseException) else arg for arg in args)
        exc_info = kwargs.pop("exc_info", None)
        if exc_info:
            msg = f"{msg} ({_exception_name(exc_info)}; details withheld)"
        super().log(level, msg, *args, **kwargs)


# Response header names a server could not have filled with reflected text.
_KNOWN_RESPONSE_HEADERS = frozenset(
    name.lower()
    for name in (
        "Connection",
        "Upgrade",
        "Sec-WebSocket-Accept",
        "Sec-WebSocket-Extensions",
        "Sec-WebSocket-Protocol",
        "Date",
        "Server",
        "Content-Length",
        "Content-Type",
    )
)


def _without_handshake_values(msg: Any, args: tuple[Any, ...]) -> tuple[Any, ...]:
    # The handshake trace logs the request path, headers and status phrase as plain text;
    # a query string, header value or phrase could carry a credential, so only names stay.
    if msg == "> GET %s HTTP/1.1" and args:
        return (str(args[0]).split("?", 1)[0], *args[1:])
    if msg == "> %s: %s" and len(args) == 2:
        return (args[0], "<withheld>")
    if msg == "< %s: %s" and len(args) == 2:
        name = args[0] if str(args[0]).lower() in _KNOWN_RESPONSE_HEADERS else "<withheld>"
        return (name, "<withheld>")
    if msg == "< HTTP/1.1 %d %s" and len(args) == 2:
        return (args[0], "<withheld>")
    return args


HEARTBEAT_TIMEOUT_CLOSE_CODE = 4000
"""The close code the exchange uses when it has had no complete frame from the client for
too long, or when a connection has not authenticated in time. Its reason text is
`heartbeat timeout`."""

TERM_CHANGE_CLOSE_CODE = 4001
"""The close code the exchange uses on every connection when one term ends and the next
begins. Its reason text is `term change`. Reconnect: the new session's calendar names the
new term, in which report numbers start again."""

# Close reasons whose exact text is fixed by the contract or by `websockets` itself, so
# they cannot carry the token and are kept. Any other reason is withheld.
_KNOWN_CLOSE_REASONS = frozenset({"heartbeat timeout", "term change", "keepalive ping timeout"})


def _without_close_reasons(error: ConnectionClosed) -> ConnectionClosed:
    """The same close, with the reason text withheld: a close reason is free text from the
    server, and the client echoes it back, so it could carry the token either way. Only
    reasons in `_KNOWN_CLOSE_REASONS`, matched exactly, are kept."""

    def withheld(close: Close | None) -> Close | None:
        if close is None:
            return None
        if close.reason in _KNOWN_CLOSE_REASONS:
            return Close(close.code, close.reason)
        return Close(close.code, "<withheld>" if close.reason else "")

    return type(error)(withheld(error.rcvd), withheld(error.sent), error.rcvd_then_sent)


def _exception_name(exc_info: Any) -> str:
    if isinstance(exc_info, BaseException):
        return type(exc_info).__name__
    if isinstance(exc_info, tuple) and exc_info and exc_info[0] is not None:
        return exc_info[0].__name__
    current = sys.exc_info()[0]
    return current.__name__ if current is not None else "error"


def _decode_error(error: Exception) -> ValueError:
    """A replacement for `error` naming only its type. The parsers keep the input they
    rejected, in the message, in attributes such as a `JSONDecodeError`'s `doc` or a
    `UnicodeDecodeError`'s `object`, and in their frames, and that input may hold the token
    in a form no text search would find (escaped, or in another encoding)."""
    return ValueError(f"{type(error).__name__}; details withheld")


@dataclass(frozen=True)
class Received:
    """A message of a type this SDK knows, decoded into its generated class.

    `payload` is the JSON payload as received. It is not compared, so two events with the
    same type, message and seq are equal however they were built.
    """

    type: str
    message: Message
    seq: int | None
    payload: dict[str, Any] | None = field(default=None, compare=False, repr=False)
    # The envelope's report_seq: set only on the team's private reports that a resume can
    # replay, None on everything else.
    report_seq: int | None = None

    def unknown_enum_names(self) -> dict[str, str]:
        """Enum names this SDK does not know, keyed by field path.

        Such a name, for example a reason code from a newer contract, decodes to the
        field's zero value (`..._UNSPECIFIED`); this returns the name the exchange sent.
        Empty when every name is known or when the event carries no payload.
        """
        if self.payload is None:
            return {}
        return codec.unknown_enum_names(self.payload, type(self.message))


@dataclass(frozen=True)
class Unknown:
    """A message of a type this SDK does not know, for example from a newer contract."""

    type: str
    payload: dict[str, Any]
    seq: int | None
    report_seq: int | None = None


@dataclass(frozen=True)
class DecodeFailed:
    """A frame that could not be decoded. It is reported and never delivered as a message.

    `error` is a `ValueError`. For a binary frame it says the wire is JSON text. Otherwise
    its message names the type of what decoding raised, for example `JSONDecodeError` for
    text that is not JSON, `ParseError` for a payload of the wrong shape, `RecursionError`
    for JSON nested too deeply or `OverflowError` for a number out of range, followed by
    "; details withheld". The parser's own error is not kept, since it holds the frame,
    which could hold the token.
    """

    type: str | None
    error: Exception
    # The envelope's report_seq, when the envelope could be read and carried one.
    report_seq: int | None = None


class DataUncertain:
    """Base of the events that mean messages may have been missed, so any state built from
    earlier messages is uncertain: `SeqGap` and `Disconnected`.

    Test for this class to handle every such event, including ones added later.
    """

    __slots__ = ()


@dataclass(frozen=True)
class SeqGap(DataUncertain):
    """Server messages were missed or reordered, so any state built from them is uncertain."""

    expected: int
    received: int


@dataclass(frozen=True)
class Disconnected(DataUncertain):
    """The data-uncertainty event: the session ended and events may have been missed.

    Market data sent while disconnected is not recovered: the next session subscribes
    again. The team's private order reports (fills, cancels, order states and the others a
    resume replays) are recovered when the next session resumes, as `ReconnectingSession`
    does by default; without a resume they are not recovered either. `error` is why the
    connection ended, or None when the exchange closed it normally. A `Connection` never
    yields this itself; `qte_sdk.reconnect.ReconnectingSession` does.
    """

    error: Exception | None


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
class Connected:
    """A session is up: authenticated, acknowledged, resumed, and subscribed to
    `instruments`.

    `reconnected` is False for the first session and True for every later one. `resume` is
    the exchange's `resume_ack`, or None when resuming is turned off or the exchange
    refused it. When it is set, the replay or snapshot it announces follows, ending with a
    `ResumeComplete`; market data sent meanwhile is lost (see `Disconnected`). A
    `Connection` never yields this itself; `qte_sdk.reconnect.ReconnectingSession` does.
    """

    info: SessionInfo
    instruments: tuple[str, ...]
    reconnected: bool
    resume: ResumeAck | None = None


@dataclass(frozen=True)
class ReportGap(DataUncertain):
    """Private order reports were missed: the next report number expected was `expected`
    but `received` arrived. That report is delivered after this event.

    Any state built from order reports, such as a `RestingOrders` view, is then uncertain.
    The next resume (see `qte_sdk.session.Session.resume`) replays the missing reports if
    the exchange still holds them. A `Connection` never yields this itself; a `Session`
    does.
    """

    expected: int
    received: int


@dataclass(frozen=True)
class ResumeComplete:
    """A resume has caught up: every private report up to `as_of_report_seq` has been
    delivered, either replayed (`replayed` True) or summed up by the `snapshot_count`
    `order_snapshot` events before this one (`replayed` False). Later reports follow as
    they arrive.

    A `Connection` never yields this itself; a `Session` does, after `Session.resume`.
    """

    replayed: bool
    as_of_report_seq: int
    snapshot_count: int


Event = Received | Unknown | DecodeFailed | SeqGap | ReportGap | ResumeComplete


class SessionRejected(Exception):
    """The exchange rejected the session.

    `reason_name` is the reason as a name. A name from a newer contract that this SDK
    does not know is kept there, while `reason_code` is `REASON_CODE_UNSPECIFIED`.
    """

    def __init__(
        self, reason_code: int, detail: str | None, *, reason_name: str | None = None
    ) -> None:
        if reason_name is None:
            try:
                reason_name = ReasonCodes.ReasonCode.Name(reason_code)
            except ValueError:  # a code from a newer contract
                reason_name = str(reason_code)
        super().__init__(f"{reason_name}: {detail}" if detail else reason_name)
        self.reason_code = reason_code
        self.detail = detail
        self.reason_name = reason_name


class ContractVersionMismatch(SessionRejected):
    """The exchange does not serve the contract version this SDK sends.

    Raised whether the exchange reports it on `session_reject` or on an order `reject`:
    every message carries the same version, so the session cannot work either way.
    """


class LivenessTimeout(TimeoutError):
    """Nothing arrived from the exchange for `timeout` seconds, so the link was presumed
    dead and dropped. A new connection may succeed."""

    def __init__(self, timeout: float) -> None:
        super().__init__(f"no message from the exchange for {timeout} s; the link is presumed dead")
        self.timeout = timeout


class _connect(connect):
    """`connect` that never follows a redirect: the Location header is server text, and a
    redirect is reported as a failed handshake instead."""

    def process_redirect(self, exc: Exception) -> Exception | str:
        return exc


class HandshakeFailed(InvalidHandshake):
    """The opening handshake failed. Only the kind of failure and the HTTP status are kept:
    the library's own error carries header values and the response, which a server could
    fill with reflected text."""

    def __init__(self, kind: str, status_code: int | None) -> None:
        super().__init__(kind, status_code)
        self.kind = kind
        self.status_code = status_code

    def __str__(self) -> str:
        status = f", HTTP {self.status_code}" if self.status_code is not None else ""
        return f"opening handshake failed ({self.kind}{status}); details withheld"


class Connection:
    def __init__(
        self,
        url: str,
        *,
        contract_version: str = CONTRACT_VERSION,
        liveness_timeout: float | None = DEFAULT_LIVENESS_TIMEOUT,
        **connect_options: Any,
    ) -> None:
        if liveness_timeout is not None and not liveness_timeout > 0:
            raise ValueError("liveness_timeout must be a positive number of seconds, or None")
        self.url = url
        self.contract_version = contract_version
        self.liveness_timeout = liveness_timeout
        # The liveness check starts once the exchange has shown it sends heartbeats.
        self._heartbeats = False
        # Any logger the caller passes is wrapped too, so no route logs the token.
        logger = connect_options.pop("logger", None) or logging.getLogger("websockets.client")
        if isinstance(logger, str):
            logger = logging.getLogger(logger)
        self._connect_options = {**connect_options, "logger": _WithoutCredentials(logger, {})}
        self._ws: ClientConnection | None = None
        self._used = False
        self._expected_seq = 1

    async def __aenter__(self) -> "Connection":
        await self.open()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    async def open(self) -> None:
        # Claimed before the await, so a second concurrent open() cannot also connect.
        if self._used:
            raise RuntimeError("a Connection is single-use; create a new one to reconnect")
        self._used = True
        failure: Exception
        try:
            self._ws = await _connect(self.url, **self._connect_options)
        except ConnectionClosed as error:
            failure = _without_close_reasons(error)
        except InvalidHandshake as error:
            status = getattr(getattr(error, "response", None), "status_code", None)
            failure = HandshakeFailed(type(error).__name__, status)
        except BaseException as error:
            # A timeout or cancellation keeps its type, but not the handshake frames below
            # this one or the chain: they hold the request headers.
            failure = error.with_traceback(None)
            failure.__cause__ = failure.__context__ = None
        else:
            return
        raise failure

    async def close(self) -> None:
        """Close the connection, waiting at most `close_timeout` seconds for the exchange to
        complete the closing handshake.

        `close_timeout` is the `websockets` connect option (default 10 seconds, None for no
        limit), passed to `Connection` like any other. It also bounds sending the close
        frame, which `websockets` alone does not: a peer that stops reading would otherwise
        keep `close()` waiting for its buffer to drain. When the time is up the socket is
        dropped without the handshake. Either way, `close()` returns normally.
        """
        ws = self._ws
        if ws is None:
            return
        try:
            async with asyncio.timeout(ws.close_timeout):
                await ws.close()
        except TimeoutError:
            # Only our own deadline raises this: websockets handles its own close timeout.
            ws.transport.abort()
            # abort() makes the transport report the connection lost, so this is prompt.
            await ws.wait_closed()

    async def send(self, type_: str, payload: Message) -> None:
        failure: BaseException
        try:
            await self._open_ws().send(codec.encode(self.contract_version, type_, payload))
        except ConnectionClosed as error:
            failure = _without_close_reasons(error)
        except BaseException as error:
            # Keep the type, but not the frames below this one or the chain: they hold the
            # encoded message, which may be `auth`.
            failure = error.with_traceback(None)
            failure.__cause__ = failure.__context__ = None
        else:
            return
        # Nor this frame's own copy of the message. Raised outside the handler, so the
        # original error is not chained to it.
        del payload
        raise failure

    def __aiter__(self) -> AsyncIterator[Event]:
        return self.events()

    async def events(self) -> AsyncIterator[Event]:
        frame: str | bytes | None = None
        event: Event | None = None
        failure: BaseException
        try:
            ws = self._open_ws()
            while True:
                try:
                    frame = await self._receive(ws)
                except ConnectionClosedOK:
                    break
                for event in self._handle(frame):
                    yield event
        except GeneratorExit:
            raise
        except ConnectionClosed as error:
            failure = _without_close_reasons(error)
        except BaseException as error:
            # Keep the type, as open() and send() do, but not the frames below or the chain.
            failure = error.with_traceback(None)
            failure.__cause__ = failure.__context__ = None
        else:
            return
        frame = event = None
        raise failure

    async def _receive(self, ws: ClientConnection) -> str | bytes:
        """The next frame, or `LivenessTimeout` once nothing has arrived for
        `liveness_timeout` seconds, after the first heartbeat. Any frame restarts the
        clock, a heartbeat or one that cannot be decoded included."""
        if self.liveness_timeout is None or not self._heartbeats:
            return await ws.recv()
        deadline = asyncio.timeout(self.liveness_timeout)
        try:
            async with deadline:
                return await ws.recv()
        except TimeoutError:
            if not deadline.expired():
                raise
        # The link is presumed dead, so there is no closing handshake to wait for.
        ws.transport.abort()
        raise LivenessTimeout(self.liveness_timeout)

    def _open_ws(self) -> ClientConnection:
        if self._ws is None:
            raise RuntimeError("connection is not open")
        return self._ws

    def _handle(self, frame: str | bytes) -> Iterator[Event]:
        # Any failure to decode one frame is reported for that frame, and delivery goes on:
        # besides ValueError and ParseError, the parsers raise RecursionError for JSON nested
        # too deeply and OverflowError for a number out of range. Only Exception is caught,
        # so KeyboardInterrupt, SystemExit, cancellation and GeneratorExit still propagate.
        if isinstance(frame, bytes):
            yield DecodeFailed(None, ValueError("binary frame; the wire is JSON text"))
            return
        failure: Exception | None = None
        try:
            decoded = codec.decode(frame)
        except Exception as error:
            failure = _decode_error(error)
        if failure is not None:
            yield DecodeFailed(None, failure)
            return

        env = decoded.envelope
        seq = env.seq if env.HasField("seq") else None
        if seq is not None:
            # Tracked before the payload is decoded, so a bad payload still counts as received.
            if seq != self._expected_seq:
                yield SeqGap(self._expected_seq, seq)
            self._expected_seq = seq + 1

        if env.type == "heartbeat":
            # Absorbed: it has counted for sequence tracking, and for liveness on arrival.
            self._heartbeats = True
            return
        report_seq = env.report_seq if env.HasField("report_seq") else None

        cls = INBOUND.get(env.type)
        if cls is None:
            yield Unknown(env.type, decoded.payload, seq, report_seq)
            return
        try:
            message = codec.unpack(decoded.payload, cls)
        except Exception as error:
            failure = _decode_error(error)
        if failure is not None:
            yield DecodeFailed(env.type, failure, report_seq)
            return

        event = Received(env.type, message, seq, decoded.payload, report_seq)
        if env.type in ("session_reject", "reject"):
            detail = message.reason_detail if message.HasField("reason_detail") else None
            if message.reason_code == ReasonCodes.VERSION_MISMATCH:
                raise ContractVersionMismatch(message.reason_code, detail)
            if env.type == "session_reject":
                name = event.unknown_enum_names().get("reason_code")
                raise SessionRejected(message.reason_code, detail, reason_name=name)
        yield event
