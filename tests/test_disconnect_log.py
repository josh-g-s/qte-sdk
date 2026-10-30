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

from qte_sdk import errors, logs, pacing
from qte_sdk.connection import (
    _CLOSE_CAUSES,
    Connected,
    Disconnected,
    LivenessTimeout,
    ResumeComplete,
)
from qte_sdk.reconnect import ReconnectingSession, Retrying
from qte_sdk.session import ResumeNotAcknowledged, SessionNotAcknowledged

LOG = "00000000000000aa"
NO_CLOSE_FRAME = "close code 1006: the connection dropped with no close frame from the exchange"


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
        {"close": "close code 1012", "cause": "", "unanswered": ""},
        {"close": "close code 1006", "cause": NO_CLOSE_FRAME[15:], "unanswered": ""},
    ]
    assert records[0].getMessage() == (
        "QTE-SESSION-DISCONNECTED: the session ended, close code 1012. Messages sent while "
        "disconnected may be missed: market data is not recovered, and your order reports "
        "only by a resume. Wait for ResumeComplete, then check positions with "
        "send_account_query; after a credential revoked close, get a new token."
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
    assert line["fields"] == {"close": "close code 1012", "cause": "", "unanswered": ""}


def emitted(monkeypatch: pytest.MonkeyPatch, disconnected: Disconnected) -> dict:
    """What `_log_disconnected` hands the warning thread, caught before it starts."""
    caught: list[dict] = []
    monkeypatch.setattr(
        pacing, "_emit", lambda message, code, **rest: caught.append(dict(rest, message=message))
    )
    ReconnectingSession("wss://example.invalid", synthetic_token())._log_disconnected(
        disconnected, None
    )
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


def every_ending() -> list[Exception]:
    endings: list[Exception] = [ConnectionClosedError(None, None), LivenessTimeout(3600.0)]
    for code, causes in _CLOSE_CAUSES.items():
        for reason in causes:
            endings.append(ConnectionClosedError(Close(code, reason or ""), None))
            revoked = reason == "credential revoked"
            for cls in (SessionNotAcknowledged, ResumeNotAcknowledged):
                endings.append(cls("closed", close_code=code, credential_revoked=revoked))
    return endings


@pytest.mark.parametrize("form", ["json", "%(message)s", logging.BASIC_FORMAT])
def test_every_ending_reaches_a_real_pipe_in_one_line_with_its_close_and_count(monkeypatch, form):
    # A pipe takes at most 512 bytes in one write (see qte_sdk.update): a longer line is
    # written in its short form there, which must fit for every close.
    from test_update import json_handler, plain_handler, read_all

    endings = every_ending()
    read, write = os.pipe()
    stream = open(write, "w", encoding="utf-8")  # noqa: SIM115
    handler = json_handler(stream) if form == "json" else plain_handler(stream, form)
    logger = logging.getLogger("qte_sdk.reconnect")
    monkeypatch.setattr(logger, "propagate", False)
    logger.addHandler(handler)
    rs = ReconnectingSession("wss://example.invalid", synthetic_token())
    data = b""
    try:
        for ending in endings:
            rs._log_disconnected(Disconnected(ending, Many()), None)
            disconnect_records(None)
            # Read after each one, so the pipe never fills and the lines keep their order.
            data += read_all(read)
    finally:
        logger.removeHandler(handler)
        stream.close()
        os.close(read)
    lines = data.splitlines(keepends=True)
    assert len(lines) == len(endings), data
    count = "; 999999999 unanswered requests (QTE-SESSION-UNANSWERED)"
    for line in lines:
        assert len(line) <= 512, (len(line), line)
        assert count.encode() in line
    if form == "json":
        parsed = [json.loads(line) for line in lines]
        assert {p["code"] for p in parsed} == {errors.SESSION_DISCONNECTED}
        assert parsed[0]["fields"]["close"] == "close code 1006"
        assert all(set(p["fields"]) == {"close"} for p in parsed)  # every one written short
        revoked = [p for p in parsed if p["fields"]["close"].endswith('"credential revoked"')]
        assert len(revoked) == 3
