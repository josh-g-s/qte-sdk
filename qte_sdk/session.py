"""Open an authenticated session on the exchange.

    session = await open_session()  # address from QTE_URL, token from QTE_TOKEN(_FILE) or .env
    async with session:
        print(session.info.team, session.info.unscored)
        async for event in session:
            ...

A session is a `Connection` that has sent `auth` with your account's token and received
`session_ack`. The token comes from the `token` argument or, failing that, the `QTE_TOKEN`
environment variable, the file named by the `QTE_TOKEN_FILE` environment variable, or
`QTE_TOKEN` in a `.env` file in the working directory, in that order (see `resolve_token`).
The exchange address comes from the `url` argument, the `QTE_URL` environment variable or
`QTE_URL` in `.env` (see `resolve_url`). Keep the token out of source files and out of the
repository.

The token is sent once, in the `auth` message, and is not kept afterwards. The SDK never
logs it or puts it in an exception: the connection drops the frame-level debug lines of
the `websockets` library, which would show the `auth` message (see `qte_sdk.connection`). While
the session opens, a message from the exchange that repeats the token is also kept out of
the exception raised; once the session is open a `Session` no longer holds the token, and
what the exchange sends is passed on as it arrives. A `qte_sdk.reconnect.ReconnectingSession`
keeps it, in a wrapper no repr shows, to authenticate each new session.
"""

import asyncio
import json
import os
from collections import deque
from collections.abc import AsyncIterator, Callable
from typing import Any

from google.protobuf.message import Message
from websockets.exceptions import ConnectionClosed

from qte_sdk.connection import (
    Connection,
    DecodeFailed,
    Event,
    Received,
    ReportGap,
    ResumeComplete,
    SeqGap,
    SessionInfo,
    SessionRejected,
    _close_detail,
    _received_close_code,
)
from qte_sdk.contract.v1.common_pb2 import RESUME, ReasonCodes
from qte_sdk.contract.v1.order_events_pb2 import Reject
from qte_sdk.contract.v1.session_pb2 import Auth, Calendar, Resume, ResumeAck, SessionAck
from qte_sdk.dotenv import DOTENV_NAME, read_value

TOKEN_ENV_VAR = "QTE_TOKEN"
TOKEN_FILE_ENV_VAR = "QTE_TOKEN_FILE"
URL_ENV_VAR = "QTE_URL"
DEFAULT_ACK_TIMEOUT = 10.0
DEFAULT_CALENDAR_TIMEOUT = 5.0


class MissingToken(ValueError):
    """No token was given, `QTE_TOKEN` is unset or empty, `QTE_TOKEN_FILE` is unset or names
    a file that holds no readable token, or `.env` holds no token the SDK may use. The
    message says which; it never holds any of the file's contents."""


class MissingURL(ValueError):
    """No exchange address was given, `QTE_URL` is unset or empty, and `.env` sets no
    usable `QTE_URL`. The message says which; it never holds any of the file's contents."""


class SessionNotAcknowledged(Exception):
    """The connection ended, or the acknowledgement could not be read, before the session
    was acknowledged.

    `close_code` is the close code the exchange sent, when it closed the connection, for
    example `qte_sdk.connection.TERM_CHANGE_CLOSE_CODE`; otherwise None.
    """

    def __init__(self, *args: object, close_code: int | None = None) -> None:
        super().__init__(*args)
        self.close_code = close_code


class AuthNotSent(SessionNotAcknowledged):
    """The `auth` message could not be encoded or sent, for a reason other than the
    connection closing, such as a bad `contract_version`. Trying again fails the same way."""


class ResumeNotAcknowledged(SessionNotAcknowledged):
    """The connection ended, or the `resume_ack` could not be decoded, before the exchange
    answered a `resume`."""


class ResumeRejected(SessionRejected):
    """The exchange refused a `resume` with a `reject` whose `request_type` is `RESUME`, or
    with one that names no request type and no `request_ref`, which is how an exchange that
    does not serve `resume` yet answers it.

    The connection stays open and the session goes on, but no report is replayed and no
    snapshot is sent.
    """


def _report_seq(event: Event) -> int | None:
    return getattr(event, "report_seq", None)


def _rejects_resume(event: Event) -> bool:
    """Whether `event` refuses a `resume`: a `reject` naming RESUME, or, from an exchange
    that does not know the message, a MALFORMED_MESSAGE `reject` that names no request type
    (not even one this SDK does not know) and no request_ref, and is not one of the team's
    order reports."""
    if not isinstance(event, Received) or not isinstance(event.message, Reject):
        return False
    message = event.message
    if message.HasField("request_type"):
        return message.request_type == RESUME
    return (
        "request_type" not in (event.payload or {})
        and "requestType" not in (event.payload or {})
        and event.report_seq is None
        and not message.HasField("request_ref")
        and message.reason_code == ReasonCodes.MALFORMED_MESSAGE
    )


class _Resume:
    """A resume in progress: waiting for `resume_ack`, then for the replay or snapshot."""

    __slots__ = ("ack", "held", "snapshots_left")

    def __init__(self) -> None:
        self.ack: ResumeAck | None = None
        self.snapshots_left = 0
        # Reports that must wait until the resume is complete, by report_seq.
        self.held: dict[int, Event] = {}


class _Reports:
    """The team's report cursor, and the resume in progress, if any.

    The cursor is the highest report_seq up to which every report has been delivered with
    no gap, or None before the first report or resume gives it a starting point. Reports
    delivered beyond a gap are remembered in `above`, so a later replay does not deliver
    them twice; the cursor never moves over a gap.
    """

    def __init__(self) -> None:
        self.cursor: int | None = None
        self.above: set[int] = set()
        self.resume: _Resume | None = None
        # What answered the latest resume: a `resume_ack`, a `reject` of it, or a
        # `resume_ack` that could not be decoded.
        self.answer: Event | None = None
        self.before_resume: tuple[int | None, frozenset[int]] = (None, frozenset())

    def saved(self) -> tuple[int | None, frozenset[int]]:
        return self.cursor, frozenset(self.above)

    def restore(self, saved: tuple[int | None, frozenset[int]]) -> None:
        self.cursor, above = saved
        self.above = set(above)
        self.resume = None

    def begin(self, last_report_seq: int) -> None:
        # Kept so a resume that is never answered leaves the cursor as it found it.
        self.before_resume = self.saved()
        self.cursor = last_report_seq
        self.above = {n for n in self.above if n > last_report_seq}
        self._advance()
        self.resume = _Resume()
        self.answer = None

    def abandon(self) -> list[Event]:
        """End a resume that was never answered, releasing what it held. The cursor goes
        back to where it was before the resume, so nothing is counted that was not read."""
        if self.resume is None:
            return []
        self._unbegin()
        return self._finish(None)

    def _unbegin(self) -> None:
        self.cursor, above = self.before_resume
        self.above = set(above)

    def route(self, event: Event) -> list[Event]:
        """The events to deliver, in order, now that `event` has arrived."""
        if self.resume is not None:
            return self._route_resuming(event, self.resume)
        report_seq = _report_seq(event)
        return [event] if report_seq is None else self._process(event, report_seq)

    def _route_resuming(self, event: Event, resume: _Resume) -> list[Event]:
        ack = resume.ack
        if ack is None:
            if isinstance(event, Received) and event.type == "resume_ack":
                assert isinstance(event.message, ResumeAck)
                self.answer = event
                return [event, *self._on_ack(event.message, resume)]
            if _rejects_resume(event) or (
                isinstance(event, DecodeFailed) and event.type == "resume_ack"
            ):
                self.answer = event
                self._unbegin()
                return [event, *self._finish(None)]
        elif isinstance(event, SeqGap) or (isinstance(event, DecodeFailed) and event.type is None):
            # A message was lost on the connection, or arrived unreadable, perhaps one the
            # resume needs to finish: stop waiting for it rather than hold later reports
            # for good.
            return [event, *self._finish(None)]
        elif (
            not ack.replayed
            and isinstance(event, Received | DecodeFailed)
            and event.type == "order_snapshot"
        ):
            resume.snapshots_left -= 1
            if resume.snapshots_left <= 0:
                return [event, *self._complete(ack)]
            return [event]
        report_seq = _report_seq(event)
        if report_seq is None:
            return [event]
        if ack is None or not ack.replayed or report_seq > ack.as_of_report_seq:
            # A live report: it waits until the replay or snapshot is complete.
            resume.held.setdefault(report_seq, event)
            return []
        routed = self._process(event, report_seq)
        if report_seq == ack.as_of_report_seq:
            routed += self._complete(ack)
        return routed

    def _on_ack(self, ack: ResumeAck, resume: _Resume) -> list[Event]:
        resume.ack = ack
        # The replay or the snapshot covers every report held so far at or below as_of.
        resume.held = {n: e for n, e in resume.held.items() if n > ack.as_of_report_seq}
        if ack.replayed:
            # Complete at once only if nothing up to as_of is missing; otherwise the replay
            # fills the gap and completes on as_of, even when as_of itself is a duplicate.
            if self.cursor is not None and self.cursor >= ack.as_of_report_seq:
                return self._complete(ack)
            return []
        resume.snapshots_left = ack.snapshot_count
        if ack.snapshot_count <= 0:
            return self._complete(ack)
        return []

    def _complete(self, ack: ResumeAck) -> list[Event]:
        if not ack.replayed:
            # The snapshot describes the team's orders as of this report, and sets the
            # cursor to exactly it, lower if report numbers restarted with a new term.
            # Nothing delivered beyond an old cursor counts any more: as_of is the newest
            # report the exchange had, and after a new term the old numbers mean nothing.
            self.cursor = ack.as_of_report_seq
            self.above = set()
        done = ResumeComplete(ack.replayed, ack.as_of_report_seq, ack.snapshot_count)
        return self._finish(done)

    def _finish(self, done: ResumeComplete | None) -> list[Event]:
        assert self.resume is not None
        held = self.resume.held
        self.resume = None
        routed: list[Event] = [done] if done is not None else []
        for report_seq in sorted(held):
            routed += self._process(held[report_seq], report_seq)
        return routed

    def _delivered(self, report_seq: int) -> bool:
        return self.cursor is not None and (report_seq <= self.cursor or report_seq in self.above)

    def _process(self, event: Event, report_seq: int) -> list[Event]:
        if self.cursor is None:
            self.cursor = report_seq  # the starting point
            return [event]
        if self._delivered(report_seq):
            return []  # a duplicate
        routed: list[Event] = []
        highest = max(self.above, default=self.cursor)
        if report_seq > highest + 1:
            routed.append(ReportGap(highest + 1, report_seq))
        if report_seq == self.cursor + 1:
            self.cursor = report_seq
            self._advance()
        else:
            self.above.add(report_seq)
        routed.append(event)
        return routed

    def _advance(self) -> None:
        assert self.cursor is not None
        while self.cursor + 1 in self.above:
            self.cursor += 1
            self.above.remove(self.cursor)


class Session:
    """An acknowledged session: the open connection and what the exchange said about it.

    Send on the session and iterate it, rather than its connection. Iterating the session
    delivers every event: anything the exchange sent before `session_ack` first, then the
    rest of the stream. A session is a `qte_sdk.orders.Sender`, so the functions in
    `qte_sdk.orders` and `qte_sdk.market_data` accept it.

    `calendar` is the exchange's session calendar, once it has arrived; see
    `wait_for_calendar` and `qte_sdk.calendar`.

    Heartbeats are absorbed and never delivered as events. `heartbeats_received`,
    `last_heartbeat_at` and `last_heartbeat_sent_at` say whether they are arriving: they
    describe this session's connection from the moment it opened, before the
    acknowledgement included, and a new session starts again from none. A heartbeat is
    counted as it is read, so they move only while the session is read: by iterating it,
    or by `wait_for_calendar` or `resume` reading ahead.

    Private reports: each of the team's order reports that the exchange can replay carries
    a `report_seq`, numbered per team without gaps. The session delivers them in that order
    and keeps `last_report_seq`, the number up to which it has delivered every one. A
    report numbered at or below it again is a duplicate and is dropped. When a number is
    skipped, a `ReportGap` (a `DataUncertain`) is delivered before the report that skipped
    it. `resume` asks the exchange for the reports a team missed.
    """

    def __init__(self, connection: Connection, info: SessionInfo, early: list[Event]) -> None:
        self.connection = connection
        self.info = info
        # Events read from the connection but not yet delivered: those that arrived before
        # the ack, and any that `wait_for_calendar` or `resume` read ahead. They are kept
        # as read in `_unrouted`, and pass through the report cursor (see `_Reports`) into
        # `_buffer` only when delivered or when `resume` needs them, so that a resume
        # holds every report from the moment the connection opened.
        self._unrouted: deque[Event] = deque()
        self._buffer: deque[Event] = deque()
        self._calendar: Calendar | None = None
        self._calendar_unreadable = False
        self._source: AsyncIterator[Event] | None = None
        self._reading = False
        self._ended = False
        self._failure: Exception | None = None
        self._reports = _Reports()
        self._resumed = False
        for event in early:
            self._keep(event)

    def __repr__(self) -> str:
        return f"Session({self.connection.url!r}, {self.info!r})"

    @property
    def calendar(self) -> Calendar | None:
        """The latest `calendar` message the exchange sent on this session, or None if none
        has been read yet.

        The exchange sends it right after it acknowledges the session, so it is usually
        None when `open_session` returns, and is set once iterating the session (or
        `wait_for_calendar`) reads it. An exchange that predates the calendar message never
        sends one, so it can stay None for the whole session.
        """
        return self._calendar

    async def wait_for_calendar(
        self, timeout: float | None = DEFAULT_CALENDAR_TIMEOUT
    ) -> Calendar | None:
        """Wait up to `timeout` seconds (None for no limit) for the exchange's calendar.

        Returns the calendar, at once if it has already arrived, or None if it does not
        arrive in time, as with an exchange that predates the calendar message, or if the
        connection ends or the calendar cannot be decoded first. It never raises for any
        of these.

        While it waits it reads the session's events ahead of you and keeps them, the
        calendar included: iterating the session afterwards still delivers every event, in
        order. If the connection failed meanwhile, iterating raises that error once the
        kept events are delivered. Call it from the task that iterates the session, not
        alongside a loop in another task: only one task can read a session at a time.
        """
        deadline = asyncio.timeout(timeout)
        try:
            async with deadline:
                while self._calendar is None and not self._calendar_unreadable:
                    if self._ended:
                        break
                    await self._read_one()
        except TimeoutError:
            if not deadline.expired():
                raise
        return self._calendar

    @property
    def heartbeats_received(self) -> int:
        """How many heartbeats this session's connection has read since it opened, 0 if
        none. See the class description: it moves only while the session is read."""
        return self.connection.heartbeats_received

    @property
    def last_heartbeat_at(self) -> float | None:
        """When the latest heartbeat was read, on the `time.monotonic()` clock, or None
        before the first; `time.monotonic() - session.last_heartbeat_at` is how many
        seconds ago that was. See `Connection.last_heartbeat_at`."""
        return self.connection.last_heartbeat_at

    @property
    def last_heartbeat_sent_at(self) -> int | None:
        """The exchange's send time of the latest heartbeat, in milliseconds since the Unix
        epoch, UTC, or None before the first or if it carried none. See
        `Connection.last_heartbeat_sent_at`."""
        return self.connection.last_heartbeat_sent_at

    @property
    def last_report_seq(self) -> int | None:
        """The `report_seq` up to which this session has read every private report with no
        gap, or None before the first report or `resume`. Pass it to the next session's
        `resume` to have the exchange replay the reports that come after it, if that session
        is in the same term (see `resume`).

        It counts reports as they are read, which can be before they are delivered (for
        example while `resume` or `wait_for_calendar` reads ahead), so take it once you
        have handled every event the session has delivered. Without a resume, the first
        report read is its starting point: nothing says whether earlier ones were missed.
        A report that could not be decoded counts too, as a `DecodeFailed`, since a replay
        would send the same bytes again."""
        return self._reports.cursor

    async def resume(
        self, last_report_seq: int, *, timeout: float | None = DEFAULT_ACK_TIMEOUT
    ) -> ResumeAck:
        """Ask the exchange for the private reports after `last_report_seq`, and wait up to
        `timeout` seconds (None for no limit) for its answer, the `resume_ack`.

        Send it at most once, right after the session opens and before reading any
        events. Pass the `last_report_seq` of the session this one replaces, or 0 to get a
        snapshot of the team's resting orders instead. Report numbers start again each
        term, so pass the earlier number only if this session's calendar names the same
        term (`term_start` and `term_end`) as the session that counted it, and 0 otherwise.
        Calling `wait_for_calendar` first is fine, since it keeps what it reads.

        The exchange then does one of two things, both delivered by iterating the session,
        after the `resume_ack` itself:

        - Replay (`replayed` True): the reports after `last_report_seq`, in order, exactly
          as they were first sent. Reports this session already delivered are dropped.
        - Snapshot (`replayed` False): `snapshot_count` `order_snapshot` events, one per
          resting order of the team, in place of the reports. A count of 0 means the team
          has no resting order, as is always so outside a session.

        Then a `ResumeComplete` is delivered, and after it any report that arrived
        meanwhile, in order. Reports can arrive from the moment the connection opens, so
        every report not yet read when `resume` is called waits too; those the replay or
        snapshot covers are dropped. Market data is not replayed: subscribe again for it. If a
        message is lost on the connection (a `SeqGap`) before the resume is complete, the
        session stops waiting for it and delivers what it held. If the connection ends
        first, what it held is not delivered and `last_report_seq` does not move past it,
        so the next resume replays it.

        Returns the `resume_ack`. Raises `ResumeRejected` if the exchange refuses the
        resume, `ResumeNotAcknowledged` if the connection ends or the answer cannot be
        decoded first, and `TimeoutError` if no answer arrives in time. Call it from the
        task that iterates the session.
        """
        if (
            isinstance(last_report_seq, bool)
            or not isinstance(last_report_seq, int)
            or last_report_seq < 0
        ):
            raise ValueError("last_report_seq must be a whole number, 0 or more")
        if self._resumed:
            raise RuntimeError("a session can be resumed only once")
        self._resumed = True
        await self.send("resume", Resume(last_report_seq=last_report_seq))
        reports = self._reports
        reports.begin(last_report_seq)
        self._route_all()
        timed_out = False
        deadline = asyncio.timeout(timeout)
        try:
            async with deadline:
                while reports.answer is None and not self._ended:
                    await self._read_one()
                    self._route_all()
        except TimeoutError:
            if not deadline.expired():
                raise
            timed_out = True
        answer = reports.answer
        if answer is None:
            self._buffer.extend(reports.abandon())
        if timed_out:
            raise TimeoutError(f"the exchange did not answer resume within {timeout} s")
        if answer is None:
            if isinstance(self._failure, SessionRejected):
                # The exchange refused the session itself; that is the error to report.
                raise self._failure
            failure = self._failure
            detail = _close_detail(failure) if isinstance(failure, ConnectionClosed) else ""
            raise ResumeNotAcknowledged(
                f"the connection closed before resume_ack{detail}",
                close_code=_received_close_code(failure),
            )
        if isinstance(answer, DecodeFailed):
            raise ResumeNotAcknowledged(f"resume_ack could not be decoded: {answer.error}")
        assert isinstance(answer, Received)
        if isinstance(answer.message, Reject):
            message = answer.message
            detail = message.reason_detail if message.HasField("reason_detail") else None
            name = answer.unknown_enum_names().get("reason_code")
            raise ResumeRejected(message.reason_code, detail, reason_name=name)
        assert isinstance(answer.message, ResumeAck)
        return answer.message

    def _raise_if_failed(self) -> None:
        """Raise the error the connection ended with, if it has ended with one while the
        session read ahead, such as a `SessionRejected` read while `wait_for_calendar`
        waited. Used by a `ReconnectingSession` before it sends anything more: a send on
        the closed connection would raise only that it is closed, which hides why, and is
        retried where a rejection must not be."""
        if self._ended and self._failure is not None:
            raise self._failure

    async def _failure_after_close(self, timeout: float) -> Exception | None:
        """Read what is left on a connection that a send found closed, for at most
        `timeout` seconds, and return the error it ended with, if any. The exchange may
        have rejected the session just before it closed, and that rejection, still unread,
        says why. Used by a `ReconnectingSession`; what is read is not delivered."""
        loop = asyncio.get_running_loop()
        end = loop.time() + timeout
        deadline = asyncio.timeout(timeout)
        try:
            async with deadline:
                # Checked between reads too: queued frames are read without the event
                # loop running, so the timeout alone might not fire.
                while not self._ended and loop.time() < end:
                    await self._read_one()
        except TimeoutError:
            if not deadline.expired():
                raise
        return self._failure

    def _withhold_in_answer(self, secret: "_Secret") -> None:
        """Replace the `reject` that refused the resume, if it repeats the token, with a
        copy that does not. Used by a `ReconnectingSession`, which holds the token and goes
        on to deliver that reject as an event."""
        answer = self._reports.answer
        if not isinstance(answer, Received) or not isinstance(answer.message, Reject):
            return
        # Read field by field: str() of the message or the payload escapes a token with a
        # backslash, newline or tab, so it could not be found there.
        if not _holds_token(answer.message, secret) and not _holds_token(answer.payload, secret):
            return
        message = Reject()
        message.CopyFrom(answer.message)
        if message.HasField("reason_detail"):
            message.reason_detail = _redact(message.reason_detail, secret)
        if _holds_token(message, secret):
            # Still in another field (request_ref or instrument): keep only the reason.
            message = Reject(reason_code=message.reason_code)
        safe = Received(answer.type, message, answer.seq, None, answer.report_seq)
        self._buffer = deque(safe if e is answer else e for e in self._buffer)
        self._reports.answer = safe

    def __aiter__(self) -> AsyncIterator[Event]:
        return self.events()

    async def events(self) -> AsyncIterator[Event]:
        while True:
            if self._buffer:
                yield self._buffer.popleft()
            elif self._unrouted:
                self._buffer.extend(self._reports.route(self._unrouted.popleft()))
            elif self._ended:
                if self._failure is not None:
                    raise self._failure
                return
            else:
                await self._read_one()

    async def _read_one(self) -> None:
        """Read the next event from the connection into the buffer. At its end, or on an
        error, mark the session ended, keeping the error for iteration to raise."""
        if self._reading:
            raise RuntimeError(
                "another task is already reading this session; read it from one place"
            )
        if self._source is None:
            self._source = self.connection.events()
        self._reading = True
        try:
            event = await anext(self._source)
        except StopAsyncIteration:
            self._ended = True
        except Exception as error:
            self._ended = True
            self._failure = error
        except BaseException:
            # Cancelled or interrupted mid-read: that iterator is finished, but receiving is
            # cancel-safe and the connection keeps its sequence tracking, so nothing was
            # lost and the next read starts a fresh one.
            self._source = None
            raise
        else:
            self._keep(event)
        finally:
            self._reading = False

    def _route_all(self) -> None:
        while self._unrouted:
            self._buffer.extend(self._reports.route(self._unrouted.popleft()))

    def _keep(self, event: Event) -> None:
        self._unrouted.append(event)
        if isinstance(event, Received) and event.type == "calendar":
            assert isinstance(event.message, Calendar)
            self._calendar = event.message
        elif isinstance(event, DecodeFailed) and event.type == "calendar":
            self._calendar_unreadable = True

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
    """The token to authenticate with, from the first of these that is present:

    - `token`, when it is not None (an empty string raises `MissingToken`);
    - the `QTE_TOKEN` environment variable, when it is set and not empty;
    - the contents of the file named by the `QTE_TOKEN_FILE` environment variable, when it
      is set and not empty, with one trailing newline removed;
    - `QTE_TOKEN` in the `.env` file in the working directory (see `qte_sdk.dotenv`).

    Each file is read only when nothing before it is present, and is read on each call.
    Raises `MissingToken` if there is no token; if the file named by `QTE_TOKEN_FILE`
    cannot be read, is not UTF-8 text, or holds nothing but whitespace (the `.env` is then
    not tried); or if the `.env` cannot be read or parsed, or, on POSIX, holds the token
    and other users can read it.
    """
    token, _, problem = _find_token(token)
    if problem is not None:
        # Raised outside any handler, from a frame that holds neither the file's path nor
        # its contents, so the exception carries neither.
        raise MissingToken(problem)
    assert token is not None
    return token


def token_source() -> str:
    """Where `resolve_token()` would take the token from, without reading it out: the name
    `QTE_TOKEN`, `QTE_TOKEN_FILE` or `.env`. Raises `MissingToken` as `resolve_token` does."""
    token, source, problem = _find_token(None)
    del token
    if problem is not None:
        raise MissingToken(problem)
    assert source is not None
    return source


def _find_token(token: str | None) -> tuple[str | None, str | None, str | None]:
    """The token, the name of its source and None; or None, None and the message for
    `MissingToken`. Never raises, so no exception carries a frame that holds the token."""
    source = None
    if token is None:
        token = os.environ.get(TOKEN_ENV_VAR) or None
        source = TOKEN_ENV_VAR
    problem = None
    if token is None:
        source = TOKEN_FILE_ENV_VAR
        token, problem = _token_from_file()
        if problem is not None:
            return None, None, f"no token: {TOKEN_FILE_ENV_VAR} names a file that {problem}"
    if token is None:
        source = DOTENV_NAME
        token, problem = read_value(TOKEN_ENV_VAR)
        if problem is not None:
            return None, None, f"no token: {DOTENV_NAME} {problem}"
    if not token:
        problem = (
            f"no token: pass token=, set the {TOKEN_ENV_VAR} environment variable, set "
            f"{TOKEN_FILE_ENV_VAR} to the path of a file holding it, or put {TOKEN_ENV_VAR} "
            f"in a {DOTENV_NAME} file in the working directory"
        )
        return None, None, problem
    return token, source, None


def resolve_url(url: str | None = None) -> str:
    """The exchange address, from the first of these that is present:

    - `url`, when it is not None (an empty string raises `MissingURL`);
    - the `QTE_URL` environment variable, when it is set and not empty;
    - `QTE_URL` in the `.env` file in the working directory (see `qte_sdk.dotenv`).

    Raises `MissingURL` if there is none, or if the `.env` cannot be read or parsed, or, on
    POSIX, holds `QTE_TOKEN` and other users can read it.
    """
    return url_source(url)[1]


def url_source(url: str | None = None) -> tuple[str, str]:
    """Where `resolve_url(url)` takes the address from, and the address: the source is
    `url`, `QTE_URL` or `.env`. Raises `MissingURL` as `resolve_url` does."""
    source = "url"
    if url is None:
        url = os.environ.get(URL_ENV_VAR) or None
        source = URL_ENV_VAR
    problem = None
    if url is None:
        source = DOTENV_NAME
        url, problem = read_value(URL_ENV_VAR)
    if problem is not None:
        raise MissingURL(f"no exchange address: {DOTENV_NAME} {problem}")
    if not url:
        raise MissingURL(
            f"no exchange address: pass url=, set the {URL_ENV_VAR} environment variable, "
            f"or put {URL_ENV_VAR} in a {DOTENV_NAME} file in the working directory"
        )
    return source, url


def _token_from_file() -> tuple[str | None, str | None]:
    """The token in the file named by `QTE_TOKEN_FILE` and None, or None and what is wrong
    with the file. (None, None) if the variable is unset or empty.

    Never raises for a bad file: a `UnicodeDecodeError` keeps the bytes it rejected and an
    `OSError` keeps the path (which a mistaken setting could make the token itself), so
    neither may reach the caller's exception as its cause or context.
    """
    path = os.environ.get(TOKEN_FILE_ENV_VAR)
    if not path:
        return None, None
    try:
        with open(path, "rb") as file:
            data = file.read()
    except OSError as error:
        return None, f"cannot be read ({error.strerror or type(error).__name__})"
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None, "is not UTF-8 text"
    if text.endswith("\r\n"):
        text = text[:-2]
    elif text.endswith("\n"):
        text = text[:-1]
    if not text.strip():
        return None, "is empty or holds only whitespace"
    return text, None


async def open_session(
    url: str | None = None,
    token: str | None = None,
    *,
    ack_timeout: float | None = DEFAULT_ACK_TIMEOUT,
    **connection_options: Any,
) -> Session:
    """Connect to `url`, authenticate, and wait for the exchange to acknowledge the session.

    `url` is the exchange address, or None to take it from `QTE_URL` in the environment or
    in `.env` (see `resolve_url`). Raises `MissingURL` before connecting if there is none.

    `connection_options` are passed to `Connection` (for example `contract_version`) and on
    to `websockets.asyncio.client.connect`.

    `ack_timeout` bounds the whole opening sequence: connecting, sending `auth` and waiting
    for `session_ack` must all finish within that many seconds of the call (None for no
    limit). The `websockets` option `open_timeout` still bounds the opening handshake on its
    own, and `close_timeout` bounds closing the connection (see `Connection.close`).

    `token` is your team token, or None to take it from the environment (see
    `resolve_token`). Raises `MissingToken` before connecting if there is no token. If the
    exchange refuses the session, raises `SessionRejected` carrying the contract reason
    code, or `ContractVersionMismatch` when the exchange does not serve this contract
    version.
    Raises `SessionNotAcknowledged` if the connection ends first, and `TimeoutError` if the
    session is not acknowledged within `ack_timeout` seconds. The connection is closed
    whenever no session is returned, which can take up to `close_timeout` seconds more.
    """
    secret = _Secret(resolve_token(token))
    del token
    return await _open_session(
        resolve_url(url), secret, None, ack_timeout=ack_timeout, **connection_options
    )


async def _open_session(
    url: str,
    secret: "_Secret",
    on_close: Callable[[int], None] | None,
    /,
    *,
    ack_timeout: float | None,
    **connection_options: Any,
) -> Session:
    """`open_session`, for a resolved address and token. `on_close`, if not None, is called
    with the close code the exchange sent, if it closed the connection before the session
    was acknowledged, even when a cancellation then replaces the error; `ReconnectingSession`
    uses it. It is positional-only, so an `on_close=` among a caller's options is passed
    on with them and never reaches it."""
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
        # For `ReconnectingSession`: the close code the exchange sent, if any, is passed on
        # even when a cancellation replaces the error, which is never chained (it may
        # repeat the token). A code is a number, so it cannot.
        close_code = conn._close_code_received()
        if on_close is not None and close_code is not None:
            on_close(close_code)
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
        if _holds_token(error, secret):
            # Its text may hold the token escaped, where a redaction would not find it.
            text = f"could not send auth: {type(error).__name__}; details withheld"
        else:
            text = f"could not send auth: {type(error).__name__}: {error}"
        replacement: BaseException = kind(text, close_code=_received_close_code(error))
    except BaseException as error:
        # Cancellation and interrupts keep their type, so they behave as they otherwise would.
        replacement = type(error)(*error.args)
    # Raised outside the handler: the frames of the interrupted send hold the auth message,
    # so the original error and its traceback are not chained to this one.
    raise replacement


async def _wait_for_ack(conn: Connection) -> tuple[SessionAck, list[Event]]:
    early: list[Event] = []
    events = conn.events()
    detail = ""
    close_code: int | None = None
    try:
        async for event in events:
            if isinstance(event, Received):
                if event.type == "session_ack":
                    assert isinstance(event.message, SessionAck)
                    return event.message, early
                if event.type == "reject" and event.report_seq is None:
                    # Before the acknowledgement, the only request in flight is `auth`. A
                    # reject with a report_seq is one of the team's order reports, which can
                    # arrive from the moment the connection opens.
                    message: Any = event.message
                    detail = message.reason_detail if message.HasField("reason_detail") else None
                    name = event.unknown_enum_names().get("reason_code")
                    raise SessionRejected(message.reason_code, detail, reason_name=name)
            elif isinstance(event, DecodeFailed) and event.type == "session_ack":
                # Not chained: the decoder's frames hold the raw payload in their locals.
                raise SessionNotAcknowledged(f"session_ack could not be decoded: {event.error}")
            early.append(event)
    except ConnectionClosed as error:
        # Only the code and a known reason are kept: any other close reason is server text
        # and could echo the token.
        detail = _close_detail(error)
        close_code = _received_close_code(error)
    finally:
        await events.aclose()
    raise SessionNotAcknowledged(
        f"the connection closed before session_ack{detail}", close_code=close_code
    )


class _Secret:
    """Holds the token so that no repr, and so no traceback that shows locals, reveals it."""

    __slots__ = ("value",)

    def __init__(self, value: str) -> None:
        self.value = value

    def __repr__(self) -> str:
        return "<token withheld>"

    __str__ = __repr__


def _detached(error: BaseException) -> BaseException:
    """`error` without its traceback or chain, for the caller to raise again from its own
    frame. Used where the frames an interruption (a Ctrl-C, say) came through can hold a
    form of the token, which a traceback that shows locals would display."""
    error = error.with_traceback(None)
    error.__cause__ = error.__context__ = None
    return error


def _token_forms(secret: _Secret) -> tuple[_Secret, ...]:
    """The token as written, and as the usual escapes write it: Python's repr of it as text
    or bytes (with a quote escaped or not) and JSON. Those escape a backslash, newline or
    tab, so text that holds an escaped copy does not hold the token as written. Longest
    first, so a redaction replaces a whole escaped copy rather than part of it. Raises no
    error of its own, even for a token that is not valid Unicode, such as one read from an
    environment variable holding bytes that are not UTF-8.

    Each form is held like the token, in a `_Secret`. An interruption while they are made
    is raised from here, without the frames below, where they are plain text."""
    failure: BaseException
    try:
        return _make_token_forms(secret)
    except BaseException as error:
        failure = _detached(error)
    # Raised outside the handler, so its own traceback is not chained to it. An exception a
    # caller is handling when this is called still becomes its context, as for any raise.
    raise failure


def _make_token_forms(secret: _Secret) -> tuple[_Secret, ...]:
    # surrogatepass: a lone surrogate would make a plain encode() raise an error that holds
    # the token.
    raw = secret.value.encode("utf-8", "surrogatepass")
    texts = {
        secret.value,
        repr(secret.value)[1:-1],
        repr(secret.value + "'\"")[1:-4],
        json.dumps(secret.value)[1:-1],
        json.dumps(secret.value, ensure_ascii=False)[1:-1],
        secret.value.encode("unicode_escape").decode("ascii"),
        repr(raw)[2:-1],
        repr(raw + b"'\"")[2:-4],
    }
    texts.discard("")
    return tuple(map(_Secret, sorted(texts, key=len, reverse=True)))


# Attributes an exception can keep its data in outside `args` and `__dict__`.
_EXCEPTION_FIELDS = ("filename", "filename2", "strerror", "object")


def _holds_token(value: object, secret: _Secret) -> bool:
    """Whether `value` holds the token, as written or escaped (see `_token_forms`), in any
    text it carries: a str or bytes, the keys and values of a dict, the items of a list,
    tuple or set, every field of a protobuf message, singular, repeated or map, and an
    exception's text, arguments and attributes. Text is read as it is held, decoded, not
    only as str() or repr() shows it, since those escape a token with a backslash, newline
    or tab.

    Raises no error of its own: something that cannot be read is taken to hold the token.
    An interruption during the scan is raised from here, without the frames below, which
    can hold the forms of the token or what was being read."""
    failure: BaseException
    try:
        return _scan_for_token(value, secret)
    except BaseException as error:
        failure = _detached(error)
    # Raised outside the handler, so its own traceback is not chained to it. An exception a
    # caller is handling when this is called still becomes its context, as for any raise.
    raise failure


def _scan_for_token(value: object, secret: _Secret) -> bool:
    forms = _token_forms(secret)
    # Every object scanned is kept here, not only its id(), until the scan ends: the upb
    # protobuf backend makes a new wrapper for a sub-message on each access, and a freed
    # wrapper's id() can be reused by the next, which would then be skipped.
    seen: dict[int, object] = {}
    pending: list[object] = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, bytes | bytearray):
            item = bytes(item).decode("utf-8", "replace")
        if isinstance(item, str):
            for form in forms:
                if form.value in item:
                    return True
            continue
        if id(item) in seen:
            continue
        seen[id(item)] = item
        try:
            if isinstance(item, dict):
                pending.extend(item.keys())
                pending.extend(item.values())
            elif isinstance(item, list | tuple | set | frozenset):
                pending.extend(item)
            elif isinstance(item, Message):
                for field, field_value in item.ListFields():
                    entry = field.message_type
                    if entry is not None and entry.GetOptions().map_entry:
                        pending.extend(field_value.keys())
                        pending.extend(field_value.values())
                    elif field.is_repeated:
                        # Known from the descriptor: upb's repeated containers have no
                        # __iter__, though they iterate through the sequence protocol.
                        pending.extend(field_value)
                    else:
                        pending.append(field_value)
            elif isinstance(item, BaseException):
                pending.extend((str(item), repr(item)))
                pending.extend(item.args)
                pending.extend(getattr(item, "__dict__", {}).values())
                pending.extend(getattr(item, name, None) for name in _EXCEPTION_FIELDS)
        except Exception:
            # Something that cannot even be looked at is taken to hold the token.
            return True
    return False


def _redact(text: str, secret: _Secret) -> str:
    for form in _token_forms(secret):
        text = text.replace(form.value, repr(secret))
    return text


def _without_token(error: BaseException, secret: _Secret) -> BaseException | None:
    """None if `error` and its chain never mention the token, else a replacement that does not.

    The exchange is not expected to echo a token back, but if a message from it did, it
    would otherwise reach an exception message. A rejection keeps its reason and detail,
    with the token redacted from them; any other error keeps only its type, since its text
    may hold the token in a form a redaction would not find.
    """
    seen: set[int] = set()
    link: BaseException | None = error
    while link is not None and id(link) not in seen:
        seen.add(id(link))
        if _holds_token(link, secret):
            break
        link = link.__cause__ or link.__context__
    else:
        return None
    if isinstance(error, SessionRejected):
        detail = None if error.detail is None else _redact(error.detail, secret)
        name = _redact(error.reason_name, secret)
        return type(error)(error.reason_code, detail, reason_name=name)
    withheld = f"{type(error).__name__}; details withheld, since they repeated the token"
    if isinstance(error, SessionNotAcknowledged):
        return type(error)(withheld, close_code=error.close_code)
    return SessionNotAcknowledged(withheld, close_code=_received_close_code(error))
