"""`QTE-SESSION-DISCONNECTED`: the WARNING a `ReconnectingSession` logs once for each
`Disconnected` it delivers, with `describe_close` of the close (#374). Every expected value
is written by hand."""

import asyncio
import json
import logging
import os
import threading
from collections.abc import Awaitable, Callable

import pytest
from fake_exchange import drop_connection, serve_local
from test_reconnect import EMPTY, Clock
from test_session import ack, synthetic_token
from test_unanswered import Exchange, accepted, two_news_and_a_cancel
from websockets.asyncio.server import ServerConnection
from websockets.exceptions import ConnectionClosedError
from websockets.frames import Close

from qte_sdk import errors, logs, pacing, reconnect
from qte_sdk.connection import (
    _CLOSE_CAUSES,
    Connected,
    Disconnected,
    LivenessTimeout,
    ResumeComplete,
    SessionRejected,
)
from qte_sdk.reconnect import Backoff, ReconnectingSession, Retrying
from qte_sdk.session import ResumeNotAcknowledged, SessionNotAcknowledged, open_session

LOG = "00000000000000aa"
NO_CLOSE_FRAME = "close code 1006: the connection dropped with no close frame from the exchange"
WAIT = "Wait for ResumeComplete, then check positions with send_account_query"
NEW_TOKEN = "Get a new token from the console; this session does not reconnect"
NO_RECONNECT = "Check positions with send_account_query; this session does not reconnect: "


def close_of(fields: dict) -> str:
    """The whole describe_close line a record's fields carry, in two parts."""
    return fields["close"] + fields["cause"]


def disconnect_records(caplog: pytest.LogCaptureFixture | None) -> list[logging.LogRecord]:
    for thread in threading.enumerate():
        if thread.name == "qte-sdk pacing warning":
            thread.join(5)
    if caplog is None:
        return []
    return [r for r in caplog.records if getattr(r, "code", None) == errors.SESSION_DISCONNECTED]


Ending = Callable[[ServerConnection], Awaitable[None]]


def closing(code: int, reason: str = "") -> Ending:
    async def end(ws: ServerConnection) -> None:
        await ws.close(code, reason)

    return end


class Ends:
    """Acks each connection and answers its resume with the empty marker, then ends the
    first connections with `endings`, one each, and holds every later one open."""

    def __init__(self, *endings: Ending) -> None:
        self.endings = endings
        self.connections = 0

    async def __call__(self, ws: ServerConnection) -> None:
        n = self.connections
        self.connections += 1
        await ws.recv()  # auth
        await ws.send(ack(log_id=LOG))
        await ws.recv()  # resume
        await ws.send(EMPTY)
        if n < len(self.endings):
            await self.endings[n](ws)
            return
        await ws.wait_closed()


async def run(handler: Callable[[ServerConnection], Awaitable[None]], count: int) -> list:
    """The events of a ReconnectingSession against `handler`, up to its `count`th
    Disconnected and the ResumeComplete after it."""
    seen: list = []
    drops = 0
    async with serve_local(handler) as url:
        rs = ReconnectingSession(url, synthetic_token(), sleep=Clock().sleep)
        async with rs, asyncio.timeout(10):
            async for event in rs:
                seen.append(event)
                if isinstance(event, Disconnected):
                    drops += 1
                elif isinstance(event, ResumeComplete) and drops == count:
                    break
    return seen


async def test_a_1012_and_a_dropped_connection_are_each_logged_once(caplog):
    with caplog.at_level(logging.WARNING, logger="qte_sdk.reconnect"):
        events = await run(Ends(closing(1012), drop_connection), 2)
        records = disconnect_records(caplog)
    assert sum(isinstance(e, Disconnected) for e in events) == 2
    assert [r.name for r in records] == ["qte_sdk.reconnect"] * 2
    assert {r.levelno for r in records} == {logging.WARNING}
    assert [r.fields for r in records] == [
        {"step": WAIT, "close": "close code 1012", "cause": "", "unanswered": ""},
        {"step": WAIT, "close": "close code 1006", "cause": NO_CLOSE_FRAME[15:], "unanswered": ""},
    ]
    assert records[0].getMessage() == (
        "QTE-SESSION-DISCONNECTED: the session ended, close code 1012. Messages sent while "
        "disconnected may be missed: market data is not recovered, and your order reports "
        "only by a resume. Wait for ResumeComplete, then check positions with "
        "send_account_query."
    )
    dropped = records[1].getMessage()
    assert dropped.startswith(f"QTE-SESSION-DISCONNECTED: the session ended, {NO_CLOSE_FRAME}. ")


async def test_a_normal_close_names_its_code_and_known_reason(caplog):
    with caplog.at_level(logging.WARNING, logger="qte_sdk.reconnect"):
        events = await run(Ends(closing(1001, "exchange stopping")), 1)
        records = disconnect_records(caplog)
    [disconnected] = [e for e in events if isinstance(e, Disconnected)]
    assert disconnected.error is None  # 1001 ends the iteration without raising
    [record] = records
    assert close_of(record.fields) == (
        'close code 1001, reason "exchange stopping": the exchange is stopping or switching; '
        "a ReconnectingSession connects again, and after a switch that normally succeeds "
        "within seconds"
    )


async def test_a_reason_the_sdk_does_not_show_is_left_out(caplog):
    with caplog.at_level(logging.WARNING, logger="qte_sdk.reconnect"):
        await run(Ends(closing(1011, "free text from the server")), 1)
        [record] = disconnect_records(caplog)
    assert close_of(record.fields) == "close code 1011: an error on the exchange"
    assert "free text" not in record.getMessage()


async def test_unanswered_requests_are_counted_not_listed(caplog):
    with caplog.at_level(logging.WARNING, logger="qte_sdk.reconnect"):
        seen: list[Disconnected] = []
        exchange = Exchange(3, [accepted("n2")])
        async with serve_local(exchange) as url:
            rs = ReconnectingSession(url, synthetic_token(), sleep=Clock().sleep)
            async with rs, asyncio.timeout(10):
                async for event in rs:
                    if isinstance(event, ResumeComplete) and exchange.connections == 1:
                        await two_news_and_a_cancel(rs)
                    elif isinstance(event, Disconnected):
                        seen.append(event)
                        break
        [record] = disconnect_records(caplog)
    assert seen[0].unanswered == (("new", "n1"), ("cancel", "c1"))
    assert close_of(record.fields) == NO_CLOSE_FRAME
    assert record.fields == {
        "step": WAIT,
        "close": "close code 1006",
        "cause": NO_CLOSE_FRAME[15:],
        "unanswered": "; 2 unanswered requests (QTE-SESSION-UNANSWERED)",
    }
    # The refs are QTE-SESSION-UNANSWERED's to name, not this line's.
    assert "n1" not in record.getMessage() and "c1" not in record.getMessage()


async def test_retries_are_not_logged_again(caplog):
    # Each later connection drops before session_ack: retried, with no Disconnected.
    class Flaky(Ends):
        async def __call__(self, ws: ServerConnection) -> None:
            if 1 <= self.connections < 4:
                self.connections += 1
                await ws.recv()
                await drop_connection(ws)
                return
            await super().__call__(ws)

    with caplog.at_level(logging.WARNING, logger="qte_sdk.reconnect"):
        events = await run(Flaky(drop_connection), 1)
        records = disconnect_records(caplog)
    assert sum(isinstance(e, Retrying) for e in events) == 4
    assert sum(isinstance(e, Disconnected) for e in events) == 1
    assert len(records) == 1


async def test_close_logs_nothing(caplog):
    with caplog.at_level(logging.WARNING, logger="qte_sdk.reconnect"):
        async with serve_local(Ends()) as url:
            rs = ReconnectingSession(url, synthetic_token(), sleep=Clock().sleep)
            async with rs, asyncio.timeout(10):
                async for event in rs:
                    if isinstance(event, Connected):
                        await rs.close()
        assert disconnect_records(caplog) == []


async def test_a_json_line_carries_the_close_as_a_field(caplog):
    with caplog.at_level(logging.WARNING, logger="qte_sdk.reconnect"):
        await run(Ends(closing(1012)), 1)
        [record] = disconnect_records(caplog)
    line = json.loads(logs.JsonFormatter().format(record))
    assert line["code"] == "QTE-SESSION-DISCONNECTED"
    assert line["message"].startswith("the session ended, close code 1012. Messages sent ")
    assert line["fields"] == {
        "step": WAIT,
        "close": "close code 1012",
        "cause": "",
        "unanswered": "",
    }


class EndsWhileResuming:
    """Acks each connection, then ends the first ones with `endings`, one each, before
    answering their resume; answers every later resume and holds it open."""

    def __init__(self, *endings: Ending) -> None:
        self.endings = endings
        self.connections = 0

    async def __call__(self, ws: ServerConnection) -> None:
        n = self.connections
        self.connections += 1
        await ws.recv()  # auth
        await ws.send(ack(log_id=LOG))
        await ws.recv()  # resume
        if n < len(self.endings):
            await self.endings[n](ws)
            return
        await ws.send(EMPTY)
        await ws.wait_closed()


async def test_a_close_with_no_code_kept_while_resuming_still_names_its_close(caplog):
    # A 1001 close or a drop before resume_ack is a ResumeNotAcknowledged with no
    # close_code: the line takes the close from the connection instead.
    with caplog.at_level(logging.WARNING, logger="qte_sdk.reconnect"):
        events = await run(
            EndsWhileResuming(closing(1001, "exchange stopping"), drop_connection), 2
        )
        records = disconnect_records(caplog)
    errors_seen = [e.error for e in events if isinstance(e, Disconnected)]
    assert [type(e) for e in errors_seen] == [ResumeNotAcknowledged] * 2
    assert [e.close_code for e in errors_seen] == [None, None]
    assert [close_of(r.fields) for r in records] == [
        'ResumeNotAcknowledged, close code 1001, reason "exchange stopping": the exchange is '
        "stopping or switching; a ReconnectingSession connects again, and after a switch "
        "that normally succeeds within seconds",
        f"ResumeNotAcknowledged, {NO_CLOSE_FRAME}",
    ]


def emitted(
    monkeypatch: pytest.MonkeyPatch,
    disconnected: Disconnected,
    no_reconnect: str | None = None,
    *,
    resume: bool = True,
) -> dict:
    """What `_log_disconnected` hands the warning thread, caught before it starts."""
    caught: list[dict] = []
    monkeypatch.setattr(
        pacing, "_emit", lambda message, code, **rest: caught.append(dict(rest, message=message))
    )
    rs = ReconnectingSession("wss://example.invalid", synthetic_token(), resume=resume)
    rs._log_disconnected(disconnected, None, no_reconnect)
    [one] = caught
    return one


def test_a_close_while_resuming_keeps_its_code_and_a_revoked_token(monkeypatch):
    failure = ResumeNotAcknowledged("closed", close_code=1008, credential_revoked=True)
    warning = emitted(monkeypatch, Disconnected(failure))
    assert close_of(warning["fields"]) == (
        "ResumeNotAcknowledged, close code 1008, "
        'reason "credential revoked": your token was revoked or rotated; get a new token, '
        "and do not connect again with this one"
    )
    failure = ResumeNotAcknowledged("closed", close_code=1012)
    warning = emitted(monkeypatch, Disconnected(failure))
    assert close_of(warning["fields"]) == "ResumeNotAcknowledged, close code 1012"
    warning = emitted(monkeypatch, Disconnected(LivenessTimeout(10.0)))
    assert close_of(warning["fields"]) == "LivenessTimeout (QTE-CONNECT-LIVENESS-TIMEOUT)"


class Many(tuple):
    """An `unanswered` whose length is the largest count shown: 9 digits."""

    def __len__(self) -> int:
        return 999_999_999


def every_ending() -> list[tuple[Exception, ConnectionClosedError | None]]:
    """Every failure a Disconnected can carry, with the close the connection was seen to
    end with: none, or for a session that closed while connecting or resuming, the close
    it kept the code of, and the close it kept no code of."""
    endings: list[tuple[Exception, ConnectionClosedError | None]] = [
        (ConnectionClosedError(None, None), None),
        (LivenessTimeout(3600.0), None),
    ]
    for code, causes in _CLOSE_CAUSES.items():
        for reason in causes:
            closed = ConnectionClosedError(Close(code, reason or ""), None)
            endings.append((closed, None))
            revoked = reason == "credential revoked"
            for cls in (SessionNotAcknowledged, ResumeNotAcknowledged):
                endings.append((cls("closed", close_code=code, credential_revoked=revoked), None))
                endings.append((cls("closed", close_code=code, credential_revoked=revoked), closed))
                endings.append((cls("closed"), closed))
    return endings


# What follows each ending: a reconnect, a revoked token, or the longest other reason.
GAVE_UP = "it gave up after 999999999 failed attempts (Backoff.max_attempts)"
AFTERWARDS = [(None, WAIT), (reconnect._REVOKED, NEW_TOKEN), (GAVE_UP, NO_RECONNECT + GAVE_UP)]


@pytest.mark.parametrize("form", ["json", "%(message)s", logging.BASIC_FORMAT])
def test_every_ending_reaches_a_real_pipe_in_one_line_with_its_close_and_count(monkeypatch, form):
    # A pipe takes at most 512 bytes in one write (see qte_sdk.update): a longer line is
    # written in its short form there, which must fit for every close and next step.
    from test_update import json_handler, plain_handler, read_all

    cases = [
        (ending, closed, after, step)
        for ending, closed in every_ending()
        for after, step in AFTERWARDS
    ]
    read, write = os.pipe()
    stream = open(write, "w", encoding="utf-8")  # noqa: SIM115
    handler = json_handler(stream) if form == "json" else plain_handler(stream, form)
    logger = logging.getLogger("qte_sdk.reconnect")
    monkeypatch.setattr(logger, "propagate", False)
    logger.addHandler(handler)
    rs = ReconnectingSession("wss://example.invalid", synthetic_token())
    data = b""
    try:
        for ending, closed, after, _ in cases:
            rs._log_disconnected(Disconnected(ending, Many()), closed, after)
            disconnect_records(None)
            # Read after each one, so the pipe never fills and the lines keep their order.
            data += read_all(read)
    finally:
        logger.removeHandler(handler)
        stream.close()
        os.close(read)
    lines = data.splitlines(keepends=True)
    assert len(lines) == len(cases), data
    count = "; 999999999 unanswered requests (QTE-SESSION-UNANSWERED)"
    for line, (_, _, _, step) in zip(lines, cases, strict=True):
        assert len(line) <= 512, (len(line), line)
        assert count.encode() in line
        if form != "json":  # a JSON line gives the step as next_step, checked below
            assert f". {step}.".encode() in line, line
    if form == "json":
        parsed = [json.loads(line) for line in lines]
        assert {p["code"] for p in parsed} == {errors.SESSION_DISCONNECTED}
        assert parsed[0]["fields"]["close"] == "close code 1006"
        assert all(set(p["fields"]) == {"close"} for p in parsed)  # every one written short
        assert all(p["next_step"] == step for p, (*_, step) in zip(parsed, cases, strict=True))
        revoked = [p for p in parsed if p["fields"]["close"].endswith('"credential revoked"')]
        # A close, then each of 2 types with its code, with its code and close, and with
        # the close only; each with 3 next steps.
        assert len(revoked) == 7 * 3
        # The field keeps a long close's reason, which the message leaves out: a failure's
        # type and its close (4 of each 3 next steps).
        long = [
            p
            for p in parsed
            if p["fields"]["close"].startswith(("SessionNotAck", "ResumeNotAck"))
            and p["fields"]["close"].endswith('"too many link_drain messages"')
        ]
        assert len(long) == 4 * 3
        assert all("link_drain" not in p["message"] for p in long)


def step_of(records: list[logging.LogRecord]) -> list[str]:
    return [r.next_step for r in records]


async def test_a_revoked_close_says_get_a_new_token(caplog):
    with caplog.at_level(logging.WARNING, logger="qte_sdk.reconnect"):
        with pytest.raises(ConnectionClosedError):
            await run(Ends(closing(1008, "credential revoked")), 1)
        records = disconnect_records(caplog)
    assert step_of(records) == [NEW_TOKEN]
    assert records[0].getMessage().endswith(f". {NEW_TOKEN}.")
    assert "ResumeComplete" not in records[0].getMessage()


async def test_a_revoked_close_while_resuming_says_get_a_new_token(caplog):
    with caplog.at_level(logging.WARNING, logger="qte_sdk.reconnect"):
        with pytest.raises(ResumeNotAcknowledged):
            await run(EndsWhileResuming(closing(1008, "credential revoked")), 1)
        records = disconnect_records(caplog)
    assert step_of(records) == [NEW_TOKEN]


async def test_a_close_while_resuming_keeps_its_known_reason(caplog):
    # The failure keeps only the code; the close the connection saw keeps the reason.
    with caplog.at_level(logging.WARNING, logger="qte_sdk.reconnect"):
        await run(EndsWhileResuming(closing(1008, "too many link_drain messages")), 1)
        [record] = disconnect_records(caplog)
    assert record.fields["close"] == (
        'ResumeNotAcknowledged, close code 1008, reason "too many link_drain messages"'
    )
    assert record.next_step == WAIT


async def drops_once(rs_options: dict, handler: Callable[[ServerConnection], Awaitable[None]]):
    """The events of one ReconnectingSession against `handler` until iteration ends."""
    seen: list = []
    async with serve_local(handler) as url:
        rs = ReconnectingSession(url, synthetic_token(), sleep=Clock().sleep, **rs_options)
        async with rs, asyncio.timeout(10):
            async for event in rs:
                seen.append(event)
    return seen


async def test_with_reconnecting_off_a_drop_says_no_reconnect_follows(caplog):
    with caplog.at_level(logging.WARNING, logger="qte_sdk.reconnect"):
        events = await drops_once({"backoff": None}, Ends(drop_connection))
        records = disconnect_records(caplog)
    assert isinstance(events[-1], Disconnected)
    assert step_of(records) == [NO_RECONNECT + "reconnecting is off (backoff=None)"]


async def test_with_reconnecting_off_a_close_while_resuming_says_no_reconnect_follows(caplog):
    with caplog.at_level(logging.WARNING, logger="qte_sdk.reconnect"):
        with pytest.raises(ResumeNotAcknowledged):
            await drops_once({"backoff": None}, EndsWhileResuming(closing(1012)))
        records = disconnect_records(caplog)
    assert step_of(records) == [NO_RECONNECT + "reconnecting is off (backoff=None)"]


async def test_giving_up_says_no_reconnect_follows(caplog):
    backoff = Backoff(max_attempts=2)
    with caplog.at_level(logging.WARNING, logger="qte_sdk.reconnect"):
        with pytest.raises(ResumeNotAcknowledged):
            await drops_once({"backoff": backoff}, EndsWhileResuming(closing(1012), closing(1012)))
        records = disconnect_records(caplog)
    # The first attempt's failure is followed by a retry; the second gives up.
    assert step_of(records) == [
        WAIT,
        NO_RECONNECT + "it gave up after 2 failed attempts (Backoff.max_attempts)",
    ]


def test_a_failure_that_is_not_retryable_says_no_reconnect_follows(monkeypatch):
    rs = ReconnectingSession("wss://example.invalid", synthetic_token())
    rejected = SessionRejected(3, None)
    assert rs._why_no_reconnect(rejected, 1) == "the failure is not retryable"
    warning = emitted(monkeypatch, Disconnected(rejected), rs._why_no_reconnect(rejected, 1))
    assert warning["next_step"] == NO_RECONNECT + "the failure is not retryable"


def test_with_resuming_off_the_step_waits_for_connected(monkeypatch):
    warning = emitted(monkeypatch, Disconnected(None), resume=False)
    assert warning["next_step"] == (
        "Wait for Connected, then check positions with send_account_query"
    )


@pytest.mark.parametrize("ending", [drop_connection, closing(1012)])
async def test_open_session_logs_nothing(caplog, ending):
    async def handler(ws: ServerConnection) -> None:
        await ws.recv()  # auth
        await ws.send(ack(log_id=LOG))
        await ending(ws)

    with caplog.at_level(logging.DEBUG):
        async with serve_local(handler) as url:
            with pytest.raises(ConnectionClosedError):
                async with asyncio.timeout(10):
                    async with await open_session(url, synthetic_token()) as session:
                        async for _ in session:
                            pass
        assert disconnect_records(caplog) == []
