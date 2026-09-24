"""A session that reconnects on its own, with a fresh session each time.

    session = ReconnectingSession("ws://127.0.0.1:8080/ws", instruments=["AAPL"])
    async with session:
        async for event in session:
            match event:
                case Connected():
                    ...  # a session is up: authenticated, and subscribed again
                case Disconnected():
                    ...  # data uncertainty: anything may have happened meanwhile
                case Retrying():
                    ...  # waiting `event.delay` seconds before the next attempt
                case _:
                    ...  # a connection event, as `Session` delivers it

The exchange does not yet offer a way to resume a session, so a dropped connection is not
resumed: the next connection is a brand-new session. Fills, order events and market data
sent while the connection was down are not recovered, and nothing tells you what they
were. Until the exchange can resume a session, a reconnect cannot fill that gap.

What happens on a disconnect, in this order:

1. The `RestingOrders` view passed as `resting`, if any, is marked incomplete, and a
   `Disconnected` event is delivered. Treat it as the moment your data became uncertain:
   positions, resting orders and the book may all have changed.
2. After a backoff delay (see `Backoff`), a new connection is opened. Its sequence tracking
   starts afresh, so the new session's numbering is not reported as a gap.
3. The new connection authenticates with the same token and, once the exchange acknowledges
   the session, subscribes again to every instrument this session is subscribed to.
4. A `Connected` event is delivered, with `reconnected=True`.

Nothing sent before a disconnect is sent again. An order in flight when the connection
dropped may or may not have reached the exchange, and the SDK never repeats it. While no
session is up, `send` raises `NotConnected` rather than queueing the message. The resting
view stays incomplete after a reconnect: no event yet reports the orders already resting
when a session starts.

Which failures are retried (`is_retryable`): a dropped or closed connection, a connection
that could not be opened (`OSError` or a timeout), a handshake answered with a malformed
response or with HTTP 5xx, 408 or 429, and a session that closed before it was
acknowledged. Anything else stops the session and is raised from the iteration, because
trying again would give the same answer: every `SessionRejected` (for example a token the
exchange does not accept), including `ContractVersionMismatch`; a certificate that failed
verification; a handshake refused with any other status, which usually means a wrong URL,
or whose negotiation failed; and any error in the SDK or your own code.

The token is resolved once, when the session is created, and kept for re-authentication in
a wrapper that no repr, str or traceback shows. It is sent only in `auth` messages. An
error this module delivers or raises that would repeat the token has it replaced first.
"""

import asyncio
import random
import ssl
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

from google.protobuf.message import Message
from websockets.exceptions import ConnectionClosed

from qte_sdk.connection import Event, HandshakeFailed, SessionRejected
from qte_sdk.contract.v1.session_pb2 import Subscribe, Unsubscribe
from qte_sdk.resting import RestingOrders
from qte_sdk.session import (
    DEFAULT_ACK_TIMEOUT,
    Session,
    SessionInfo,
    SessionNotAcknowledged,
    _Secret,
    _without_token,
    open_session,
    resolve_token,
)

__all__ = [
    "DEFAULT_BACKOFF",
    "Backoff",
    "Connected",
    "Disconnected",
    "NotConnected",
    "ReconnectEvent",
    "ReconnectingSession",
    "Retrying",
    "is_retryable",
]


@dataclass(frozen=True)
class Backoff:
    """How long to wait between connection attempts: capped exponential backoff with jitter.

    The wait before attempt `n` (from 1) is `initial * factor ** (n - 1)`, capped at
    `maximum`, then reduced by a random fraction of up to `jitter` of itself, so it is never
    more than `maximum`. Jitter spreads out the reconnects of many clients that dropped at
    once. With `max_attempts` set, the session gives up after that many attempts in a row
    have failed and raises the last error; `None` keeps trying.
    """

    initial: float = 0.5
    maximum: float = 30.0
    factor: float = 2.0
    jitter: float = 0.5
    max_attempts: int | None = None

    def __post_init__(self) -> None:
        if not 0 <= self.initial <= self.maximum:
            raise ValueError("need 0 <= initial <= maximum")
        if self.factor < 1:
            raise ValueError("factor must be at least 1")
        if not 0 <= self.jitter <= 1:
            raise ValueError("jitter must be between 0 and 1")
        if self.max_attempts is not None and self.max_attempts < 1:
            raise ValueError("max_attempts must be at least 1, or None to keep trying")

    def delay(self, attempt: int, rng: float = 0.0) -> float:
        """Seconds to wait before attempt number `attempt`, given a random draw in [0, 1)."""
        try:
            base = self.initial * self.factor ** (attempt - 1)
        except OverflowError:
            base = self.maximum
        return min(base, self.maximum) * (1 - self.jitter * rng)


DEFAULT_BACKOFF = Backoff()
"""0.5 s, doubling to at most 30 s, with up to half of each wait taken off at random."""


@dataclass(frozen=True)
class Connected:
    """A session is up: authenticated, acknowledged, and subscribed to `instruments`.

    `reconnected` is False for the first session and True for every later one. After a
    reconnect, what happened while disconnected is unknown; see `Disconnected`.
    """

    info: SessionInfo
    instruments: tuple[str, ...]
    reconnected: bool


@dataclass(frozen=True)
class Disconnected:
    """The data-uncertainty event: the session ended and events may have been missed.

    Fills, order events and market data sent while disconnected are not recovered. `error`
    is why the connection ended, or None when the exchange closed it normally.
    """

    error: Exception | None


@dataclass(frozen=True)
class Retrying:
    """The next connection attempt, number `attempt`, starts after `delay` seconds.

    `error` is why the previous attempt failed, or None when this is the first attempt
    after a disconnect.
    """

    attempt: int
    delay: float
    error: Exception | None


ReconnectEvent = Event | Connected | Disconnected | Retrying
"""What a `ReconnectingSession` yields: connection events plus its own."""


class NotConnected(RuntimeError):
    """No session is up, so the message was not sent. Nothing is queued for later."""


def is_retryable(error: BaseException) -> bool:
    """Whether a new connection attempt could succeed after `error`.

    True for a dropped connection, a connection that could not be opened or timed out
    (other than a certificate that failed verification), a handshake answered with a
    malformed response or with HTTP 5xx, 408 or 429, and a session that closed before it
    was acknowledged. False for everything else, including every `SessionRejected`.
    """
    if isinstance(error, SessionRejected | ssl.SSLCertVerificationError):
        return False
    if isinstance(error, HandshakeFailed):
        status = error.status_code
        if status is None:
            # A malformed or cut-off response; the other kinds are a failed negotiation.
            return error.kind == "InvalidMessage"
        return status >= 500 or status in (408, 429)
    return isinstance(error, ConnectionClosed | SessionNotAcknowledged | TimeoutError | OSError)


class ReconnectingSession:
    """A session on the exchange that reconnects, with a new session, when it drops.

    Iterate it once to connect and to receive every event; the first connection is made
    when iteration starts. Send messages with `send` (it is a `Sender`, so the functions in
    `qte_sdk.orders` and `qte_sdk.market_data` accept it). Subscriptions sent through it,
    and the `instruments` given here, are made again on every new session. Close it, or use
    it with `async with`, when done.

    `token` is used as `open_session` uses it, falling back to `QTE_TOKEN`. `resting`, if
    given, is updated from every event and marked incomplete on every disconnect.
    `backoff=None` turns reconnecting off: the first failure to connect is raised, and
    iteration ends after the first `Disconnected`. `sleep` and `rng` wait and draw the
    jitter; replace them in tests. `ack_timeout` and `connection_options` are passed to
    `open_session` for every connection.

    Raises `MissingToken` here, before any connection, if there is no token.
    """

    def __init__(
        self,
        url: str,
        token: str | None = None,
        *,
        instruments: Iterable[str] = (),
        resting: RestingOrders | None = None,
        backoff: Backoff | None = DEFAULT_BACKOFF,
        ack_timeout: float | None = DEFAULT_ACK_TIMEOUT,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        rng: Callable[[], float] = random.random,
        **connection_options: Any,
    ) -> None:
        self._secret = _Secret(resolve_token(token))
        del token
        self.url = url
        self.resting = resting
        self.backoff = backoff
        self._instruments = dict.fromkeys(_instrument_list(instruments))
        self._ack_timeout = ack_timeout
        self._sleep = sleep
        self._rng = rng
        self._connection_options = connection_options
        self._session: Session | None = None
        self._up = False
        self._info: SessionInfo | None = None
        self._iterated = False
        self._closed = False
        self._pending: asyncio.Future[Any] | None = None

    def __repr__(self) -> str:
        state = "closed" if self._closed else "connected" if self._up else "not connected"
        return f"ReconnectingSession({self.url!r}, {state})"

    @property
    def connected(self) -> bool:
        """True while a session is up, from its `Connected` to its `Disconnected`."""
        return self._up

    @property
    def info(self) -> SessionInfo | None:
        """The acknowledgement of the latest session, or None before the first."""
        return self._info

    @property
    def instruments(self) -> tuple[str, ...]:
        """The instruments subscribed to again on every new session."""
        return tuple(self._instruments)

    async def send(self, type_: str, payload: Message) -> None:
        """Send one message on the current session.

        Raises `NotConnected` while no session is up; nothing is queued or sent later.
        A `subscribe` or `unsubscribe` sent here also changes which instruments a new
        session subscribes to.
        """
        session = self._session
        if session is None or not self._up:
            raise NotConnected(f"no session is up, so {type_} was not sent")
        if isinstance(payload, Subscribe):
            self._instruments.update(dict.fromkeys(payload.instruments))
        elif isinstance(payload, Unsubscribe):
            for instrument in payload.instruments:
                self._instruments.pop(instrument, None)
        failure: BaseException
        try:
            await session.connection.send(type_, payload)
        except BaseException as error:
            failure = error
        else:
            return
        # Not this frame's copy of the message either: it may be `auth`.
        del payload
        raise failure

    def __aiter__(self) -> AsyncIterator[ReconnectEvent]:
        return self.events()

    async def events(self) -> AsyncIterator[ReconnectEvent]:
        """Connect, then deliver every event, reconnecting as described in the module.

        Ends when `close` is called, or after a disconnect with reconnecting off. Raises an
        error that is not retryable, and the last error once `Backoff.max_attempts` attempts
        in a row have failed. A session can be iterated only once.
        """
        if self._iterated:
            raise RuntimeError("a ReconnectingSession can be iterated only once")
        self._iterated = True
        reconnected = False
        try:
            while True:
                failures = 0
                last: Exception | None = None
                session: Session | None = None
                while session is None:
                    # A reconnect waits before its first attempt too, so a session that
                    # drops at once cannot make the client reconnect in a tight loop.
                    waits = failures + 1 if reconnected else failures
                    if waits and self.backoff is not None:
                        delay = self.backoff.delay(waits, self._rng())
                        yield Retrying(failures + 1, delay, last)
                        if await self._unless_closed(self._sleep(delay)) is _CLOSED:
                            return
                    if self._closed:
                        return
                    session, failure = await self._attempt()
                    if self._closed:
                        return
                    if failure is not None:
                        failures += 1
                        last = failure
                        if self._gives_up(failure, failures):
                            raise failure

                assert session is not None
                self._info = session.info
                self._up = True
                yield Connected(session.info, self.instruments, reconnected)
                reconnected = True

                failure = None
                try:
                    async for event in session:
                        if self.resting is not None:
                            self.resting.apply(event)
                        yield event
                except Exception as error:
                    failure = self._safe(error)
                # Uncertainty is flagged before anything else, the close included.
                self._up = False
                if self.resting is not None:
                    self.resting.mark_incomplete()
                self._session = None
                with suppress(Exception):
                    await session.close()
                if self._closed:
                    return
                yield Disconnected(failure)
                if failure is not None and not is_retryable(failure):
                    raise failure
                if self.backoff is None:
                    return
        finally:
            # However iteration ends (returned, raised, cancelled or closed early), no
            # later event can reach the view, and the session cannot be iterated again.
            self._closed = True
            self._up = False
            if self.resting is not None:
                self.resting.mark_incomplete()
            session = self._session
            self._session = None
            if session is not None:
                with suppress(Exception):
                    await session.close()

    async def close(self) -> None:
        """Close the current session, if any, and stop reconnecting; iteration then ends.

        A backoff wait or a connection attempt in progress is cut short. The resting view,
        if any, is marked incomplete, since no later event will reach it.
        """
        self._closed = True
        self._up = False
        if self.resting is not None:
            self.resting.mark_incomplete()
        pending = self._pending
        if pending is not None:
            pending.cancel()
        session = self._session
        self._session = None
        if session is not None:
            await session.close()

    async def __aenter__(self) -> "ReconnectingSession":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    async def _attempt(self) -> tuple[Session | None, Exception | None]:
        """One connection attempt: open a session, then subscribe again. Returns the session,
        or the reason it failed; neither if the session was closed meanwhile."""
        session: Session | None = None
        failure: Exception | None = None
        try:
            opened = await self._unless_closed(
                open_session(
                    self.url,
                    self._secret.value,
                    ack_timeout=self._ack_timeout,
                    **self._connection_options,
                )
            )
            if opened is _CLOSED:
                return None, None
            session = opened
            # Held here at once, so close() reaches it even while it subscribes.
            self._session = session
            if self._instruments and not self._closed:
                subscription = Subscribe(instruments=list(self._instruments))
                await session.connection.send("subscribe", subscription)
        except Exception as error:
            failure = self._safe(error)
        if session is not None and (failure is not None or self._closed):
            self._session = None
            with suppress(Exception):
                await session.close()
            session = None
        return session, failure

    async def _unless_closed(self, awaitable: Awaitable[Any]) -> Any:
        """Await `awaitable`, or return `_CLOSED` if `close()` cuts it short.

        Cancelling the task that iterates the session still raises `CancelledError`.
        """
        task = asyncio.ensure_future(awaitable)
        self._pending = task
        try:
            return await task
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if self._closed and task.cancelled() and (current is None or not current.cancelling()):
                return _CLOSED
            raise
        finally:
            self._pending = None

    def _gives_up(self, failure: Exception, failures: int) -> bool:
        if self.backoff is None or not is_retryable(failure):
            return True
        limit = self.backoff.max_attempts
        return limit is not None and failures >= limit

    def _safe(self, error: Exception) -> Exception:
        """`error`, or a replacement for it if it would repeat the token."""
        replacement = _without_token(error, self._secret)
        if replacement is None:
            return error
        assert isinstance(replacement, Exception)
        return replacement


_CLOSED = object()


def _instrument_list(instruments: Iterable[str]) -> list[str]:
    # A bare string is iterable too, and would otherwise subscribe to each of its letters.
    if isinstance(instruments, str):
        raise TypeError('pass a list of instrument ids, such as ["AAPL"], not a single string')
    return list(instruments)
