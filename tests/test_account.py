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
from qte_sdk.contract.v1.common_pb2 import (
    ACCOUNT_QUERY,
    LIMIT_GROSS,
    LIMIT_INSTRUMENT,
    LOSS_LEVEL_NONE,
    ReasonCodes,
)
from qte_sdk.contract.v1.order_events_pb2 import ObligationState, Reject
from qte_sdk.contract.v1.session_pb2 import AccountQuery
from qte_sdk.options import is_option_symbol
from qte_sdk.orders import is_order_event, reason_code_name, request_ref_of

PREVIOUS_CLOSE_EQUITY = 12_000_000_000


def summary_for(cash: int, positions: list[dict], timestamp: int) -> dict:
    """An account summary consistent with its reply: equity is cash plus each position at
    its price, and the timestamp is the reply's `as_of`."""
    equity = cash + sum(int(p["quantity"]) * int(p["price"]) for p in positions)
    return {
        "equity": str(equity),
        "cash": str(cash),
        "previous_close_equity": str(PREVIOUS_CLOSE_EQUITY),
        "daily_pnl": str(equity - PREVIOUS_CLOSE_EQUITY),
        "loss_level": "LOSS_LEVEL_NONE",
        "timestamp": str(timestamp),
    }


TWO_POSITIONS = [
    {"instrument": "AAPL", "quantity": "100", "price": "199970000"},
    {"instrument": "MSFT", "quantity": "-20", "price": "410250000"},
]


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
                "summary": summary_for(800_000_000, TWO_POSITIONS, 42),
                "positions": TWO_POSITIONS,
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
            equity=12_592_000_000,
            cash=800_000_000,
            previous_close_equity=12_000_000_000,
            daily_pnl=592_000_000,
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


async def test_reply_summary_keeps_limits_and_optional_fields():
    summary = {
        **summary_for(800_000_000, [], 42),
        "loss_warning_amount": "30000000",
        "loss_halt_amount": "50000000",
        "limits": [
            {"kind": "LIMIT_GROSS", "used": "300000000", "cap": "2000000000"},
            {"kind": "LIMIT_INSTRUMENT", "scope": "AAPL", "used": "0", "cap": "500000000"},
        ],
        "in_cure": True,
        "cure_deadline": "900",
        "cure_paused": False,
    }
    [event] = await received(
        frame(
            "account_state",
            {
                "request_ref": "acct-5",
                "summary": summary,
                "valuation_basis": "LIVE_MARK",
                "session_date": "2026-10-02",
                "as_of": "42",
                "cash": "800000000",
            },
            1,
        )
    )
    got = event.message.summary
    assert got.loss_warning_amount == 30_000_000
    assert got.loss_halt_amount == 50_000_000
    assert [(lim.kind, lim.HasField("scope"), lim.used) for lim in got.limits] == [
        (LIMIT_GROSS, False, 300_000_000),
        (LIMIT_INSTRUMENT, True, 0),
    ]
    assert got.in_cure and got.cure_deadline == 900
    assert got.HasField("cure_paused") and not got.cure_paused


async def test_reply_for_an_execution_desk_has_neither_summary_nor_cash():
    [event] = await received(
        frame(
            "account_state",
            {
                "request_ref": "acct-2",
                "positions": [{"instrument": "AAPL", "quantity": "-300", "price": "199970000"}],
                "valuation_basis": "LIVE_MARK",
                "session_date": "2026-10-02",
                "as_of": "7",
            },
            1,
        )
    )
    assert is_account_state(event)
    state = event.message
    assert not state.HasField("summary")
    assert not state.HasField("cash")
    assert [(p.instrument, p.quantity) for p in state.positions] == [("AAPL", -300)]
    assert state.HasField("session_date")


async def test_house_reply_has_cash_but_no_summary():
    [event] = await received(
        frame(
            "account_state",
            {
                "request_ref": "acct-6",
                "valuation_basis": "LAST_OFFICIAL_CLOSE",
                "session_date": "2026-10-02",
                "as_of": "7",
                "cash": "-1500000",
            },
            1,
        )
    )
    state = event.message
    assert not state.HasField("summary")
    assert state.HasField("cash") and state.cash == -1_500_000
    assert list(state.positions) == []
    assert state.valuation_basis == LAST_OFFICIAL_CLOSE


async def test_reply_with_zero_cash_still_has_cash():
    [event] = await received(
        frame(
            "account_state",
            {
                "request_ref": "acct-7",
                "valuation_basis": "LIVE_MARK",
                "session_date": "2026-10-02",
                "as_of": "1",
                "cash": "0",
            },
            1,
        )
    )
    assert event.message.HasField("cash")
    assert event.message.cash == 0


CARRIED = [{"instrument": "MSFT", "quantity": "50", "price": "410250000"}]


async def test_reply_between_terms_carries_positions_valued_at_the_last_close():
    [event] = await received(
        frame(
            "account_state",
            {
                "request_ref": "acct-8",
                "summary": summary_for(800_000_000, CARRIED, 9),
                "positions": CARRIED,
                "valuation_basis": "LAST_OFFICIAL_CLOSE",
                "session_date": "2026-06-30",
                "as_of": "9",
                "cash": "800000000",
            },
            1,
        )
    )
    state = event.message
    assert state.valuation_basis == LAST_OFFICIAL_CLOSE
    assert state.session_date == "2026-06-30"
    assert [(p.instrument, p.quantity, p.price) for p in state.positions] == [
        ("MSFT", 50, 410_250_000)
    ]


async def test_reply_before_the_competitions_first_session_has_no_session_date_or_report_seq():
    # The only time a reply outside a session has no session_date: nothing has closed and
    # the book is empty.
    [event] = await received(
        frame(
            "account_state",
            {
                "request_ref": "acct-3",
                "valuation_basis": "LAST_OFFICIAL_CLOSE",
                "as_of": "1",
                "cash": "1000000000",
            },
            1,
        )
    )
    assert not event.message.HasField("session_date")
    assert not event.message.HasField("as_of_report_seq")
    assert list(event.message.positions) == []


async def test_reply_after_a_first_session_with_no_official_close_has_its_date():
    # The competition's first session ended with no official close for any instrument:
    # outside a session the reply still carries that session's date, and a position in an
    # instrument that has never had an official close is valued at 0.
    unclosed = [{"instrument": "AAPL", "quantity": "100", "price": "0"}]
    [event] = await received(
        frame(
            "account_state",
            {
                "request_ref": "acct-13",
                "summary": summary_for(1_000_000_000, unclosed, 5),
                "positions": unclosed,
                "valuation_basis": "LAST_OFFICIAL_CLOSE",
                "session_date": "2026-09-01",
                "as_of": "5",
                "cash": "1000000000",
            },
            1,
        )
    )
    state = event.message
    assert state.valuation_basis == LAST_OFFICIAL_CLOSE
    assert state.HasField("session_date") and state.session_date == "2026-09-01"
    [position] = state.positions
    assert (position.instrument, position.quantity, position.price) == ("AAPL", 100, 0)
    assert state.summary.equity == state.cash


@pytest.mark.parametrize(
    ("break_day_close_has_run", "session_date"),
    [(True, "2026-07-01"), (False, "2026-06-30")],
)
async def test_reply_on_a_break_day_dates_the_latest_close_that_has_run(
    break_day_close_has_run, session_date
):
    # 2026-06-30 is the term's last session and 2026-07-01 a break day with its own
    # official close. Once that close has run, the reply carries the break day's date, not
    # the last session's, and the positions are valued at that close.
    price = "410500000" if break_day_close_has_run else "410250000"
    positions = [{"instrument": "MSFT", "quantity": "50", "price": price}]
    [event] = await received(
        frame(
            "account_state",
            {
                "request_ref": "acct-14",
                "summary": summary_for(800_000_000, positions, 7),
                "positions": positions,
                "valuation_basis": "LAST_OFFICIAL_CLOSE",
                "session_date": session_date,
                "as_of": "7",
                "cash": "800000000",
            },
            1,
        )
    )
    state = event.message
    assert state.valuation_basis == LAST_OFFICIAL_CLOSE
    assert state.session_date == session_date
    [position] = state.positions
    assert (position.instrument, position.price) == ("MSFT", int(price))


async def test_reply_carries_the_report_seq_it_reflects():
    [event] = await received(
        frame(
            "account_state",
            {
                "request_ref": "acct-9",
                "valuation_basis": "LIVE_MARK",
                "session_date": "2026-10-02",
                "as_of": "1",
                "cash": "1000000000",
                "as_of_report_seq": "1234",
            },
            1,
        )
    )
    assert event.message.HasField("as_of_report_seq")
    assert event.message.as_of_report_seq == 1234


async def test_reply_before_the_competitions_first_session_with_reports_has_no_session_date():
    # A team can have private reports before the first session, such as an order's
    # delayed reject, and still no session_date.
    big = 2**53 + 1  # above what a JSON number holds exactly
    [event] = await received(
        frame(
            "account_state",
            {
                "request_ref": "acct-11",
                "valuation_basis": "LAST_OFFICIAL_CLOSE",
                "as_of": str(big),
                "cash": "1000000000",
                "as_of_report_seq": str(big),
            },
            1,
        )
    )
    state = event.message
    assert not state.HasField("session_date")
    assert state.as_of_report_seq == big
    assert state.as_of == big


@pytest.mark.parametrize("reason", ["NOT_AUTHENTICATED", "MALFORMED_MESSAGE", "TEAM_DISABLED"])
async def test_refused_query_is_a_reject_echoing_its_request_ref(reason):
    [event] = await received(
        frame(
            "reject",
            {
                "request_ref": "acct-4",
                "request_type": "ACCOUNT_QUERY",
                "reason_code": reason,
                "receipt_time": "5",
            },
            1,
        )
    )
    assert is_order_event(event)
    assert not is_account_state(event)
    assert event.message == Reject(
        request_ref="acct-4",
        request_type=ACCOUNT_QUERY,
        reason_code=ReasonCodes.ReasonCode.Value(reason),
        receipt_time=5,
    )
    assert event.unknown_enum_names() == {}
    assert request_ref_of(event.message) == "acct-4"
    assert reason_code_name(event.message.reason_code) == reason


async def test_account_summary_and_obligation_state_decode_typed():
    # The contract still lists `account_summary` as a message type, so one that arrives on
    # its own decodes typed, though the exchange sends a summary only inside account_state.
    events = await received(
        frame("account_summary", summary_for(800_000_000, [], 42), 1),
        frame("obligation_state", {"entries": [{"instrument": "AAPL"}], "timestamp": "3"}, 2),
    )
    assert all(isinstance(e, Received) for e in events)
    summary, obligations = (e.message for e in events)
    assert isinstance(summary, AccountSummary)
    assert summary.equity == 800_000_000
    assert not summary.HasField("loss_halt_amount")
    assert not summary.HasField("cure_deadline")
    assert isinstance(obligations, ObligationState)
    assert obligations.entries[0].instrument == "AAPL"


async def test_query_accepts_a_request_ref_of_exactly_32_bytes():
    sender = Recorder()
    ref = await send_account_query(sender, request_ref="x" * 32)
    assert sender.sent == [("account_query", AccountQuery(request_ref=ref))]


async def test_an_option_position_is_named_by_its_occ_symbol():
    occ = "AAPL261218C00200000"  # unpadded, with no spaces
    assert is_option_symbol(occ)
    [event] = await received(
        frame(
            "account_state",
            {
                "request_ref": "acct-10",
                "positions": [{"instrument": occ, "quantity": "-3", "price": "4150000"}],
                "valuation_basis": "LIVE_MARK",
                "session_date": "2026-10-02",
                "as_of": "1",
                "cash": "0",
            },
            1,
        )
    )
    [position] = event.message.positions
    assert (position.instrument, position.quantity) == (occ, -3)


async def test_reply_inside_the_first_session_has_its_date_before_any_close():
    [event] = await received(
        frame(
            "account_state",
            {
                "request_ref": "acct-12",
                "valuation_basis": "LIVE_MARK",
                "session_date": "2026-09-01",
                "as_of": "1",
                "cash": "1000000000",
            },
            1,
        )
    )
    state = event.message
    assert state.valuation_basis == LIVE_MARK
    assert state.HasField("session_date") and state.session_date == "2026-09-01"
