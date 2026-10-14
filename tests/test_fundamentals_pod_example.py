"""Tests of `examples/fundamentals_pod.py`, run as a student would start it, against a
scripted local exchange that serves a Fundamentals pod under automatic execution."""

import json
from typing import Any

from fake_exchange import CONTRACT_VERSION, serve_local
from test_examples import run_example, synthetic_token
from test_tickets import accepted_payload, route_payload, service_state_payload
from websockets.asyncio.server import ServerConnection
from websockets.exceptions import ConnectionClosed

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
    ) -> None:
        self.route = route
        self.accounts = list(accounts)
        self.after = after
        self.refuse_vwap = refuse_vwap
        self.received: list[dict[str, Any]] = []
        self._seq = 0
        self._report_seq = 0

    async def send(self, ws: ServerConnection, type_: str, payload: dict[str, Any]) -> None:
        self._seq += 1
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
        if algorithm == "VWAP" and self.refuse_vwap:
            reject = {
                "request_ref": payload["request_ref"],
                "request_kind": "TICKET_REQUEST_SUBMIT",
                "reason_code": "TICKET_ALGORITHM_UNAVAILABLE",
                "receipt_time": "1",
            }
            await self.send(ws, "ticket_reject", reject)
            return
        accepted = accepted_payload(
            request_ref=payload["request_ref"], algorithm=algorithm, executor="SERVICE"
        )
        await self.send(ws, "ticket_accepted", accepted)
        for type_, frame in self.after:
            await self.send(ws, type_, frame)

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


async def test_a_paused_route_is_named_and_the_ticket_is_followed_not_assumed_to_catch_up():
    after = [("ticket_state", state(10, activity="SERVICE_PAUSED"))]
    exchange = PodExchange(route=route_payload(paused=True), accounts=[account(None)], after=after)
    code, out, err = await run_pod(exchange, "--seconds", "1")
    assert code == 0, out + err
    assert "routing_in_force ROUTING_SERVICE, paused True" in out
    assert "the service is paused" in out
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
