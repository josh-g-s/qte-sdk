"""Tests of `examples/fundamentals_pod.py`, run as a student would start it, against a
scripted local exchange that serves a Fundamentals pod under automatic execution."""

import asyncio
import json
from typing import Any

import pytest
from fake_exchange import CONTRACT_VERSION, frame, serve_local
from test_examples import load_example, run_example, synthetic_token
from test_tickets import accepted_payload, received, route_payload, service_state_payload
from websockets.asyncio.server import ServerConnection
from websockets.exceptions import ConnectionClosed

from qte_sdk.connection import LivenessTimeout

INSTRUMENT = "AAPL"
ARGS = (
    "--instrument",
    INSTRUMENT,
    "--side",
    "buy",
    "--shares",
    "500",
    "--limit",
    "201.50",
    "--thesis-id",
    "t-aapl",
    "--thesis-version",
    "1",
)


def account(quantity: int | None, summary: bool = True) -> dict[str, Any]:
    state: dict[str, Any] = {"valuation_basis": "LIVE_MARK", "cash": "1000000000"}
    if quantity is not None:
        state["positions"] = [
            {"instrument": INSTRUMENT, "quantity": str(quantity), "price": "200000000"}
        ]
    if summary:
        state["summary"] = {"equity": "1000000000", "cash": "1000000000"}
    return state


def state(update_time: int, **extra: Any) -> dict[str, Any]:
    fields = {"algorithm": "TWAP", "behind_schedule": False, "filled_shares": "0"}
    return service_state_payload(update_time, **{**fields, **extra})


def fill(size: int, origin: str = "AUTO_EXECUTION") -> dict[str, Any]:
    return {
        "exec_id": f"x-{size}",
        "origin": origin,
        "instrument": INSTRUMENT,
        "side": "BUY",
        "fill_price": "200100000",
        "fill_size": str(size),
        "remaining_size": "0",
        "parent_ticket_id": "17",
    }


HAPPY = [
    ("ticket_state", state(10, activity="TAKING", due_shares="100", filled_shares="0")),
    ("execution", fill(200)),
    ("ticket_state", state(20, activity="POSTING", due_shares="300", filled_shares="200")),
    ("execution", fill(300)),
    (
        "ticket_state",
        state(30, status="TICKET_COMPLETE", due_shares="500", filled_shares="500"),
    ),
]


class PodExchange:
    """Acknowledges a pod's session, sends `route` (unless None), answers each
    account_query with the next of `accounts`, and answers each ticket: with `refuse_vwap`
    a VWAP ticket is refused 1713; otherwise it is accepted with the algorithm it names
    (TWAP when unset) and `after` follows, frames of (type, payload)."""

    def __init__(
        self,
        *,
        route: dict[str, Any] | None,
        accounts: list[dict[str, Any]],
        after: list[tuple[str, dict[str, Any]]],
        refuse_vwap: bool = False,
        vwap_detail: str | None = None,
        reject: str | None = None,
        before: list[tuple[str, dict[str, Any]]] | None = None,
        stray_account: dict[str, Any] | None = None,
    ) -> None:
        # With `reject`, every ticket is refused with that reason.
        self.reject = reject
        # `before` is sent ahead of the ticket_accepted. A frame of type "gap" sends
        # nothing but skips a seq, as a message lost on the way would.
        self.before = before or []
        # With `stray_account`, an account_state answering someone else's query is sent
        # straight after the route.
        self.stray_account = stray_account
        self.route = route
        self.accounts = list(accounts)
        self.after = after
        self.refuse_vwap = refuse_vwap
        # With `vwap_detail`, the 1713 refusal carries it as its reason_detail.
        self.vwap_detail = vwap_detail
        self.received: list[dict[str, Any]] = []
        self._seq = 0
        self._report_seq = 0

    async def send(self, ws: ServerConnection, type_: str, payload: dict[str, Any]) -> None:
        self._seq += 1
        if type_ == "gap":
            return
        env: dict[str, Any] = {
            "version": CONTRACT_VERSION,
            "type": type_,
            "payload": payload,
            "seq": self._seq,
        }
        if type_ == "execution":
            self._report_seq += 1
            env["report_seq"] = self._report_seq
        await ws.send(json.dumps(env))

    async def __call__(self, ws: ServerConnection) -> None:
        self.received.append(json.loads(await ws.recv()))
        ack = {
            "session_id": "s-1",
            "team": "pod-a",
            "server_time": "1",
            "contract_version": CONTRACT_VERSION,
            "unscored": True,
        }
        await self.send(ws, "session_ack", ack)
        if self.route is not None:
            await self.send(ws, "execution_route", self.route)
        if self.stray_account is not None:
            await self.send(ws, "account_state", self.stray_account)
        try:
            async for raw in ws:
                message = json.loads(raw)
                self.received.append(message)
                payload = message["payload"]
                if message["type"] == "account_query":
                    if self.accounts:
                        reply = {**self.accounts.pop(0), "request_ref": payload["request_ref"]}
                        await self.send(ws, "account_state", reply)
                elif message["type"] == "ticket":
                    await self.answer_ticket(ws, payload)
        except ConnectionClosed:
            pass

    async def answer_ticket(self, ws: ServerConnection, payload: dict[str, Any]) -> None:
        algorithm = payload.get("algorithm", "TWAP")
        reason = self.reject
        if algorithm == "VWAP" and self.refuse_vwap:
            reason = "TICKET_ALGORITHM_UNAVAILABLE"
        if reason is not None:
            reject = {
                "request_ref": payload["request_ref"],
                "request_kind": "TICKET_REQUEST_SUBMIT",
                "reason_code": reason,
                "receipt_time": "1",
            }
            if reason == "TICKET_ALGORITHM_UNAVAILABLE" and self.vwap_detail is not None:
                reject["reason_detail"] = self.vwap_detail
            await self.send(ws, "ticket_reject", reject)
            return
        accepted = accepted_payload(
            request_ref=payload["request_ref"], algorithm=algorithm, executor="SERVICE"
        )
        for type_, frame_ in self.before:
            await self.send(ws, type_, frame_)
        await self.send(ws, "ticket_accepted", accepted)
        for type_, frame_ in self.after:
            await self.send(ws, type_, frame_)

    def tickets(self) -> list[dict[str, Any]]:
        return [m["payload"] for m in self.received if m["type"] == "ticket"]

    def types(self) -> list[str]:
        return [m["type"] for m in self.received]


async def run_pod(exchange: PodExchange, *args: str) -> tuple[int, str, str]:
    async with serve_local(exchange) as url:
        return await run_example("fundamentals_pod.py", url, synthetic_token(), *ARGS, *args)


async def test_a_pod_follows_its_service_ticket_to_complete_and_counts_no_fill():
    exchange = PodExchange(
        route=route_payload(), accounts=[account(None), account(500)], after=HAPPY
    )
    code, out, err = await run_pod(exchange)
    assert code == 0, out + err
    assert "routing_in_force ROUTING_SERVICE, paused False, config_version 4" in out
    assert "position in AAPL at the start, from account_state: 0" in out
    [ticket] = exchange.tickets()
    assert ticket["instrument"] == INSTRUMENT
    assert (ticket["side"], ticket["shares"], ticket["algorithm"]) == ("BUY", "500", "TWAP")
    assert int(ticket["limit_price"]) == 201_500_000
    assert ticket["thesis_id"] == "t-aapl"
    assert "accepted: ticket 17, algorithm TWAP, executor SERVICE" in out
    assert (
        "ticket 17: TICKET_WORKING, filled 0 of 500, due 100, activity TAKING, "
        "behind_schedule False" in out
    )
    assert "ticket 17: TICKET_WORKING, filled 200 of 500, due 300, activity POSTING" in out
    assert "ticket 17: TICKET_COMPLETE, filled 500 of 500" in out
    assert "the ticket stopped: TICKET_COMPLETE" in out
    # The service's fills are printed, as information only.
    assert "fill of the exchange's child (AUTO_EXECUTION) for ticket 17: 200 @ 200.1" in out
    assert "fill of the exchange's child (AUTO_EXECUTION) for ticket 17: 300 @ 200.1" in out
    assert out.count("already in the ticket's filled_shares") == 2
    # The position after the stop is the second reply's (the next test tells them apart).
    assert "position in AAPL after the stop, from account_state: 500" in out
    assert exchange.types().count("account_query") == 2
    # The second query is sent after the stop.
    assert exchange.types()[-1] == "account_query"


def point(time: int, due: int, filled: int) -> dict[str, str]:
    return {"time": str(time), "due_shares": str(due), "filled_shares": str(filled)}


async def test_each_state_prints_its_newest_progress_point_and_none_without_progress():
    after = [
        ("ticket_state", state(10, activity="TAKING")),
        ("ticket_state", state(20, activity="POSTING", progress=[point(15, 100, 0)])),
        (
            "ticket_state",
            state(
                30,
                status="TICKET_COMPLETE",
                filled_shares="500",
                progress=[point(15, 100, 0), point(25, 300, 200), point(30, 500, 500)],
            ),
        ),
    ]
    exchange = PodExchange(
        route=route_payload(), accounts=[account(None), account(500)], after=after
    )
    code, out, err = await run_pod(exchange)
    assert code == 0, out + err
    # A state with no progress prints no progress line; each other prints its last point.
    assert out.count("  progress at ") == 2
    assert "  progress at 15: due 100, filled 0" in out
    assert "  progress at 30: due 500, filled 500" in out
    assert "progress at 25" not in out


async def test_the_position_comes_from_account_state_not_from_the_fills():
    # The reply after the stop says 120 (a cure the pod never saw, say): that is printed.
    exchange = PodExchange(
        route=route_payload(), accounts=[account(None), account(120, summary=False)], after=HAPPY
    )
    code, out, err = await run_pod(exchange)
    assert code == 0, out + err
    assert "position in AAPL after the stop, from account_state: 120" in out
    assert "no account summary: equity unknown" in out


async def test_a_vwap_ticket_refused_1713_is_sent_again_once_as_twap():
    exchange = PodExchange(
        route=route_payload(),
        accounts=[account(None), account(500)],
        after=HAPPY,
        refuse_vwap=True,
    )
    code, out, err = await run_pod(exchange, "--algorithm", "vwap")
    assert code == 0, out + err
    assert [t["algorithm"] for t in exchange.tickets()] == ["VWAP", "TWAP"]
    assert "VWAP refused: TICKET_ALGORITHM_UNAVAILABLE (1713) for AAPL: no reason given" in out
    assert "sending the ticket again as TWAP (this example's choice)" in out
    assert "accepted: ticket 17, algorithm TWAP, executor SERVICE" in out
    assert "the ticket stopped: TICKET_COMPLETE" in out


async def test_the_1713_message_prints_the_refusals_reason_detail():
    detail = "no VWAP profile in force for AAPL; TWAP is available"
    exchange = PodExchange(
        route=route_payload(),
        accounts=[account(None), account(500)],
        after=HAPPY,
        refuse_vwap=True,
        vwap_detail=detail,
    )
    code, out, err = await run_pod(exchange, "--algorithm", "vwap")
    assert code == 0, out + err
    assert f"VWAP refused: TICKET_ALGORITHM_UNAVAILABLE (1713) for AAPL: {detail};" in out
    assert "no VWAP profile for AAPL;" not in out
    assert [t["algorithm"] for t in exchange.tickets()] == ["VWAP", "TWAP"]


async def test_a_1700_refusal_prints_what_thesis_to_pass():
    exchange = PodExchange(
        route=route_payload(),
        accounts=[account(None)],
        after=HAPPY,
        reject="TICKET_THESIS_NOT_FILED",
    )
    code, out, err = await run_pod(exchange)
    assert code == 0, out + err
    assert "REJECTED: TICKET_THESIS_NOT_FILED;" in out
    assert "from the Research page of the Team Lead console" in out
    assert "(t-aapl is a placeholder)" in out
    assert len(exchange.tickets()) == 1


async def test_another_refusal_prints_no_thesis_hint():
    exchange = PodExchange(
        route=route_payload(),
        accounts=[account(None)],
        after=HAPPY,
        reject="TICKET_INSTRUMENT_WORKING",
    )
    code, out, err = await run_pod(exchange)
    assert code == 0, out + err
    assert "REJECTED: TICKET_INSTRUMENT_WORKING;" in out
    assert "Team Lead console" not in out


async def test_a_status_this_sdk_does_not_know_is_a_stop_named_as_sent():
    after = [
        ("ticket_state", state(10, activity="TAKING")),
        ("ticket_state", state(20, status="TICKET_CANCELLED_BY_SOMEONE_NEW")),
    ]
    exchange = PodExchange(
        route=route_payload(), accounts=[account(None), account(80)], after=after
    )
    code, out, err = await run_pod(exchange)
    assert code == 0, out + err
    assert "the ticket stopped: TICKET_CANCELLED_BY_SOMEONE_NEW (not known to this SDK)" in out
    assert "position in AAPL after the stop, from account_state: 80" in out


async def test_a_directors_amend_is_not_a_stop_and_the_replacement_is_followed():
    replacement = {"ticket_id": "18", "replaces_ticket_id": "17", "shares": "700"}
    after = [
        ("ticket_state", state(10, activity="TAKING")),
        ("ticket_state", state(20, status="TICKET_AMENDED_BY_DIRECTOR", filled_shares="80")),
        ("ticket_state", state(20, activity="TAKING", **replacement)),
        (
            "ticket_state",
            state(30, status="TICKET_COMPLETE", filled_shares="620", **replacement),
        ),
    ]
    exchange = PodExchange(
        route=route_payload(), accounts=[account(None), account(700)], after=after
    )
    code, out, err = await run_pod(exchange, "--seconds", "5")
    assert code == 0, out + err
    assert "a Director amended the ticket: following its replacement" in out
    assert "ticket 17 was replaced by ticket 18" in out
    assert "ticket 18: TICKET_WORKING, filled 0 of 700" in out
    assert "the ticket stopped: TICKET_AMENDED_BY_DIRECTOR" not in out
    assert "the ticket stopped: TICKET_COMPLETE" in out
    assert out.index("ticket 17 was replaced") < out.index("ticket 18: TICKET_COMPLETE")
    assert "position in AAPL after the stop, from account_state: 700" in out
    # The replacement came with no ticket_accepted, and the pod sent only its own ticket.
    assert len(exchange.tickets()) == 1


async def test_a_replacement_held_before_the_amend_is_followed_at_the_amend():
    replacement = {"ticket_id": "18", "replaces_ticket_id": "17", "shares": "700"}
    before = [("ticket_state", state(20, activity="TAKING", **replacement))]
    after = [
        ("ticket_state", state(20, status="TICKET_AMENDED_BY_DIRECTOR")),
        ("ticket_state", state(30, status="TICKET_COMPLETE", **replacement)),
    ]
    exchange = PodExchange(
        route=route_payload(),
        accounts=[account(None), account(700)],
        after=after,
        before=before,
    )
    code, out, err = await run_pod(exchange, "--seconds", "5")
    assert code == 0, out + err
    assert out.index("a Director amended") < out.index("ticket 17 was replaced by ticket 18")
    assert "the ticket stopped: TICKET_COMPLETE" in out


async def test_a_paused_route_still_sends_the_ticket_and_says_the_service_will_catch_up():
    after = [("ticket_state", state(10, activity="SERVICE_PAUSED"))]
    exchange = PodExchange(route=route_payload(paused=True), accounts=[account(None)], after=after)
    code, out, err = await run_pod(exchange, "--seconds", "1")
    assert code == 0, out + err
    assert "routing_in_force ROUTING_SERVICE, paused True" in out
    assert "the service is paused" in out
    assert "the service catches up when it lifts" in out
    assert "accepted: ticket 17, algorithm TWAP, executor SERVICE" in out
    assert "activity SERVICE_PAUSED" in out
    assert "ticket 17 is still working on the exchange" in out
    assert len(exchange.tickets()) == 1


async def test_a_session_with_no_route_is_not_a_pods_and_sends_no_ticket():
    exchange = PodExchange(route=None, accounts=[account(None)], after=[])
    async with serve_local(exchange) as url:
        # A desk is never sent execution_route; --seconds bounds the wait for it too.
        code, out, err = await run_example(
            "fundamentals_pod.py", url, synthetic_token(), *ARGS, "--seconds", "1"
        )
    assert code == 1, out + err
    assert "no execution_route arrived" in out
    assert exchange.tickets() == []


async def test_another_reject_is_printed_by_name_and_says_nothing_stopped():
    # 1713 only is resent; TWAP refused 1713, or any other reason, is printed and the
    # example stops. A ticket already working in the instrument keeps working.
    exchange = PodExchange(
        route=route_payload(),
        accounts=[account(None)],
        after=[],
        reject="TICKET_INSTRUMENT_WORKING",
    )
    code, out, err = await run_pod(exchange, "--algorithm", "vwap")
    assert code == 0, out + err
    assert len(exchange.tickets()) == 1
    assert "REJECTED: TICKET_INSTRUMENT_WORKING; any ticket already working is unaffected" in out
    assert "no ticket is working" not in out


class DeadLink:
    """A session whose link is presumed dead: iterating it raises LivenessTimeout."""

    def __aiter__(self) -> "DeadLink":
        return self

    async def __anext__(self) -> Any:
        raise LivenessTimeout(5.0)


async def test_a_dead_link_is_not_mistaken_for_the_watch_running_out():
    # LivenessTimeout is a TimeoutError: it must not read as --seconds being up.
    example = load_example("fundamentals_pod.py")
    args = example.parse_args(["--shares", "1", "--limit", "1"])
    pod = example.Pod(DeadLink(), args)
    queue: asyncio.Queue = asyncio.Queue()
    await queue.put(LivenessTimeout(5.0))  # as the reader passes on what ended the session
    with pytest.raises(LivenessTimeout):
        await example.drive(queue, pod, lambda: False, 600)


async def test_a_desk_route_sends_nothing_since_the_desk_does_not_trade_this_term():
    exchange = PodExchange(
        route=route_payload(routing_in_force="ROUTING_DESK"), accounts=[account(None)], after=[]
    )
    code, out, err = await run_pod(exchange)
    assert code == 1, out + err
    assert "routing_in_force ROUTING_DESK, paused False" in out
    assert "would go to the pod's Execution desk (executor DESK), which is paused" in out
    assert "no ticket was sent" in out
    assert exchange.tickets() == []


async def test_a_twap_ticket_refused_1713_is_not_sent_again():
    exchange = PodExchange(
        route=route_payload(),
        accounts=[account(None)],
        after=[],
        reject="TICKET_ALGORITHM_UNAVAILABLE",
    )
    code, out, err = await run_pod(exchange)
    assert code == 0, out + err
    assert [t["algorithm"] for t in exchange.tickets()] == ["TWAP"]
    assert "REJECTED: TICKET_ALGORITHM_UNAVAILABLE" in out
    assert "sending the ticket again" not in out


async def test_a_state_that_comes_before_the_acceptance_is_applied_once_it_is_accepted():
    before = [("ticket_state", state(30, status="TICKET_COMPLETE", filled_shares="500"))]
    exchange = PodExchange(
        route=route_payload(), accounts=[account(None), account(500)], after=[], before=before
    )
    code, out, err = await run_pod(exchange, "--seconds", "5")
    assert code == 0, out + err
    assert out.index("accepted: ticket 17") < out.index("ticket 17: TICKET_COMPLETE")
    assert "the ticket stopped: TICKET_COMPLETE" in out
    assert "position in AAPL after the stop, from account_state: 500" in out


async def test_a_state_older_than_the_one_held_is_not_applied():
    # The stale TICKET_COMPLETE (update_time 10) comes after a newer working state.
    after = [
        ("ticket_state", state(20, activity="TAKING")),
        ("ticket_state", state(10, status="TICKET_COMPLETE", filled_shares="500")),
    ]
    exchange = PodExchange(route=route_payload(), accounts=[account(None)], after=after)
    code, out, err = await run_pod(exchange, "--seconds", "1")
    assert code == 0, out + err
    assert "TICKET_COMPLETE" not in out
    assert "ticket 17 is still working on the exchange" in out


async def test_an_account_state_answering_another_query_is_not_taken():
    stray = {**account(999), "request_ref": "someone-else"}
    exchange = PodExchange(
        route=route_payload(),
        accounts=[account(None), account(500)],
        after=HAPPY,
        stray_account=stray,
    )
    code, out, err = await run_pod(exchange)
    assert code == 0, out + err
    assert "position in AAPL at the start, from account_state: 0" in out
    assert "999" not in out


async def test_a_refused_account_query_ends_the_wait_for_its_answer():
    example = load_example("fundamentals_pod.py")
    pod = example.Pod(DeadLink(), example.parse_args(["--shares", "1", "--limit", "1"]))
    pod.account_ref = "a-1"
    refusal = {
        "request_ref": "a-1",
        "request_type": "ACCOUNT_QUERY",
        "reason_code": "MALFORMED_MESSAGE",
        "receipt_time": "1",
    }
    [event] = await received(frame("reject", refusal, 1))
    await pod.on_event(event)
    assert pod.account_answered
    assert pod.account is None


async def test_missed_messages_are_named_when_the_watch_runs_out():
    after = [("gap", {}), ("ticket_state", state(10, activity="TAKING"))]
    exchange = PodExchange(route=route_payload(), accounts=[account(None)], after=after)
    code, out, err = await run_pod(exchange, "--seconds", "1")
    assert code == 0, out + err
    assert "ticket 17 is still working on the exchange" in out
    assert "messages were missed, so the ticket's states printed may be incomplete" in out


@pytest.mark.parametrize("seconds", ["0", "-5", "nan"])
def test_seconds_must_be_above_zero(seconds, capsys):
    example = load_example("fundamentals_pod.py")
    with pytest.raises(SystemExit) as exit_:
        example.parse_args(["--shares", "1", "--limit", "1", "--seconds", seconds])
    assert exit_.value.code == 2
    assert "--seconds must be above 0" in capsys.readouterr().err


class RestartingPodExchange(PodExchange):
    """A PodExchange whose first connection is closed with `close_code` and `reason`
    once the ticket is answered (with `before_answer`, as soon as the ticket arrives,
    before any answer). Each later connection is sent the route and then `on_reconnect`,
    the pod's ticket states for the term, as the exchange sends them on every connection."""

    def __init__(
        self,
        *,
        close_code: int = 1012,
        reason: str = "service restart",
        before_answer: bool = False,
        on_reconnect: list[tuple[str, dict[str, Any]]] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.close_code = close_code
        self.reason = reason
        self.before_answer = before_answer
        self.on_reconnect = on_reconnect or []
        self.connections = 0

    async def __call__(self, ws: ServerConnection) -> None:
        self.connections += 1
        self._seq = 0  # each connection numbers its messages from 1
        await super().__call__(ws)

    async def send(self, ws: ServerConnection, type_: str, payload: dict[str, Any]) -> None:
        await super().send(ws, type_, payload)
        if type_ == "execution_route" and self.connections > 1:
            for frame_type, frame_ in self.on_reconnect:
                await super().send(ws, frame_type, frame_)

    async def answer_ticket(self, ws: ServerConnection, payload: dict[str, Any]) -> None:
        if self.connections == 1 and self.before_answer:
            await ws.close(self.close_code, self.reason)
            return
        await super().answer_ticket(ws, payload)
        if self.connections == 1:
            await ws.close(self.close_code, self.reason)


WORKING = [("ticket_state", state(10, activity="TAKING", due_shares="100", filled_shares="0"))]
COMPLETE = (
    "ticket_state",
    state(30, status="TICKET_COMPLETE", due_shares="500", filled_shares="500"),
)


@pytest.mark.parametrize(("close_code", "reason"), [(1012, "service restart"), (1001, "")])
async def test_a_pod_survives_a_close_and_follows_its_ticket_from_the_states_sent_again(
    close_code: int, reason: str
):
    # The ticket is accepted and working when the exchange restarts. It completes while
    # the example is down; the new connection's ticket states say so.
    exchange = RestartingPodExchange(
        close_code=close_code,
        reason=reason,
        route=route_payload(),
        accounts=[account(0), account(500)],
        after=WORKING,
        on_reconnect=[COMPLETE],
    )
    code, out, err = await run_pod(exchange)
    assert code == 0, out + err
    assert exchange.connections == 2
    assert len(exchange.tickets()) == 1  # never sent again
    assert "the connection dropped (" in out
    assert "reconnected: the ticket states are sent again" in out
    assert "ticket 17: TICKET_COMPLETE, filled 500 of 500" in out
    assert "the ticket stopped: TICKET_COMPLETE" in out
    assert (
        out.splitlines()[-2] == f"position in {INSTRUMENT} after the stop, from account_state: 500"
    )


async def test_a_pod_whose_ticket_answer_is_lost_with_a_close_does_not_send_it_again():
    # Ticket answers are never sent again, so the example cannot tell whether it works.
    exchange = RestartingPodExchange(
        before_answer=True, route=route_payload(), accounts=[account(0)], after=HAPPY
    )
    code, out, err = await run_pod(exchange)
    assert code == 1, out + err
    assert len(exchange.tickets()) == 1
    assert "whether the ticket is working is not known. It is not sent again" in out


async def test_a_pod_whose_exchange_starts_a_new_log_says_its_ticket_is_not_known():
    # A new event log (a new term, say) holds none of the old tickets: the example must
    # not claim the ticket is still working.
    class NewLogOnReconnect(RestartingPodExchange):
        async def send(self, ws: ServerConnection, type_: str, payload: dict[str, Any]) -> None:
            if type_ == "session_ack":
                payload = {**payload, "log_id": f"{self.connections:016x}"}
            await super().send(ws, type_, payload)

    exchange = NewLogOnReconnect(
        close_code=4001,
        reason="term change",
        route=route_payload(),
        accounts=[account(0)],
        after=WORKING,
    )
    code, out, err = await run_pod(exchange)
    assert code == 1, out + err
    assert len(exchange.tickets()) == 1
    assert "reconnected to a new event log" in out
    assert "what became of the ticket is not known here. It is not sent again" in out
    assert "is still working on the exchange" not in out


async def test_a_pod_asks_for_the_account_again_after_a_close_drops_its_query():
    # The session does not send account_query again: the example does, once reconnected.
    class DropsFirstQuery(RestartingPodExchange):
        async def __call__(self, ws: ServerConnection) -> None:
            if self.connections == 0:
                self.connections = 1
                self._seq = 0
                self.received.append(json.loads(await ws.recv()))
                ack = {
                    "session_id": "s-1",
                    "team": "pod-a",
                    "server_time": "1",
                    "contract_version": CONTRACT_VERSION,
                    "unscored": True,
                }
                await self.send(ws, "session_ack", ack)
                self.received.append(json.loads(await ws.recv()))  # left unanswered
                await ws.close(1012, "service restart")
                return
            await super().__call__(ws)

    exchange = DropsFirstQuery(
        route=route_payload(), accounts=[account(0), account(500)], after=HAPPY
    )
    code, out, err = await run_pod(exchange)
    assert code == 0, out + err
    assert "asking for the account again: the query sent was not answered" in out
    assert exchange.types().count("account_query") == 3  # the lost one, again, after the stop
    assert len(exchange.tickets()) == 1


async def test_a_pod_ends_on_a_revoked_credential_and_names_the_close():
    exchange = RestartingPodExchange(
        close_code=1008,
        reason="credential revoked",
        route=route_payload(),
        accounts=[account(0)],
        after=WORKING,
    )
    code, out, err = await run_pod(exchange)
    assert code == 1, out + err
    assert exchange.connections == 1
    assert 'close code 1008, reason "credential revoked": your token' in err
    assert "A ticket sent may still be working: read its ticket_state" in err
    assert "reconnecting in" not in out


# Review of #375: the log a ticket was sent on, the account on each connection, and what
# the pod forgets or keeps at a drop.

LOG_A, LOG_B = "a" * 16, "b" * 16


class LogsPerConnection(PodExchange):
    """A PodExchange whose session_acks name `logs`, one per connection. The first
    connection answers the account and drops before execution_route; the second accepts
    the ticket and then closes; the third sends `on_third`. With `revoke_third`, the
    third connection is closed 1008 "credential revoked" before its session_ack."""

    def __init__(
        self,
        *,
        logs: list[str | None],
        on_third: list[tuple[str, dict[str, Any]]] | None = None,
        revoke_third: bool = False,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.logs = logs
        self.on_third = on_third or []
        self.revoke_third = revoke_third
        self.connections = 0

    async def __call__(self, ws: ServerConnection) -> None:
        self.connections += 1
        self._seq = 0
        if self.connections == 3 and self.revoke_third:
            self.received.append(json.loads(await ws.recv()))
            await ws.close(1008, "credential revoked")
            return
        await super().__call__(ws)

    async def send(self, ws: ServerConnection, type_: str, payload: dict[str, Any]) -> None:
        if type_ == "session_ack":
            payload = {**payload, "log_id": self.logs[self.connections - 1]}
            if payload["log_id"] is None:
                del payload["log_id"]
        if type_ == "execution_route" and self.connections == 1:
            return  # the first connection drops before its route
        await super().send(ws, type_, payload)
        if type_ == "account_state" and self.connections == 1:
            await ws.close(1012, "service restart")
        if type_ == "execution_route" and self.connections == 3:
            for frame_type, frame_ in self.on_third:
                await super().send(ws, frame_type, frame_)

    async def answer_ticket(self, ws: ServerConnection, payload: dict[str, Any]) -> None:
        await super().answer_ticket(ws, payload)
        if self.connections == 2:
            await ws.close(1012, "service restart")


async def test_a_pod_compares_the_log_its_ticket_was_sent_on_not_its_first_connections():
    # Logs A, then B, then B: the ticket is sent and accepted on B, so the third
    # connection is on the ticket's own log and the pod keeps following it.
    exchange = LogsPerConnection(
        logs=[LOG_A, LOG_B, LOG_B],
        route=route_payload(),
        accounts=[account(100), account(0)],
        after=WORKING,
        on_third=WORKING,
    )
    code, out, err = await run_pod(exchange, "--seconds", "4")
    assert code == 0, out + err
    assert exchange.connections == 3
    assert len(exchange.tickets()) == 1
    assert "reconnected to a new event log" in out  # A to B, before the ticket
    assert "what became of the ticket is not known here" not in out
    assert "ticket 17 is still working on the exchange" in out


async def test_a_pod_reads_its_position_again_on_a_reconnect_before_its_ticket():
    # The account is answered on log A and the link drops before the route; the route
    # comes on log B. The position the ticket is sent on is B's, not A's.
    exchange = LogsPerConnection(
        logs=[LOG_A, LOG_B, LOG_B],
        route=route_payload(),
        accounts=[account(100), account(0)],
        after=WORKING,
        on_third=WORKING,
    )
    code, out, err = await run_pod(exchange, "--seconds", "4")
    assert code == 0, out + err
    assert "asking for the account again: the ticket is sent on this connection" in out
    assert f"position in {INSTRUMENT} at the start, from account_state: 0" in out
    assert "from account_state: 100" not in out
    sent = [m["type"] for m in exchange.received]
    assert sent.index("ticket") > [i for i, t in enumerate(sent) if t == "account_query"][1]


async def test_a_pod_takes_no_log_change_from_an_unknown_log_id():
    # The ticket is sent on a connection that names no log; the next one names one. An
    # unknown id says nothing either way, so this is not a new log.
    exchange = LogsPerConnection(
        logs=[None, None, LOG_B],
        route=route_payload(),
        accounts=[account(0), account(0)],
        after=WORKING,
        on_third=WORKING,
    )
    code, out, err = await run_pod(exchange, "--seconds", "4")
    assert code == 0, out + err
    assert "reconnected to a new event log" not in out
    assert "what became of the ticket is not known here" not in out
    assert "ticket 17 is still working on the exchange" in out


async def test_a_pod_warns_its_ticket_may_be_working_when_a_reconnect_fails_for_good():
    # The third connection is refused for a revoked credential, which is not retried.
    exchange = LogsPerConnection(
        logs=[LOG_A, LOG_A, LOG_A],
        revoke_third=True,
        route=route_payload(),
        accounts=[account(0), account(0)],
        after=WORKING,
    )
    code, out, err = await run_pod(exchange, "--seconds", "6")
    assert code == 1, out + err
    assert exchange.connections == 3
    assert "a ticket sent may still be working on the exchange" in err


@pytest.mark.parametrize("stopped", [False, True], ids=["working", "stopped"])
async def test_a_pod_warns_of_a_working_ticket_only_until_it_stops(
    stopped: bool, capsys: pytest.CaptureFixture[str]
):
    # A reconnect that fails for good after the ticket has stopped (say during the account
    # query that follows the stop) says nothing of a ticket that may still be working.
    from qte_sdk.connection import SessionRejected

    example = load_example("fundamentals_pod.py")
    seen: list[Any] = []

    async def fail_for_good(pod: Any, queue: Any, args: Any) -> int:
        pod.ticket_ref = "ticket-ref"
        pod.stopped = stopped
        seen.append(pod)
        raise SessionRejected(1, None)

    example.follow_pod = fail_for_good
    with pytest.raises(SessionRejected):
        await example.follow(RecordingPodSession(), asyncio.Queue(), example.parse_args(list(ARGS)))
    assert seen[0].ticket_out() is not stopped
    err = capsys.readouterr().err
    assert ("a ticket sent may still be working on the exchange" in err) is not stopped


class RecordingPodSession:
    """A session that records each message type sent, raising `error` on each send."""

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.sent: list[str] = []

    async def send(self, type_: str, payload: Any) -> None:
        self.sent.append(type_)
        if self.error is not None:
            raise self.error


def pod_connected(log_id: str | None, reconnected: bool) -> Any:
    from qte_sdk.connection import Connected, SessionInfo

    info = SessionInfo("s-1", "pod-a", 1, CONTRACT_VERSION, True, log_id)
    return Connected(info, (), reconnected)


def test_a_pod_takes_no_log_change_before_its_ticket_is_sent():
    from qte_sdk.connection import Disconnected

    example = load_example("fundamentals_pod.py")
    pod = example.Pod(RecordingPodSession(), example.parse_args(list(ARGS)))
    pod.on_connected(pod_connected(LOG_A, False))
    pod.on_disconnected(Disconnected(error=None))
    pod.on_connected(pod_connected(LOG_B, True))
    assert not pod.log_changed


def test_a_pod_forgets_the_ticket_states_it_kept_at_a_drop():
    # LatestTickets forgets them at a drop; the events kept for their unknown enum names
    # go with them, so a stale state is never taken for a replacement or a newest state.
    from qte_sdk.connection import Disconnected, Received
    from qte_sdk.contract.v1.order_events_pb2 import TicketState

    example = load_example("fundamentals_pod.py")
    pod = example.Pod(RecordingPodSession(), example.parse_args(list(ARGS)))
    pod.state_events["17"] = Received("ticket_state", TicketState(ticket_id="17"), 1)
    pod.on_disconnected(Disconnected(error=None))
    assert pod.state_events == {}


async def test_a_pod_takes_a_ticket_not_sent_while_down_as_not_sent():
    from qte_sdk.reconnect import NotConnected

    example = load_example("fundamentals_pod.py")
    pod = example.Pod(RecordingPodSession(NotConnected("down")), example.parse_args(list(ARGS)))
    await pod.send()
    assert pod.unsent
    assert pod.ticket_ref is None
    assert not pod.ticket_out()


@pytest.mark.parametrize(
    "error", [ConnectionClosed(None, None), None], ids=["connection-closed", "not-connected"]
)
async def test_a_pod_asks_for_the_account_again_later_when_its_query_cannot_be_sent(error):
    from qte_sdk.reconnect import NotConnected

    example = load_example("fundamentals_pod.py")
    session = RecordingPodSession(error or NotConnected("down"))
    pod = example.Pod(session, example.parse_args(list(ARGS)))
    await pod.query_account()  # does not raise
    assert pod.account_ref is None
    assert pod.account_wanted and not pod.account_answered


@pytest.mark.parametrize(("last", "changed"), [(LOG_B, True), (LOG_A, False)], ids=["B", "A"])
def test_a_pod_compares_with_its_tickets_log_across_a_connection_of_unknown_log(
    last: str, changed: bool
):
    # The ticket is sent on A; a connection names no log; the next names `last`. The
    # unknown one in between says nothing: B is another log than the ticket's, A is not.
    from qte_sdk.connection import Disconnected

    example = load_example("fundamentals_pod.py")
    pod = example.Pod(RecordingPodSession(), example.parse_args(list(ARGS)))
    pod.on_connected(pod_connected(LOG_A, False))
    pod.ticket_ref, pod.ticket_log_id = "r-1", LOG_A  # as send() records them
    pod.on_disconnected(Disconnected(error=None))
    pod.on_connected(pod_connected(None, True))
    assert not pod.log_changed
    pod.on_disconnected(Disconnected(error=None))
    pod.on_connected(pod_connected(last, True))
    assert pod.log_changed is changed
