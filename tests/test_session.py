import asyncio
import dataclasses
import json
import logging
import secrets
import traceback
from collections.abc import Iterator

import pytest
from fake_exchange import CONTRACT_VERSION, frame, serve_local
from websockets.asyncio.client import ClientConnection
from websockets.asyncio.server import ServerConnection

from qte_sdk.connection import ContractVersionMismatch, Received, SessionRejected
from qte_sdk.contract.v1.common_pb2 import ReasonCodes
from qte_sdk.contract.v1.market_data_pb2 import Book
from qte_sdk.contract.v1.session_pb2 import Heartbeat
from qte_sdk.session import (
    TOKEN_ENV_VAR,
    MissingToken,
    SessionInfo,
    SessionNotAcknowledged,
    open_session,
)


def synthetic_token() -> str:
    return secrets.token_urlsafe(32)


def ack(unscored: bool = False) -> str:
    payload = {
        "session_id": "s-1",
        "team": "team-a",
        "server_time": "1700000000000000000",
        "contract_version": CONTRACT_VERSION,
        "unscored": unscored,
    }
    return frame("session_ack", payload, 1)


def session_reject(reason: str, detail: str | None = None) -> str:
    payload = {"reason_code": reason}
    if detail is not None:
        payload["reason_detail"] = detail
    return frame("session_reject", payload, 1)


class Server:
    """Records what each connection sends first, then answers with scripted frames."""

    def __init__(self, *replies: str, hold_open: bool = True) -> None:
        self.replies = replies
        self.hold_open = hold_open
        self.connections = 0
        self.received: list[dict] = []
        self.client_closed = asyncio.Event()

    async def __call__(self, ws: ServerConnection) -> None:
        self.connections += 1
        self.received.append(json.loads(await ws.recv()))
        for reply in self.replies:
            await ws.send(reply)
        if self.hold_open:
            await ws.wait_closed()
            self.client_closed.set()
        else:
            await ws.close()


@pytest.fixture(autouse=True)
def no_token_in_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(TOKEN_ENV_VAR, raising=False)


# Successful sessions


@pytest.mark.parametrize("unscored", [True, False])
async def test_an_acknowledged_session_exposes_every_ack_field(unscored: bool):
    token = synthetic_token()
    server = Server(ack(unscored=unscored))
    async with serve_local(server) as url:
        async with await open_session(url, token) as session:
            info = session.info
    assert server.received == [
        {"version": CONTRACT_VERSION, "type": "auth", "payload": {"token": token}}
    ]
    assert info == SessionInfo(
        session_id="s-1",
        team="team-a",
        server_time=1_700_000_000_000_000_000,
        contract_version=CONTRACT_VERSION,
        unscored=unscored,
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        info.unscored = not unscored  # type: ignore[misc]


async def test_the_token_is_read_from_the_environment_when_not_passed(monkeypatch):
    token = synthetic_token()
    monkeypatch.setenv(TOKEN_ENV_VAR, token)
    server = Server(ack())
    async with serve_local(server) as url:
        async with await open_session(url):
            pass
    assert server.received[0]["payload"] == {"token": token}


async def test_a_token_argument_takes_precedence_over_the_environment(monkeypatch):
    token = synthetic_token()
    monkeypatch.setenv(TOKEN_ENV_VAR, synthetic_token())
    server = Server(ack())
    async with serve_local(server) as url:
        async with await open_session(url, token):
            pass
    assert server.received[0]["payload"] == {"token": token}


async def test_iterating_the_session_delivers_events_from_before_and_after_the_ack():
    book = frame("book", {"instrument": "AAPL", "grid_time": "2", "bid_levels": []}, 2)
    heartbeat = frame("heartbeat", {})
    server = Server(heartbeat, ack(), book, hold_open=False)
    async with serve_local(server) as url:
        async with await open_session(url, synthetic_token()) as session:
            events = [event async for event in session]
    assert events == [
        Received("heartbeat", Heartbeat(), None),
        Received("book", Book(instrument="AAPL", grid_time=2), 2),
    ]


async def test_closing_the_session_closes_the_connection():
    server = Server(ack())
    async with serve_local(server) as url:
        async with await open_session(url, synthetic_token()):
            pass
        await asyncio.wait_for(server.client_closed.wait(), 5)


# A missing token


@pytest.mark.parametrize("env_value", [None, ""])
@pytest.mark.parametrize("argument", [None, ""])
async def test_a_missing_token_fails_before_any_connection_attempt(
    monkeypatch, env_value: str | None, argument: str | None
):
    if env_value is not None:
        monkeypatch.setenv(TOKEN_ENV_VAR, env_value)
    server = Server(ack())
    async with serve_local(server) as url:
        with pytest.raises(MissingToken, match=TOKEN_ENV_VAR):
            await open_session(url, argument)
    assert server.connections == 0


# Refused sessions


async def test_a_reject_surfaces_its_reason_code_and_closes_the_connection():
    token = synthetic_token()
    server = Server(session_reject("NOT_AUTHENTICATED", "unknown credentials"))
    async with serve_local(server) as url:
        with pytest.raises(SessionRejected) as caught:
            await open_session(url, token)
        await asyncio.wait_for(server.client_closed.wait(), 5)
    error = caught.value
    assert type(error) is SessionRejected
    assert error.reason_code == ReasonCodes.NOT_AUTHENTICATED
    assert error.detail == "unknown credentials"
    assert str(error) == "NOT_AUTHENTICATED: unknown credentials"
    assert token not in str(error) and token not in repr(error)


async def test_a_version_mismatch_is_raised_as_its_own_error():
    server = Server(session_reject("VERSION_MISMATCH", "contract version not served"))
    async with serve_local(server) as url:
        with pytest.raises(ContractVersionMismatch) as caught:
            await open_session(url, synthetic_token())
    assert caught.value.reason_code == ReasonCodes.VERSION_MISMATCH


async def test_a_reject_before_the_ack_refuses_the_session():
    reject = frame("reject", {"reason_code": "MALFORMED_MESSAGE", "reason_detail": "envelope"}, 1)
    server = Server(reject)
    async with serve_local(server) as url:
        with pytest.raises(SessionRejected) as caught:
            await open_session(url, synthetic_token())
        await asyncio.wait_for(server.client_closed.wait(), 5)
    assert caught.value.reason_code == ReasonCodes.MALFORMED_MESSAGE


async def test_a_connection_that_closes_before_the_ack_is_an_error():
    server = Server(hold_open=False)
    async with serve_local(server) as url:
        with pytest.raises(SessionNotAcknowledged):
            await open_session(url, synthetic_token())


async def test_an_undecodable_ack_is_an_error():
    bad = frame("session_ack", {"server_time": "not a number"}, 1)
    server = Server(bad)
    async with serve_local(server) as url:
        with pytest.raises(SessionNotAcknowledged):
            await open_session(url, synthetic_token())
        await asyncio.wait_for(server.client_closed.wait(), 5)


async def test_no_ack_within_the_timeout_is_an_error_and_closes_the_connection():
    server = Server()
    async with serve_local(server) as url:
        with pytest.raises(TimeoutError):
            await open_session(url, synthetic_token(), ack_timeout=0.2)
        await asyncio.wait_for(server.client_closed.wait(), 5)


# The token never reaches an exception, a log, stdout or stderr


def shown(error: BaseException) -> str:
    """What a traceback showing local variables could print for `error`, its causes and
    contexts (even suppressed ones), leaving out this test module's own frames, which hold
    the token by design."""
    parts = [str(error), repr(error)]
    pending = [traceback.TracebackException.from_exception(error, capture_locals=True)]
    seen: set[int] = set()
    while pending:
        link = pending.pop()
        if id(link) in seen:
            continue
        seen.add(id(link))
        parts.extend(link.format_exception_only())
        for summary in link.stack:
            if summary.filename != __file__:
                parts.append(f"{summary.filename}:{summary.lineno} {summary.locals}")
        pending.extend(n for n in (link.__cause__, link.__context__) if n is not None)
    return "\n".join(parts)


async def test_a_token_echoed_in_a_reject_is_withheld_and_the_reason_code_kept():
    token = synthetic_token()
    server = Server(session_reject("NOT_AUTHENTICATED", f"bad token {token}"))
    async with serve_local(server) as url:
        with pytest.raises(SessionRejected) as caught:
            await open_session(url, token)
    assert caught.value.reason_code == ReasonCodes.NOT_AUTHENTICATED
    assert caught.value.detail == "bad token <token withheld>"
    assert_token_absent(token, shown(caught.value))


async def test_a_token_echoed_in_a_close_reason_is_withheld():
    token = synthetic_token()

    async def handler(ws: ServerConnection) -> None:
        await ws.recv()
        await ws.close(4000, token)

    async with serve_local(handler) as url:
        with pytest.raises(SessionNotAcknowledged) as caught:
            await open_session(url, token)
    assert_token_absent(token, shown(caught.value))


@pytest.mark.parametrize("field", ["server_time", "session_id"])
async def test_a_token_echoed_in_an_undecodable_ack_is_withheld(field: str):
    token = synthetic_token()
    payload = {"session_id": "s-1", "server_time": "not a number", field: token}
    server = Server(frame("session_ack", payload, 1))
    async with serve_local(server) as url:
        with pytest.raises(SessionNotAcknowledged) as caught:
            await open_session(url, token)
    assert_token_absent(token, shown(caught.value))


async def test_cancelling_while_auth_is_sent_does_not_show_the_token(monkeypatch):
    token = synthetic_token()
    sending = asyncio.Event()

    async def stalled_send(self: ClientConnection, message: object) -> None:
        # The SDK's own frames above this one hold the auth message while it is sent.
        sending.set()
        await asyncio.Event().wait()

    async def idle(ws: ServerConnection) -> None:
        await ws.wait_closed()

    monkeypatch.setattr(ClientConnection, "send", stalled_send)
    async with serve_local(idle) as url:
        task = asyncio.create_task(open_session(url, token))
        await asyncio.wait_for(sending.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError) as caught:
            await task
    assert_token_absent(token, shown(caught.value))


async def test_a_failed_connection_attempt_does_not_show_the_token(monkeypatch):
    token = synthetic_token()
    monkeypatch.setenv(TOKEN_ENV_VAR, token)
    async with serve_local(Server()) as url:
        pass  # the server is gone, so its port refuses connections
    with pytest.raises(OSError) as caught:
        await open_session(url)
    assert_token_absent(token, shown(caught.value))


class Recorder(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.setFormatter(logging.Formatter("%(name)s %(levelname)s %(message)s"))
        self.records: list[logging.LogRecord] = []
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)
        self.lines.append(self.format(record))


@pytest.fixture
def every_logger_at_debug() -> Iterator[Recorder]:
    """Every logger, including `websockets`, at DEBUG and recorded from the root."""
    root = logging.getLogger()
    for name in ("websockets", "websockets.client", "websockets.server", "qte_sdk"):
        logging.getLogger(name)
    loggers = [root] + [
        logger
        for logger in logging.root.manager.loggerDict.values()
        if isinstance(logger, logging.Logger)
    ]
    saved = [(logger, logger.level, logger.disabled) for logger in loggers]
    recorder = Recorder()
    root.addHandler(recorder)
    for logger in loggers:
        logger.setLevel(logging.DEBUG)
        logger.disabled = False
    try:
        yield recorder
    finally:
        root.removeHandler(recorder)
        for logger, level, disabled in saved:
            logger.setLevel(level)
            logger.disabled = disabled


def assert_token_absent(token: str, text: str) -> None:
    # A frame logged by `websockets` is shown with its middle elided, so a leak can be
    # partial: check every run of 8 characters, not only the whole token.
    for start in range(len(token) - 7):
        assert token[start : start + 8] not in text


def client_side(recorder: Recorder) -> list[str]:
    # The fake exchange logs what it receives through `websockets.server`; that is the
    # server's log, not the SDK's.
    return [
        line
        for record, line in zip(recorder.records, recorder.lines, strict=True)
        if not record.name.startswith("websockets.server")
    ]


@pytest.mark.parametrize(
    "reply",
    [ack(), session_reject("NOT_AUTHENTICATED", "unknown credentials")],
    ids=["ack", "reject"],
)
async def test_the_token_is_never_logged_at_any_level(every_logger_at_debug, capsys, reply):
    token = synthetic_token()
    server = Server(reply)
    async with serve_local(server) as url:
        try:
            async with await open_session(url, token):
                pass
        except SessionRejected:
            pass
    lines = client_side(every_logger_at_debug)
    # Not vacuous: the client's `websockets` debug log was on and recorded something.
    assert any(line.startswith("websockets.client DEBUG") for line in lines)
    assert_token_absent(token, "\n".join(lines))
    captured = capsys.readouterr()
    assert_token_absent(token, captured.out + captured.err)


async def test_a_logger_the_caller_passes_does_not_log_frames_either(every_logger_at_debug):
    token = synthetic_token()
    mine = logging.getLogger("an_application.ws")
    mine.setLevel(logging.DEBUG)
    server = Server(ack())
    async with serve_local(server) as url:
        async with await open_session(url, token, logger=mine):
            pass
    lines = [
        line
        for record, line in zip(
            every_logger_at_debug.records, every_logger_at_debug.lines, strict=True
        )
        if record.name == "an_application.ws"
    ]
    assert lines
    assert_token_absent(token, "\n".join(lines))
