"""`Disconnected.unanswered`: the requests a `ReconnectingSession` sent on a session that
dropped and that got no answer echoing their request_ref, and the `QTE-SESSION-UNANSWERED`
warning that names them (#343). Every expected value is written by hand."""

import asyncio
import json
import logging
import threading
from collections.abc import Awaitable, Callable

import pytest
from fake_exchange import drop_connection, frame, serve_local
from test_reconnect import EMPTY, Clock
from test_session import ack, synthetic_token
from websockets.asyncio.server import ServerConnection
from websockets.exceptions import ConnectionClosedOK
from websockets.frames import Close

from qte_sdk import errors
from qte_sdk.account import send_account_query
from qte_sdk.connection import Disconnected, ResumeComplete, describe_close
from qte_sdk.contract.v1.common_pb2 import BUY, LIMIT
from qte_sdk.contract.v1.order_entry_pb2 import NewOrder, SubmitBreakRequest, SubmitTicket
from qte_sdk.orders import send_amend, send_cancel, send_mass_cancel, send_new
from qte_sdk.reconnect import ReconnectingSession

PRICE = 199_970_000
LOG = "00000000000000aa"

# Answers, each echoing the request_ref it is given.


def accepted(ref: str, request_type: str = "NEW") -> str:
    payload = {"request_ref": ref, "request_type": request_type, "receipt_time": "1"}
    return frame("accepted", payload)


def reject(ref: str | None, request_type: str = "NEW") -> str:
    payload = {"request_type": request_type, "reason_code": "MALFORMED_MESSAGE"}
    if ref is not None:
        payload["request_ref"] = ref
    return frame("reject", payload)


def order_cancelled(ref: str) -> str:
    payload = {
        "origin": "TEAM",
        "strat_id": "mm-1",
        "instrument": "AAPL",
        "side": "BUY",
        "price": str(PRICE),
        "cancelled_size": "100",
        "reason_code": "REASON_CODE_UNSPECIFIED",
        "request_ref": ref,
        "timestamp": "3",
    }
    return frame("order_cancelled", payload)


def account_state(ref: str) -> str:
    return frame("account_state", {"request_ref": ref, "session_date": "2027-06-14"})


def ticket_accepted(ref: str) -> str:
    return frame("ticket_accepted", {"request_ref": ref, "ticket_id": "tk-1"})


def break_request_state(ref: str) -> str:
    return frame("break_request_state", {"break_request_id": "br-1", "request_ref": ref})


class Exchange:
    """Acks each connection and answers its resume with the empty marker. On the first
    connection it then reads `count` requests, sends `answers`, and drops the connection;
    each later connection runs the next of `later` ("drop" drops it at once, "hold" holds it
    open, the last repeating)."""

    def __init__(self, count: int, answers: list[str], later: tuple[str, ...] = ("hold",)):
        self.count = count
        self.answers = answers
        self.later = later
        self.connections = 0
        self.requests: list[dict] = []

    async def __call__(self, ws: ServerConnection) -> None:
        n = self.connections
        self.connections += 1
        await ws.recv()  # auth
        await ws.send(ack(log_id=LOG))
        await ws.recv()  # resume
        await ws.send(EMPTY)
        if n == 0:
            for _ in range(self.count):
                self.requests.append(json.loads(await ws.recv()))
            for answer in self.answers:
                await ws.send(answer)
            await drop_connection(ws)
            return
        if self.later[min(n - 1, len(self.later) - 1)] == "drop":
            await drop_connection(ws)
            return
        await ws.wait_closed()


Send = Callable[[ReconnectingSession], Awaitable[None]]


async def drops(exchange: Exchange, send: Send, count: int = 1) -> list[Disconnected]:
    """Run a ReconnectingSession against `exchange`, calling `send` once the first session's
    resume completes, and return its first `count` Disconnected events."""
    seen: list[Disconnected] = []
    async with serve_local(exchange) as url:
        rs = ReconnectingSession(url, synthetic_token(), sleep=Clock().sleep)
        async with rs, asyncio.timeout(10):
            async for event in rs:
                if isinstance(event, ResumeComplete) and exchange.connections == 1:
                    await send(rs)
                elif isinstance(event, Disconnected):
                    seen.append(event)
                    if len(seen) == count:
                        break
    return seen


def unanswered_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    for thread in threading.enumerate():
        if thread.name == "qte-sdk pacing warning":
            thread.join(5)
    return [r for r in caplog.records if getattr(r, "code", None) == errors.SESSION_UNANSWERED]


async def new(rs: ReconnectingSession, ref: str) -> None:
    await send_new(rs, strat_id="mm-1", instrument="AAPL", side=BUY, order_type=LIMIT,
                   price=PRICE, size=100, request_ref=ref)  # fmt: skip


async def two_news_and_a_cancel(rs: ReconnectingSession) -> None:
    await new(rs, "n1")
    await new(rs, "n2")
    await send_cancel(rs, instrument="AAPL", side=BUY, price=PRICE, request_ref="c1")


async def test_a_drop_names_the_requests_that_got_no_answer_in_the_order_sent():
    exchange = Exchange(3, [accepted("n2")])
    [disconnected] = await drops(exchange, two_news_and_a_cancel)
    assert disconnected.unanswered == (("new", "n1"), ("cancel", "c1"))


async def test_every_kind_of_answer_that_echoes_the_ref_clears_its_request():
    async def send(rs: ReconnectingSession) -> None:
        await new(rs, "n1")
        await new(rs, "n2")
        await send_cancel(rs, instrument="AAPL", side=BUY, price=PRICE, request_ref="c1")
        await send_amend(rs, instrument="AAPL", side=BUY, price=PRICE, new_size=50,
                         request_ref="a1")  # fmt: skip
        await send_account_query(rs, request_ref="q1")
        await rs.send("ticket", SubmitTicket(request_ref="t1"))
        await rs.send("break_request", SubmitBreakRequest(request_ref="b1"))
        await send_mass_cancel(rs, request_ref="m1")

    answers = [
        accepted("n1"),
        reject("n2"),
        order_cancelled("c1"),
        accepted("a1", "AMEND"),
        account_state("q1"),
        ticket_accepted("t1"),
        break_request_state("b1"),
    ]
    [disconnected] = await drops(Exchange(8, answers), send)
    # Only the mass cancel, which nothing answered, is left.
    assert disconnected.unanswered == (("mass_cancel", "m1"),)


async def test_a_reject_with_no_request_ref_leaves_its_request_listed():
    async def send(rs: ReconnectingSession) -> None:
        await new(rs, "n1")

    [disconnected] = await drops(Exchange(1, [reject(None)]), send)
    assert disconnected.unanswered == (("new", "n1"),)


async def test_nothing_carries_over_to_the_next_session():
    seen_on_connect: list[dict[str, str]] = []
    exchange = Exchange(3, [accepted("n2")], later=("drop", "hold"))
    disconnects: list[Disconnected] = []
    async with serve_local(exchange) as url:
        rs = ReconnectingSession(url, synthetic_token(), sleep=Clock().sleep)
        async with rs, asyncio.timeout(10):
            async for event in rs:
                if isinstance(event, ResumeComplete):
                    seen_on_connect.append(dict(rs._unanswered))
                    if exchange.connections == 1:
                        await two_news_and_a_cancel(rs)
                elif isinstance(event, Disconnected):
                    disconnects.append(event)
                    if len(disconnects) == 2:
                        break
    assert [d.unanswered for d in disconnects] == [(("new", "n1"), ("cancel", "c1")), ()]
    # The second session started with no request noted.
    assert seen_on_connect == [{}, {}]


async def test_the_warning_names_the_count_and_refs_and_no_payload(caplog):
    exchange = Exchange(3, [accepted("n2")])
    with caplog.at_level(logging.WARNING, logger="qte_sdk.reconnect"):
        await drops(exchange, two_news_and_a_cancel)
        records = unanswered_records(caplog)
    assert len(records) == 1
    record = records[0]
    assert record.name == "qte_sdk.reconnect"
    assert record.levelno == logging.WARNING
    assert record.fields == {"count": 2, "refs": "new n1, cancel c1"}
    message = record.getMessage()
    assert message.startswith(
        "QTE-SESSION-UNANSWERED: the connection dropped; unconfirmed requests (2): "
        "new n1, cancel c1. "
    )
    # Nothing of the messages themselves: no instrument, strategy or price.
    for detail in ("AAPL", "mm-1", str(PRICE)):
        assert detail not in message


async def test_no_warning_when_every_request_was_answered(caplog):
    async def send(rs: ReconnectingSession) -> None:
        await new(rs, "n1")

    with caplog.at_level(logging.WARNING, logger="qte_sdk.reconnect"):
        [disconnected] = await drops(Exchange(1, [accepted("n1")]), send)
        records = unanswered_records(caplog)
    assert disconnected.unanswered == ()
    assert records == []


def test_a_ref_shaped_like_a_token_is_shortened_and_one_holding_the_token_withheld(
    monkeypatch,
):
    from qte_sdk import pacing

    caught: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        pacing, "_emit", lambda message, code, **kw: caught.append((message, kw["fields"]))
    )
    token = synthetic_token()
    rs = ReconnectingSession("ws://127.0.0.1:1", token)
    # The shape new_request_ref makes: 32 hex digits, as a token is.
    made = "0123456789abcdef0123456789abcdef"
    rs._unanswered = {made: "new", token: "cancel"}
    # The event keeps each ref whole.
    assert rs._take_unanswered() == (("new", made), ("cancel", token))
    [(message, fields)] = caught
    assert fields["refs"] == f"new 0123456789ab..., cancel {errors.WITHHELD}"
    assert token not in message


async def test_a_json_log_line_shows_a_shortened_made_ref(caplog):
    from qte_sdk.logs import JsonFormatter

    made = "0123456789abcdef0123456789abcdef"
    rs = ReconnectingSession("ws://127.0.0.1:1", synthetic_token())
    rs._unanswered = {made: "new"}
    with caplog.at_level(logging.WARNING, logger="qte_sdk.reconnect"):
        rs._take_unanswered()
        [record] = unanswered_records(caplog)
    line = json.loads(JsonFormatter().format(record))
    assert line["fields"] == {"count": 1, "refs": "new 0123456789ab..."}
    assert line["message"].startswith(
        "the connection dropped; unconfirmed requests (1): new 0123456789ab"
    )
    assert made not in line["message"]


def test_the_token_in_qte_token_is_withheld_from_the_log_too(monkeypatch):
    from qte_sdk import pacing
    from qte_sdk.session import TOKEN_ENV_VAR

    caught: list[dict] = []
    monkeypatch.setattr(pacing, "_emit", lambda message, code, **kw: caught.append(kw["fields"]))
    # A second token, not the one this session holds: only QTE_TOKEN names it.
    other = synthetic_token()
    monkeypatch.setenv(TOKEN_ENV_VAR, other)
    rs = ReconnectingSession("ws://127.0.0.1:1", synthetic_token())
    rs._unanswered = {other: "new"}
    assert rs._take_unanswered() == (("new", other),)
    [fields] = caught
    assert fields["refs"] == f"new {errors.WITHHELD}"


def test_disconnected_lists_nothing_by_default():
    assert Disconnected(None).unanswered == ()
    assert Disconnected(None) == Disconnected(None, ())


class _Wire:
    def __init__(self, error: BaseException | None) -> None:
        self.error = error

    async def send(self, type_: str, payload: object) -> None:
        if self.error is not None:
            raise self.error


class _Session:
    def __init__(self, error: BaseException | None) -> None:
        self.connection = _Wire(error)


async def test_a_send_that_failed_before_writing_anything_is_not_listed():
    rs = ReconnectingSession("ws://127.0.0.1:1", synthetic_token())
    # As for a message that could not be encoded.
    session = _Session(ValueError("cannot encode"))
    rs._session = session  # type: ignore[assignment]
    with pytest.raises(ValueError):
        await rs._send_noted(session, "new", NewOrder(request_ref="n1"))  # type: ignore[arg-type]
    assert rs._unanswered == {}


@pytest.mark.parametrize(
    "error",
    [
        ConnectionClosedOK(Close(1000, ""), None),
        ConnectionResetError(),
        TimeoutError(),
        asyncio.CancelledError(),
    ],
)
async def test_a_send_that_failed_as_a_connection_does_stays_listed(error: BaseException):
    # The frame may have been written before the connection failed or the send was cut off.
    rs = ReconnectingSession("ws://127.0.0.1:1", synthetic_token())
    session = _Session(error)
    rs._session = session  # type: ignore[assignment]
    with pytest.raises(type(error)):
        await rs._send_noted(session, "new", NewOrder(request_ref="n1"))  # type: ignore[arg-type]
    assert rs._unanswered == {"n1": "new"}


def test_a_1001_close_says_the_exchange_is_stopping_or_switching_and_will_reconnect():
    error = ConnectionClosedOK(Close(1001, "exchange stopping"), None)
    assert describe_close(error) == (
        'close code 1001, reason "exchange stopping": the exchange is stopping or switching; '
        "a ReconnectingSession connects again, and after a switch that normally succeeds "
        "within seconds"
    )


async def test_a_ref_sent_again_while_unanswered_is_listed_once_under_its_first_type():
    rs = ReconnectingSession("ws://127.0.0.1:1", synthetic_token())
    session = _Session(None)
    rs._session = session  # type: ignore[assignment]
    await rs._send_noted(session, "new", NewOrder(request_ref="n1"))  # type: ignore[arg-type]
    await rs._send_noted(session, "cancel", NewOrder(request_ref="n1"))  # type: ignore[arg-type]
    assert rs._unanswered == {"n1": "new"}
    # A later send with the same ref that fails before writing anything leaves the first
    # request listed.
    session.connection.error = ValueError("cannot encode")
    with pytest.raises(ValueError):
        await rs._send_noted(session, "amend", NewOrder(request_ref="n1"))  # type: ignore[arg-type]
    assert rs._unanswered == {"n1": "new"}


async def test_the_first_answer_clears_a_ref_sent_twice():
    async def send(rs: ReconnectingSession) -> None:
        await new(rs, "n1")
        await new(rs, "n1")

    [disconnected] = await drops(Exchange(2, [accepted("n1")]), send)
    assert disconnected.unanswered == ()
