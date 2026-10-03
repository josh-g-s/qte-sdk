import asyncio
import builtins
import dataclasses
import json
import logging
import os
import secrets
import sys
import traceback
from collections.abc import Iterator
from pathlib import Path

import pytest
from fake_exchange import CONTRACT_VERSION, frame, serve_local, silent_server
from websockets.asyncio.client import ClientConnection
from websockets.asyncio.server import ServerConnection

import qte_sdk.session
from qte_sdk.connection import Connection, ContractVersionMismatch, Received, SessionRejected
from qte_sdk.contract.v1.common_pb2 import ReasonCodes
from qte_sdk.contract.v1.market_data_pb2 import Book
from qte_sdk.contract.v1.session_pb2 import Auth, Subscribe
from qte_sdk.session import (
    TOKEN_ENV_VAR,
    TOKEN_FILE_ENV_VAR,
    MissingToken,
    SessionInfo,
    SessionNotAcknowledged,
    open_session,
    resolve_token,
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
    monkeypatch.delenv(TOKEN_FILE_ENV_VAR, raising=False)


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


async def test_iterating_the_session_delivers_events_around_the_ack_but_no_heartbeat():
    book = frame("book", {"instrument": "AAPL", "grid_time": "2", "bid_levels": []}, 2)
    heartbeat = frame("heartbeat", {})
    server = Server(heartbeat, ack(), book, hold_open=False)
    async with serve_local(server) as url:
        async with await open_session(url, synthetic_token()) as session:
            events = [event async for event in session]
    # The heartbeat is absorbed, not delivered.
    assert events == [Received("book", Book(instrument="AAPL", grid_time=2), 2)]


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


# The token from a file named by QTE_TOKEN_FILE


def token_file(tmp_path: Path, content: bytes) -> Path:
    path = tmp_path / "token"
    path.write_bytes(content)
    return path


@pytest.mark.parametrize("ending", ["", "\n", "\r\n"])
def test_the_token_is_read_from_the_file_without_one_trailing_newline(
    monkeypatch, tmp_path, ending: str
):
    token = synthetic_token()
    monkeypatch.setenv(TOKEN_FILE_ENV_VAR, str(token_file(tmp_path, (token + ending).encode())))
    assert resolve_token() == token


def test_only_one_trailing_newline_is_removed(monkeypatch, tmp_path):
    token = synthetic_token()
    monkeypatch.setenv(TOKEN_FILE_ENV_VAR, str(token_file(tmp_path, (token + "\n\n").encode())))
    assert resolve_token() == token + "\n"


def test_an_empty_qte_token_falls_through_to_the_file(monkeypatch, tmp_path):
    token = synthetic_token()
    monkeypatch.setenv(TOKEN_ENV_VAR, "")
    monkeypatch.setenv(TOKEN_FILE_ENV_VAR, str(token_file(tmp_path, token.encode())))
    assert resolve_token() == token


async def test_a_session_opens_with_the_token_from_the_file(monkeypatch, tmp_path):
    token = synthetic_token()
    monkeypatch.setenv(TOKEN_FILE_ENV_VAR, str(token_file(tmp_path, (token + "\n").encode())))
    server = Server(ack())
    async with serve_local(server) as url:
        async with await open_session(url):
            pass
    assert server.received[0]["payload"] == {"token": token}


@pytest.mark.parametrize("source", ["argument", "empty argument", "environment"])
def test_the_file_is_never_opened_when_a_higher_source_is_present(
    monkeypatch, tmp_path, source: str
):
    path = str(token_file(tmp_path, synthetic_token().encode()))
    monkeypatch.setenv(TOKEN_FILE_ENV_VAR, path)
    opened: list[object] = []
    real_open = builtins.open

    def watching_open(file, *args, **kwargs):
        opened.append(file)
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", watching_open)
    token = synthetic_token()
    if source == "argument":
        assert resolve_token(token) == token
    elif source == "empty argument":
        with pytest.raises(MissingToken):
            resolve_token("")
    else:
        monkeypatch.setenv(TOKEN_ENV_VAR, token)
        assert resolve_token() == token
    assert path not in opened


def file_problem(tmp_path: Path, case: str, token: str) -> str:
    """Set up the file for one failure `case`, holding `token` where it can, and return the
    path to put in QTE_TOKEN_FILE."""
    if case == "missing":
        return str(tmp_path / "absent")
    if case == "directory":
        return str(tmp_path)
    if case == "unreadable":
        path = token_file(tmp_path, token.encode())
        path.chmod(0)
        return str(path)
    if case == "not utf-8":
        return str(token_file(tmp_path, b"\xff" + token.encode() + b"\n"))
    if case == "invalid utf-8 after the token":
        return str(token_file(tmp_path, token.encode() + b"\xc3\x28\n"))
    contents = {"empty": b"", "newline only": b"\n", "whitespace only": b" \t\r\n \n"}[case]
    return str(token_file(tmp_path, contents))


FILE_PROBLEMS = [
    "missing",
    "directory",
    "unreadable",
    "not utf-8",
    "invalid utf-8 after the token",
    "empty",
    "newline only",
    "whitespace only",
]


@pytest.mark.parametrize("case", FILE_PROBLEMS)
async def test_a_bad_token_file_raises_missing_token_without_its_contents(
    monkeypatch, tmp_path, case: str
):
    if case == "unreadable" and (sys.platform == "win32" or os.geteuid() == 0):
        pytest.skip("file permissions do not stop this user reading the file")
    token = synthetic_token()
    monkeypatch.setenv(TOKEN_FILE_ENV_VAR, file_problem(tmp_path, case, token))
    server = Server(ack())
    async with serve_local(server) as url:
        with pytest.raises(MissingToken, match=TOKEN_FILE_ENV_VAR) as caught:
            await open_session(url)
    assert server.connections == 0
    error = caught.value
    assert error.__cause__ is None and error.__context__ is None
    assert error.__suppress_context__ is False
    assert vars(error) == {}
    assert_token_absent(token, shown(error))


def test_a_token_mistakenly_given_as_the_file_path_is_not_shown(monkeypatch):
    token = synthetic_token()
    monkeypatch.setenv(TOKEN_FILE_ENV_VAR, token)
    with pytest.raises(MissingToken, match=TOKEN_FILE_ENV_VAR) as caught:
        resolve_token()
    assert_token_absent(token, shown(caught.value))


def test_reading_the_token_file_logs_nothing(monkeypatch, tmp_path, caplog):
    token = synthetic_token()
    monkeypatch.setenv(TOKEN_FILE_ENV_VAR, str(token_file(tmp_path, token.encode())))
    with caplog.at_level(logging.DEBUG):
        assert resolve_token() == token
        monkeypatch.setenv(TOKEN_FILE_ENV_VAR, file_problem(tmp_path, "not utf-8", token))
        with pytest.raises(MissingToken):
            resolve_token()
    assert caplog.records == []


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


async def test_an_abnormal_close_before_the_ack_is_an_error():
    async def handler(ws: ServerConnection) -> None:
        await ws.recv()
        await ws.close(1011, "internal error")

    async with serve_local(handler) as url:
        with pytest.raises(SessionNotAcknowledged, match="close code 1011"):
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


async def test_a_stalled_auth_send_times_out_and_closes_the_connection(monkeypatch):
    token = synthetic_token()

    async def stalled_send(self: ClientConnection, message: object) -> None:
        await asyncio.Event().wait()  # as if the socket never drains

    closed = asyncio.Event()

    async def idle(ws: ServerConnection) -> None:
        await ws.wait_closed()
        closed.set()

    monkeypatch.setattr(ClientConnection, "send", stalled_send)
    async with serve_local(idle) as url:
        started = asyncio.get_running_loop().time()
        with pytest.raises(TimeoutError) as caught:
            await asyncio.wait_for(open_session(url, token, ack_timeout=0.2), 5)
        elapsed = asyncio.get_running_loop().time() - started
        await asyncio.wait_for(closed.wait(), 5)
    assert elapsed < 2
    assert caught.value.__cause__ is None and caught.value.__context__ is None
    assert_token_absent(token, shown(caught.value))


async def test_the_timeout_also_bounds_the_opening_handshake():
    token = synthetic_token()
    async with silent_server() as url:
        started = asyncio.get_running_loop().time()
        with pytest.raises(TimeoutError) as caught:
            # The handshake's own limit is longer, so only the session's limit can end it.
            await asyncio.wait_for(open_session(url, token, ack_timeout=0.2, open_timeout=10), 5)
        elapsed = asyncio.get_running_loop().time() - started
    assert elapsed < 2
    assert caught.value.__cause__ is None and caught.value.__context__ is None
    assert_token_absent(token, shown(caught.value))


async def test_a_session_timeout_withholds_the_token_and_the_chain():
    token = synthetic_token()
    async with serve_local(Server()) as url:
        with pytest.raises(TimeoutError) as caught:
            await open_session(url, token, ack_timeout=0.2)
    assert "0.2" in str(caught.value)
    assert caught.value.__cause__ is None and caught.value.__context__ is None
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


async def test_a_reject_before_the_ack_keeps_a_reason_name_from_a_newer_contract():
    server = Server(frame("reject", {"reason_code": "A_REASON_FROM_A_NEWER_CONTRACT"}, 1))
    async with serve_local(server) as url:
        with pytest.raises(SessionRejected) as caught:
            await open_session(url, synthetic_token())
    assert caught.value.reason_code == ReasonCodes.REASON_CODE_UNSPECIFIED
    assert caught.value.reason_name == "A_REASON_FROM_A_NEWER_CONTRACT"


async def test_withholding_an_echoed_token_keeps_a_reason_name_from_a_newer_contract():
    token = synthetic_token()
    reject = session_reject("A_REASON_FROM_A_NEWER_CONTRACT", f"bad token {token}")
    async with serve_local(Server(reject)) as url:
        with pytest.raises(SessionRejected) as caught:
            await open_session(url, token)
    assert caught.value.reason_code == ReasonCodes.REASON_CODE_UNSPECIFIED
    assert caught.value.reason_name == "A_REASON_FROM_A_NEWER_CONTRACT"
    assert caught.value.detail == "bad token <token withheld>"
    assert_token_absent(token, shown(caught.value))


async def test_a_token_echoed_as_the_reason_name_is_withheld():
    token = synthetic_token()
    async with serve_local(Server(session_reject(token))) as url:
        with pytest.raises(SessionRejected) as caught:
            await open_session(url, token)
    assert caught.value.reason_name == "<token withheld>"
    assert_token_absent(token, shown(caught.value))


async def test_cancelling_while_a_failed_open_closes_waits_for_the_close(monkeypatch):
    closes: list[str] = []
    real_close = Connection.close

    async def slow_close(self: Connection) -> None:
        closes.append("started")
        await asyncio.sleep(0.1)
        await real_close(self)
        closes.append("finished")

    monkeypatch.setattr(Connection, "close", slow_close)
    async with serve_local(Server()) as url:
        task = asyncio.create_task(open_session(url, synthetic_token(), ack_timeout=0.05))
        while not closes:  # the ack timed out and the connection is closing
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert closes == ["started", "finished"]


# Sending on the session


async def test_the_session_sends_on_its_connection():
    received: list[dict] = []

    async def handler(ws: ServerConnection) -> None:
        received.append(json.loads(await ws.recv()))
        await ws.send(ack())
        received.append(json.loads(await ws.recv()))
        await ws.close()

    async with serve_local(handler) as url:
        async with await open_session(url, synthetic_token()) as session:
            await session.send("subscribe", Subscribe(instruments=["AAPL"]))
            assert [event async for event in session] == []
    assert received[1] == {
        "version": CONTRACT_VERSION,
        "type": "subscribe",
        "payload": {"instruments": ["AAPL"]},
    }


async def test_a_send_on_a_closed_session_fails_as_on_its_connection():
    async with serve_local(Server(ack())) as url:
        session = await open_session(url, synthetic_token())
        await session.close()
        with pytest.raises(Exception) as on_connection:
            await session.connection.send("subscribe", Subscribe(instruments=["AAPL"]))
        with pytest.raises(type(on_connection.value)):
            await session.send("subscribe", Subscribe(instruments=["AAPL"]))


async def test_a_failed_send_on_the_session_keeps_the_message_out_of_the_traceback():
    token = synthetic_token()
    async with serve_local(Server(ack())) as url:
        session = await open_session(url, synthetic_token())
        await session.close()
        with pytest.raises(Exception) as caught:
            await session.send("auth", Auth(token=token))
    assert_token_absent(token, shown(caught.value))


# A token that str() and repr() write escaped: one with a backslash, newline or tab

escaping = pytest.mark.parametrize(
    "special", ["\\", "\n", "\t"], ids=["backslash", "newline", "tab"]
)


def assert_no_form_of(token: str, text: str) -> None:
    assert_token_absent(token, text)
    for form in qte_sdk.session._token_forms(qte_sdk.session._Secret(token)):
        assert form.value not in text


@escaping
@pytest.mark.parametrize(
    "make",
    [
        KeyError,  # whose str() is the repr of its key
        lambda token: OSError(2, "No such file", token),  # the filename shown as a repr
        lambda token: ValueError(f"bad value {token!r}"),  # text that quotes it escaped
    ],
    ids=["key-error", "os-error-filename", "quoted-escaped"],
)
def test_an_error_holding_a_token_that_str_escapes_is_replaced(special, make):
    token = synthetic_token() + special + synthetic_token()
    safe = qte_sdk.session._without_token(make(token), qte_sdk.session._Secret(token))
    assert isinstance(safe, SessionNotAcknowledged)
    assert_no_form_of(token, shown(safe))


@escaping
def test_a_rejection_quoting_the_token_escaped_is_redacted(special):
    token = synthetic_token() + special + synthetic_token()
    rejected = SessionRejected(ReasonCodes.NOT_AUTHENTICATED, f"bad token {token!r}")
    safe = qte_sdk.session._without_token(rejected, qte_sdk.session._Secret(token))
    assert isinstance(safe, SessionRejected)
    assert safe.detail == "bad token '<token withheld>'"
    assert_no_form_of(token, shown(safe))


def test_a_token_that_is_not_valid_unicode_is_still_found_and_withheld():
    # A lone surrogate, as from an environment variable holding bytes that are not UTF-8:
    # looking for the token must not itself raise an error that holds it.
    token = synthetic_token() + "\udc80" + synthetic_token()
    secret = qte_sdk.session._Secret(token)
    error = UnicodeEncodeError("utf-8", f"auth {token}", 50, 51, "surrogates not allowed")
    assert qte_sdk.session._holds_token(error, secret)
    safe = qte_sdk.session._without_token(error, secret)
    assert isinstance(safe, SessionNotAcknowledged)
    assert_no_form_of(token, shown(safe))


async def test_auth_with_a_token_that_is_not_valid_unicode_fails_without_showing_it():
    token = synthetic_token() + "\udc80" + synthetic_token()
    async with serve_local(Server(ack())) as url:
        with pytest.raises(qte_sdk.session.AuthNotSent) as caught:
            await open_session(url, token)
    assert_no_form_of(token, shown(caught.value))


@escaping
async def test_auth_that_cannot_be_sent_withholds_a_token_it_quotes_escaped(special):
    token = synthetic_token() + special + synthetic_token()

    class Refusing:
        async def send(self, type_: str, payload: Auth) -> None:
            raise ValueError(f"cannot encode {payload.token!r}")

    with pytest.raises(qte_sdk.session.AuthNotSent) as caught:
        await qte_sdk.session._send_auth(Refusing(), qte_sdk.session._Secret(token))  # type: ignore[arg-type]
    assert_no_form_of(token, shown(caught.value))
