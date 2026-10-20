"""Tests of `examples/fundamentals_pod.py`, run as a student would start it, against a
scripted local exchange that serves a Fundamentals pod under automatic execution."""

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
    ("execution", fill(300, "FALLBACK")),
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
    # The service's and the engine's fills are printed, as information only.
    assert "fill of the exchange's child (AUTO_EXECUTION) for ticket 17: 200 @ 200.1" in out
    assert "fill of the exchange's child (FALLBACK) for ticket 17: 300 @ 200.1" in out
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
    assert "VWAP refused: TICKET_ALGORITHM_UNAVAILABLE (1713)" in out
    assert "sending the ticket again as TWAP" in out
    assert "accepted: ticket 17, algorithm TWAP, executor SERVICE" in out
    assert "the ticket stopped: TICKET_COMPLETE" in out


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
    with pytest.raises(LivenessTimeout):
        await example.drive(DeadLink(), pod, lambda: False, 600)


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
