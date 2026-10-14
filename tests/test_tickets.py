import asyncio
import doctest
import inspect
import json
from typing import Any

import pytest
from fake_exchange import exchange, frame, serve_local
from google.protobuf.message import Message
from test_reconnect import Clock, resume_ack
from test_resume import NEXT_TERM, TERM, Scripted, book, kinds, order_state
from test_session import synthetic_token

from qte_sdk import tickets
from qte_sdk.connection import (
    Connection,
    ContractVersionMismatch,
    Disconnected,
    Received,
    SeqGap,
)
from qte_sdk.contract.registry import INBOUND
from qte_sdk.contract.v1.common_pb2 import (
    AUTO_EXECUTION,
    BUY,
    DESK,
    ENGINE,
    FALLBACK,
    LIMIT,
    MESSAGE_BUDGET,
    ROUTING_DESK,
    ROUTING_SERVICE,
    SELL,
    SERVICE,
    SIDE_UNSPECIFIED,
    TAKING,
    TICKET_ACTIVITY_UNSPECIFIED,
    TICKET_ALGORITHM_UNSPECIFIED,
    TICKET_CANCELLED_BY_POD,
    TICKET_COMPLETE,
    TICKET_LIMIT_CANCELLED,
    TICKET_REQUEST_CANCEL,
    TICKET_REQUEST_SUBMIT,
    TICKET_SPLIT_CANCELLED,
    TICKET_STATUS_UNSPECIFIED,
    TICKET_URGENCY_UNSPECIFIED,
    TICKET_WORKING,
    ReasonCodes,
    TicketReasonCodes,
    TicketStatus,
    TicketUrgency,
)
from qte_sdk.contract.v1.order_entry_pb2 import (
    AmendOrder,
    CancelOrder,
    CancelTicket,
    NewOrder,
    RaiseTicketUrgency,
    SubmitTicket,
)
from qte_sdk.contract.v1.order_events_pb2 import (
    Execution,
    TicketAccepted,
    TicketReject,
    TicketState,
)
from qte_sdk.contract.v1.session_pb2 import ExecutionRoute
from qte_sdk.market_data import as_market_data
from qte_sdk.orders import (
    is_order_event,
    reason_code_name,
    send_amend,
    send_cancel,
    send_new,
)
from qte_sdk.reconnect import ReconnectingSession
from qte_sdk.session import open_session
from qte_sdk.tickets import (
    EXECUTION_ROUTE_TYPE,
    TICKET_EVENT_TYPES,
    TWAP,
    URGENCY_HIGH,
    URGENCY_LOW,
    URGENCY_MEDIUM,
    VWAP,
    LatestTickets,
    is_execution_route,
    is_ticket_event,
    parent_ticket_of,
    send_ticket,
    send_ticket_cancel,
    send_ticket_urgency,
    stopped_mark_of,
    stopped_time_of,
    ticket_reason_code_name,
    ticket_request_ref_of,
)

LIMIT_PRICE = 201_500_000  # $201.50 in micro-dollars


class Recorder:
    """A `Sender` that keeps what it is given, for tests that need no network."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, Message]] = []

    async def send(self, type_: str, payload: Message) -> None:
        self.sent.append((type_, payload))


async def one_sent(send) -> tuple[str, str, Any]:
    conn = Recorder()
    ref = await send(conn)
    [(type_, msg)] = conn.sent
    return ref, type_, msg


async def received(*frames: str) -> list:
    async with exchange(list(frames)) as url:
        async with Connection(url) as conn:
            return [event async for event in conn]


# Sending.


async def test_an_ordinary_ticket_carries_exactly_what_was_given():
    ref, type_, msg = await one_sent(
        lambda c: send_ticket(
            c,
            instrument="AAPL",
            side=BUY,
            shares=500,
            limit_price=LIMIT_PRICE,
            urgency=URGENCY_MEDIUM,
            thesis_id="t-aapl",
            thesis_version=2,
            notes="work it patiently",
            request_ref="r-1",
        )
    )
    assert ref == "r-1"
    assert type_ == "ticket"
    assert msg == SubmitTicket(
        request_ref="r-1",
        thesis_id="t-aapl",
        thesis_version=2,
        instrument="AAPL",
        side=BUY,
        shares=500,
        limit_price=LIMIT_PRICE,
        urgency=URGENCY_MEDIUM,
        notes="work it patiently",
    )
    assert not msg.HasField("cure")
    assert not msg.HasField("replaces_ticket_id")


async def test_a_reducing_ticket_may_name_no_thesis_and_sends_none():
    _, _, msg = await one_sent(
        lambda c: send_ticket(
            c, instrument="AAPL", side=SELL, shares=100, limit_price=1, urgency=URGENCY_LOW
        )
    )
    assert not msg.HasField("thesis_id")
    assert not msg.HasField("thesis_version")
    assert not msg.HasField("notes")
    assert len(msg.request_ref) == 32


async def test_a_replacement_names_the_cancelled_ticket():
    _, _, msg = await one_sent(
        lambda c: send_ticket(
            c,
            instrument="AAPL",
            side=BUY,
            shares=1,
            limit_price=LIMIT_PRICE,
            urgency=URGENCY_HIGH,
            replaces_ticket_id="42",
        )
    )
    assert msg.replaces_ticket_id == "42"


async def test_a_cure_ticket_is_flagged_and_carries_no_limit():
    _, _, msg = await one_sent(
        lambda c: send_ticket(
            c, instrument="AAPL", side=SELL, shares=300, urgency=URGENCY_HIGH, cure=True
        )
    )
    assert msg.cure is True
    assert not msg.HasField("limit_price")


async def test_a_ticket_goes_on_the_wire_as_its_canonical_json():
    inbox: list[str] = []
    async with exchange([], inbox) as url:
        async with Connection(url) as conn:
            await send_ticket(
                conn,
                instrument="AAPL",
                side=BUY,
                shares=500,
                limit_price=LIMIT_PRICE,
                urgency=URGENCY_MEDIUM,
                request_ref="r-1",
            )
            async for _ in conn:
                pass
    [raw] = inbox
    envelope = json.loads(raw)
    assert envelope["type"] == "ticket"
    assert envelope["payload"] == {
        "request_ref": "r-1",
        "instrument": "AAPL",
        "side": "BUY",
        "shares": "500",
        "limit_price": str(LIMIT_PRICE),
        "urgency": "URGENCY_MEDIUM",
        # No algorithm given: the unset value, which the exchange reads as TWAP (an
        # exchange from before the field ignores it, as it ignores any unknown field).
        "algorithm": "TICKET_ALGORITHM_UNSPECIFIED",
    }


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"cure": False}, "^an ordinary ticket needs a limit_price"),
        ({"cure": True, "limit_price": LIMIT_PRICE}, "^a cure ticket carries no limit_price"),
        (
            {"cure": True, "replaces_ticket_id": "7"},
            "^a cure ticket names no replaces_ticket_id",
        ),
        ({"limit_price": 1, "thesis_id": "t"}, "^thesis_id and thesis_version go together"),
        ({"limit_price": 1, "thesis_version": 1}, "^thesis_id and thesis_version go together"),
        ({"limit_price": 1, "thesis_id": "x" * 33, "thesis_version": 1}, "^thesis_id must be"),
        ({"limit_price": 1, "notes": "n" * 281}, "^notes must be at most 280 characters"),
        ({"limit_price": 1, "notes": "a\0b"}, "^notes must not contain the NUL"),
        ({"limit_price": 1, "replaces_ticket_id": "12a"}, "^replaces_ticket_id must be"),
        ({"limit_price": 1, "replaces_ticket_id": ""}, "^replaces_ticket_id must be"),
        ({"limit_price": 1, "instrument": "x" * 33}, "^instrument must be"),
        ({"limit_price": 1, "request_ref": ""}, "^request_ref must be"),
        ({"limit_price": 1, "side": SIDE_UNSPECIFIED}, "^side must be BUY or SELL"),
        ({"limit_price": 1, "urgency": TICKET_URGENCY_UNSPECIFIED}, "^urgency must be"),
        (
            {"limit_price": 1, "algorithm": TICKET_ALGORITHM_UNSPECIFIED},
            "^algorithm must be TWAP or VWAP",
        ),
        ({"limit_price": 1, "algorithm": 3}, "^algorithm must be TWAP or VWAP"),
        ({"limit_price": 1, "algorithm": True}, "^algorithm must be TWAP or VWAP"),
        ({"limit_price": 1, "algorithm": "VWAP"}, "^algorithm must be TWAP or VWAP"),
    ],
)
async def test_a_ticket_that_breaks_a_rule_is_refused_before_sending(kwargs, message):
    args: dict[str, Any] = {
        "instrument": "AAPL",
        "side": BUY,
        "shares": 1,
        "urgency": URGENCY_MEDIUM,
    }
    args.update(kwargs)
    conn = Recorder()
    with pytest.raises(ValueError, match=message):
        await send_ticket(conn, **args)
    assert conn.sent == []


async def test_notes_of_exactly_280_characters_are_allowed():
    _, _, msg = await one_sent(
        lambda c: send_ticket(
            c,
            instrument="AAPL",
            side=BUY,
            shares=1,
            limit_price=1,
            urgency=URGENCY_LOW,
            notes="é" * 280,
        )
    )
    assert msg.notes == "é" * 280


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"notes": 5}, "^notes must be a str"),
        ({"cure": 1}, "^cure must be a bool"),
        ({"replaces_ticket_id": 7}, "^replaces_ticket_id must be a str"),
    ],
)
async def test_a_ticket_field_of_the_wrong_type_is_refused(kwargs, message):
    conn = Recorder()
    with pytest.raises(TypeError, match=message):
        await send_ticket(
            conn,
            instrument="AAPL",
            side=BUY,
            shares=1,
            limit_price=1,
            urgency=URGENCY_LOW,
            **kwargs,
        )
    assert conn.sent == []


async def test_a_ticket_cancel_names_the_ticket():
    ref, type_, msg = await one_sent(lambda c: send_ticket_cancel(c, ticket_id="17"))
    assert type_ == "ticket_cancel"
    assert msg == CancelTicket(request_ref=ref, ticket_id="17")
    assert not msg.HasField("replacing")


async def test_a_plain_ticket_cancel_carries_no_replacing_field():
    ref, _, msg = await one_sent(lambda c: send_ticket_cancel(c, ticket_id="17", replacing=False))
    assert msg == CancelTicket(request_ref=ref, ticket_id="17")
    assert not msg.HasField("replacing")


async def test_a_replacing_ticket_cancel_sets_the_field():
    ref, type_, msg = await one_sent(
        lambda c: send_ticket_cancel(c, ticket_id="17", replacing=True, request_ref="t-2a")
    )
    assert (ref, type_) == ("t-2a", "ticket_cancel")
    assert msg.HasField("replacing") and msg.replacing is True


@pytest.mark.parametrize("bad", [1, 0, "true", None, 1.0])
async def test_a_replacing_that_is_not_a_bool_is_refused_before_sending(bad):
    conn = Recorder()
    with pytest.raises(TypeError, match="^replacing must be a bool"):
        await send_ticket_cancel(conn, ticket_id="17", replacing=bad)
    assert conn.sent == []


@pytest.mark.parametrize("replacing", [False, True])
async def test_the_replacing_flag_goes_on_the_wire_only_when_true(replacing):
    inbox: list[str] = []
    async with exchange([], inbox) as url:
        async with Connection(url) as conn:
            await send_ticket_cancel(
                conn, ticket_id="48211", replacing=replacing, request_ref="t-2a"
            )
            async for _ in conn:
                pass
    [raw] = inbox
    payload = json.loads(raw)["payload"]
    assert payload["ticket_id"] == "48211"
    if replacing:
        assert payload["replacing"] is True
    else:
        assert "replacing" not in payload


async def test_an_urgency_change_names_the_ticket_and_the_new_urgency():
    ref, type_, msg = await one_sent(
        lambda c: send_ticket_urgency(c, ticket_id="17", urgency=URGENCY_HIGH, request_ref="u")
    )
    assert ref == "u"
    assert type_ == "ticket_urgency"
    assert msg == RaiseTicketUrgency(request_ref="u", ticket_id="17", urgency=URGENCY_HIGH)


@pytest.mark.parametrize("bad", ["", "seventeen", "1 7", "١٧", "-1"])
async def test_a_bad_ticket_id_is_refused_before_sending(bad):
    conn = Recorder()
    with pytest.raises(ValueError, match="^ticket_id must be"):
        await send_ticket_cancel(conn, ticket_id=bad)
    with pytest.raises(ValueError, match="^ticket_id must be"):
        await send_ticket_urgency(conn, ticket_id=bad, urgency=URGENCY_HIGH)
    assert conn.sent == []


async def test_an_urgency_change_needs_a_real_urgency():
    conn = Recorder()
    with pytest.raises(ValueError, match="^urgency must be"):
        await send_ticket_urgency(conn, ticket_id="1", urgency=TICKET_URGENCY_UNSPECIFIED)
    assert conn.sent == []


def test_the_urgencies_are_exactly_high_medium_and_low():
    # The contract's urgency enum is closed: these three and the unspecified zero.
    assert dict(TicketUrgency.items()) == {
        "TICKET_URGENCY_UNSPECIFIED": 0,
        "URGENCY_HIGH": 1,
        "URGENCY_MEDIUM": 2,
        "URGENCY_LOW": 3,
    }


@pytest.mark.parametrize("urgency", [4, -1])
async def test_an_urgency_outside_the_three_is_refused_before_sending(urgency):
    conn = Recorder()
    with pytest.raises(ValueError, match="^urgency must be"):
        await send_ticket_urgency(conn, ticket_id="1", urgency=urgency)
    with pytest.raises(ValueError, match="^urgency must be"):
        await send_ticket(
            conn, instrument="AAPL", side=BUY, shares=1, limit_price=1, urgency=urgency
        )
    assert conn.sent == []


# house_team is for Director sessions only, and the order helpers never offer it.


@pytest.mark.parametrize("send", [send_new, send_cancel, send_amend])
def test_no_order_helper_takes_house_team(send):
    assert "house_team" not in inspect.signature(send).parameters


async def test_no_order_helper_sets_house_team():
    conn = Recorder()
    await send_new(
        conn, strat_id="s", instrument="AAPL", side=BUY, order_type=LIMIT, price=1, size=1
    )
    await send_cancel(conn, instrument="AAPL", side=BUY, price=1)
    await send_amend(conn, instrument="AAPL", side=BUY, price=1, new_size=2)
    kinds = [type(msg) for _, msg in conn.sent]
    assert kinds == [NewOrder, CancelOrder, AmendOrder]
    for _, msg in conn.sent:
        assert not msg.HasField("house_team")


# Receiving.


def accepted_payload(**extra: Any) -> dict[str, Any]:
    return {
        "request_ref": "r-1",
        "request_kind": "TICKET_REQUEST_SUBMIT",
        "ticket_id": "17",
        "receipt_time": "1791207000000",
        **extra,
    }


def state_payload(update_time: int, status: str = "TICKET_WORKING", **extra: Any) -> dict:
    return {
        "ticket_id": "17",
        "pod": "pod-a",
        "desk": "desk-1",
        "instrument": "AAPL",
        "side": "BUY",
        "shares": "500",
        "limit_price": str(LIMIT_PRICE),
        "urgency": "URGENCY_MEDIUM",
        "decision_price": "200000000",
        "accepted_time": "1791207000000",
        "planned_completion_time": "1791300000000",
        "due_shares": "100",
        "filled_shares": "80",
        "remaining_shares": "420",
        "average_fill_price": "200100000",
        "net_fee": "-1200",
        "status": status,
        "cure": False,
        "update_time": str(update_time),
        **extra,
    }


async def test_the_three_ticket_answers_are_decoded_and_none_is_an_order_event():
    events = await received(
        frame(
            "ticket_accepted",
            accepted_payload(
                decision_price="200000000",
                accepted_time="1791207000000",
                planned_completion_time="1791300000000",
            ),
            1,
        ),
        frame(
            "ticket_reject",
            {
                "request_ref": "r-2",
                "request_kind": "TICKET_REQUEST_CANCEL",
                "reason_code": "TICKET_NOT_FOUND",
                "receipt_time": "1791207000001",
            },
            2,
        ),
        frame("ticket_state", state_payload(1791207000002), 3),
    )
    assert [e.type for e in events] == ["ticket_accepted", "ticket_reject", "ticket_state"]
    assert all(isinstance(e, Received) for e in events)
    assert all(is_ticket_event(e) for e in events)
    assert not any(is_order_event(e) for e in events)
    accepted, reject, state = (e.message for e in events)
    assert isinstance(accepted, TicketAccepted)
    assert accepted.request_kind == TICKET_REQUEST_SUBMIT
    assert accepted.decision_price == 200_000_000
    assert isinstance(reject, TicketReject)
    assert reject.request_kind == TICKET_REQUEST_CANCEL
    assert ticket_reason_code_name(reject.reason_code) == "TICKET_NOT_FOUND"
    assert isinstance(state, TicketState)
    assert state.filled_shares == 80
    assert state.net_fee == -1200
    assert [e.report_seq for e in events] == [None, None, None]


async def test_a_ticket_reject_with_an_unknown_reason_keeps_its_name():
    [event] = await received(
        frame(
            "ticket_reject",
            {
                "request_kind": "TICKET_REQUEST_SUBMIT",
                "reason_code": "TICKET_BRAND_NEW_REASON",
                "receipt_time": "1",
            },
            1,
        )
    )
    assert event.message.reason_code == 0
    assert event.unknown_enum_names() == {"reason_code": "TICKET_BRAND_NEW_REASON"}
    assert ticket_request_ref_of(event.message) is None


async def test_a_ticket_reject_for_a_version_mismatch_raises_like_any_other_reject():
    async with exchange(
        [
            frame(
                "ticket_reject",
                {
                    "request_ref": "r",
                    "request_kind": "TICKET_REQUEST_SUBMIT",
                    "reason_code": "VERSION_MISMATCH",
                    "receipt_time": "1",
                },
                1,
            )
        ]
    ) as url:
        async with Connection(url) as conn:
            with pytest.raises(ContractVersionMismatch):
                async for _ in conn:
                    pass


def test_the_ticket_types_are_registered():
    assert TICKET_EVENT_TYPES <= INBOUND.keys()
    assert INBOUND["ticket_accepted"] is TicketAccepted
    assert INBOUND["ticket_reject"] is TicketReject
    assert INBOUND["ticket_state"] is TicketState


def test_shared_ticket_reasons_have_the_order_reasons_numbers():
    for name, number in TicketReasonCodes.TicketReasonCode.items():
        if number < 1700 and number != 0:
            assert ReasonCodes.ReasonCode.Value(name) == number
    assert ticket_reason_code_name(1707) == "TICKET_NOT_FOUND"
    assert ticket_reason_code_name(1799) == "1799"


def test_request_ref_of_each_ticket_answer():
    assert ticket_request_ref_of(TicketAccepted(request_ref="a")) == "a"
    assert ticket_request_ref_of(TicketReject(request_ref="b")) == "b"
    assert ticket_request_ref_of(TicketReject()) is None
    assert ticket_request_ref_of(TicketState(ticket_id="1")) is None


def test_a_childs_fill_names_its_ticket():
    assert parent_ticket_of(Execution(exec_id="e", parent_ticket_id="17")) == "17"
    assert parent_ticket_of(Execution(exec_id="e")) is None


async def test_a_child_cancelled_because_its_ticket_stopped_names_parent_stopped():
    [event] = await received(
        frame(
            "order_cancelled",
            {
                "origin": "TEAM",
                "strat_id": "desk-strat",
                "instrument": "AAPL",
                "side": "BUY",
                "price": str(LIMIT_PRICE),
                "cancelled_size": "100",
                "reason_code": "PARENT_STOPPED",
                "timestamp": "1",
            },
            1,
        )
    )
    assert is_order_event(event)
    assert not is_ticket_event(event)
    assert event.message.reason_code == ReasonCodes.PARENT_STOPPED
    assert reason_code_name(event.message.reason_code) == "PARENT_STOPPED"
    assert event.unknown_enum_names() == {}


async def test_a_stopped_ticket_carries_its_stop_time_and_mark():
    [event] = await received(
        frame(
            "ticket_state",
            state_payload(
                30,
                "TICKET_LIMIT_CANCELLED",
                stopped_time="1791207000500",
                stopped_mark="199000000",
            ),
            1,
        )
    )
    stopped = event.message
    assert stopped.status == TICKET_LIMIT_CANCELLED
    assert stopped_time_of(stopped) == 1_791_207_000_500
    assert stopped_mark_of(stopped) == 199_000_000


async def test_a_split_cancelled_ticket_decodes_by_name_and_number_and_is_stopped():
    [event] = await received(
        frame(
            "ticket_state",
            state_payload(
                30, "TICKET_SPLIT_CANCELLED", stopped_time="1791207000000", stopped_mark="0"
            ),
            1,
        )
    )
    stopped = event.message
    assert stopped.status == TICKET_SPLIT_CANCELLED == 7
    assert TicketStatus.Name(7) == "TICKET_SPLIT_CANCELLED"
    [by_number] = await received(frame("ticket_state", state_payload(31, 7), 1))
    assert by_number.message.status == TICKET_SPLIT_CANCELLED
    assert event.unknown_enum_names() == {}
    # The split's open has no valid mark, so the stop has none.
    assert stopped_mark_of(stopped) is None
    latest = LatestTickets()
    latest.update(state(stopped.ticket_id, 1))
    assert latest.update(stopped)
    assert latest.working() == []


async def test_a_stop_with_no_valid_mark_reads_as_no_mark():
    [event] = await received(
        frame(
            "ticket_state",
            state_payload(30, "TICKET_EXPIRED", stopped_time="1791207000500", stopped_mark="0"),
            1,
        )
    )
    assert event.message.HasField("stopped_mark")
    assert stopped_mark_of(event.message) is None
    assert stopped_time_of(event.message) == 1_791_207_000_500


def test_a_working_ticket_has_no_stop():
    working = state("17", 1)
    assert stopped_time_of(working) is None
    assert stopped_mark_of(working) is None


async def test_ticket_answers_are_not_market_data():
    events = await received(
        frame("ticket_accepted", accepted_payload(), 1),
        frame("ticket_state", state_payload(1), 2),
    )
    assert [as_market_data(e) for e in events] == [None, None]


# LatestTickets.


def state(ticket_id: str, update_time: int, status: int = TICKET_WORKING) -> TicketState:
    return TicketState(ticket_id=ticket_id, update_time=update_time, status=status)


def test_latest_tickets_keeps_the_newest_state_of_each_ticket():
    latest = LatestTickets()
    assert latest.update(state("17", 10))
    assert latest.update(state("17", 20, TICKET_COMPLETE))
    assert not latest.update(state("17", 15))
    assert latest.get("17").status == TICKET_COMPLETE
    assert latest.get("18") is None
    assert len(latest) == 1


def test_a_state_with_the_same_update_time_replaces_the_one_held():
    latest = LatestTickets()
    latest.update(state("17", 10))
    assert latest.update(state("17", 10, TICKET_CANCELLED_BY_POD))
    assert latest.get("17").status == TICKET_CANCELLED_BY_POD


def test_working_lists_only_working_tickets_in_numeric_order():
    latest = LatestTickets()
    for ticket_id, status in [("10", TICKET_WORKING), ("9", TICKET_WORKING), ("11", 99)]:
        latest.update(state(ticket_id, 1, status))
    latest.update(state("12", 1, TICKET_COMPLETE))
    assert [s.ticket_id for s in latest.working()] == ["9", "10"]
    assert [s.ticket_id for s in latest] == ["9", "10", "11", "12"]


async def test_latest_tickets_takes_events_and_ignores_everything_else():
    events = await received(
        frame("ticket_state", state_payload(5), 1),
        frame("ticket_accepted", accepted_payload(), 2),
        frame("book", {"instrument": "AAPL", "grid_time": "1"}, 3),
        frame("ticket_state", state_payload(3, "TICKET_CANCELLED_BY_POD"), 4),
    )
    latest = LatestTickets()
    changed = [latest.update(e) for e in events]
    assert changed == [True, False, False, False]
    assert latest.get("17").status == TICKET_WORKING
    latest.clear()
    assert len(latest) == 0


def test_a_disconnect_forgets_every_ticket_so_the_next_connection_rebuilds_them():
    latest = LatestTickets()
    latest.update(state("17", 10))
    latest.update(state("18", 10, TICKET_COMPLETE))
    assert latest.update(Disconnected(None))
    assert len(latest) == 0
    assert not latest.update(Disconnected(None))
    # The next connection sends every ticket of the term, at the connection time; a
    # ticket it leaves out is no longer held.
    assert latest.update(state("18", 20, TICKET_COMPLETE))
    assert [s.ticket_id for s in latest] == ["18"]


def test_a_sequence_gap_leaves_the_tickets_held():
    latest = LatestTickets()
    latest.update(state("17", 10))
    assert not latest.update(SeqGap(2, 4))
    assert latest.get("17") is not None


def ticket_frame(ticket_id: str, update_time: int, status: str = "TICKET_WORKING") -> str:
    payload = {**state_payload(update_time, status), "ticket_id": ticket_id}
    if status != "TICKET_WORKING":
        payload.update(stopped_time=str(update_time), stopped_mark="200050000")
    return frame("ticket_state", payload)


CANCEL_REFUSED = {
    "request_ref": "r-9",
    "request_kind": "TICKET_REQUEST_CANCEL",
    "reason_code": "TICKET_CURE_NOT_CANCELLABLE",
    "receipt_time": "60",
}


# The instruments table, which the exchange sends straight after the calendar.
INSTRUMENTS = frame(
    "instruments",
    {
        "instruments": [
            {
                "instrument": "AAPL",
                "kind": "EQUITY",
                "tick_size": "10000",
                "lot_size": "1",
                "status": "INSTRUMENT_TRADING",
                "tradable": True,
                "sector_limit": "Information Technology",
            }
        ]
    },
)


def connect_group(*states: str) -> list[str]:
    """What the exchange sends a pod or desk after the ack and the calendar, before it
    has read any resume: the instruments table, then the whole set of ticket states."""
    return [INSTRUMENTS, *states]


@pytest.mark.parametrize("wait_for_table", [False, True])
async def test_a_fresh_session_delivers_the_ticket_states_read_ahead_by_its_resume(
    wait_for_table,
):
    exchange = Scripted(
        {
            "term": TERM,
            "before_resume": connect_group(
                ticket_frame("17", 10), ticket_frame("18", 10, "TICKET_COMPLETE")
            ),
            # A replay of the reports after the cursor this session resumes from.
            "answer": [resume_ack(True, 3), order_state(2), order_state(3)],
            "after": [book(7)],
        }
    )
    latest = LatestTickets()
    async with serve_local(exchange) as url:
        async with await open_session(url, synthetic_token()) as session:
            if wait_for_table:
                assert await session.wait_for_instrument_table(timeout=5) is not None
            # The exchange sends the ticket states before it reads the resume, so the
            # session reads them ahead while it waits for the resume_ack.
            await session.resume(1)
            events = []
            async with asyncio.timeout(5):
                async for event in session:
                    events.append(event)
                    latest.update(event)
                    if isinstance(event, Received) and event.type == "book":
                        break
            assert session.last_report_seq == 3
    assert kinds(events) == [
        "calendar:None",
        "instruments:None",
        "ticket_state:None",
        "ticket_state:None",
        "resume_ack:None",
        "order_state:2",
        "order_state:3",
        "ResumeComplete",
        "book:None",
    ]
    assert [(s.ticket_id, s.status) for s in latest] == [
        ("17", TICKET_WORKING),
        ("18", TICKET_COMPLETE),
    ]


async def read_until_book(exchange: Scripted) -> tuple[list, LatestTickets, int | None]:
    latest = LatestTickets()
    async with serve_local(exchange) as url:
        rs = ReconnectingSession(url, synthetic_token(), sleep=Clock().sleep)
        async with rs:
            events = []
            async with asyncio.timeout(5):
                async for event in rs:
                    events.append(event)
                    latest.update(event)
                    if isinstance(event, Received) and event.type == "book":
                        break
            return events, latest, rs.last_report_seq


async def test_a_reconnect_delivers_the_new_connections_ticket_states_before_its_replay():
    exchange = Scripted(
        {
            "term": TERM,
            "before_resume": connect_group(ticket_frame("17", 10), ticket_frame("18", 10)),
            "answer": [resume_ack()],
            "after": [order_state(1)],
            "drop": True,
        },
        {
            "term": TERM,
            # Every ticket of the term again, stopped or working: 17 stopped meanwhile.
            "before_resume": connect_group(
                ticket_frame("17", 50, "TICKET_CANCELLED_BY_POD"), ticket_frame("18", 50)
            ),
            "answer": [resume_ack(True, 2), order_state(2)],
            # A refused cancel changes nothing: 18 is still working.
            "after": [frame("ticket_reject", CANCEL_REFUSED), book(7)],
        },
    )
    events, latest, last_report_seq = await read_until_book(exchange)
    # Ticket answers carry no report number, so they move no resume cursor.
    assert last_report_seq == 2
    reconnected = kinds(events)[kinds(events).index("Disconnected") :]
    assert reconnected[:3] == ["Disconnected", "Retrying", "Connected"]
    assert reconnected[3:] == [
        "calendar:None",
        "instruments:None",
        "ticket_state:None",
        "ticket_state:None",
        "resume_ack:None",
        "order_state:2",
        "ResumeComplete",
        "ticket_reject:None",
        "book:None",
    ]
    assert [(s.ticket_id, s.status) for s in latest] == [
        ("17", TICKET_CANCELLED_BY_POD),
        ("18", TICKET_WORKING),
    ]
    assert stopped_mark_of(latest.get("17")) == 200_050_000
    assert [s.ticket_id for s in latest.working()] == ["18"]


async def test_a_ticket_of_an_earlier_term_is_not_held_after_reconnecting_in_a_new_one():
    exchange = Scripted(
        {
            "term": TERM,
            "before_resume": connect_group(ticket_frame("17", 10, "TICKET_EXPIRED")),
            "answer": [resume_ack()],
            "drop": True,
        },
        # The new term has no tickets yet, so none is sent.
        {
            "term": NEXT_TERM,
            "before_resume": connect_group(),
            "answer": [resume_ack()],
            "after": [book(7)],
        },
    )
    events, latest, _ = await read_until_book(exchange)
    assert kinds(events).count("ticket_state:None") == 1
    assert len(latest) == 0


def test_docstring_examples():
    assert doctest.testmod(tickets).failed == 0


# Automatic execution: the algorithm, the SERVICE executor, execution_route and the fills
# of the exchange's own children.


@pytest.mark.parametrize("algorithm", [TWAP, VWAP])
async def test_a_ticket_carries_the_algorithm_given(algorithm):
    _, _, msg = await one_sent(
        lambda c: send_ticket(
            c,
            instrument="AAPL",
            side=BUY,
            shares=500,
            limit_price=LIMIT_PRICE,
            urgency=URGENCY_MEDIUM,
            algorithm=algorithm,
        )
    )
    assert msg.algorithm == algorithm


async def test_a_ticket_with_no_algorithm_sends_the_unset_value_the_exchange_reads_as_twap():
    _, _, msg = await one_sent(
        lambda c: send_ticket(
            c, instrument="AAPL", side=BUY, shares=1, limit_price=1, urgency=URGENCY_LOW
        )
    )
    assert msg.algorithm == TICKET_ALGORITHM_UNSPECIFIED


@pytest.mark.parametrize("algorithm", [TWAP, VWAP])
async def test_a_cure_ticket_may_name_either_algorithm_and_sends_it_as_given(algorithm):
    # The exchange works a cure ticket as TWAP whatever it names, so naming VWAP is not
    # an error to refuse: ticket_accepted.algorithm says TWAP.
    _, _, msg = await one_sent(
        lambda c: send_ticket(
            c,
            instrument="AAPL",
            side=SELL,
            shares=1,
            urgency=URGENCY_LOW,
            cure=True,
            algorithm=algorithm,
        )
    )
    assert msg.cure and msg.algorithm == algorithm


async def test_a_vwap_ticket_goes_on_the_wire_by_name():
    inbox: list[str] = []
    async with exchange([], inbox) as url:
        async with Connection(url) as conn:
            await send_ticket(
                conn,
                instrument="AAPL",
                side=BUY,
                shares=500,
                limit_price=LIMIT_PRICE,
                urgency=URGENCY_MEDIUM,
                algorithm=VWAP,
                request_ref="r-1",
            )
            async for _ in conn:
                pass
    [raw] = inbox
    assert json.loads(raw)["payload"]["algorithm"] == "VWAP"


def test_the_algorithms_are_the_contracts_twap_and_vwap():
    assert (tickets.TWAP, tickets.VWAP) == (1, 2)
    assert {"TWAP", "VWAP"} <= set(tickets.__all__)


def test_the_vwap_refusal_is_named():
    assert ticket_reason_code_name(1713) == "TICKET_ALGORITHM_UNAVAILABLE"
    assert TicketReasonCodes.TICKET_ALGORITHM_UNAVAILABLE == 1713


def test_the_route_off_refusal_is_named():
    assert ticket_reason_code_name(1714) == "TICKET_SERVICE_OFF"
    assert TicketReasonCodes.TICKET_SERVICE_OFF == 1714


def service_state_payload(update_time: int, **extra: Any) -> dict:
    fields = {
        "algorithm": "VWAP",
        "executor": "SERVICE",
        "activity": "TAKING",
        "behind_schedule": True,
        "revised_completion_time": "1791400000000",
        "config_version": 4,
        "last_assessment_time": "1791207000500",
        "filled_notional": "16008000000",
    }
    payload = state_payload(update_time, **{**fields, **extra})
    del payload["desk"]  # a SERVICE ticket has no desk
    return payload


async def test_a_service_ticket_is_accepted_with_its_algorithm_and_executor():
    [event] = await received(
        frame("ticket_accepted", accepted_payload(algorithm="VWAP", executor="SERVICE"), 1)
    )
    assert event.message.algorithm == VWAP
    assert event.message.executor == SERVICE
    assert event.unknown_enum_names() == {}


async def test_a_service_tickets_progress_reaches_latest_tickets():
    events = await received(
        frame("ticket_state", service_state_payload(10), 1),
        frame("ticket_state", service_state_payload(20, activity="MESSAGE_BUDGET"), 2),
    )
    latest = LatestTickets()
    assert [latest.update(e) for e in events] == [True, True]
    held = latest.get("17")
    assert held.desk == ""
    assert held.executor == SERVICE
    assert held.algorithm == VWAP
    assert held.activity == MESSAGE_BUDGET
    assert held.behind_schedule is True
    assert held.revised_completion_time == 1_791_400_000_000
    assert held.planned_completion_time == 1_791_300_000_000
    assert held.config_version == 4
    assert held.last_assessment_time == 1_791_207_000_500
    assert held.filled_notional == 16_008_000_000
    assert [s.ticket_id for s in latest.working()] == ["17"]
    assert events[0].message.activity == TAKING


async def test_an_activity_this_sdk_does_not_know_keeps_its_name():
    [event] = await received(
        frame("ticket_state", service_state_payload(10, activity="SOMETHING_NEW"), 1)
    )
    assert event.message.activity == TICKET_ACTIVITY_UNSPECIFIED
    assert event.unknown_enum_names() == {"activity": "SOMETHING_NEW"}


async def test_a_status_this_sdk_does_not_know_is_a_stop_that_keeps_its_name():
    # A newer contract can add a way a ticket stops (a Director's cancel, say): it decodes,
    # keeps the name the exchange sent, and the ticket is no longer working.
    events = await received(
        frame("ticket_state", state_payload(10), 1),
        frame("ticket_state", state_payload(20, "TICKET_CANCELLED_BY_SOMEONE_NEW"), 2),
    )
    latest = LatestTickets()
    assert [latest.update(e) for e in events] == [True, True]
    assert events[1].message.status == TICKET_STATUS_UNSPECIFIED
    assert events[1].unknown_enum_names() == {"status": "TICKET_CANCELLED_BY_SOMEONE_NEW"}
    assert latest.working() == []


async def test_a_desk_ticket_has_no_service_fields_and_a_handover_carries_the_desks_part():
    events = await received(
        frame("ticket_state", state_payload(10, executor="DESK"), 1),
        frame(
            "ticket_state",
            state_payload(
                20,
                executor="ENGINE",
                handover_time="1791207000400",
                desk_filled_shares="50",
                desk_net_fee="-700",
                desk_average_fill_price="200050000",
            ),
            2,
        ),
    )
    desk, engine = (e.message for e in events)
    assert desk.executor == DESK
    assert desk.activity == TICKET_ACTIVITY_UNSPECIFIED
    assert not desk.HasField("handover_time")
    assert not desk.HasField("revised_completion_time")
    assert desk.config_version == 0
    assert engine.executor == ENGINE
    assert engine.status == TICKET_WORKING
    assert engine.handover_time == 1_791_207_000_400
    assert (engine.desk_filled_shares, engine.desk_net_fee) == (50, -700)
    assert engine.desk_average_fill_price == 200_050_000


@pytest.mark.parametrize("origin", ["AUTO_EXECUTION", "FALLBACK"])
async def test_a_fill_of_the_exchanges_child_names_its_ticket_and_origin(origin):
    [event] = await received(
        frame(
            "execution",
            {
                "exec_id": "x-1",
                "origin": origin,
                "instrument": "AAPL",
                "side": "BUY",
                "fill_price": "200100000",
                "fill_size": "40",
                "remaining_size": "0",
                "parent_ticket_id": "17",
            },
            1,
            report_seq=5,
        )
    )
    assert is_order_event(event)
    assert event.message.origin == {"AUTO_EXECUTION": AUTO_EXECUTION, "FALLBACK": FALLBACK}[origin]
    assert event.unknown_enum_names() == {}
    assert parent_ticket_of(event.message) == "17"
    assert not event.message.HasField("strat_id")


def test_a_handed_over_childs_reject_is_named():
    assert reason_code_name(ReasonCodes.PARENT_HANDED_OVER) == "PARENT_HANDED_OVER"


def route_payload(**extra: Any) -> dict[str, Any]:
    return {"routing_in_force": "ROUTING_SERVICE", "config_version": 4, **extra}


def test_execution_route_is_registered():
    assert INBOUND[EXECUTION_ROUTE_TYPE] is ExecutionRoute
    assert EXECUTION_ROUTE_TYPE not in TICKET_EVENT_TYPES


async def test_execution_route_is_decoded_and_is_neither_a_ticket_nor_an_order_event():
    [event] = await received(
        frame("execution_route", route_payload(routing_next_open="ROUTING_DESK", paused=True), 1)
    )
    assert isinstance(event, Received)
    assert is_execution_route(event)
    assert not is_ticket_event(event)
    assert not is_order_event(event)
    assert as_market_data(event) is None
    route = event.message
    assert route.routing_in_force == ROUTING_SERVICE
    assert route.HasField("routing_next_open") and route.routing_next_open == ROUTING_DESK
    assert route.config_version == 4
    assert route.paused is True
    assert event.report_seq is None


async def test_latest_tickets_keeps_the_newest_route_and_forgets_it_on_a_disconnect():
    events = await received(
        frame("execution_route", {"routing_in_force": "ROUTING_DESK"}, 1),
        frame("execution_route", route_payload(), 2),
        frame("execution_route", route_payload(), 3),
    )
    latest = LatestTickets()
    assert latest.route is None
    assert [latest.update(e) for e in events] == [True, True, False]
    assert latest.route.routing_in_force == ROUTING_SERVICE
    assert not latest.route.HasField("routing_next_open")
    assert len(latest) == 0
    assert latest.update(Disconnected(None))
    assert latest.route is None
    latest.update(events[0])
    latest.clear()
    assert latest.route is None


DESK_ROUTE = frame("execution_route", {"routing_in_force": "ROUTING_DESK"})
SERVICE_ROUTE = frame("execution_route", route_payload())
# A control logged mid-session: the service paused, under a new configuration.
PAUSED_ROUTE = frame("execution_route", route_payload(config_version=5, paused=True))


@pytest.mark.parametrize("first", [True, False], ids=["before-states", "after-states"])
async def test_a_reconnect_delivers_each_connections_route_before_its_replay(first):
    def group(route: str, *states: str) -> list[str]:
        # Contract 01 sends the calendar and the instruments straight after the ack; the
        # route follows, at a point among the ticket states the contract does not fix.
        return connect_group(route, *states) if first else connect_group(*states, route)

    exchange = Scripted(
        {
            "term": TERM,
            "before_resume": group(DESK_ROUTE, ticket_frame("17", 10)),
            "answer": [resume_ack()],
            "after": [order_state(1)],
            "drop": True,
        },
        {
            "term": TERM,
            "before_resume": group(SERVICE_ROUTE, ticket_frame("17", 50)),
            # A report missed while disconnected is replayed after the route arrives.
            "answer": [resume_ack(True, 2), order_state(2)],
            "after": [PAUSED_ROUTE, book(7)],
        },
    )
    events, latest, last_report_seq = await read_until_book(exchange)
    assert last_report_seq == 2
    reconnected = kinds(events)[kinds(events).index("Disconnected") :]
    states = ["ticket_state:None"]
    expected = ["execution_route:None", *states] if first else [*states, "execution_route:None"]
    assert reconnected[3:] == [
        "calendar:None",
        "instruments:None",
        *expected,
        "resume_ack:None",
        "order_state:2",
        "ResumeComplete",
        "execution_route:None",
        "book:None",
    ]
    routes = [e.message for e in events if is_execution_route(e)]
    assert [(r.routing_in_force, r.config_version, r.paused) for r in routes] == [
        (ROUTING_DESK, 0, False),
        (ROUTING_SERVICE, 4, False),
        (ROUTING_SERVICE, 5, True),
    ]
    assert latest.route == routes[-1]
    assert latest.get("17").update_time == 50
