"""Send one Fundamentals ticket while the automatic execution service is on, and follow it.

Set QTE_URL and QTE_TOKEN first, in ./.env or the environment (see docs/quickstart.md),
using a Fundamentals pod's token, then:

    python examples/fundamentals_pod.py --instrument AAPL --side buy --shares 500 \\
        --limit 201.50 --thesis-id t-aapl --thesis-version 1 --algorithm vwap

What it does, in order:

1. It opens a session and passes every event to one `qte_sdk.tickets.LatestTickets`, from
   the first, so the route and the ticket states are never missed.
2. It asks for the team's account (`send_account_query`) and prints the position in
   --instrument from `account_state.positions`.
3. It waits at most ROUTE_SECONDS (or --seconds, if less) for `execution_route` and
   prints `routing_in_force` and `paused`. With `ROUTING_SERVICE` the service works a
   ticket sent now. With `ROUTING_DESK` the service is off and a ticket sent now would go
   to the pod's Execution desk (`executor` `DESK`), which is paused and does not trade
   this term, so the example says so, sends nothing and exits with status 1. Only a pod
   is sent a route: a desk never is, so with no route it says so, sends nothing and
   exits with status 1. This is a pod's example.
4. It sends one ticket (`send_ticket`) for --shares of --instrument at the hard limit
   --limit, with --algorithm twap or vwap (default twap). If the exchange refuses a VWAP
   ticket with `TICKET_ALGORITHM_UNAVAILABLE` (1713, no VWAP profile for the
   instrument), it says so and sends it once more as TWAP. Any other reject is printed by
   name and the example stops with status 0. It prints the `ticket_accepted`'s ticket
   id, `algorithm` and `executor` (`SERVICE` or `DESK`).
5. It prints each `ticket_state` of that ticket: status, `filled_shares` of the ticket's
   shares, `due_shares`, `activity` and `behind_schedule`, until the status is anything
   but `TICKET_WORKING`, which is a stop, a status this SDK does not know included. It
   prints the stop reason (the status's name, or the unknown name the exchange sent).
   When the state carries `progress` (on `main` only until the next release), it also
   prints the newest point: its `time`, `due_shares` and `filled_shares`. Each state
   carries the whole list, so the newest point is always the list's last.
6. It prints each fill of the service's child orders (an `execution` with `origin`
   `AUTO_EXECUTION`) as information only.
7. After the stop it asks for the account again and prints the position from that reply.
   Status 0.

The rules it follows, which a pod program should follow too:

- The position comes from `account_state.positions`, the exchange's own count. A reply
  includes every fill up to its `as_of_report_seq`, and an instrument it does not list
  is a position of 0. A missing `summary` is unknown, never zero.
- A service fill is already in the ticket's `filled_shares` and in the
  team's position: never add it on top of the ticket's totals or of a reply that covers
  it. To keep a running position between queries, use `qte_sdk.account.AccountReports`,
  which applies each fill once against the reply. Under the service the engine's own
  cure ticket and its fills are sent to no one, so ask for `account_state` again after a
  ticket stops, as this example does.
- Off or paused: with `routing_in_force` `ROUTING_DESK` the service is off and a new
  ticket would go to the pod's Execution desk (`executor` `DESK`), which is paused and
  does not trade this term, so do not send one; from the Winter term (26 October) the
  exchange is to refuse it at once instead. With `execution_route.paused` true a ticket
  is still accepted, no child is placed and resting children are cancelled, and the
  service catches up when the pause lifts; its `activity` can read `SERVICE_PAUSED`.
- 1713 (`TICKET_ALGORITHM_UNAVAILABLE`) refuses VWAP only: send the ticket again as TWAP,
  or later. A desk's ticket is always TWAP, whatever it names: read
  `ticket_accepted.algorithm`.
- A `ticket_reject` never stops a ticket; only a `ticket_state` whose status is not
  `TICKET_WORKING` does.

It sends one ticket, and a second only for the 1713 resend. A ticket is an instruction
the exchange keeps working after this program exits: if --seconds pass before the ticket
stops, the example says the ticket is still working and names it (on Ctrl+C it says a
ticket sent may still be working; its id is on the `accepted` line), and you can cancel
it with `qte_sdk.tickets.send_ticket_cancel`. It does not reconnect; a pod program that
runs for a session should use `qte_sdk.reconnect.ReconnectingSession`, whose
`Disconnected` makes `LatestTickets` forget what it held so the next connection's ticket
states rebuild it.

An opening or increasing ticket names the filed thesis that covers it with --thesis-id
and --thesis-version; a reducing ticket may leave both out. Whether a ticket is accepted
(its thesis, its size against the position, its limit, the schedule) is for the exchange
to judge.
"""

import argparse
import asyncio
import contextlib
import os
import sys
from collections.abc import AsyncIterator, Callable

from google.protobuf.message import Message
from websockets.exceptions import ConnectionClosedError, InvalidHandshake

import qte_sdk.logs
from qte_sdk.account import is_account_state, send_account_query
from qte_sdk.connection import (
    DecodeFailed,
    Event,
    LivenessTimeout,
    Received,
    SeqGap,
    SessionRejected,
)
from qte_sdk.contract.v1.common_pb2 import (
    AUTO_EXECUTION,
    BUY,
    ROUTING_DESK,
    SELL,
    TICKET_WORKING,
    URGENCY_HIGH,
    URGENCY_LOW,
    URGENCY_MEDIUM,
    ExecutionRouting,
    Origin,
    TicketActivity,
    TicketAlgorithm,
    TicketExecutor,
    TicketReasonCodes,
    TicketStatus,
)
from qte_sdk.contract.v1.order_events_pb2 import (
    AccountState,
    Execution,
    TicketAccepted,
    TicketReject,
    TicketState,
)
from qte_sdk.orders import is_order_event, reason_code_name, request_ref_of
from qte_sdk.session import (
    MissingToken,
    MissingURL,
    Session,
    SessionNotAcknowledged,
    open_session,
    resolve_url,
)
from qte_sdk.tickets import (
    TWAP,
    VWAP,
    LatestTickets,
    is_ticket_event,
    parent_ticket_of,
    send_ticket,
    ticket_reason_code_name,
    ticket_request_ref_of,
)
from qte_sdk.units import to_decimal, to_micros

# The longest wait for `execution_route`, and for each `account_state`. The exchange sends
# the route straight after the session_ack, the calendar and the instruments table, so
# this is far more than it needs. Local choices, not values the exchange sets.
ROUTE_SECONDS = 10.0
ACCOUNT_SECONDS = 10.0

ALGORITHMS = {"twap": TWAP, "vwap": VWAP}
URGENCIES = {"low": URGENCY_LOW, "medium": URGENCY_MEDIUM, "high": URGENCY_HIGH}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Send one Fundamentals ticket and follow it while the service is on."
    )
    parser.color = False  # Python 3.14 colours argparse's output; it is read as plain text.
    parser.add_argument(
        "--instrument",
        default=os.environ.get("QTE_INSTRUMENT", "AAPL"),
        help="instrument of the ticket (default: $QTE_INSTRUMENT, else AAPL)",
    )
    parser.add_argument("--side", choices=["buy", "sell"], default="buy")
    parser.add_argument("--shares", type=int, help="the ticket's size in shares (required)")
    parser.add_argument(
        "--limit", help="the hard execution limit price in dollars, such as 201.50 (required)"
    )
    parser.add_argument("--algorithm", choices=sorted(ALGORITHMS), default="twap")
    parser.add_argument("--urgency", choices=list(URGENCIES), default="medium")
    parser.add_argument("--thesis-id", help="the filed thesis covering an opening ticket")
    parser.add_argument("--thesis-version", type=int, help="that thesis's version")
    parser.add_argument(
        "--seconds",
        type=float,
        default=600.0,
        help="stop following the ticket after this long (default 600)",
    )
    args = parser.parse_args(argv)
    if args.shares is None or args.shares <= 0:
        parser.error("pass --shares, a whole number of shares above 0")
    if args.limit is None:
        parser.error("pass --limit, the ticket's hard execution limit in dollars")
    try:
        args.limit_price = to_micros(args.limit)
    except (ValueError, ArithmeticError):
        parser.error(f"--limit must be a price in dollars, such as 201.50, not {args.limit!r}")
    if not args.seconds > 0:  # NaN too
        parser.error(f"--seconds must be above 0, not {args.seconds:g}")
    if (args.thesis_id is None) != (args.thesis_version is None):
        parser.error("--thesis-id and --thesis-version go together: give both or neither")
    return args


def enum_name(event: Received, field: str, enum: type, value: int) -> str:
    """The name of an enum field, or the name the exchange sent when this SDK does not
    know it (it then decodes as the zero value)."""
    unknown = event.unknown_enum_names().get(field)
    if unknown is not None:
        return f"{unknown} (not known to this SDK)"
    try:
        return enum.Name(value)
    except ValueError:
        return str(value)


class Pod:
    """Everything the example has read, from one stream of events."""

    def __init__(self, session: Session, args: argparse.Namespace) -> None:
        self.session = session
        self.args = args
        self.instrument: str = args.instrument
        # Every event goes to this, from the first: the route and the ticket states.
        self.tickets = LatestTickets()
        self.account_ref: str | None = None
        self.account: AccountState | None = None
        self.account_answered = False
        self.ticket_ref: str | None = None
        self.algorithm: TicketAlgorithm = ALGORITHMS[args.algorithm]
        self.resent = False
        self.ticket_id: str | None = None
        self.rejected = False
        self.stopped = False
        self.missed = False  # whether any message from the exchange was missed
        # The event of each ticket's newest state, kept for the names this SDK does not
        # know (`unknown_enum_names()`), which the message alone does not carry.
        self.state_events: dict[str, Received] = {}

    async def query_account(self) -> None:
        self.account = None
        self.account_answered = False
        self.account_ref = await send_account_query(self.session)

    async def send(self) -> None:
        verb = "buy" if self.args.side == "buy" else "sell"
        name = TicketAlgorithm.Name(self.algorithm)
        print(
            f"sending a {name} ticket to {verb} {self.args.shares} {self.instrument} "
            f"with a limit of {to_decimal(self.args.limit_price)}"
        )
        self.ticket_ref = await send_ticket(
            self.session,
            instrument=self.instrument,
            side=BUY if self.args.side == "buy" else SELL,
            shares=self.args.shares,
            limit_price=self.args.limit_price,
            urgency=URGENCIES[self.args.urgency],
            thesis_id=self.args.thesis_id,
            thesis_version=self.args.thesis_version,
            algorithm=self.algorithm,
        )

    async def on_event(self, event: Event) -> None:
        self.tickets.update(event)  # every event, so nothing it keeps is missed
        if isinstance(event, SeqGap | DecodeFailed):
            print("warning: a message from the exchange was missed or unreadable")
            self.missed = True
        elif is_account_state(event):
            if event.message.request_ref == self.account_ref:
                self.account = event.message
                self.account_answered = True
        elif is_ticket_event(event):
            await self.on_ticket_event(event)
        elif is_order_event(event):
            self.on_order_event(event)

    async def on_ticket_event(self, event: Received) -> None:
        message = event.message
        ref = ticket_request_ref_of(message)
        if isinstance(message, TicketReject) and ref is not None and ref == self.ticket_ref:
            if (
                message.reason_code == TicketReasonCodes.TICKET_ALGORITHM_UNAVAILABLE
                and self.algorithm == VWAP
                and not self.resent
            ):
                print(
                    "VWAP refused: TICKET_ALGORITHM_UNAVAILABLE (1713), no VWAP profile for "
                    f"{self.instrument}; sending the ticket again as TWAP"
                )
                self.algorithm = TWAP
                self.resent = True
                await self.send()
                return
            reason = ticket_reason_code_name(message.reason_code)
            unknown = event.unknown_enum_names().get("reason_code")
            detail = f" ({message.reason_detail})" if message.HasField("reason_detail") else ""
            # A reject refuses this one message and stops nothing: a ticket sent earlier
            # (TICKET_INSTRUMENT_WORKING, say) is still working.
            print(
                f"REJECTED: {unknown or reason}{detail}; any ticket already working is unaffected"
            )
            self.rejected = True
        elif isinstance(message, TicketAccepted) and ref is not None and ref == self.ticket_ref:
            self.ticket_id = message.ticket_id
            algorithm = enum_name(event, "algorithm", TicketAlgorithm, message.algorithm)
            executor = enum_name(event, "executor", TicketExecutor, message.executor)
            print(
                f"accepted: ticket {message.ticket_id}, algorithm {algorithm}, executor {executor}"
            )
            # Its newest state may have come before the acceptance.
            newest = self.state_events.get(message.ticket_id)
            if newest is not None:
                self.on_state(newest)
        elif isinstance(message, TicketState):
            if self.tickets.get(message.ticket_id) is message:  # newer than the one held
                self.state_events[message.ticket_id] = event
                if message.ticket_id == self.ticket_id:
                    self.on_state(event)

    def on_state(self, event: Received) -> None:
        state: TicketState = event.message
        status = enum_name(event, "status", TicketStatus, state.status)
        activity = enum_name(event, "activity", TicketActivity, state.activity)
        print(
            f"ticket {state.ticket_id}: {status}, filled {state.filled_shares} of "
            f"{state.shares}, due {state.due_shares}, activity {activity}, "
            f"behind_schedule {state.behind_schedule}"
        )
        if state.progress:  # the whole history, in ascending time: the last is the newest
            point = state.progress[-1]
            print(
                f"  progress at {point.time}: due {point.due_shares}, filled {point.filled_shares}"
            )
        # Any status but TICKET_WORKING is a stop, one this SDK does not know included.
        if state.status != TICKET_WORKING:
            print(f"the ticket stopped: {status}")
            self.stopped = True

    def on_order_event(self, event: Received) -> None:
        message: Message = event.message
        if isinstance(message, Execution) and message.origin == AUTO_EXECUTION:
            # Information only. This fill is already in the ticket's filled_shares and in
            # the team's position, so adding it on top of either counts it twice. The
            # position comes from account_state (or from AccountReports, which applies
            # each fill once against a reply); this example only prints the fill.
            origin = Origin.Name(message.origin)
            print(
                f"fill of the exchange's child ({origin}) for ticket "
                f"{parent_ticket_of(message)}: {message.fill_size} @ "
                f"{to_decimal(message.fill_price)}; already in the ticket's filled_shares"
            )
        elif request_ref_of(message) == self.account_ref and self.account_ref is not None:
            # The account query was refused: the position is unknown.
            print(f"the account query was refused: {reason_code_name(message.reason_code)}")
            self.account_answered = True

    def warn_if_missed(self) -> None:
        if self.missed:
            print("messages were missed, so the ticket's states printed may be incomplete")

    def print_position(self, when: str) -> None:
        if self.account is None:
            print(f"position {when}: unknown (no account_state)")
            return
        # positions lists every nonzero position; an instrument not listed holds 0.
        quantity = next(
            (p.quantity for p in self.account.positions if p.instrument == self.instrument), 0
        )
        print(f"position in {self.instrument} {when}, from account_state: {quantity}")
        if self.account.HasField("summary"):
            print(f"  equity {to_decimal(self.account.summary.equity)}")
        else:
            print("  no account summary: equity unknown (figures not shown)")


async def drive(session: Session, pod: Pod, until: Callable[[], bool], seconds: float) -> str:
    """Pass events to `pod` until `until()` holds ("done"), `seconds` pass ("time") or the
    exchange closes the connection ("closed")."""
    if until():
        return "done"
    deadline = asyncio.timeout(seconds)
    try:
        async with deadline:
            async for event in session:
                await pod.on_event(event)
                if until():
                    return "done"
    except TimeoutError:
        # Only this wait's own deadline is "time". A LivenessTimeout (no heartbeat, the
        # link presumed dead) is a TimeoutError too, and must not read as one.
        if deadline.expired():
            return "time"
        raise
    return "closed"


# The longest wait for the connection to close at the end. A local choice, not a value
# the exchange sets. If the close does not finish, the connection is dropped anyway when
# the example exits.
CLOSE_SECONDS = 5.0


@contextlib.asynccontextmanager
async def closing(session: Session) -> AsyncIterator[Session]:
    """Like `async with session:`, but waits at most CLOSE_SECONDS for the close, so a
    connection that has stopped taking data cannot keep the example from exiting."""
    try:
        yield session
    finally:
        try:
            async with asyncio.timeout(CLOSE_SECONDS):
                await session.close()
        except TimeoutError:
            print("the connection did not close in time; it is dropped as the example exits")


async def follow(session: Session, args: argparse.Namespace) -> int:
    print(f"connected: team {session.info.team}, unscored session: {session.info.unscored}")
    pod = Pod(session, args)
    await pod.query_account()
    why = await drive(
        session,
        pod,
        lambda: pod.tickets.route is not None and pod.account_answered,
        min(ROUTE_SECONDS, args.seconds),
    )
    if why == "closed":
        print("the exchange closed the connection; no ticket was sent")
        return 1
    route = pod.tickets.route
    if route is None:
        print(
            "no execution_route arrived: only a Fundamentals pod is sent one (a desk never "
            "is), and this is a pod's example; no ticket was sent"
        )
        return 1
    pod.print_position("at the start")
    print(
        f"routing_in_force {ExecutionRouting.Name(route.routing_in_force)}, "
        f"paused {route.paused}, config_version {route.config_version}"
    )
    if route.HasField("routing_next_open"):
        print(f"from the next open: {ExecutionRouting.Name(route.routing_next_open)}")
    if route.routing_in_force == ROUTING_DESK:
        print(
            "the service is off: a ticket sent now would go to the pod's Execution desk "
            "(executor DESK), which is paused and does not trade this term; no ticket was sent"
        )
        return 1
    if route.paused:
        print(
            "the service is paused: a ticket is still accepted, but no child is "
            "placed while the pause lasts; the service catches up when it lifts"
        )

    await pod.send()
    why = await drive(session, pod, lambda: pod.rejected or pod.stopped, args.seconds)
    if pod.rejected:
        return 0
    if why == "closed":
        print("the exchange closed the connection")
        if pod.ticket_ref is not None and not pod.stopped:
            print("WARNING: the ticket may still be working; reconnect and read its state")
        pod.warn_if_missed()
        return 1
    if why == "time":
        if pod.ticket_id is None:
            print(f"stopped after {args.seconds:g} seconds: the ticket's answer is not known")
        else:
            print(
                f"stopped watching after {args.seconds:g} seconds: ticket {pod.ticket_id} "
                "is still working on the exchange; cancel it with send_ticket_cancel if "
                "you do not want it to continue"
            )
        pod.warn_if_missed()
        return 0

    # The ticket stopped. Ask again: the engine's cure fills, if any, show only here.
    await pod.query_account()
    await drive(session, pod, lambda: pod.account_answered, ACCOUNT_SECONDS)
    pod.print_position("after the stop")
    pod.warn_if_missed()
    return 0


async def run(url: str, args: argparse.Namespace) -> int:
    # The token comes from QTE_TOKEN, or from the file named by QTE_TOKEN_FILE.
    session = await open_session(url)
    async with closing(session):
        return await follow(session, args)


def fail(message: str) -> int:
    print(message, file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    # With QTE_LOG_FORMAT=json, the SDK's log records are JSON lines on stderr.
    qte_sdk.logs.configure()
    try:
        url = resolve_url()  # QTE_URL from the environment, or from ./.env
    except MissingURL as error:
        fail(f"{error}; for example ws://127.0.0.1:8080/ws (see docs/quickstart.md)")
        return 2
    try:
        return asyncio.run(run(url, args))
    except MissingToken as error:
        # The message names the variable and the problem, never the token.
        fail(f"{error} (see docs/quickstart.md)")
        return 2
    except ValueError as error:
        return fail(f"not sent: {error}")
    except SessionRejected as error:
        return fail(f"the exchange refused the session: {error}")
    except LivenessTimeout as error:
        return fail(
            f"{error}. This example does not reconnect. A ticket sent may still be "
            "working: reconnect and read its ticket_state"
        )
    except (OSError, InvalidHandshake, SessionNotAcknowledged) as error:
        return fail(f"could not connect to QTE_URL: {error}")
    except ConnectionClosedError:
        return fail(
            "the connection dropped, and this example does not reconnect. A ticket sent "
            "may still be working: reconnect and read its ticket_state"
        )
    except KeyboardInterrupt:
        # The ticket, if one was accepted, keeps working on the exchange.
        print("interrupted: a ticket sent may still be working; read its ticket_state")
        return 130


if __name__ == "__main__":
    sys.exit(main())
