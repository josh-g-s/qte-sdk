import asyncio
import json
import logging
import secrets
import traceback
from collections.abc import Callable
from typing import Any

import pytest
from fake_exchange import exchange, frame, serve_local
from websockets.asyncio.server import ServerConnection, serve
from websockets.exceptions import ConnectionClosedError, InvalidHandshake

from qte_sdk.connection import (
    Connection,
    ContractVersionMismatch,
    DecodeFailed,
    HandshakeFailed,
    Received,
    SeqGap,
    SessionRejected,
    Unknown,
)
from qte_sdk.contract.v1.common_pb2 import BUY, LIMIT, ReasonCodes
from qte_sdk.contract.v1.market_data_pb2 import Book
from qte_sdk.contract.v1.order_entry_pb2 import NewOrder
from qte_sdk.contract.v1.order_events_pb2 import Execution, Reject
from qte_sdk.contract.v1.session_pb2 import Auth, Subscribe

BIG = 9_007_199_254_740_993  # 2**53 + 1


def book(seq: int | None) -> str:
    return frame("book", {"instrument": "AAPL", "grid_time": "1", "bid_levels": []}, seq)


async def collect(url: str, before: Callable[[Connection], Any] | None = None) -> list:
    async with Connection(url) as conn:
        if before is not None:
            await before(conn)
        return [event async for event in conn]


async def test_a_known_message_type_reaches_the_caller_typed():
    async with exchange([book(1)]) as url:
        events = await collect(url)
    assert len(events) == 1
    assert isinstance(events[0], Received)
    assert events[0].type == "book"
    assert events[0].message == Book(instrument="AAPL", grid_time=1)
    assert events[0].seq == 1


async def test_unknown_fields_are_ignored_at_both_levels():
    payload = {"exec_id": "e-1", "instrument": "AAPL", "a_field_from_a_newer_contract": True}
    async with exchange([frame("execution", payload, 1, top_level_extra="x")]) as url:
        [event] = await collect(url)
    assert isinstance(event, Received)
    assert event.message == Execution(exec_id="e-1", instrument="AAPL")


async def test_an_unknown_message_type_is_reported_and_does_not_stop_the_client():
    async with exchange([frame("new_kind_of_message", {"x": 1}, 1), book(2)]) as url:
        events = await collect(url)
    assert events[0] == Unknown("new_kind_of_message", {"x": 1}, 1)
    assert isinstance(events[1], Received)


async def test_a_seq_gap_is_reported_as_data_uncertainty_and_delivery_continues():
    async with exchange([book(1), book(2), book(5), book(6)]) as url:
        events = await collect(url)
    assert [type(e) for e in events] == [Received, Received, SeqGap, Received, Received]
    assert events[2] == SeqGap(expected=3, received=5)


async def test_a_bad_payload_still_advances_seq_and_a_missing_seq_is_not_a_gap():
    bad = frame("book", {"instrument": "AAPL", "grid_time": "not a number"}, 2)
    async with exchange([book(1), bad, book(None), book(3)]) as url:
        events = await collect(url)
    assert [type(e) for e in events] == [Received, DecodeFailed, Received, Received]
    assert events[1].type == "book"


@pytest.mark.parametrize(
    "bad_frame",
    [
        "not json",
        '["an", "array"]',
        json.dumps({"version": "0.x", "type": "book", "payload": 3}),
        json.dumps({"version": "0.x", "type": "book", "seq": "abc", "payload": {}}),
        json.dumps({"version": "0.x", "type": "execution"}),
        json.dumps({"version": "0.x", "payload": {}}),
        json.dumps({"version": "0.x", "type": None, "payload": {}}),
        json.dumps({"type": "book", "payload": {}}),
        json.dumps({"version": "0.x", "type": "", "payload": {}}),
        json.dumps({"version": "", "type": "book", "payload": {}}),
        json.dumps({"version": None, "type": "book", "payload": {}}),
        b"\x00binary",
    ],
)
async def test_a_malformed_frame_is_reported_and_never_delivered(bad_frame):
    async with exchange([bad_frame, book(1)]) as url:
        events = await collect(url)
    assert isinstance(events[0], DecodeFailed)
    assert [type(e) for e in events[1:]] == [Received]


@pytest.mark.parametrize("type_", ["session_reject", "reject"])
async def test_a_version_mismatch_raises_a_typed_error_on_either_reject(type_):
    payload = {"reason_code": "VERSION_MISMATCH", "reason_detail": "contract version not served"}
    async with exchange([frame(type_, payload, 1)]) as url:
        with pytest.raises(ContractVersionMismatch) as info:
            await collect(url)
    assert info.value.reason_code == ReasonCodes.VERSION_MISMATCH
    assert info.value.detail == "contract version not served"
    assert str(info.value) == "VERSION_MISMATCH: contract version not served"


async def test_any_other_session_reject_raises_session_rejected():
    async with exchange([frame("session_reject", {"reason_code": "NOT_AUTHENTICATED"}, 1)]) as url:
        with pytest.raises(SessionRejected) as info:
            await collect(url)
    assert not isinstance(info.value, ContractVersionMismatch)
    assert info.value.reason_code == ReasonCodes.NOT_AUTHENTICATED
    assert info.value.detail is None


async def test_a_session_reject_with_a_reason_code_from_a_newer_contract_still_raises():
    async with exchange([frame("session_reject", {"reason_code": 1999}, 1)]) as url:
        with pytest.raises(SessionRejected, match="1999"):
            await collect(url)


async def test_an_ordinary_order_reject_is_delivered_as_a_message():
    payload = {"reason_code": "MARKET_CLOSED", "receipt_time": "5"}
    async with exchange([frame("reject", payload, 1)]) as url:
        [event] = await collect(url)
    assert isinstance(event, Received)
    assert isinstance(event.message, Reject)
    assert event.message.reason_code == ReasonCodes.MARKET_CLOSED


async def test_an_int64_above_2_to_the_53_arrives_exactly():
    payload = {"exec_id": "e-1", "fill_price": str(BIG), "fill_size": "1"}
    async with exchange([frame("execution", payload, 1)]) as url:
        [event] = await collect(url)
    assert event.message.fill_price == BIG


async def test_send_writes_an_envelope_with_the_contract_version_and_no_seq():
    inbox: list[str] = []
    order = NewOrder(
        request_ref="r-1",
        strat_id="s",
        instrument="AAPL",
        side=BUY,
        order_type=LIMIT,
        price=BIG,
        size=1,
    )
    async with exchange([], inbox) as url:
        await collect(url, before=lambda conn: conn.send("new", order))
    sent = json.loads(inbox[0])
    assert sent["version"] == "0.x"
    assert sent["type"] == "new"
    assert "seq" not in sent
    assert sent["payload"]["price"] == str(BIG)


async def test_the_contract_version_can_be_overridden():
    inbox: list[str] = []
    async with exchange([], inbox) as url:
        async with Connection(url, contract_version="9.9") as conn:
            await conn.send("subscribe", Subscribe(instruments=["AAPL"]))
    sent = json.loads(inbox[0])
    assert sent["version"] == "9.9"
    assert sent["payload"] == {"instruments": ["AAPL"]}


async def test_an_int64_above_2_to_the_53_survives_a_send_and_return_trip():
    async def echo(ws: ServerConnection) -> None:
        sent = json.loads(await ws.recv())
        price = sent["payload"]["price"]
        await ws.send(frame("execution", {"exec_id": "e-1", "fill_price": price}, 1))
        await ws.close()

    order = NewOrder(request_ref="r", strat_id="s", instrument="AAPL", side=BUY, price=BIG, size=1)
    async with serve_local(echo) as url:
        [event] = await collect(url, before=lambda conn: conn.send("new", order))
    assert event.message.fill_price == BIG


async def test_concurrent_opens_connect_only_once():
    async with exchange([]) as url:
        conn = Connection(url)
        results = await asyncio.gather(conn.open(), conn.open(), return_exceptions=True)
        await conn.close()
    assert sum(isinstance(r, RuntimeError) for r in results) == 1


async def test_a_connection_is_single_use():
    async with exchange([]) as url:
        conn = Connection(url)
        async with conn:
            with pytest.raises(RuntimeError):
                await conn.open()


async def echo_then_close(ws: ServerConnection) -> None:
    await ws.send(await ws.recv())
    await ws.close()


def pieces(token: str, size: int = 6) -> set[str]:
    return {token[i : i + size] for i in range(len(token) - size + 1)}


def client_log(caplog: pytest.LogCaptureFixture, name: str) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name.startswith(name)]


@pytest.mark.parametrize("logger_name", [None, "student.client"])
async def test_the_token_never_reaches_the_debug_log(caplog, logger_name):
    caplog.set_level(logging.DEBUG)
    token = secrets.token_hex(32)
    options = {} if logger_name is None else {"logger": logging.getLogger(logger_name)}
    async with serve_local(echo_then_close) as url:
        async with Connection(url, **options) as conn:
            await conn.send("auth", Auth(token=token))
            [event async for event in conn]
    lines = client_log(caplog, logger_name or "websockets.client")
    assert lines, "the frame trace should still be logged"
    for line in lines:
        assert not any(piece in line for piece in pieces(token)), line


async def test_connection_events_are_still_logged_without_frames(caplog):
    caplog.set_level(logging.DEBUG)
    async with serve_local(echo_then_close) as url:
        async with Connection(url) as conn:
            await conn.send("subscribe", Subscribe(instruments=["AAPL"]))
            [event async for event in conn]
    records = [r for r in caplog.records if r.name.startswith("websockets.client")]
    lines = [r.getMessage() for r in records]
    assert any("connection is" in line for line in lines)
    assert not any(line.startswith(FRAME_TRACES) for line in lines)
    # Once connected, records carry a snapshot of the connection, never the connection.
    attached = [r.websocket for r in records if getattr(r, "websocket", None) is not None]
    assert attached and all(w.id is not None for w in attached)
    assert not any(hasattr(w, "protocol") or hasattr(w, "response") for w in attached)


FRAME_TRACES = tuple(
    f"{d} {op}" for d in "<>" for op in ("TEXT", "BINARY", "CONT", "PING", "PONG", "CLOSE")
)


def rendered(caplog: pytest.LogCaptureFixture, name: str) -> list[str]:
    formatter = logging.Formatter("%(message)s")
    out = []
    for record in caplog.records:
        if record.name.startswith(name):
            out.append(formatter.format(record))
    return out


def assert_no_token(lines: list[str], token: str) -> None:
    for line in lines:
        assert not any(piece in line for piece in pieces(token)), line


async def test_a_fragmented_echo_does_not_leak_the_token(caplog):
    caplog.set_level(logging.DEBUG)
    token = secrets.token_hex(32)

    async def fragmented_echo(ws: ServerConnection) -> None:
        text = await ws.recv()
        await ws.send([text[i : i + 10] for i in range(0, len(text), 10)])
        await ws.close()

    async with serve_local(fragmented_echo) as url:
        async with Connection(url) as conn:
            await conn.send("auth", Auth(token=token))
            [event async for event in conn]
    assert_no_token(rendered(caplog, "websockets.client"), token)


async def test_control_frames_carrying_the_token_are_not_logged(caplog):
    caplog.set_level(logging.DEBUG)
    token = secrets.token_hex(16)

    async def ping_then_close(ws: ServerConnection) -> None:
        await ws.recv()
        await ws.ping(token.encode())
        await asyncio.sleep(0.05)
        await ws.close(4000, token)

    async with serve_local(ping_then_close) as url:
        async with Connection(url) as conn:
            await conn.send("auth", Auth(token=token))
            with pytest.raises(ConnectionClosedError):
                [event async for event in conn]
    lines = rendered(caplog, "websockets.client")
    assert not any(line.startswith(FRAME_TRACES) for line in lines)
    assert_no_token(lines, token)


def test_exceptions_are_logged_by_type_only(caplog):
    from qte_sdk.connection import _WithoutCredentials

    caplog.set_level(logging.DEBUG)
    token = secrets.token_hex(32)
    adapter = _WithoutCredentials(logging.getLogger("websockets.client"), {})
    try:
        raise ValueError(f"bad frame {token}")
    except ValueError as error:
        adapter.error("parser failed", exc_info=True)
        adapter.warning("closing: %s", error)
    lines = rendered(caplog, "websockets.client")
    assert any("ValueError" in line for line in lines)
    assert all(record.exc_info is None for record in caplog.records)
    assert_no_token(lines, token)


async def test_a_character_split_across_fragments_still_arrives_with_debug_on(caplog):
    caplog.set_level(
        logging.DEBUG, logger="websockets.client"
    )  # the fake server's own trace cannot render a split character
    envelope = {"version": "0.x", "type": "book", "seq": 1}
    envelope["payload"] = {"instrument": "\u00e9t\u00e9", "grid_time": "1"}
    encoded = json.dumps(envelope, ensure_ascii=False).encode()
    cut = encoded.index("\u00e9".encode()) + 1  # inside the two-byte character

    async def split_utf8(ws: ServerConnection) -> None:
        async with ws.send_context():
            ws.protocol.send_text(encoded[:cut], fin=False)
            ws.protocol.send_continuation(encoded[cut:], fin=True)
        await ws.close()

    async with serve_local(split_utf8) as url:
        async with Connection(url) as conn:
            events = [event async for event in conn]
    assert len(events) == 1
    assert isinstance(events[0], Received)
    assert events[0].message.instrument == "\u00e9t\u00e9"


async def test_handshake_trace_withholds_query_and_header_values(caplog):
    caplog.set_level(logging.DEBUG)
    token = secrets.token_hex(32)
    async with serve_local(echo_then_close) as url:
        options = {"additional_headers": {"X-Api-Key": token}}
        async with Connection(f"{url}/ws?key={token}", **options) as conn:
            await conn.send("subscribe", Subscribe(instruments=["AAPL"]))
            [event async for event in conn]
    lines = rendered(caplog, "websockets.client")
    assert any(line.startswith("> GET /ws ") for line in lines)
    assert any(line.startswith("> X-Api-Key: ") for line in lines)
    assert_no_token(lines, token)


def assert_withheld(error: BaseException, token: str) -> None:
    shown = [str(error), repr(error)]
    for close in (getattr(error, "rcvd", None), getattr(error, "sent", None)):
        if close is not None:
            shown.append(close.reason)
    assert error.__cause__ is None and error.__context__ is None
    assert_no_token(shown, token)


async def test_a_close_reason_carrying_the_token_is_withheld_from_the_error():
    token = secrets.token_hex(16)

    async def close_with_token(ws: ServerConnection) -> None:
        await ws.recv()
        await ws.close(4000, token)

    async with serve_local(close_with_token) as url:
        async with Connection(url) as conn:
            await conn.send("auth", Auth(token=token))
            with pytest.raises(ConnectionClosedError) as caught:
                [event async for event in conn]
    assert caught.value.rcvd is not None and caught.value.rcvd.code == 4000
    assert caught.value.rcvd_then_sent is True
    assert_withheld(caught.value, token)


async def test_sending_after_a_close_withholds_the_reason_too():
    token = secrets.token_hex(16)

    async def close_with_token(ws: ServerConnection) -> None:
        await ws.recv()
        await ws.close(4000, token)

    async with serve_local(close_with_token) as url:
        async with Connection(url) as conn:
            await conn.send("auth", Auth(token=token))
            with pytest.raises(ConnectionClosedError):
                [event async for event in conn]
            with pytest.raises(ConnectionClosedError) as caught:
                await conn.send("subscribe", Subscribe(instruments=["AAPL"]))
    assert_withheld(caught.value, token)


async def test_a_close_during_the_handshake_withholds_the_reason(monkeypatch):
    from websockets.frames import Close

    import qte_sdk.connection as connection_module

    token = secrets.token_hex(16)

    async def closed_during_handshake(*args, **kwargs):
        raise ConnectionClosedError(Close(4000, token), Close(4000, token), True)

    monkeypatch.setattr(connection_module, "_connect", closed_during_handshake)
    with pytest.raises(ConnectionClosedError) as caught:
        await Connection("ws://127.0.0.1:1").open()
    assert caught.value.rcvd.code == 4000
    assert_withheld(caught.value, token)


def shown_with_locals(error: BaseException) -> list[str]:
    """What a traceback that prints local variables would show, leaving out this test
    module's own frames, which hold the token by design."""
    parts = [str(error), repr(error)]
    pending = [traceback.TracebackException.from_exception(error, capture_locals=True)]
    while pending:
        link = pending.pop()
        parts.extend(link.format_exception_only())
        for summary in link.stack:
            if summary.filename != __file__:
                parts.append(f"{summary.filename}:{summary.lineno} {summary.locals}")
        pending.extend(n for n in (link.__cause__, link.__context__) if n is not None)
    return parts


async def test_no_traceback_local_shows_the_token_after_a_close():
    token = secrets.token_hex(16)

    async def echo_then_close_with_token(ws: ServerConnection) -> None:
        await ws.send(await ws.recv())
        await ws.close(4000, token)

    async with serve_local(echo_then_close_with_token) as url:
        async with Connection(url) as conn:
            await conn.send("auth", Auth(token=token))
            with pytest.raises(ConnectionClosedError) as reading:
                [event async for event in conn]
            with pytest.raises(ConnectionClosedError) as sending:
                await conn.send("auth", Auth(token=token))
    assert_no_token(shown_with_locals(reading.value), token)
    assert_no_token(shown_with_locals(sending.value), token)


def reflect_into_upgrade(connection, request, response):
    del response.headers["Upgrade"]
    response.headers["Upgrade"] = request.headers.get("X-Key", "")
    return response


def refuse_with_token(connection, request):
    return connection.respond(403, f"denied {request.headers.get('X-Key', '')}")


@pytest.mark.parametrize(
    ("hooks", "kind", "status"),
    [
        ({"process_response": reflect_into_upgrade}, "InvalidUpgrade", None),
        ({"process_request": refuse_with_token}, "InvalidStatus", 403),
    ],
)
async def test_a_failed_handshake_withholds_reflected_values(caplog, hooks, kind, status):
    caplog.set_level(logging.DEBUG, logger="websockets.client")
    token = secrets.token_hex(16)
    async with serve(echo_then_close, "127.0.0.1", 0, **hooks) as server:
        port = server.sockets[0].getsockname()[1]
        conn = Connection(f"ws://127.0.0.1:{port}", additional_headers={"X-Key": token})
        with pytest.raises(HandshakeFailed) as caught:
            await conn.open()
    error = caught.value
    assert isinstance(error, InvalidHandshake)
    assert (error.kind, error.status_code) == (kind, status)
    assert error.__cause__ is None and error.__context__ is None
    assert_no_token([*shown_with_locals(error), repr(vars(error))], token)
    assert_no_token(rendered(caplog, "websockets.client"), token)


async def test_a_reflected_response_header_name_is_withheld_from_the_log(caplog):
    caplog.set_level(logging.DEBUG)
    token = secrets.token_hex(16)

    def add_reflected_header(connection, request, response):
        response.headers[request.headers.get("X-Key", "x")] = "1"
        return response

    async with serve(
        echo_then_close, "127.0.0.1", 0, process_response=add_reflected_header
    ) as server:
        port = server.sockets[0].getsockname()[1]
        url = f"ws://127.0.0.1:{port}"
        async with Connection(url, additional_headers={"X-Key": token}) as conn:
            await conn.send("subscribe", Subscribe(instruments=["AAPL"]))
            [event async for event in conn]
    lines = rendered(caplog, "websockets.client")
    assert any(line.startswith("< Upgrade: ") for line in lines)
    assert any(line.startswith("< <withheld>: ") for line in lines)
    assert_no_token(lines, token)


def redirect_to_token(connection, request):
    response = connection.respond(302, "")
    response.headers["Location"] = f"/{request.headers.get('X-Key', '')}"
    return response


async def test_a_redirect_is_not_followed_and_its_location_is_withheld(caplog):
    caplog.set_level(logging.DEBUG, logger="websockets.client")
    token = secrets.token_hex(16)
    async with serve(echo_then_close, "127.0.0.1", 0, process_request=redirect_to_token) as server:
        port = server.sockets[0].getsockname()[1]
        conn = Connection(f"ws://127.0.0.1:{port}", additional_headers={"X-Key": token})
        with pytest.raises(HandshakeFailed) as caught:
            await conn.open()
    assert (caught.value.kind, caught.value.status_code) == ("InvalidStatus", 302)
    assert_no_token([*shown_with_locals(caught.value), repr(vars(caught.value))], token)
    lines = rendered(caplog, "websockets.client")
    assert sum(line.startswith("> GET ") for line in lines) == 1
    assert_no_token(lines, token)


async def test_a_send_before_open_keeps_the_token_out_of_the_traceback():
    token = secrets.token_hex(16)
    with pytest.raises(RuntimeError) as caught:
        await Connection("ws://127.0.0.1:1").send("auth", Auth(token=token))
    assert_no_token(shown_with_locals(caught.value), token)


async def test_any_send_failure_keeps_the_token_out_of_the_traceback(monkeypatch):
    token = secrets.token_hex(16)

    async def failing_send(message, text=None):
        raise ValueError("the socket refused the write")

    async with serve_local(echo_then_close) as url:
        async with Connection(url) as conn:
            monkeypatch.setattr(conn._ws, "send", failing_send)
            with pytest.raises(ValueError) as caught:
                await conn.send("auth", Auth(token=token))
    assert caught.value.__context__ is None
    assert_no_token(shown_with_locals(caught.value), token)


async def test_log_records_expose_no_route_to_reflected_text(caplog):
    caplog.set_level(logging.DEBUG, logger="websockets.client")
    token = secrets.token_hex(16)

    def reflect_header(connection, request, response):
        response.headers["X-Reflected"] = request.headers.get("X-Key", "")
        return response

    async def close_with_token(ws: ServerConnection) -> None:
        await ws.recv()
        await ws.close(4000, token)

    async with serve(close_with_token, "127.0.0.1", 0, process_response=reflect_header) as server:
        port = server.sockets[0].getsockname()[1]
        conn = Connection(f"ws://127.0.0.1:{port}", additional_headers={"X-Key": token})
        async with conn:
            await conn.send("subscribe", Subscribe(instruments=["AAPL"]))
            with pytest.raises(ConnectionClosedError):
                [event async for event in conn]
    for record in caplog.records:
        if not record.name.startswith("websockets.client"):
            continue
        reachable = [repr(vars(record))]
        attached = getattr(record, "websocket", None)
        for path in ("protocol.close_rcvd.reason", "response.headers", "request.headers"):
            value = attached
            for name in path.split("."):
                value = getattr(value, name, None)
            reachable.append(repr(value))
        assert_no_token(reachable, token)
