import json
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from fake_exchange import exchange, frame, serve_local
from google.protobuf.message import Message
from websockets.asyncio.server import ServerConnection

from qte_sdk import orders
from qte_sdk.connection import Connection, Received
from qte_sdk.contract.v1.common_pb2 import (
    BUY,
    CANCEL,
    LIMIT,
    LOSS_WARNING,
    MAKER,
    MARKET,
    NEW,
    RESTING,
    SELL,
    SIDE_UNSPECIFIED,
    STUDENT_TO_WALL,
    SUBSCRIBE,
    TAKER,
    TEAM,
    ReasonCodes,
)
from qte_sdk.contract.v1.order_events_pb2 import (
    Accepted,
    Execution,
    OrderCancelled,
    OrderState,
    Reject,
    RiskNotice,
)
from qte_sdk.orders import (
    ORDER_EVENT_TYPES,
    is_order_event,
    reason_code_name,
    request_ref_of,
    send_amend,
    send_cancel,
    send_mass_cancel,
    send_new,
)
from qte_sdk.session import Session, SessionInfo

PRICE = 199_970_000  # $199.97 in micro-dollars


async def sent_by(send: Callable[[Connection], Awaitable[Any]]) -> tuple[Any, dict[str, Any]]:
    """Run `send` against a local server; return its result and the envelope the server got."""
    inbox: list[str] = []
    async with exchange([], inbox) as url:
        async with Connection(url) as conn:
            result = await send(conn)
            async for _ in conn:  # the server closes once it has read the message
                pass
    [raw] = inbox
    return result, json.loads(raw)


async def received(*frames: str) -> list:
    async with exchange(list(frames)) as url:
        async with Connection(url) as conn:
            return [event async for event in conn]


class Recorder:
    """A `Sender` that keeps what it is given, for tests that need no network."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, Message]] = []

    async def send(self, type_: str, payload: Message) -> None:
        self.sent.append((type_, payload))


# Sending: the exact wire fields of each message.


async def test_new_limit_sends_every_field_and_returns_its_request_ref():
    ref, env = await sent_by(
        lambda conn: send_new(
            conn,
            strat_id="mm-1",
            instrument="AAPL",
            side=BUY,
            order_type=LIMIT,
            price=PRICE,
            size=100,
        )
    )
    assert env["version"] == "0.x"
    assert env["type"] == "new"
    assert env["payload"] == {
        "request_ref": ref,
        "strat_id": "mm-1",
        "instrument": "AAPL",
        "side": "BUY",
        "order_type": "LIMIT",
        "price": str(PRICE),
        "size": "100",
    }


async def test_new_market_sends_no_price():
    ref, env = await sent_by(
        lambda conn: send_new(
            conn, strat_id="mt-1", instrument="MSFT", side=SELL, order_type=MARKET, size=5
        )
    )
    assert env["type"] == "new"
    assert env["payload"] == {
        "request_ref": ref,
        "strat_id": "mt-1",
        "instrument": "MSFT",
        "side": "SELL",
        "order_type": "MARKET",
        "size": "5",
    }


async def test_cancel_addresses_a_price_level_and_carries_no_strategy():
    ref, env = await sent_by(
        lambda conn: send_cancel(conn, instrument="AAPL", side=SELL, price=PRICE)
    )
    assert env["type"] == "cancel"
    assert env["payload"] == {
        "request_ref": ref,
        "instrument": "AAPL",
        "side": "SELL",
        "price": str(PRICE),
    }


async def test_a_size_only_amend_sends_new_price_equal_to_price():
    ref, env = await sent_by(
        lambda conn: send_amend(conn, instrument="AAPL", side=BUY, price=PRICE, new_size=8)
    )
    assert env["type"] == "amend"
    assert env["payload"] == {
        "request_ref": ref,
        "instrument": "AAPL",
        "side": "BUY",
        "price": str(PRICE),
        "new_price": str(PRICE),
        "new_size": "8",
    }


async def test_a_price_amend_sends_the_new_price_and_the_new_remaining_size():
    ref, env = await sent_by(
        lambda conn: send_amend(
            conn, instrument="AAPL", side=BUY, price=PRICE, new_price=PRICE - 10_000, new_size=3
        )
    )
    assert env["payload"] == {
        "request_ref": ref,
        "instrument": "AAPL",
        "side": "BUY",
        "price": str(PRICE),
        "new_price": str(PRICE - 10_000),
        "new_size": "3",
    }


async def test_mass_cancel_carries_only_its_request_ref():
    ref, env = await sent_by(lambda conn: send_mass_cancel(conn))
    assert env["type"] == "mass_cancel"
    assert env["payload"] == {"request_ref": ref}


async def test_a_caller_supplied_request_ref_is_sent_as_given():
    ref, env = await sent_by(lambda conn: send_mass_cancel(conn, request_ref="my-ref-7"))
    assert ref == "my-ref-7"
    assert env["payload"] == {"request_ref": "my-ref-7"}


async def test_every_send_gets_a_distinct_request_ref():
    conn = Recorder()
    refs = [
        await send_new(
            conn, strat_id="s", instrument="AAPL", side=BUY, order_type=LIMIT, price=1, size=1
        ),
        await send_cancel(conn, instrument="AAPL", side=BUY, price=1),
        await send_amend(conn, instrument="AAPL", side=BUY, price=1, new_size=1),
        await send_mass_cancel(conn),
        await send_mass_cancel(conn),
    ]
    assert len(set(refs)) == len(refs)
    assert all(ref for ref in refs)
    assert [msg.request_ref for _, msg in conn.sent] == refs
    assert [type_ for type_, _ in conn.sent] == [
        "new",
        "cancel",
        "amend",
        "mass_cancel",
        "mass_cancel",
    ]


@pytest.mark.parametrize(
    "type_, send",
    [
        (
            "new",
            lambda s: send_new(
                s, strat_id="s", instrument="AAPL", side=BUY, order_type=LIMIT, price=1, size=1
            ),
        ),
        ("cancel", lambda s: send_cancel(s, instrument="AAPL", side=BUY, price=1)),
        ("amend", lambda s: send_amend(s, instrument="AAPL", side=BUY, price=1, new_size=1)),
        ("mass_cancel", lambda s: send_mass_cancel(s)),
    ],
    ids=["new", "cancel", "amend", "mass_cancel"],
)
async def test_every_send_takes_a_session(type_: str, send: Callable[[Any], Awaitable[str]]):
    info = SessionInfo("s-1", "team-a", 1, "0.x", True)
    ref, env = await sent_by(lambda conn: send(Session(conn, info, [])))
    assert env["type"] == type_
    assert env["payload"]["request_ref"] == ref


@pytest.mark.parametrize(
    "kwargs, match",
    [
        ({"order_type": LIMIT}, "LIMIT order needs a price"),
        ({"order_type": MARKET, "price": PRICE}, "MARKET order carries no price"),
        ({"order_type": 0, "price": PRICE}, "LIMIT or MARKET"),
        ({"order_type": LIMIT, "price": PRICE, "side": SIDE_UNSPECIFIED}, "BUY or SELL"),
    ],
)
async def test_a_new_the_contract_cannot_carry_is_refused_before_sending(kwargs, match):
    conn = Recorder()
    args: dict[str, Any] = {"strat_id": "s", "instrument": "AAPL", "side": BUY, "size": 1}
    with pytest.raises(ValueError, match=match):
        await send_new(conn, **{**args, **kwargs})
    assert conn.sent == []


async def test_cancel_and_amend_refuse_an_unspecified_side_before_sending():
    conn = Recorder()
    with pytest.raises(ValueError, match="BUY or SELL"):
        await send_cancel(conn, instrument="AAPL", side=SIDE_UNSPECIFIED, price=PRICE)
    with pytest.raises(ValueError, match="BUY or SELL"):
        await send_amend(conn, instrument="AAPL", side=SIDE_UNSPECIFIED, price=PRICE, new_size=1)
    assert conn.sent == []


# Identifiers: each one a message carries is checked before anything is sent.

SENDERS: dict[str, tuple[Callable[..., Awaitable[str]], dict[str, Any]]] = {
    "new": (
        send_new,
        {
            "strat_id": "s",
            "instrument": "AAPL",
            "side": BUY,
            "order_type": LIMIT,
            "price": PRICE,
            "size": 1,
        },
    ),
    "cancel": (send_cancel, {"instrument": "AAPL", "side": BUY, "price": PRICE}),
    "amend": (send_amend, {"instrument": "AAPL", "side": BUY, "price": PRICE, "new_size": 1}),
    "mass_cancel": (send_mass_cancel, {}),
}

# Every identifier each sender puts on its message.
IDENTIFIERS = [
    ("new", "request_ref"),
    ("new", "strat_id"),
    ("new", "instrument"),
    ("cancel", "request_ref"),
    ("cancel", "instrument"),
    ("amend", "request_ref"),
    ("amend", "instrument"),
    ("mass_cancel", "request_ref"),
]

LONGEST_ID = "é" * 16  # 16 characters, 32 bytes of UTF-8: the longest allowed
BAD_IDS = {
    "empty": "",
    "33 bytes": "é" * 16 + "x",  # only 17 characters, but 33 bytes
    "33 multi-byte": "€" * 11,  # 3 bytes each
    "NUL": "ab\0cd",
    "not UTF-8": "ab\ud800",  # a lone surrogate has no UTF-8 encoding
}


async def messages_sent_by(send: Callable[[Connection], Awaitable[Any]]) -> list[dict[str, Any]]:
    """Run `send` against a local server; return every message the server received."""
    inbox: list[dict[str, Any]] = []

    async def handler(ws: ServerConnection) -> None:
        async for message in ws:
            inbox.append(json.loads(message))

    async with serve_local(handler) as url:
        async with Connection(url) as conn:
            await send(conn)
    return inbox


def test_the_longest_and_the_bad_identifiers_are_what_they_claim():
    assert len(LONGEST_ID.encode("utf-8")) == 32
    assert len(BAD_IDS["33 bytes"].encode("utf-8")) == 33
    assert len(BAD_IDS["33 multi-byte"].encode("utf-8")) == 33


@pytest.mark.parametrize("sender, field", IDENTIFIERS)
async def test_a_32_byte_multi_byte_identifier_is_sent_as_given(sender, field):
    send, args = SENDERS[sender]
    [env] = await messages_sent_by(lambda conn: send(conn, **{**args, field: LONGEST_ID}))
    assert env["type"] == sender
    assert env["payload"][field] == LONGEST_ID


@pytest.mark.parametrize("bad", BAD_IDS.values(), ids=BAD_IDS.keys())
@pytest.mark.parametrize("sender, field", IDENTIFIERS)
async def test_a_bad_identifier_is_refused_and_nothing_is_sent(sender, field, bad):
    if field == "instrument" and bad == "":
        pytest.skip("an empty instrument is only limited in length; see the test below")
    send, args = SENDERS[sender]
    errors: list[ValueError] = []

    async def refused(conn: Connection) -> None:
        with pytest.raises(ValueError, match=f"^{field} ") as err:
            await send(conn, **{**args, field: bad})
        errors.append(err.value)

    assert await messages_sent_by(refused) == []
    [error] = errors
    # The message names the field, never the value, raw or escaped.
    assert str(error).startswith(f"{field} must ")
    if bad:
        assert bad not in str(error)
        assert repr(bad)[1:-1] not in str(error)


@pytest.mark.parametrize("sender", ["new", "cancel", "amend"])
async def test_an_empty_instrument_is_sent_as_given(sender):
    # The contract limits instrument to at most 32 bytes and does not require it to be
    # non-empty, so the SDK leaves an empty one for the exchange to judge.
    send, args = SENDERS[sender]
    [env] = await messages_sent_by(lambda conn: send(conn, **{**args, "instrument": ""}))
    assert env["payload"]["instrument"] == ""


@pytest.mark.parametrize("sender", SENDERS)
async def test_a_generated_request_ref_is_checked_too(sender, monkeypatch):
    send, args = SENDERS[sender]
    monkeypatch.setattr(orders, "new_request_ref", lambda: "x" * 33)
    conn = Recorder()
    with pytest.raises(ValueError, match="^request_ref "):
        await send(conn, **args)
    assert conn.sent == []


async def test_an_identifier_that_is_not_text_is_refused_before_sending():
    conn = Recorder()
    with pytest.raises(TypeError, match="^instrument "):
        await send_cancel(conn, instrument=None, side=BUY, price=PRICE)  # type: ignore[arg-type]
    assert conn.sent == []


# Receiving: each order event decodes into its generated type.

EXECUTION = {
    "exec_id": "x-1",
    "origin": "TEAM",
    "strat_id": "mt-1",
    "instrument": "AAPL",
    "side": "BUY",
    "order_price": "200100000",
    "fill_price": "200040000",
    "fill_size": "40",
    "remaining_size": "60",
    "fill_kind": "STUDENT_TO_WALL",
    "liquidity": "TAKER",
    "fee": "-240048",
    "timestamp": "1000",
}


async def test_each_order_event_type_decodes_typed_and_correlates_where_it_can():
    events = await received(
        frame(
            "accepted",
            {"request_ref": "r-1", "request_type": "NEW", "receipt_time": "1", "release_time": "2"},
            1,
        ),
        frame(
            "reject",
            {
                "request_ref": "r-2",
                "request_type": "CANCEL",
                "reason_code": "NO_ORDER_AT_LEVEL",
                "reason_detail": "no order at that level",
                "receipt_time": "3",
            },
            2,
        ),
        frame("execution", EXECUTION, 3),
        frame(
            "order_cancelled",
            {
                "origin": "TEAM",
                "strat_id": "mm-1",
                "instrument": "AAPL",
                "side": "SELL",
                "price": "200100000",
                "cancelled_size": "20",
                "reason_code": "CANCEL_REQUEST",
                "request_ref": "r-3",
                "timestamp": "4",
            },
            4,
        ),
        frame(
            "order_state",
            {
                "strat_id": "mm-1",
                "instrument": "AAPL",
                "side": "BUY",
                "price": str(PRICE),
                "state": "RESTING",
                "remaining_size": "100",
                "timestamp": "5",
            },
            5,
        ),
        frame("risk_notice", {"kind": "LOSS_WARNING", "timestamp": "6"}, 6),
    )
    assert all(is_order_event(e) for e in events)
    assert [e.type for e in events] == [
        "accepted",
        "reject",
        "execution",
        "order_cancelled",
        "order_state",
        "risk_notice",
    ]
    accepted, reject, execution, cancelled, state, notice = (e.message for e in events)

    assert accepted == Accepted(request_ref="r-1", request_type=NEW, receipt_time=1, release_time=2)
    assert reject == Reject(
        request_ref="r-2",
        request_type=CANCEL,
        reason_code=ReasonCodes.NO_ORDER_AT_LEVEL,
        reason_detail="no order at that level",
        receipt_time=3,
    )
    assert execution == Execution(
        exec_id="x-1",
        origin=TEAM,
        strat_id="mt-1",
        instrument="AAPL",
        side=BUY,
        order_price=200_100_000,
        fill_price=200_040_000,
        fill_size=40,
        remaining_size=60,
        fill_kind=STUDENT_TO_WALL,
        liquidity=TAKER,
        fee=-240_048,
        timestamp=1000,
    )
    assert cancelled.reason_code == ReasonCodes.CANCEL_REQUEST
    assert cancelled.cancelled_size == 20
    assert state == OrderState(
        strat_id="mm-1",
        instrument="AAPL",
        side=BUY,
        price=PRICE,
        state=RESTING,
        remaining_size=100,
        timestamp=5,
    )
    assert notice == RiskNotice(kind=LOSS_WARNING, timestamp=6)

    assert [request_ref_of(e.message) for e in events] == ["r-1", "r-2", None, "r-3", None, None]
    assert reason_code_name(reject.reason_code) == "NO_ORDER_AT_LEVEL"
    assert reason_code_name(cancelled.reason_code) == "CANCEL_REQUEST"


async def test_a_zero_remaining_size_and_zero_fee_decode_as_real_zeros():
    payload = {**EXECUTION, "remaining_size": "0", "fee": "0", "liquidity": "MAKER"}
    [event] = await received(frame("execution", payload, 1))
    assert event.message.remaining_size == 0
    assert event.message.fee == 0
    assert event.message.liquidity == MAKER


async def test_a_cancel_the_exchange_made_itself_carries_no_request_ref():
    payload = {
        "origin": "TEAM",
        "strat_id": "mm-1",
        "instrument": "AAPL",
        "side": "BUY",
        "price": str(PRICE),
        "cancelled_size": "10",
        "reason_code": "PURGE_STALE",
        "timestamp": "9",
    }
    [event] = await received(frame("order_cancelled", payload, 1))
    assert isinstance(event.message, OrderCancelled)
    assert event.message.reason_code == ReasonCodes.PURGE_STALE
    assert request_ref_of(event.message) is None


async def test_a_reject_that_could_not_read_a_request_ref_carries_none():
    payload = {"reason_code": "MALFORMED_MESSAGE", "receipt_time": "1"}
    [event] = await received(frame("reject", payload, 1))
    assert request_ref_of(event.message) is None
    assert not event.message.HasField("request_type")


async def test_a_rejected_subscription_is_an_order_event_with_its_request_type():
    payload = {
        "request_type": "SUBSCRIBE",
        "reason_code": "UNKNOWN_INSTRUMENT",
        "receipt_time": "1",
        "instrument": "NOPE",
    }
    [event] = await received(frame("reject", payload, 1))
    assert is_order_event(event)
    assert event.message.request_type == SUBSCRIBE
    assert event.message.instrument == "NOPE"


@pytest.mark.parametrize(
    "type_, payload",
    [
        ("reject", {"request_ref": "r-9", "reason_code": 1999, "receipt_time": "1"}),
        (
            "order_cancelled",
            {"instrument": "AAPL", "side": "BUY", "reason_code": 1899, "timestamp": "1"},
        ),
    ],
)
async def test_an_unknown_reason_code_number_is_surfaced_without_crashing(type_, payload):
    code = payload["reason_code"]
    events = await received(frame(type_, payload, 1), frame("risk_notice", {"kind": 1}, 2))
    assert [type(e) for e in events] == [Received, Received]
    assert events[0].message.reason_code == code
    assert reason_code_name(events[0].message.reason_code) == str(code)
    assert events[1].message.kind == LOSS_WARNING


async def test_an_unrecognised_reason_token_decodes_as_unspecified():
    payload = {"request_ref": "r-1", "reason_code": "A_CODE_FROM_A_NEWER_CONTRACT"}
    [event] = await received(frame("reject", payload, 1))
    assert event.message.reason_code == ReasonCodes.REASON_CODE_UNSPECIFIED
    assert reason_code_name(event.message.reason_code) == "REASON_CODE_UNSPECIFIED"
    assert request_ref_of(event.message) == "r-1"


async def test_market_data_is_not_an_order_event():
    [event] = await received(frame("book", {"instrument": "AAPL", "bid_levels": []}, 1))
    assert isinstance(event, Received)
    assert not is_order_event(event)


# End to end: a response is matched to the message that caused it.


async def test_the_accepted_for_a_new_is_matched_by_the_returned_request_ref():
    async def handler(ws: ServerConnection) -> None:
        sent = json.loads(await ws.recv())
        ref = sent["payload"]["request_ref"]
        await ws.send(frame("execution", EXECUTION, 1))
        await ws.send(
            frame(
                "accepted",
                {"request_ref": "someone-else", "request_type": "NEW", "receipt_time": "1"},
                2,
            )
        )
        await ws.send(
            frame("accepted", {"request_ref": ref, "request_type": "NEW", "receipt_time": "2"}, 3)
        )
        await ws.close()

    async with serve_local(handler) as url:
        async with Connection(url) as conn:
            ref = await send_new(
                conn, strat_id="s", instrument="AAPL", side=BUY, order_type=LIMIT, price=1, size=1
            )
            matched = [
                event.message
                async for event in conn
                if is_order_event(event) and request_ref_of(event.message) == ref
            ]
    assert len(matched) == 1
    assert isinstance(matched[0], Accepted)
    assert matched[0].receipt_time == 2


# Scope.


def test_order_event_types_are_the_six_outbound_order_events():
    assert ORDER_EVENT_TYPES == {
        "accepted",
        "reject",
        "execution",
        "order_cancelled",
        "order_state",
        "risk_notice",
    }


def test_only_the_four_order_messages_and_event_helpers_are_exposed():
    assert set(orders.__all__) == {
        "ORDER_EVENT_TYPES",
        "OrderEvent",
        "Sender",
        "is_order_event",
        "new_request_ref",
        "reason_code_name",
        "request_ref_of",
        "send_amend",
        "send_cancel",
        "send_mass_cancel",
        "send_new",
    }
    public = {name.lower() for name in dir(orders) if not name.startswith("_")}
    for word in ("option", "ticket", "agent"):
        assert not any(word in name for name in public), word
