"""A WebSocket connection that carries QTE contract envelopes.

    async with Connection("ws://127.0.0.1:8080/ws") as conn:
        await conn.send("subscribe", Subscribe(instruments=["AAPL"]))
        async for event in conn:
            ...

A connection is single-use: it does not authenticate, reconnect or resubscribe. Iteration
ends when the server closes the connection normally and raises
`websockets.exceptions.ConnectionClosedError` when it drops.
"""

from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from typing import Any

from google.protobuf.json_format import ParseError
from google.protobuf.message import Message
from websockets.asyncio.client import ClientConnection, connect

from qte_sdk.contract import codec
from qte_sdk.contract.registry import CONTRACT_VERSION, INBOUND
from qte_sdk.contract.v1.common_pb2 import ReasonCodes


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


class Connection:
    def __init__(
        self, url: str, *, contract_version: str = CONTRACT_VERSION, **connect_options: Any
    ) -> None:
        self.url = url
        self.contract_version = contract_version
        self._connect_options = connect_options
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
        self._ws = await connect(self.url, **self._connect_options)

    async def close(self) -> None:
        if self._ws is not None:
            await self._ws.close()

    async def send(self, type_: str, payload: Message) -> None:
        await self._open_ws().send(codec.encode(self.contract_version, type_, payload))

    def __aiter__(self) -> AsyncIterator[Event]:
        return self.events()

    async def events(self) -> AsyncIterator[Event]:
        async for frame in self._open_ws():
            for event in self._handle(frame):
                yield event

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

        event = Received(env.type, message, seq, decoded.payload)
        if env.type in ("session_reject", "reject"):
            detail = message.reason_detail if message.HasField("reason_detail") else None
            if message.reason_code == ReasonCodes.VERSION_MISMATCH:
                raise ContractVersionMismatch(message.reason_code, detail)
            if env.type == "session_reject":
                name = event.unknown_enum_names().get("reason_code")
                raise SessionRejected(message.reason_code, detail, reason_name=name)
        yield event
