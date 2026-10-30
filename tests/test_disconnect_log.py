"""`QTE-SESSION-DISCONNECTED`: the WARNING a `ReconnectingSession` logs once for each
`Disconnected` it delivers, with `describe_close` of the close (#374). Every expected value
is written by hand."""

import asyncio
import json
import logging
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
from qte_sdk.connection import _CLOSE_CAUSES, Connected, Disconnected, ResumeComplete
from qte_sdk.reconnect import ReconnectingSession, Retrying
from qte_sdk.session import ResumeNotAcknowledged

LOG = "00000000000000aa"
NO_CLOSE_FRAME = "close code 1006: the connection dropped with no close frame from the exchange"


def disconnect_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    for thread in threading.enumerate():
        if thread.name == "qte-sdk pacing warning":
            thread.join(5)
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
        {"close": "close code 1012", "unanswered": ""},
        {"close": NO_CLOSE_FRAME, "unanswered": ""},
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
    assert record.fields["close"] == (
        'close code 1001, reason "exchange stopping": the exchange is stopping or switching; '
        "a ReconnectingSession connects again, and after a switch that normally succeeds "
        "within seconds"
    )


async def test_a_reason_the_sdk_does_not_show_is_left_out(caplog):
    with caplog.at_level(logging.WARNING, logger="qte_sdk.reconnect"):
        await run(Ends(closing(1011, "free text from the server")), 1)
        [record] = disconnect_records(caplog)
    assert record.fields["close"] == "close code 1011: an error on the exchange"
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
    assert record.fields == {
        "close": NO_CLOSE_FRAME,
        "unanswered": "; 2 requests were unanswered (QTE-SESSION-UNANSWERED)",
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
    assert line["fields"] == {"close": "close code 1012", "unanswered": ""}


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
    assert warning["fields"]["close"] == (
        "ResumeNotAcknowledged (QTE-SESSION-RESUME-NOT-ACKNOWLEDGED), close code 1008, "
        'reason "credential revoked": your token was revoked or rotated; get a new token, '
        "and do not connect again with this one"
    )
    failure = ResumeNotAcknowledged("closed", close_code=1012)
    warning = emitted(monkeypatch, Disconnected(failure))
    assert warning["fields"]["close"] == (
        "ResumeNotAcknowledged (QTE-SESSION-RESUME-NOT-ACKNOWLEDGED), close code 1012"
    )


def test_the_short_line_keeps_the_close_and_count_and_fits_512_bytes(monkeypatch):
    longest = 0
    for code, causes in _CLOSE_CAUSES.items():
        for reason in causes:
            error = ConnectionClosedError(Close(code, reason or ""), None)
            unanswered = tuple(("new", f"r{n}") for n in range(999_999_999 // 10**8))
            warning = emitted(monkeypatch, Disconnected(error, unanswered))
            head = warning["fields"]["close"].split(": ", 1)[0]
            assert warning["short_fields"] == {
                "close": head,
                "unanswered": warning["fields"]["unanswered"],
            }
            assert warning["short"].startswith(
                f"QTE-SESSION-DISCONNECTED: the session ended, {head}. "
            )
            record = logging.LogRecord(
                "qte_sdk.reconnect", logging.WARNING, "", 0, warning["short"], (), None
            )
            record.code = errors.SESSION_DISCONNECTED
            record.next_step = warning["next_step"]
            record.fields = dict(
                warning["short_fields"],
                unanswered="; 999999999 requests were unanswered (QTE-SESSION-UNANSWERED)",
            )
            line = logs.JsonFormatter().format(record).encode()
            longest = max(longest, len(line) + 1, len(warning["short"].encode()) + 1)
    assert longest <= 512
