"""Applying `account_state.as_of_report_seq`, so a report is counted once: `covers` and
`AccountReports`, on decoded frames and against a scripted exchange."""

import asyncio
import json

import pytest
from fake_exchange import frame, serve_local
from test_orders import Recorder, received
from test_session import ack, synthetic_token
from websockets.asyncio.server import ServerConnection

from qte_sdk.account import (
    ACCOUNT_REPORT_TYPES,
    TERM_CLOSE_FILL_TYPE,
    AccountReports,
    covers,
    is_account_state,
    is_term_close_fill,
    term_close_position_change,
)
from qte_sdk.connection import DecodeFailed, Disconnected, Received, SeqGap, Unknown
from qte_sdk.contract.v1.common_pb2 import BUY, SELL, SIDE_UNSPECIFIED
from qte_sdk.contract.v1.order_events_pb2 import AccountState, Execution, TermCloseFill
from qte_sdk.contract.v1.session_pb2 import AccountQuery
from qte_sdk.session import open_session

PRICE = 199_970_000

# Frames as the exchange sends them, with report_seq on the envelope. None carries a seq.


def fill(report_seq: int | None, size: int, side: str = "BUY") -> str:
    payload = {
        "exec_id": f"x-{report_seq}",
        "origin": "TEAM",
        "strat_id": "mm-1",
        "instrument": "AAPL",
        "side": side,
        "order_price": str(PRICE),
        "fill_price": str(PRICE),
        "fill_size": str(size),
        "remaining_size": "0",
        "timestamp": "2",
    }
    if report_seq is None:
        return frame("execution", payload)
    return frame("execution", payload, report_seq=str(report_seq))


def risk_notice(report_seq: int) -> str:
    payload = {"kind": "LOSS_WARNING", "timestamp": "6"}
    return frame("risk_notice", payload, report_seq=str(report_seq))


def term_close(report_seq: int | None, size: int, side: str = "SELL", house: bool = False) -> str:
    """A `term_close_fill` closing a position of `size` AAPL shares at the official close."""
    proceeds = size * PRICE if side == "SELL" else -size * PRICE
    payload = {
        "instrument": "AAPL",
        "side": side,
        "size": str(size),
        "price": str(PRICE),
        "proceeds": str(proceeds),
        "session_date": "2026-12-04",
        "house": house,
    }
    if report_seq is None:
        return frame("term_close_fill", payload)
    return frame("term_close_fill", payload, report_seq=str(report_seq))


def accepted(report_seq: int) -> str:
    payload = {"request_ref": "r-1", "receipt_time": "1", "release_time": "2"}
    return frame("accepted", payload, report_seq=str(report_seq))


def order_state(report_seq: int) -> str:
    payload = {
        "strat_id": "mm-1",
        "instrument": "AAPL",
        "side": "BUY",
        "price": str(PRICE),
        "state": "RESTING",
        "remaining_size": "100",
        "timestamp": "1",
    }
    return frame("order_state", payload, report_seq=str(report_seq))


def cancelled(report_seq: int) -> str:
    payload = {
        "origin": "TEAM",
        "strat_id": "mm-1",
        "instrument": "AAPL",
        "side": "BUY",
        "price": str(PRICE),
        "cancelled_size": "90",
        "reason_code": "CANCEL_REQUEST",
        "request_ref": "r-2",
        "timestamp": "3",
    }
    return frame("order_cancelled", payload, report_seq=str(report_seq))


def delayed_reject(report_seq: int) -> str:
    payload = {
        "request_ref": "r-3",
        "request_type": "NEW",
        "reason_code": "PRICE_COLLAR",
        "receipt_time": "1",
    }
    return frame("reject", payload, report_seq=str(report_seq))


def reply(
    request_ref: str, as_of_report_seq: int | None, aapl: int = 0, cash: int = 1_000_000_000
) -> str:
    payload: dict = {
        "request_ref": request_ref,
        "valuation_basis": "LIVE_MARK",
        "session_date": "2026-10-02",
        "as_of": "1",
        "cash": str(cash),
    }
    if aapl:
        payload["positions"] = [{"instrument": "AAPL", "quantity": str(aapl), "price": "1"}]
    if as_of_report_seq is not None:
        payload["as_of_report_seq"] = str(as_of_report_seq)
    return frame("account_state", payload)


def query_reject(request_ref: str) -> str:
    payload = {
        "request_ref": request_ref,
        "request_type": "ACCOUNT_QUERY",
        "reason_code": "TEAM_DISABLED",
        "receipt_time": "5",
    }
    return frame("reject", payload)


def book() -> str:
    return frame("book", {"instrument": "AAPL", "grid_time": "1"})


async def decoded(*frames: str) -> list:
    """`frames` as a connection delivers them."""
    events = await received(*frames)
    assert len(events) == len(frames)
    return events


async def state_of(as_of_report_seq: int | None) -> AccountState:
    [event] = await decoded(reply("acct-1", as_of_report_seq))
    assert is_account_state(event)
    return event.message


# covers: the rule on its own.


async def test_a_fill_before_the_reply_is_covered():
    state = await state_of(5)
    [event] = await decoded(fill(4, 10))
    assert event.report_seq == 4
    assert covers(state, event)


async def test_a_fill_after_the_reply_is_not_covered():
    state = await state_of(5)
    [event] = await decoded(fill(9, 10))
    assert not covers(state, event)


async def test_around_the_reply_the_cut_is_at_as_of_report_seq():
    state = await state_of(5)
    at, just_above = await decoded(fill(5, 10), fill(6, 10))
    assert covers(state, at)
    assert not covers(state, just_above)


async def test_a_risk_notice_is_cut_like_a_fill():
    state = await state_of(5)
    at, just_above = await decoded(risk_notice(5), risk_notice(6))
    assert covers(state, at)
    assert not covers(state, just_above)


async def test_reports_that_do_not_change_the_account_always_apply():
    state = await state_of(100)
    events = await decoded(accepted(1), delayed_reject(2), order_state(3), cancelled(4))
    assert [e.type for e in events] == ["accepted", "reject", "order_state", "order_cancelled"]
    assert all(e.report_seq is not None and e.report_seq <= 100 for e in events)
    assert not any(covers(state, e) for e in events)
    assert ACCOUNT_REPORT_TYPES == {"execution", "risk_notice", "term_close_fill"}


async def test_a_term_close_fill_is_cut_like_a_fill():
    state = await state_of(5)
    at, above = await decoded(term_close(5, 40), term_close(6, 40))
    assert is_term_close_fill(at) and at.type == TERM_CLOSE_FILL_TYPE
    assert covers(state, at)
    assert not covers(state, above)


async def test_a_house_term_close_fill_is_never_covered():
    # Sent to a Director session only, unnumbered.
    state = await state_of(5)
    [house] = await decoded(term_close(None, 5, "BUY", house=True))
    assert house.report_seq is None
    assert not covers(state, house)


async def test_with_no_as_of_report_seq_every_report_applies():
    state = await state_of(None)
    assert not state.HasField("as_of_report_seq")
    events = await decoded(fill(1, 10), risk_notice(2), order_state(3))
    assert not any(covers(state, e) for e in events)


async def test_before_any_reply_every_report_applies():
    events = await decoded(fill(1, 10), risk_notice(2))
    assert not any(covers(None, e) for e in events)


async def test_a_report_with_no_report_seq_is_never_covered():
    # From an exchange that does not number its reports: it applies.
    state = await state_of(5)
    [event] = await decoded(fill(None, 10))
    assert event.report_seq is None
    assert not covers(state, event)


async def test_events_that_are_not_reports_are_never_covered():
    state = await state_of(5)
    [market] = await decoded(book())
    assert not covers(state, market)
    assert not covers(state, SeqGap(1, 3))
    assert not covers(state, Unknown("execution_v2", {}, None, 1))
    assert not covers(state, Execution(fill_size=10))  # a bare message has no number


async def test_a_report_that_could_not_be_decoded_is_judged_by_its_number():
    state = await state_of(5)
    broken = frame("execution", {"fill_size": "not a number"}, report_seq="4")
    later = frame("execution", {"fill_size": "not a number"}, report_seq="6")
    at, above = await decoded(broken, later)
    assert isinstance(at, DecodeFailed) and at.report_seq == 4
    assert covers(state, at)
    assert not covers(state, above)


# AccountReports: the rule applied in one loop, across the reply.


def execution(report_seq: int | None, size: int) -> Received:
    return Received(
        "execution",
        Execution(instrument="AAPL", side=BUY, fill_size=size),
        None,
        report_seq=report_seq,
    )


def closing(report_seq: int | None, size: int, house: bool = False) -> Received:
    fill_ = TermCloseFill(
        instrument="AAPL", side=SELL, size=size, price=PRICE, proceeds=size * PRICE, house=house
    )
    return Received("term_close_fill", fill_, None, report_seq=report_seq)


def answer(request_ref: str, as_of_report_seq: int | None) -> Received:
    state = AccountState(request_ref=request_ref)
    if as_of_report_seq is not None:
        state.as_of_report_seq = as_of_report_seq
    return Received("account_state", state, None)


async def test_query_sends_an_account_query_and_waits_for_its_answer():
    sender = Recorder()
    reports = AccountReports()
    ref = await reports.query(sender)
    assert sender.sent == [("account_query", AccountQuery(request_ref=ref))]
    assert len(ref) == 32


async def test_every_query_has_a_fresh_request_ref():
    # A request_ref used twice could not tell the two replies apart, so none is accepted.
    sender = Recorder()
    reports = AccountReports()
    refs = {await reports.query(sender) for _ in range(5)}
    assert len(refs) == 5
    with pytest.raises(TypeError):
        await reports.query(sender, request_ref="acct-1")  # type: ignore[call-arg]


async def test_a_report_before_any_reply_applies_once():
    reports = AccountReports()
    first = execution(1, 10)
    assert reports.update(first) == [first]
    assert reports.state is None


async def test_the_reply_comes_first_and_a_covered_report_after_it_is_skipped():
    reports = AccountReports()
    ref = await reports.query(Recorder())
    the_reply = answer(ref, 5)
    assert reports.update(the_reply) == [the_reply]
    assert reports.state is the_reply.message
    assert reports.update(execution(5, 10)) == []
    later = execution(6, 10)
    assert reports.update(later) == [later]


async def test_a_report_between_query_and_reply_that_the_reply_misses_is_applied_again():
    reports = AccountReports()
    ref = await reports.query(Recorder())
    in_it, not_in_it = execution(3, 10), execution(4, 20)
    assert reports.update(in_it) == [in_it]
    assert reports.update(not_in_it) == [not_in_it]
    # The reply replaces the account both were applied to, and includes only the first.
    the_reply = answer(ref, 3)
    assert reports.update(the_reply) == [the_reply, not_in_it]
    # Answered, so another reply to it is not taken.
    assert reports.update(answer(ref, 3)) == []


async def test_with_no_as_of_report_seq_every_held_report_is_applied_again():
    reports = AccountReports()
    ref = await reports.query(Recorder())
    held = [execution(1, 10), execution(2, 20)]
    for report in held:
        reports.update(report)
    the_reply = answer(ref, None)
    assert reports.update(the_reply) == [the_reply, *held]
    assert reports.update(execution(3, 10)) == [execution(3, 10)]


async def test_reports_that_do_not_change_the_account_are_never_returned():
    reports = AccountReports()
    ref = await reports.query(Recorder())
    events = await decoded(accepted(1), order_state(2), cancelled(3), delayed_reject(4), book())
    for event in events:
        assert reports.update(event) == []
    the_reply = answer(ref, None)
    assert reports.update(the_reply) == [the_reply]


async def test_a_term_close_fill_applies_once_before_and_after_the_reply():
    reports = AccountReports()
    first = closing(1, 10)
    assert reports.update(first) == [first]
    ref = await reports.query(Recorder())
    the_reply = answer(ref, 2)
    assert reports.update(the_reply) == [the_reply]
    # The reply covers 2, wherever it arrives; 3 is new.
    assert reports.update(closing(2, 10)) == []
    later = closing(3, 10)
    assert reports.update(later) == [later]


async def test_a_term_close_fill_the_reply_misses_is_applied_again():
    reports = AccountReports()
    ref = await reports.query(Recorder())
    in_it, not_in_it = closing(3, 10), closing(4, 20)
    assert reports.update(in_it) == [in_it]
    assert reports.update(not_in_it) == [not_in_it]
    the_reply = answer(ref, 3)
    assert reports.update(the_reply) == [the_reply, not_in_it]


async def test_a_house_term_close_fill_is_never_applied_or_held():
    reports = AccountReports()
    ref = await reports.query(Recorder())
    assert reports.update(closing(None, 5, house=True)) == []
    # Even a numbered one, which the exchange never sends: the house is not the team.
    assert reports.update(closing(7, 5, house=True)) == []
    the_reply = answer(ref, None)
    assert reports.update(the_reply) == [the_reply]


def test_a_closing_fill_names_its_position_change():
    assert term_close_position_change(TermCloseFill(side=SELL, size=40)) == -40
    assert term_close_position_change(TermCloseFill(side=BUY, size=5)) == 5
    with pytest.raises(ValueError, match="side must be BUY or SELL"):
        term_close_position_change(TermCloseFill(side=SIDE_UNSPECIFIED, size=5))


async def test_a_reply_to_a_query_not_waited_for_is_not_taken():
    # For example one sent with send_account_query: update returns nothing for it.
    reports = AccountReports()
    report = execution(4, 10)
    assert reports.update(answer("elsewhere", 1)) == []
    assert reports.state is None
    assert reports.update(report) == [report]


async def test_risk_notices_are_held_and_cut_like_fills():
    reports = AccountReports()
    ref = await reports.query(Recorder())
    in_it, not_in_it = await decoded(risk_notice(2), risk_notice(3))
    assert reports.update(in_it) == [in_it]
    assert reports.update(not_in_it) == [not_in_it]
    the_reply = answer(ref, 2)
    assert reports.update(the_reply) == [the_reply, not_in_it]
    [covered_after] = await decoded(risk_notice(1))
    assert reports.update(covered_after) == []


async def test_a_refused_query_stops_the_wait():
    reports = AccountReports()
    ref = await reports.query(Recorder())
    reports.update(execution(4, 10))
    [refusal] = await decoded(query_reject(ref))
    assert reports.update(refusal) == []
    # No longer waited for, so a reply to it would not be taken.
    assert reports.update(answer(ref, 1)) == []
    assert reports.state is None


async def test_a_reject_of_another_request_does_not_stop_the_wait():
    reports = AccountReports()
    ref = await reports.query(Recorder())
    held = execution(4, 10)
    reports.update(held)
    [other] = await decoded(delayed_reject(5))
    # The same request_ref on another kind of request, or one this SDK does not know, in
    # either spelling of the field the decoder accepts.
    same_ref = [
        frame("reject", {"request_ref": ref, field: kind, "reason_code": "PRICE_COLLAR"})
        for kind in ("NEW", "A_NEWER_KIND")
        for field in ("request_type", "requestType")
    ]
    for event in [other, *await decoded(*same_ref)]:
        assert reports.update(event) == []
    the_reply = answer(ref, 1)
    assert reports.update(the_reply) == [the_reply, held]


async def test_a_refusal_that_names_no_request_type_stops_the_wait():
    # As an exchange that does not know the query might answer it.
    reports = AccountReports()
    ref = await reports.query(Recorder())
    reports.update(execution(4, 10))
    [refusal] = await decoded(
        frame("reject", {"request_ref": ref, "reason_code": "MALFORMED_MESSAGE"})
    )
    assert reports.update(refusal) == []
    assert reports.update(answer(ref, 1)) == []


async def test_a_disconnect_stops_the_wait_but_keeps_the_reply():
    reports = AccountReports()
    ref = await reports.query(Recorder())
    first_reply = answer(ref, 5)
    assert reports.update(first_reply) == [first_reply]
    second = await reports.query(Recorder())
    held = execution(6, 10)
    assert reports.update(held) == [held]
    assert reports.update(Disconnected(None)) == []
    # The reply still covers what it included, a report replayed after a reconnect too.
    assert reports.update(execution(5, 10)) == []
    # The second query's answer never comes, and is not taken if it does.
    assert reports.update(answer(second, 5)) == []
    assert reports.state is first_reply.message


async def test_a_second_query_takes_the_place_of_the_first():
    reports = AccountReports()
    first = await reports.query(Recorder())
    early = execution(2, 10)
    assert reports.update(early) == [early]
    second = await reports.query(Recorder())
    late = execution(3, 20)
    assert reports.update(late) == [late]
    # The first query's reply, even arriving first, is not taken.
    assert reports.update(answer(first, 1)) == []
    assert reports.state is None
    # The second's is, with only what arrived since it was sent: the report before it
    # is in it.
    second_reply = answer(second, 2)
    assert reports.update(second_reply) == [second_reply, late]
    assert reports.update(answer("third", 0)) == []


async def test_a_late_reply_to_a_replaced_query_cannot_undo_a_fill():
    # Query A, a fill, query B: B's reply includes the fill. A's reply, read before the
    # fill and arriving last, must not be taken, or the fill would be lost.
    reports = AccountReports()
    a = await reports.query(Recorder())
    reports.update(execution(2, 10))
    b = await reports.query(Recorder())
    b_reply = answer(b, 2)
    assert reports.update(b_reply) == [b_reply]
    assert reports.update(answer(a, 1)) == []
    assert reports.state is b_reply.message

    # The same when B is refused before A's reply arrives.
    reports = AccountReports()
    a = await reports.query(Recorder())
    reports.update(execution(2, 10))
    b = await reports.query(Recorder())
    [refusal] = await decoded(query_reject(b))
    assert reports.update(refusal) == []
    assert reports.update(answer(a, 1)) == []
    assert reports.state is None


async def test_a_new_query_holds_nothing_from_before_it():
    # A report held for an unanswered query, perhaps from a session that ended in the term
    # before, is not applied again on a later query's reply.
    reports = AccountReports()
    await reports.query(Recorder())
    reports.update(execution(100, 10))
    ref = await reports.query(Recorder())
    new_term_reply = answer(ref, None)
    assert reports.update(new_term_reply) == [new_term_reply]


async def test_unnumbered_reports_are_not_held():
    reports = AccountReports()
    ref = await reports.query(Recorder())
    unnumbered = execution(None, 10)
    assert reports.update(unnumbered) == [unnumbered]
    # Taken to be in the reply that follows it, the only order such an exchange gives.
    the_reply = answer(ref, None)
    assert reports.update(the_reply) == [the_reply]


class Broken:
    """A sender whose every send fails, after noting the request_ref it was given."""

    def __init__(self) -> None:
        self.refs: list[str] = []

    async def send(self, type_, payload):
        self.refs.append(payload.request_ref)
        raise ConnectionError("gone")


async def test_a_query_that_fails_to_send_waits_for_nothing():
    reports = AccountReports()
    broken = Broken()
    with pytest.raises(ConnectionError):
        await reports.query(broken)
    reports.update(execution(4, 10))
    [failed] = broken.refs
    assert reports.update(answer(failed, 1)) == []

    # A failed query that took another's place leaves none waited for.
    first = await reports.query(Recorder())
    reports.update(execution(5, 10))
    with pytest.raises(ConnectionError):
        await reports.query(Broken())
    assert reports.update(answer(first, 4)) == []
    assert reports.state is None


async def test_a_query_that_fails_after_a_later_one_was_sent_leaves_the_later_one():
    class FirstFails:
        """Holds the first send until a second has gone out, then fails it."""

        def __init__(self) -> None:
            self.sends = 0
            self.second_sent = asyncio.Event()

        async def send(self, type_, payload):
            self.sends += 1
            if self.sends == 1:
                await self.second_sent.wait()
                raise ConnectionError("gone")
            self.second_sent.set()

    sender = FirstFails()
    reports = AccountReports()
    first = asyncio.create_task(reports.query(sender))
    await asyncio.sleep(0)  # the first query is now waiting to send
    second = await reports.query(sender)
    held = execution(4, 10)
    reports.update(held)
    with pytest.raises(ConnectionError):
        await first
    second_reply = answer(second, 3)
    assert reports.update(second_reply) == [second_reply, held]


# Against a scripted exchange: each report counted once, wherever it falls.


async def run_program(
    script, cash: list[int] | None = None
) -> tuple[dict[str, int], list[int | str]]:
    """Run the quickstart's loop against an exchange that answers the session, waits for
    the account query the program sends first and then follows `script(ref)`. Returns the
    positions the program ends with and what it applied, in order: "reply" for a reply,
    a report's number for a report. If `cash` is given, it ends holding the cash the program
    keeps: the latest reply's, plus the proceeds of each closing fill applied after it (the
    fills' own cash is left out)."""

    async def handler(ws: ServerConnection) -> None:
        await ws.recv()  # auth
        await ws.send(ack())
        query = json.loads(await ws.recv())
        assert query["type"] == "account_query"
        for f in script(query["payload"]["request_ref"]):
            await ws.send(f)
        await ws.close()

    positions: dict[str, int] = {}
    applied: list[int | str] = []
    async with serve_local(handler) as url:
        session = await open_session(url, synthetic_token())
        async with session, asyncio.timeout(5):
            account = AccountReports()
            await account.query(session)
            async for event in session:
                for item in account.update(event):
                    if is_account_state(item):
                        positions = {p.instrument: p.quantity for p in item.message.positions}
                        if cash is not None:
                            cash[:] = [item.message.cash]
                        applied.append("reply")
                    else:
                        if item.type == "execution":
                            fill_ = item.message
                            change = fill_.fill_size if fill_.side == BUY else -fill_.fill_size
                            positions[fill_.instrument] = (
                                positions.get(fill_.instrument, 0) + change
                            )
                        elif is_term_close_fill(item):
                            close = item.message
                            positions[close.instrument] = positions.get(
                                close.instrument, 0
                            ) + term_close_position_change(close)
                            if cash:  # cash is known once a reply has come
                                cash[0] += close.proceeds
                        applied.append(item.report_seq)
    return positions, applied


# Fills of size n for report n, so a position shows which fills it counts.


async def test_a_fill_the_reply_counts_that_arrives_after_it_is_not_counted_twice():
    # The reply counts 1 to 3; 2 and 3 arrive after it.
    def script(ref: str) -> list[str]:
        return [fill(1, 1), reply(ref, 3, aapl=1 + 2 + 3), fill(2, 2), fill(3, 3), fill(4, 4)]

    positions, applied = await run_program(script)
    assert positions == {"AAPL": 1 + 2 + 3 + 4}
    assert applied == [1, "reply", 4]


async def test_a_fill_the_reply_misses_that_arrives_before_it_is_not_lost():
    # The reply counts 1 and 2; 3 arrives before the reply but is not in it.
    def script(ref: str) -> list[str]:
        return [fill(1, 1), fill(2, 2), fill(3, 3), reply(ref, 2, aapl=1 + 2), fill(4, 4)]

    positions, applied = await run_program(script)
    assert positions == {"AAPL": 1 + 2 + 3 + 4}
    assert applied == [1, 2, 3, "reply", 3, 4]


async def test_a_reply_with_no_as_of_report_seq_counts_no_fill():
    # Read before the team's first report of the term, so every report applies.
    def script(ref: str) -> list[str]:
        return [fill(1, 1), reply(ref, None), fill(2, 2)]

    positions, applied = await run_program(script)
    assert positions == {"AAPL": 1 + 2}
    assert applied == [1, "reply", 1, 2]


async def test_a_sell_and_risk_notices_cross_the_reply():
    def script(ref: str) -> list[str]:
        # The reply includes the notice numbered 2, which arrives after it.
        return [
            fill(1, 1),
            reply(ref, 2, aapl=1),
            risk_notice(2),
            risk_notice(3),
            fill(4, 1, "SELL"),
        ]

    positions, applied = await run_program(script)
    assert positions == {"AAPL": 0}
    assert applied == [1, "reply", 3, 4]


async def test_a_term_close_fill_the_reply_covers_is_not_applied_twice():
    # The final close: 1 to 3 build a long of 6, the close flattens it as report 4, and a
    # query answered after the close counts it. The reply is flat; 4 arrives after it.
    def script(ref: str) -> list[str]:
        return [fill(1, 1), fill(2, 2), fill(3, 3), reply(ref, 4), term_close(4, 6)]

    positions, applied = await run_program(script)
    assert positions == {}
    assert applied == [1, 2, 3, "reply"]


async def test_a_term_close_fill_the_reply_misses_is_applied_once_on_top_of_it():
    # The reply was read before the close, at 3, and the close's fill arrives before it.
    def script(ref: str) -> list[str]:
        return [fill(1, 1), fill(2, 2), fill(3, 3), term_close(4, 6), reply(ref, 3, aapl=6)]

    positions, applied = await run_program(script)
    assert positions == {"AAPL": 0}
    assert applied == [1, 2, 3, 4, "reply", 4]


async def test_a_short_closed_by_a_buy_crosses_the_reply():
    def script(ref: str) -> list[str]:
        return [fill(1, 5, "SELL"), reply(ref, 1, aapl=-5), term_close(2, 5, "BUY")]

    positions, applied = await run_program(script)
    assert positions == {"AAPL": 0}
    assert applied == [1, "reply", 2]


async def test_a_closing_fill_s_proceeds_are_added_to_cash_once():
    # Covered: the reply's cash already holds the SELL's proceeds, so nothing is added.
    def covered(ref: str) -> list[str]:
        return [fill(1, 6), reply(ref, 2, cash=1_000_000_000 + 6 * PRICE), term_close(2, 6)]

    cash: list[int] = []
    positions, applied = await run_program(covered, cash)
    assert (positions, applied, cash) == ({}, [1, "reply"], [1_000_000_000 + 6 * PRICE])

    # Not covered: a BUY closing a short pays its cost out of the reply's cash, once.
    def missed(ref: str) -> list[str]:
        return [fill(1, 5, "SELL"), term_close(2, 5, "BUY"), reply(ref, 1, aapl=-5)]

    cash = []
    positions, applied = await run_program(missed, cash)
    assert positions == {"AAPL": 0}
    assert applied == [1, 2, "reply", 2]
    assert cash == [1_000_000_000 - 5 * PRICE]
