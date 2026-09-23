"""A WebSocket connection that carries QTE contract envelopes.

    async with Connection("ws://127.0.0.1:8080/ws") as conn:
        await conn.send("subscribe", Subscribe(instruments=["AAPL"]))
        async for event in conn:
            ...

A connection is single-use: it does not authenticate, reconnect or resubscribe. Iteration
ends when the server closes the connection normally and raises
`websockets.exceptions.ConnectionClosedError` when it drops.
"""

import logging
import sys
from collections.abc import AsyncIterator, Iterator, MutableMapping
from dataclasses import dataclass
from typing import Any

from google.protobuf.json_format import ParseError
from google.protobuf.message import Message
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed
from websockets.frames import Close, Frame

from qte_sdk.contract import codec
from qte_sdk.contract.registry import CONTRACT_VERSION, INBOUND
from qte_sdk.contract.v1.common_pb2 import ReasonCodes


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
        # Pass the `extra` websockets attaches to each record through unchanged.
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


def _without_handshake_values(msg: Any, args: tuple[Any, ...]) -> tuple[Any, ...]:
    # The handshake trace logs the request path, headers and status phrase as plain text;
    # a query string, header value or phrase could carry a credential, so only names stay.
    if msg == "> GET %s HTTP/1.1" and args:
        return (str(args[0]).split("?", 1)[0], *args[1:])
    if msg in ("> %s: %s", "< %s: %s") and len(args) == 2:
        return (args[0], "<withheld>")
    if msg == "< HTTP/1.1 %d %s" and len(args) == 2:
        return (args[0], "<withheld>")
    return args


def _without_close_reasons(error: ConnectionClosed) -> ConnectionClosed:
    """The same close, with the reason text withheld: a close reason is free text from the
    server, and the client echoes it back, so it could carry the token either way."""

    def withheld(close: Close | None) -> Close | None:
        if close is None:
            return None
        return Close(close.code, "<withheld>" if close.reason else "")

    return type(error)(withheld(error.rcvd), withheld(error.sent), error.rcvd_then_sent)


def _exception_name(exc_info: Any) -> str:
    if isinstance(exc_info, BaseException):
        return type(exc_info).__name__
    if isinstance(exc_info, tuple) and exc_info and exc_info[0] is not None:
        return exc_info[0].__name__
    current = sys.exc_info()[0]
    return current.__name__ if current is not None else "error"


@dataclass(frozen=True)
class Received:
    """A message of a type this SDK knows, decoded into its generated class."""

    type: str
    message: Message
    seq: int | None


@dataclass(frozen=True)
class Unknown:
    """A message of a type this SDK does not know, for example from a newer contract."""

    type: str
    payload: dict[str, Any]
    seq: int | None


@dataclass(frozen=True)
class DecodeFailed:
    """A frame that could not be decoded. It is reported and never delivered as a message."""

    type: str | None
    error: Exception


@dataclass(frozen=True)
class SeqGap:
    """Server messages were missed or reordered, so any state built from them is uncertain."""

    expected: int
    received: int


Event = Received | Unknown | DecodeFailed | SeqGap


class SessionRejected(Exception):
    """The exchange rejected the session."""

    def __init__(self, reason_code: int, detail: str | None) -> None:
        try:
            name = ReasonCodes.ReasonCode.Name(reason_code)
        except ValueError:  # a code from a newer contract
            name = str(reason_code)
        super().__init__(f"{name}: {detail}" if detail else name)
        self.reason_code = reason_code
        self.detail = detail


class ContractVersionMismatch(SessionRejected):
    """The exchange does not serve the contract version this SDK sends.

    Raised whether the exchange reports it on `session_reject` or on an order `reject`:
    every message carries the same version, so the session cannot work either way.
    """


class Connection:
    def __init__(
        self, url: str, *, contract_version: str = CONTRACT_VERSION, **connect_options: Any
    ) -> None:
        self.url = url
        self.contract_version = contract_version
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
        try:
            self._ws = await connect(self.url, **self._connect_options)
        except ConnectionClosed as error:
            closed = _without_close_reasons(error)
        else:
            return
        raise closed

    async def close(self) -> None:
        if self._ws is not None:
            await self._ws.close()

    async def send(self, type_: str, payload: Message) -> None:
        text = codec.encode(self.contract_version, type_, payload)
        try:
            await self._open_ws().send(text)
        except ConnectionClosed as error:
            closed = _without_close_reasons(error)
        else:
            return
        # Raised outside the handler, so the original, which holds the reasons, is not chained.
        raise closed

    def __aiter__(self) -> AsyncIterator[Event]:
        return self.events()

    async def events(self) -> AsyncIterator[Event]:
        try:
            async for frame in self._open_ws():
                for event in self._handle(frame):
                    yield event
        except ConnectionClosed as error:
            closed = _without_close_reasons(error)
        else:
            return
        raise closed

    def _open_ws(self) -> ClientConnection:
        if self._ws is None:
            raise RuntimeError("connection is not open")
        return self._ws

    def _handle(self, frame: str | bytes) -> Iterator[Event]:
        if isinstance(frame, bytes):
            yield DecodeFailed(None, ValueError("binary frame; the wire is JSON text"))
            return
        try:
            decoded = codec.decode(frame)
        except (ValueError, ParseError) as error:
            yield DecodeFailed(None, error)
            return

        env = decoded.envelope
        seq = env.seq if env.HasField("seq") else None
        if seq is not None:
            # Tracked before the payload is decoded, so a bad payload still counts as received.
            if seq != self._expected_seq:
                yield SeqGap(self._expected_seq, seq)
            self._expected_seq = seq + 1

        cls = INBOUND.get(env.type)
        if cls is None:
            yield Unknown(env.type, decoded.payload, seq)
            return
        try:
            message = codec.unpack(decoded.payload, cls)
        except ParseError as error:
            yield DecodeFailed(env.type, error)
            return

        if env.type in ("session_reject", "reject"):
            detail = message.reason_detail if message.HasField("reason_detail") else None
            if message.reason_code == ReasonCodes.VERSION_MISMATCH:
                raise ContractVersionMismatch(message.reason_code, detail)
            if env.type == "session_reject":
                raise SessionRejected(message.reason_code, detail)
        yield Received(env.type, message, seq)
