import pytest
from fake_exchange import frame
from test_orders import Recorder, received, sent_by

from qte_sdk.account import (
    LAST_OFFICIAL_CLOSE,
    LIVE_MARK,
    AccountState,
    AccountSummary,
    PositionValue,
    is_account_state,
    send_account_query,
)
from qte_sdk.connection import Received
from qte_sdk.contract.v1.common_pb2 import LOSS_LEVEL_NONE, ReasonCodes
from qte_sdk.contract.v1.order_events_pb2 import ObligationState, Reject
from qte_sdk.contract.v1.session_pb2 import AccountQuery
from qte_sdk.orders import is_order_event, reason_code_name, request_ref_of

SUMMARY = {
    "equity": "1000500000",
    "cash": "800000000",
    "previous_close_equity": "1000000000",
    "daily_pnl": "500000",
    "loss_level": "LOSS_LEVEL_NONE",
    "timestamp": "42",
}


# Sending.


async def test_query_sends_only_its_request_ref():
    ref, env = await sent_by(lambda conn: send_account_query(conn))
    assert env["version"] == "0.x"
    assert env["type"] == "account_query"
    assert env["payload"] == {"request_ref": ref}
    assert len(ref) == 32


async def test_query_uses_a_given_request_ref():
    sender = Recorder()
    ref = await send_account_query(sender, request_ref="acct-1")
    assert ref == "acct-1"
    assert sender.sent == [("account_query", AccountQuery(request_ref="acct-1"))]


@pytest.mark.parametrize("bad", ["", "x" * 33, "a\0b"])
async def test_query_refuses_a_bad_request_ref_and_sends_nothing(bad):
    sender = Recorder()
    with pytest.raises(ValueError, match="request_ref"):
        await send_account_query(sender, request_ref=bad)
    assert sender.sent == []


# Receiving.


async def test_reply_with_summary_and_positions_decodes_typed():
    [event] = await received(
        frame(
            "account_state",
            {
                "request_ref": "acct-1",
                "summary": SUMMARY,
                "positions": [
                    {"instrument": "AAPL", "quantity": "100", "price": "199970000"},
                    {"instrument": "MSFT", "quantity": "-20", "price": "410250000"},
                ],
                "valuation_basis": "LIVE_MARK",
                "session_date": "2026-10-02",
                "as_of": "42",
                "cash": "800000000",
            },
            1,
        )
    )
    assert is_account_state(event)
    assert not is_order_event(event)
    state = event.message
    assert state == AccountState(
        request_ref="acct-1",
        summary=AccountSummary(
            equity=1_000_500_000,
            cash=800_000_000,
            previous_close_equity=1_000_000_000,
            daily_pnl=500_000,
            loss_level=LOSS_LEVEL_NONE,
            timestamp=42,
        ),
        positions=[
            PositionValue(instrument="AAPL", quantity=100, price=199_970_000),
            PositionValue(instrument="MSFT", quantity=-20, price=410_250_000),
        ],
        valuation_basis=LIVE_MARK,
        session_date="2026-10-02",
        as_of=42,
        cash=800_000_000,
    )
    assert state.HasField("summary")
    assert state.summary.cash == state.cash


async def test_reply_for_a_desk_or_the_house_has_no_summary():
    [event] = await received(
        frame(
            "account_state",
            {
                "request_ref": "acct-2",
                "valuation_basis": "LAST_OFFICIAL_CLOSE",
                "session_date": "2026-10-02",
                "as_of": "7",
                "cash": "-1500000",
            },
            1,
        )
    )
    assert is_account_state(event)
    state = event.message
    assert not state.HasField("summary")
    assert list(state.positions) == []
    assert state.valuation_basis == LAST_OFFICIAL_CLOSE
    assert state.cash == -1_500_000
    assert state.HasField("session_date")


async def test_reply_before_the_first_session_has_no_session_date():
    [event] = await received(
        frame(
            "account_state",
            {"request_ref": "acct-3", "valuation_basis": "LAST_OFFICIAL_CLOSE", "as_of": "1"},
            1,
        )
    )
    assert not event.message.HasField("session_date")


async def test_refused_query_is_a_reject_echoing_its_request_ref():
    [event] = await received(
        frame(
            "reject",
            {"request_ref": "acct-4", "reason_code": "TEAM_DISABLED", "receipt_time": "5"},
            1,
        )
    )
    assert is_order_event(event)
    assert not is_account_state(event)
    assert event.message == Reject(
        request_ref="acct-4", reason_code=ReasonCodes.TEAM_DISABLED, receipt_time=5
    )
    assert request_ref_of(event.message) == "acct-4"
    assert not event.message.HasField("request_type")
    assert reason_code_name(event.message.reason_code) == "TEAM_DISABLED"


async def test_account_summary_and_obligation_state_decode_typed():
    events = await received(
        frame("account_summary", SUMMARY, 1),
        frame("obligation_state", {"entries": [{"instrument": "AAPL"}], "timestamp": "3"}, 2),
    )
    assert all(isinstance(e, Received) for e in events)
    summary, obligations = (e.message for e in events)
    assert isinstance(summary, AccountSummary)
    assert summary.equity == 1_000_500_000
    assert isinstance(obligations, ObligationState)
    assert obligations.entries[0].instrument == "AAPL"
