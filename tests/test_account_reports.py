"""Applying `account_state.as_of_report_seq`, so a report is counted once: `covers` and
`AccountReports`, on decoded frames and against a scripted exchange."""

import asyncio
import json

import pytest
from fake_exchange import frame, serve_local
from test_orders import Recorder, received
from test_session import ack, synthetic_token
from websockets.asyncio.server import ServerConnection

from qte_sdk.account import ACCOUNT_REPORT_TYPES, AccountReports, covers, is_account_state
from qte_sdk.connection import DecodeFailed, Disconnected, Received, SeqGap, Unknown
from qte_sdk.contract.v1.common_pb2 import BUY
from qte_sdk.contract.v1.order_events_pb2 import AccountState, Execution
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
        "request_type": "NEW_ORDER",
        "reason_code": "PRICE_COLLAR",
        "receipt_time": "1",
    }
    return frame("reject", payload, report_seq=str(report_seq))


def reply(request_ref: str, as_of_report_seq: int | None, aapl: int = 0) -> str:
    payload: dict = {
        "request_ref": request_ref,
        "valuation_basis": "LIVE_MARK",
        "session_date": "2026-10-02",
        "as_of": "1",
        "cash": "1000000000",
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
    assert ACCOUNT_REPORT_TYPES == {"execution", "risk_notice"}


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
    assert await reports.query(sender, request_ref="acct-1") == "acct-1"
    with pytest.raises(ValueError, match="request_ref"):
        await reports.query(sender, request_ref="")
    assert len(sender.sent) == 2


async def test_a_report_before_any_reply_applies_once():
    reports = AccountReports()
    first = execution(1, 10)
    assert reports.update(first) == [first]
    assert reports.state is None


async def test_a_covered_report_after_the_reply_is_skipped():
    reports = AccountReports()
    ref = await reports.query(Recorder())
    assert reports.update(answer(ref, 5)) == []
    assert reports.state is not None and reports.state.as_of_report_seq == 5
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
    assert reports.update(answer(ref, 3)) == [not_in_it]
    # Answered, so nothing is held any longer.
    assert reports.update(answer("other", 3)) == []


async def test_with_no_as_of_report_seq_every_held_report_is_applied_again():
    reports = AccountReports()
    ref = await reports.query(Recorder())
    held = [execution(1, 10), execution(2, 20)]
    for report in held:
        reports.update(report)
    assert reports.update(answer(ref, None)) == held


async def test_reports_that_do_not_change_the_account_are_never_returned():
    reports = AccountReports()
    ref = await reports.query(Recorder())
    events = await decoded(accepted(1), order_state(2), cancelled(3), delayed_reject(4), book())
    for event in events:
        assert reports.update(event) == []
    assert reports.update(answer(ref, None)) == []


async def test_a_refused_query_stops_the_wait():
    reports = AccountReports()
    ref = await reports.query(Recorder())
    reports.update(execution(4, 10))
    [refusal] = await decoded(query_reject(ref))
    assert reports.update(refusal) == []
    # Nothing is held any longer, so a later reply returns nothing again.
    assert reports.update(answer("other", 1)) == []


async def test_a_reject_of_another_request_does_not_stop_the_wait():
    reports = AccountReports()
    ref = await reports.query(Recorder())
    held = execution(4, 10)
    reports.update(held)
    [other] = await decoded(delayed_reject(5))
    # The same request_ref on another kind of request, or one this SDK does not know.
    same_ref = [
        frame("reject", {"request_ref": ref, "request_type": kind, "reason_code": "PRICE_COLLAR"})
        for kind in ("NEW", "A_NEWER_KIND")
    ]
    for event in [other, *await decoded(*same_ref)]:
        assert reports.update(event) == []
    assert reports.update(answer(ref, 1)) == [held]


async def test_a_refusal_that_names_no_request_type_stops_the_wait():
    # As an exchange that does not know the query might answer it.
    reports = AccountReports()
    ref = await reports.query(Recorder())
    reports.update(execution(4, 10))
    [refusal] = await decoded(
        frame("reject", {"request_ref": ref, "reason_code": "MALFORMED_MESSAGE"})
    )
    assert reports.update(refusal) == []
    assert reports.update(answer("other", 1)) == []


async def test_a_disconnect_stops_the_wait_but_keeps_the_reply():
    reports = AccountReports()
    ref = await reports.query(Recorder())
    assert reports.update(answer(ref, 5)) == []
    await reports.query(Recorder())
    held = execution(6, 10)
    assert reports.update(held) == [held]
    assert reports.update(Disconnected(None)) == []
    # The reply still covers what it included, a report replayed after a reconnect too.
    assert reports.update(execution(5, 10)) == []
    # The second query's answer never comes, and nothing is held for it any longer.
    assert reports.update(answer("other", 5)) == []


async def test_a_second_query_takes_the_place_of_the_first():
    reports = AccountReports()
    first = await reports.query(Recorder())
    early = execution(2, 10)
    reports.update(early)
    second = await reports.query(Recorder())
    late = execution(3, 20)
    reports.update(late)
    # The first query's reply is still a reply, but the wait goes on for the second.
    assert reports.update(answer(first, 1)) == [early, late]
    assert reports.update(answer(second, 2)) == [late]
    assert reports.update(answer("third", 0)) == []


async def test_unnumbered_reports_are_not_held():
    reports = AccountReports()
    ref = await reports.query(Recorder())
    unnumbered = execution(None, 10)
    assert reports.update(unnumbered) == [unnumbered]
    # Taken to be in the reply that follows it, the only order such an exchange gives.
    assert reports.update(answer(ref, None)) == []


async def test_a_query_that_fails_to_send_waits_for_nothing():
    class Broken:
        async def send(self, type_, payload):
            raise ConnectionError("gone")

    reports = AccountReports()
    with pytest.raises(ConnectionError):
        await reports.query(Broken())
    reports.update(execution(4, 10))
    assert reports.update(answer("any", 1)) == []

    # A failed second query leaves the first one waited for.
    first = await reports.query(Recorder())
    held = execution(5, 10)
    reports.update(held)
    with pytest.raises(ConnectionError):
        await reports.query(Broken())
    assert reports.update(answer(first, 4)) == [held]


# Against a scripted exchange: each report counted once, wherever it falls.


def apply(positions: dict[str, int], report: Received) -> None:
    if report.type == "execution":
        message = report.message
        change = message.fill_size if message.side == BUY else -message.fill_size
        positions[message.instrument] = positions.get(message.instrument, 0) + change


async def run_program(script) -> tuple[dict[str, int], list[int]]:
    """Run the quickstart's loop against an exchange that sends a fill, waits for the
    account query and then follows `script(ref)`. Returns the positions the program ends
    with and the report numbers it applied, in order."""

    async def handler(ws: ServerConnection) -> None:
        await ws.recv()  # auth
        await ws.send(ack())
        await ws.send(fill(1, 1))
        query = json.loads(await ws.recv())
        assert query["type"] == "account_query"
        for f in script(query["payload"]["request_ref"]):
            await ws.send(f)
        await ws.close()

    positions: dict[str, int] = {}
    applied: list[int] = []
    async with serve_local(handler) as url:
        session = await open_session(url, synthetic_token())
        async with session, asyncio.timeout(5):
            account = AccountReports()
            async for event in session:
                to_apply = account.update(event)
                if is_account_state(event):
                    positions = {p.instrument: p.quantity for p in event.message.positions}
                for report in to_apply:
                    apply(positions, report)
                    applied.append(report.report_seq)
                if isinstance(event, Received) and event.report_seq == 1:
                    await account.query(session)
    return positions, applied


async def test_a_fill_the_reply_counts_that_arrives_after_it_is_not_counted_twice():
    # Fills of size n for report n: the reply counts 1 to 3, and 2 and 3 arrive after it.
    def script(ref: str) -> list[str]:
        return [reply(ref, 3, aapl=1 + 2 + 3), fill(2, 2), fill(3, 3), fill(4, 4)]

    positions, applied = await run_program(script)
    assert positions == {"AAPL": 1 + 2 + 3 + 4}
    assert applied == [1, 4]


async def test_a_fill_the_reply_misses_that_arrives_before_it_is_not_lost():
    # The reply counts 1 and 2; 3 arrived after the query but before the reply.
    def script(ref: str) -> list[str]:
        return [fill(2, 2), fill(3, 3), reply(ref, 2, aapl=1 + 2), fill(4, 4)]

    positions, applied = await run_program(script)
    assert positions == {"AAPL": 1 + 2 + 3 + 4}
    assert applied == [1, 2, 3, 3, 4]


async def test_a_sell_and_a_risk_notice_cross_the_reply():
    def script(ref: str) -> list[str]:
        return [risk_notice(2), reply(ref, 2, aapl=1), risk_notice(3), fill(4, 1, "SELL")]

    positions, applied = await run_program(script)
    assert positions == {"AAPL": 0}
    assert applied == [1, 2, 3, 4]
