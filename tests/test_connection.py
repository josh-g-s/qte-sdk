import asyncio
import json
from collections.abc import Callable
from typing import Any

import pytest
from fake_exchange import exchange, frame, serve_local
from websockets.asyncio.server import ServerConnection

from qte_sdk.connection import (
    Connection,
    ContractVersionMismatch,
    DecodeFailed,
    Received,
    SeqGap,
    SessionRejected,
    Unknown,
)
from qte_sdk.contract.v1.common_pb2 import BUY, LIMIT, ReasonCodes
from qte_sdk.contract.v1.market_data_pb2 import Book
from qte_sdk.contract.v1.order_entry_pb2 import NewOrder
from qte_sdk.contract.v1.order_events_pb2 import Execution, Reject
from qte_sdk.contract.v1.session_pb2 import Subscribe

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


async def test_an_order_reject_with_an_unknown_reason_name_keeps_the_name():
    payload = {"reason_code": "BRAND_NEW_REASON", "receipt_time": "5"}
    async with exchange([frame("reject", payload, 1)]) as url:
        [event] = await collect(url)
    assert isinstance(event, Received)
    assert event.message.reason_code == ReasonCodes.REASON_CODE_UNSPECIFIED
    assert event.message.receipt_time == 5
    assert event.payload == payload
    assert event.unknown_enum_names() == {"reason_code": "BRAND_NEW_REASON"}


async def test_an_order_reject_with_an_unknown_numeric_reason_code_keeps_the_number():
    async with exchange([frame("reject", {"reason_code": 1999}, 1)]) as url:
        [event] = await collect(url)
    assert event.message.reason_code == 1999
    assert event.unknown_enum_names() == {}


async def test_a_session_reject_with_an_unknown_reason_name_raises_with_the_name():
    payload = {"reason_code": "BRAND_NEW_REASON", "reason_detail": "try later"}
    async with exchange([frame("session_reject", payload, 1)]) as url:
        with pytest.raises(SessionRejected) as info:
            await collect(url)
    assert not isinstance(info.value, ContractVersionMismatch)
    assert info.value.reason_code == ReasonCodes.REASON_CODE_UNSPECIFIED
    assert info.value.reason_name == "BRAND_NEW_REASON"
    assert str(info.value) == "BRAND_NEW_REASON: try later"


def test_session_rejected_names_known_and_numeric_codes():
    assert SessionRejected(ReasonCodes.NOT_AUTHENTICATED, None).reason_name == "NOT_AUTHENTICATED"
    assert SessionRejected(1999, None).reason_name == "1999"


async def test_received_built_positionally_still_works_and_equals_a_decoded_event():
    async with exchange([book(1)]) as url:
        [event] = await collect(url)
    built = Received("book", Book(instrument="AAPL", grid_time=1), 1)
    assert built.payload is None
    assert built.unknown_enum_names() == {}
    assert event.payload is not None
    assert event == built
