"""The term-end close and the break requests (qte-platform #2633 and #2481, contract at
7e47b07b): `term_close_fill`, `term_end`, `break_request_state` and `break_request_reject`
decode as named types, and a Fundamentals pod sends `break_request` and
`break_request_withdraw` with checks made before anything is sent."""

from pathlib import Path

import pytest
from fake_exchange import frame
from test_orders import Recorder, received

from qte_sdk import tickets
from qte_sdk.connection import Received, Unknown
from qte_sdk.contract.registry import INBOUND, OUTBOUND
from qte_sdk.contract.v1 import common_pb2, order_entry_pb2, order_events_pb2
from qte_sdk.contract.v1.common_pb2 import (
    BREAK_REQUEST_APPROVED,
    BREAK_REQUEST_EXPIRED,
    BREAK_REQUEST_FILLED,
    BREAK_REQUEST_PENDING,
    BREAK_REQUEST_REFUSED,
    BREAK_REQUEST_STATUS_UNSPECIFIED,
    BREAK_REQUEST_SUBMIT,
    BREAK_REQUEST_WITHDRAW,
    BREAK_REQUEST_WITHDRAWN,
    BUY,
    SELL,
    SIDE_UNSPECIFIED,
    TicketReasonCodes,
)
from qte_sdk.contract.v1.order_entry_pb2 import SubmitBreakRequest, WithdrawBreakRequest
from qte_sdk.contract.v1.order_events_pb2 import (
    BreakRequestReject,
    BreakRequestState,
    TermCloseFill,
    TermEnd,
)
from qte_sdk.tickets import (
    BREAK_REQUEST_EVENT_TYPES,
    break_request_ref_of,
    is_break_request_event,
    is_ticket_event,
    send_break_request,
    send_break_request_withdraw,
    ticket_reason_code_name,
)

# Frames as the exchange sends them (schemas/contract/03 and 11 at 7e47b07b).

TEAM_CLOSE = {
    "instrument": "AAPL",
    "side": "SELL",
    "size": "40",
    "price": "200000000",
    "proceeds": "8000000000",
    "session_date": "2026-12-04",
    "frozen": False,
    "prior_close": False,
    "house": False,
}

HOUSE_CLOSE = {
    "instrument": "MSFT",
    "side": "BUY",
    "size": "5",
    "price": "100000000",
    "proceeds": "-500000000",
    "session_date": "2026-12-04",
    "frozen": True,
    "prior_close": False,
    "house": True,
}

APPROVED = {
    "break_request_id": "73104",
    "instrument": "AAPL",
    "side": "SELL",
    "shares": "5000",
    "thesis_id": "TH-2026-014",
    "thesis_version": 2,
    "risk_statement": "Earnings land on the second break day.",
    "status": "BREAK_REQUEST_APPROVED",
    "accepted_time": "100",
    "decided_time": "200",
    "decision_reason": "Earnings gap risk accepted.",
    "update_time": "200",
}

NOT_IN_BREAK = {
    "request_ref": "b-1",
    "request_kind": "BREAK_REQUEST_SUBMIT",
    "reason_code": "BREAK_REQUEST_NOT_IN_BREAK",
    "reason_detail": "a break request is taken only in an inter-term break",
    "receipt_time": "5",
}


# The registry.


def test_the_new_inbound_types_are_registered():
    assert INBOUND["term_close_fill"] is TermCloseFill
    assert INBOUND["term_end"] is TermEnd
    assert INBOUND["break_request_state"] is BreakRequestState
    assert INBOUND["break_request_reject"] is BreakRequestReject
    assert BREAK_REQUEST_EVENT_TYPES <= INBOUND.keys()


def test_the_break_request_messages_are_registered_outbound():
    assert OUTBOUND["break_request"] is SubmitBreakRequest
    assert OUTBOUND["break_request_withdraw"] is WithdrawBreakRequest


def test_every_outbound_type_is_an_order_entry_or_session_message():
    for type_, cls in OUTBOUND.items():
        package = cls.DESCRIPTOR.file.package
        assert package == "qte.contract.v1", type_
        assert cls.DESCRIPTOR.file.name.split("/")[-1] in {
            "order_entry.proto",
            "session.proto",
        }, type_
    assert not OUTBOUND.keys() & INBOUND.keys()


# Decoding: named types, never Unknown.


async def test_a_team_term_close_fill_decodes_with_its_report_seq():
    [event] = await received(frame("term_close_fill", TEAM_CLOSE, report_seq="412"))
    assert isinstance(event, Received)
    assert event.type == "term_close_fill"
    assert event.report_seq == 412
    assert event.message == TermCloseFill(
        instrument="AAPL",
        side=SELL,
        size=40,
        price=200_000_000,
        proceeds=8_000_000_000,
        session_date="2026-12-04",
    )


async def test_a_house_term_close_fill_decodes_unnumbered():
    [event] = await received(frame("term_close_fill", HOUSE_CLOSE))
    assert isinstance(event, Received)
    assert event.report_seq is None
    fill = event.message
    assert (fill.side, fill.size, fill.proceeds) == (BUY, 5, -500_000_000)
    assert fill.frozen and fill.house and not fill.prior_close


async def test_term_end_decodes():
    payload = {"session_date": "2026-12-04", "term_id": "winter-2026"}
    [event] = await received(frame("term_end", payload))
    assert isinstance(event, Received) and not isinstance(event, Unknown)
    assert event.report_seq is None
    assert event.message == TermEnd(session_date="2026-12-04", term_id="winter-2026")
    assert not is_break_request_event(event)


async def test_a_break_request_state_decodes():
    [event] = await received(frame("break_request_state", APPROVED))
    assert is_break_request_event(event)
    assert not is_ticket_event(event)
    state = event.message
    assert isinstance(state, BreakRequestState)
    assert state.status == BREAK_REQUEST_APPROVED
    assert (state.break_request_id, state.side, state.shares) == ("73104", SELL, 5000)
    assert (state.thesis_id, state.thesis_version) == ("TH-2026-014", 2)
    assert state.HasField("decided_time") and state.decided_time == 200
    assert state.decision_reason == "Earnings gap risk accepted."
    assert event.report_seq is None
    # A decision answers no message of the pod's.
    assert break_request_ref_of(state) is None


async def test_a_break_request_state_names_its_pod():
    # BreakRequestState.pod (field 14) from qte-platform 8deb128e: the pod that sent the
    # request, on every copy, the pod's own included.
    [event] = await received(frame("break_request_state", {**APPROVED, "pod": "pod-3"}))
    state = event.message
    assert isinstance(state, BreakRequestState)
    assert state.pod == "pod-3"
    assert BreakRequestState.DESCRIPTOR.fields_by_name["pod"].number == 14
    assert BreakRequestState.FromString(state.SerializeToString()).pod == "pod-3"


async def test_a_pending_state_echoes_the_request_ref_and_has_no_decision():
    pending = {**APPROVED, "status": "BREAK_REQUEST_PENDING", "request_ref": "b-1"}
    del pending["decided_time"], pending["decision_reason"]
    [event] = await received(frame("break_request_state", pending))
    state = event.message
    assert state.status == BREAK_REQUEST_PENDING
    assert not state.HasField("decided_time")
    assert not state.HasField("decision_reason")
    assert break_request_ref_of(state) == "b-1"


async def test_a_break_request_reject_decodes_with_its_reason():
    [event] = await received(frame("break_request_reject", NOT_IN_BREAK))
    assert is_break_request_event(event)
    reject = event.message
    assert isinstance(reject, BreakRequestReject)
    assert reject.request_kind == BREAK_REQUEST_SUBMIT
    assert reject.reason_code == TicketReasonCodes.BREAK_REQUEST_NOT_IN_BREAK == 1721
    assert ticket_reason_code_name(reject.reason_code) == "BREAK_REQUEST_NOT_IN_BREAK"
    assert break_request_ref_of(reject) == "b-1"
    assert event.report_seq is None


async def test_a_break_request_reject_with_no_request_ref_echoes_none():
    payload = {
        "request_kind": "BREAK_REQUEST_WITHDRAW",
        "reason_code": "MALFORMED_MESSAGE",
        "receipt_time": "5",
    }
    [event] = await received(frame("break_request_reject", payload))
    assert event.message.request_kind == BREAK_REQUEST_WITHDRAW
    assert break_request_ref_of(event.message) is None


async def test_a_status_this_sdk_does_not_know_decodes_as_unspecified():
    newer = {**APPROVED, "status": "BREAK_REQUEST_SOMETHING_NEW"}
    [event] = await received(frame("break_request_state", newer))
    assert event.message.status == BREAK_REQUEST_STATUS_UNSPECIFIED
    assert "BREAK_REQUEST_SOMETHING_NEW" in str(event.unknown_enum_names())


def test_break_request_ref_of_reads_nothing_from_other_messages():
    assert break_request_ref_of(TermCloseFill()) is None
    assert break_request_ref_of(order_events_pb2.TicketReject(request_ref="t-1")) is None


def test_the_contract_numbers_the_new_codes_and_statuses():
    assert TicketReasonCodes.BREAK_REQUEST_NOT_IN_BREAK == 1721
    assert TicketReasonCodes.BREAK_REQUEST_EXISTS == 1722
    assert ticket_reason_code_name(1722) == "BREAK_REQUEST_EXISTS"
    assert [
        BREAK_REQUEST_PENDING,
        BREAK_REQUEST_APPROVED,
        BREAK_REQUEST_REFUSED,
        BREAK_REQUEST_WITHDRAWN,
        BREAK_REQUEST_FILLED,
        BREAK_REQUEST_EXPIRED,
    ] == [1, 2, 3, 4, 5, 6]
    assert (BREAK_REQUEST_SUBMIT, BREAK_REQUEST_WITHDRAW) == (1, 2)


def test_the_vendored_field_numbers_are_the_contracts():
    def numbers(cls) -> dict[str, int]:
        return {f.name: f.number for f in cls.DESCRIPTOR.fields}

    assert numbers(TermCloseFill) == {
        "instrument": 1,
        "side": 2,
        "size": 3,
        "price": 4,
        "proceeds": 5,
        "session_date": 6,
        "frozen": 7,
        "prior_close": 8,
        "house": 9,
    }
    assert numbers(TermEnd) == {"session_date": 1, "term_id": 2}
    assert numbers(SubmitBreakRequest) == {
        "request_ref": 1,
        "instrument": 2,
        "side": 3,
        "shares": 4,
        "thesis_id": 5,
        "thesis_version": 6,
        "risk_statement": 7,
    }
    assert numbers(WithdrawBreakRequest) == {"request_ref": 1, "break_request_id": 2}
    assert numbers(BreakRequestReject) == {
        "request_ref": 1,
        "request_kind": 2,
        "reason_code": 3,
        "reason_detail": 4,
        "receipt_time": 5,
    }
    assert numbers(BreakRequestState)["update_time"] == 13
    assert numbers(BreakRequestState)["pod"] == 14
    assert len(BreakRequestState.DESCRIPTOR.fields) == 14
    # The modules the generator wrote are the ones the registry imports.
    assert order_entry_pb2.SubmitBreakRequest is SubmitBreakRequest
    assert common_pb2.BreakRequestStatus.Name(4) == "BREAK_REQUEST_WITHDRAWN"


# Sending a break request.


def send_request(conn, **overrides):
    kwargs = {
        "instrument": "AAPL",
        "side": SELL,
        "shares": 5000,
        "thesis_id": "TH-2026-014",
        "thesis_version": 2,
        "risk_statement": "Earnings land on the second break day.",
    }
    kwargs.update(overrides)
    return send_break_request(conn, **kwargs)


async def test_a_break_request_sends_every_field_and_returns_its_request_ref():
    conn = Recorder()
    ref = await send_request(conn, request_ref="b-1")
    assert ref == "b-1"
    assert conn.sent == [
        (
            "break_request",
            SubmitBreakRequest(
                request_ref="b-1",
                instrument="AAPL",
                side=SELL,
                shares=5000,
                thesis_id="TH-2026-014",
                thesis_version=2,
                risk_statement="Earnings land on the second break day.",
            ),
        )
    ]


async def test_a_break_request_makes_a_fresh_request_ref():
    conn = Recorder()
    ref = await send_request(conn, side=BUY)
    [(_, msg)] = conn.sent
    assert len(ref) == 32 and msg.request_ref == ref and msg.side == BUY


async def test_a_risk_statement_of_280_characters_is_sent():
    conn = Recorder()
    await send_request(conn, risk_statement="r" * 280)
    assert conn.sent[0][1].risk_statement == "r" * 280


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"request_ref": ""}, "^request_ref must be 1 to 32 bytes"),
        ({"instrument": ""}, "^instrument must be 1 to 32 bytes"),
        ({"instrument": "A" * 33}, "^instrument must be 1 to 32 bytes"),
        ({"thesis_id": ""}, "^thesis_id must be 1 to 32 bytes"),
        ({"thesis_id": "T\0"}, "^thesis_id must not contain"),
        ({"side": SIDE_UNSPECIFIED}, "^side must be BUY or SELL"),
        ({"risk_statement": ""}, "^risk_statement must be 1 to 280 characters, got 0"),
        ({"risk_statement": "r" * 281}, "^risk_statement must be 1 to 280 characters, got 281"),
        ({"risk_statement": "a\0b"}, "^risk_statement must not contain the NUL character"),
    ],
)
async def test_a_break_request_that_breaks_a_rule_is_not_sent(overrides, message):
    conn = Recorder()
    with pytest.raises(ValueError, match=message):
        await send_request(conn, **overrides)
    assert conn.sent == []


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"instrument": 1}, "^instrument must be a str"),
        ({"thesis_id": 2}, "^thesis_id must be a str"),
        ({"risk_statement": None}, "^risk_statement must be a str"),
    ],
)
async def test_a_break_request_field_of_the_wrong_type_is_refused(overrides, message):
    conn = Recorder()
    with pytest.raises(TypeError, match=message):
        await send_request(conn, **overrides)
    assert conn.sent == []


async def test_a_break_request_withdraw_names_the_request():
    conn = Recorder()
    ref = await send_break_request_withdraw(conn, break_request_id="73104", request_ref="b-2")
    assert ref == "b-2"
    assert conn.sent == [
        (
            "break_request_withdraw",
            WithdrawBreakRequest(request_ref="b-2", break_request_id="73104"),
        )
    ]


@pytest.mark.parametrize("bad", ["", "7a", "-1", "１２"])
async def test_a_withdraw_needs_a_decimal_break_request_id(bad):
    conn = Recorder()
    with pytest.raises(ValueError, match="^break_request_id must be a non-empty string"):
        await send_break_request_withdraw(conn, break_request_id=bad)
    assert conn.sent == []


async def test_a_break_request_withdraw_makes_a_fresh_request_ref():
    conn = Recorder()
    ref = await send_break_request_withdraw(conn, break_request_id="73104")
    [(_, msg)] = conn.sent
    assert len(ref) == 32 and msg.request_ref == ref


@pytest.mark.parametrize("bad", ["", "r" * 33])
async def test_a_withdraw_with_a_bad_request_ref_is_not_sent(bad):
    conn = Recorder()
    with pytest.raises(ValueError, match="^request_ref must be 1 to 32 bytes"):
        await send_break_request_withdraw(conn, break_request_id="73104", request_ref=bad)
    assert conn.sent == []


async def test_a_withdraw_of_the_wrong_type_is_refused():
    conn = Recorder()
    with pytest.raises(TypeError, match="^break_request_id must be a str"):
        await send_break_request_withdraw(conn, break_request_id=73104)  # type: ignore[arg-type]
    assert conn.sent == []


def test_the_send_helpers_are_exported_and_documented():
    for name in (
        "send_break_request",
        "send_break_request_withdraw",
        "is_break_request_event",
        "break_request_ref_of",
        "BREAK_REQUEST_EVENT_TYPES",
        "BreakRequestEvent",
    ):
        assert name in tickets.__all__
    doc = send_break_request.__doc__ or ""
    assert "BREAK_REQUEST_NOT_IN_BREAK" in doc and "(1721)" in doc
    assert "BREAK_REQUEST_EXISTS" in doc and "(1722)" in doc
    assert "after the close of" in doc and "before the next term's first" in doc


ROOT = Path(__file__).resolve().parent.parent
RESENT_ON_CONNECT = (
    "once on each connection, after its `ticket_state` messages, the pod's session is sent"
    " one `break_request_state` for each of the pod's requests, in ascending"
    " `break_request_id`, and not again after a resume"
)


@pytest.mark.parametrize(
    "path", ["AGENTS.md", "qte_sdk/AGENTS.md", "docs/automatic-execution.md", "docs/quickstart.md"]
)
def test_the_docs_say_break_requests_are_resent_on_connect(path):
    text = (ROOT / path).read_text(encoding="utf-8")
    assert RESENT_ON_CONNECT in text
    assert "a later session is not sent them" not in text
    assert "a session that connects later is not sent the state" not in text
    assert "as it arrives: none is resent" not in text


def test_the_tickets_docstring_says_break_requests_are_resent_on_connect():
    doc = " ".join((tickets.__doc__ or "").split())
    assert (
        "once on each connection, after that connection's `ticket_state` messages, the pod's"
        " session is sent one `break_request_state` for each of the pod's requests" in doc
    )
    assert "not again after a resume" in doc
    assert "is not sent the request's state" not in doc
    assert "Its `pod` names the pod that sent the request" in doc
